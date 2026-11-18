"""The job queue: claiming runs, executing them, recording the outcome.

A request never runs the checks. It inserts a `queued` row and returns; a
worker claims the row, runs locqa in a child process, and writes the result
back. Four properties make that safe, and each has one line of code to point
at:

1. **One run, one worker.** Claiming is a compare-and-set: the UPDATE that
   marks a row `running` repeats the "still claimable" condition, so of two
   workers racing for one row exactly one sees `rowcount == 1`. On Postgres
   the candidate row is also selected `FOR UPDATE SKIP LOCKED`, so a second
   worker skips to the next row instead of queueing behind the first.

2. **A dead worker does not strand a run.** A claim is a lease
   (`locked_until`), not ownership. If the worker dies mid-run the lease
   lapses and the row becomes claimable again, up to `max_attempts`, after
   which it is failed with a message saying so.

3. **A stale worker cannot overwrite a newer attempt.** `attempts` is a
   fencing token: a result is written only if the row is still on the
   attempt that produced it.

4. **A slow or hostile file cannot hang the worker.** The checks run in a
   child process with a hard timeout. A timed-out child is killed; the
   worker itself never blocks on locqa.

What is *not* retried: a file locqa cannot parse, a config that does not
validate, and a timeout. All three are properties of the input and would
fail identically on the next attempt. Retries are for lost workers only.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from .db import utcnow
from .models import DONE, FAILED, QUEUED, RUNNING, Run
from .settings import Settings

log = logging.getLogger("locqa_web.jobs")

# How long past the job timeout a lease lasts. The worker kills its child at
# the timeout and then needs a moment to write the result; a lease that ran
# out exactly at the timeout would let a second worker claim a row the first
# is about to finish.
LEASE_MARGIN = timedelta(seconds=30)


def _claimable(now):
    return or_(
        Run.status == QUEUED,
        and_(Run.status == RUNNING, Run.locked_until < now),
    )


def expire_abandoned(Session: sessionmaker, settings: Settings) -> int:
    """Fail runs whose lease lapsed on their last allowed attempt."""
    now = utcnow()
    with Session.begin() as s:
        result = s.execute(
            update(Run)
            .where(
                Run.status == RUNNING,
                Run.locked_until < now,
                Run.attempts >= settings.max_attempts,
            )
            .values(
                status=FAILED,
                finished_at=now,
                locked_until=None,
                error=f"The worker processing this run stopped responding "
                f"{settings.max_attempts} times. Try again; if it keeps happening, "
                "the file may be crashing locqa.",
            )
        )
        return result.rowcount or 0


def claim(Session: sessionmaker, settings: Settings) -> tuple[int, int] | None:
    """Claim the oldest claimable run. Returns (run id, attempt) or None."""
    expire_abandoned(Session, settings)
    now = utcnow()
    lease = timedelta(seconds=settings.job_timeout_seconds) + LEASE_MARGIN
    try:
        with Session.begin() as s:
            run_id = s.execute(
                select(Run.id)
                .where(_claimable(now), Run.attempts < settings.max_attempts)
                .order_by(Run.created_at, Run.id)
                .limit(1)
                # Rendered on Postgres; SQLite has no row locks and ignores it,
                # relying on the compare-and-set below alone.
                .with_for_update(skip_locked=True)
            ).scalar()
            if run_id is None:
                return None
            result = s.execute(
                update(Run)
                .where(Run.id == run_id, _claimable(now))
                .values(
                    status=RUNNING,
                    attempts=Run.attempts + 1,
                    locked_until=now + lease,
                    started_at=now,
                )
            )
            if result.rowcount != 1:
                return None                     # another worker won the race
            attempt = s.execute(select(Run.attempts).where(Run.id == run_id)).scalar_one()
            return run_id, attempt
    except OperationalError:
        # SQLite reports a lost write race as "database is locked". It means
        # the same thing as rowcount 0: someone else has it. Try next poll.
        log.debug("claim lost a write race; retrying on next poll")
        return None


# The child is `python -m locqa_web.analysis`, found through this checkout
# whether or not the package is installed.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


def run_isolated(kwargs: dict, timeout: float) -> tuple[str, object]:
    """Run analysis.analyze in a child process. Returns (kind, payload).

    kind is "ok" (payload: result dict), "input" (the user's file or config is
    wrong), "timeout", or "crash".

    A fresh interpreter via subprocess, not multiprocessing: spawn-mode
    multiprocessing re-imports the parent's __main__ in the child, so the
    child's behaviour would depend on whatever launched the worker. Here the
    child runs one known module, and `communicate` returns the moment it
    exits — a child that dies at startup is a crash now, not a timeout later.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [_PACKAGE_ROOT, env.get("PYTHONPATH")]))
    proc = subprocess.Popen(
        [sys.executable, "-m", "locqa_web.analysis"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        out, err = proc.communicate(pickle.dumps(kwargs), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return "timeout", (
            f"Checking this file took longer than {timeout:g} seconds and was stopped."
        )
    if proc.returncode != 0 or not out:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        return "crash", f"checker exited with code {proc.returncode}: {tail[0]}"
    try:
        # Our own child, our own bytes: pickle is safe here and keeps the
        # result's types intact without a second serialization format.
        return pickle.loads(out)
    except Exception as exc:                    # noqa: BLE001
        return "crash", f"unreadable result from checker: {exc}"


def execute(Session: sessionmaker, settings: Settings, run_id: int, attempt: int) -> str:
    """Run one claimed job to completion. Returns the final status."""
    with Session() as s:
        run = s.get(Run, run_id)
        if run is None:
            return FAILED
        kwargs = {
            "filename": run.filename,
            "data": run.file_bytes,
            "config_yaml": run.config_yaml,
            "config_name": run.config_name,
            "translation_filename": run.translation_filename,
            "translation_data": run.translation_bytes,
        }

    kind, payload = run_isolated(kwargs, settings.job_timeout_seconds)

    values: dict = {"finished_at": utcnow(), "locked_until": None}
    if kind == "ok":
        values.update(
            status=DONE,
            error=None,
            summary_json=json.dumps(payload["summary"], ensure_ascii=False),
            findings_json=json.dumps(payload["findings"], ensure_ascii=False),
            report_html=payload["html"],
        )
    elif kind == "crash":
        # The traceback is for the logs. The user gets told it is not their file.
        log.error("run %s crashed: %s", run_id, payload)
        first_line = str(payload).splitlines()[0] if payload else "unknown error"
        values.update(
            status=FAILED,
            error=f"locqa hit an internal error ({first_line}). "
            "This is a bug in the checker, not a problem with your file.",
        )
    else:
        values.update(status=FAILED, error=str(payload))

    with Session.begin() as s:
        result = s.execute(
            update(Run)
            # The fencing token: only the attempt that was claimed may finish.
            .where(Run.id == run_id, Run.status == RUNNING, Run.attempts == attempt)
            .values(**values)
        )
        if result.rowcount != 1:
            log.warning("run %s attempt %s finished after losing its lease; "
                        "result discarded", run_id, attempt)
            return "superseded"
    return values["status"]


def run_once(Session: sessionmaker, settings: Settings) -> int | None:
    """Claim and execute at most one run. Returns its id, or None if idle."""
    claimed = claim(Session, settings)
    if claimed is None:
        return None
    run_id, attempt = claimed
    execute(Session, settings, run_id, attempt)
    return run_id


# -- waking an idle worker -----------------------------------------------------
#
# Polling once a second forever is the simple way to find new work, and the
# expensive one: on a serverless Postgres (Neon) a query every second keeps
# the database awake around the clock and spends its compute allowance doing
# nothing. So an idle worker backs off — 1s, 2s, 4s … up to
# idle_poll_max_seconds — and new work does not wait for the poll:
#
#   inline worker   the upload request sets a threading.Event (Wakeup)
#   worker process  on Postgres, the upload's transaction sends NOTIFY and the
#                   worker LISTENs (PgListener); on SQLite it only polls
#
# The poll remains as the safety net. A lapsed lease produces no notification,
# so it is the poll that finds a run whose worker died.

NOTIFY_CHANNEL = "locqa_runs"


class Wakeup:
    """In-process: the web app calls notify(), the inline worker wakes."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def notify(self) -> None:
        self._event.set()

    def wait(self, timeout: float, stop: threading.Event) -> bool:
        fired = self._event.wait(timeout)       # stop also calls notify()
        self._event.clear()
        return fired


class SleepWaiter:
    """No way to be told about new work; just wait out the poll interval."""

    def notify(self) -> None:
        pass

    def wait(self, timeout: float, stop: threading.Event) -> bool:
        return stop.wait(timeout)


class PgListener:
    """A worker process on Postgres: LISTEN on a dedicated connection.

    Waits in short slices so a shutdown request is seen within a second, and
    reconnects on the next wait if the connection drops (a database restart
    or a serverless compute going to sleep).
    """

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._conn = None

    def _connect(self):
        import psycopg

        conn = psycopg.connect(self._conninfo, autocommit=True)
        conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
        return conn

    def notify(self) -> None:
        pass

    def wait(self, timeout: float, stop: threading.Event) -> bool:
        import psycopg

        remaining = timeout
        while remaining > 0 and not stop.is_set():
            slice_ = min(1.0, remaining)
            try:
                if self._conn is None:
                    self._conn = self._connect()
                for _ in self._conn.notifies(timeout=slice_, stop_after=1):
                    return True
            except psycopg.Error:
                log.warning("LISTEN connection lost; reconnecting on next wait")
                self.close()
                stop.wait(slice_)
            remaining -= slice_
        return stop.is_set()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:                   # noqa: BLE001
                pass
            self._conn = None


def notify_new_run(session, run_id: int) -> None:
    """Call inside the transaction that inserts a run. Delivered at commit."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": NOTIFY_CHANNEL, "payload": str(run_id)},
        )


def listener_for(engine) -> PgListener | SleepWaiter:
    """The right waiter for a standalone worker process on this database."""
    if engine.dialect.name == "postgresql":
        conninfo = engine.url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        return PgListener(conninfo)
    return SleepWaiter()


def work(Session: sessionmaker, settings: Settings, stop: threading.Event,
         waiter=None) -> None:
    """Process runs until `stop` is set, backing off while the queue is empty."""
    waiter = waiter or SleepWaiter()
    delay = settings.poll_interval_seconds
    log.info("worker started")
    while not stop.is_set():
        try:
            if run_once(Session, settings) is not None:
                delay = settings.poll_interval_seconds
                continue                        # there may be more; look again now
        except Exception:                       # noqa: BLE001 - a worker must not die
            log.exception("worker loop error; continuing")
        woken = waiter.wait(delay, stop)
        delay = (
            settings.poll_interval_seconds if woken
            else min(delay * 2, settings.idle_poll_max_seconds)
        )
    log.info("worker stopped")
