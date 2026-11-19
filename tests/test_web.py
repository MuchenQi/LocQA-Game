"""Web front end tests. Run: python -m unittest discover -s tests -v

Skipped entirely when the `web` extra is not installed, so the core suite
still runs on a bare `pip install -e .`.

By default each test gets a fresh SQLite file. Set LOCQA_TEST_DATABASE_URL to
run the same tests against Postgres instead (CI does both):

    LOCQA_TEST_DATABASE_URL=postgresql://user:pass@localhost/locqa_test

The database is wiped before every test, so never point it at real data.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from sqlalchemy import select, update
except ImportError:                              # pragma: no cover - env-dependent
    TestClient = None

if TestClient is not None:
    from locqa import Config, load, run_checks
    from locqa_web import jobs, security
    from locqa_web.app import COOKIE, create_app
    from locqa_web.db import utcnow
    from locqa_web.models import (
        DONE, FAILED, QUEUED, RUNNING, AuthSession, PasswordReset, RateEvent, Run,
    )
    from locqa_web.settings import Settings, normalize_db_url

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")
PASSWORD = "correct horse"
TEST_DB = os.environ.get("LOCQA_TEST_DATABASE_URL", "").strip()
ON_POSTGRES = TEST_DB.startswith(("postgres://", "postgresql"))


def _reset_database(url: str) -> None:
    """Empty the test database, so every test starts from migrations."""
    from sqlalchemy import text as sql

    from locqa_web.db import make_engine

    engine = make_engine(normalize_db_url(url))
    with engine.begin() as conn:
        conn.execute(sql("DROP SCHEMA public CASCADE"))
        conn.execute(sql("CREATE SCHEMA public"))
    engine.dispose()


def _sample(name: str) -> bytes:
    with open(os.path.join(SAMPLES, name), "rb") as fh:
        return fh.read()


@unittest.skipIf(TestClient is None, "web extra not installed: pip install -e .[web]")
class WebCase(unittest.TestCase):
    """Each test gets its own SQLite file and app. The worker is driven by
    hand (jobs.run_once) unless a test is specifically about the thread."""

    settings_overrides: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        if TEST_DB:
            _reset_database(TEST_DB)
        self.settings = Settings(**{
            "database_url": normalize_db_url(TEST_DB) if TEST_DB
            else f"sqlite:///{self._tmp.name}/test.db",
            "inline_worker": False,
            "mail_backend": "memory",
            **self.settings_overrides,
        })
        self.app = create_app(self.settings)
        self.Session = self.app.state.Session
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.app.state.engine.dispose()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def register(self, email="kestrel@example.com", client=None):
        client = client or self.client
        r = client.post("/register", data={"email": email, "password": PASSWORD},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return client

    def csrf(self, client=None):
        page = (client or self.client).get("/runs/new")
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def upload(self, name, data, config="default", translation=None, client=None,
               csrf=None):
        client = client or self.client
        files = {"file": (name, data, "application/octet-stream")}
        if translation:
            files["translation"] = (translation[0], translation[1], "application/octet-stream")
        return client.post(
            "/runs", data={"csrf": csrf or self.csrf(client), "config": config},
            files=files, follow_redirects=False,
        )

    def run_id(self, response) -> int:
        self.assertEqual(response.status_code, 303, response.text[:500])
        return int(response.headers["location"].rsplit("/", 1)[1])

    def drain(self):
        while jobs.run_once(self.Session, self.settings) is not None:
            pass

    def row(self, run_id) -> Run:
        with self.Session() as s:
            return s.get(Run, run_id)

    def other_client(self, email="aldis@example.com"):
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return self.register(email, client)


class TestAuth(WebCase):
    def test_register_sets_an_httponly_session_cookie(self):
        r = self.client.post("/register", data={"email": "K@Example.com ", "password": PASSWORD},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["set-cookie"]
        self.assertIn(f"{COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertEqual(self.client.get("/runs").status_code, 200)

    def test_session_token_is_stored_hashed(self):
        self.register()
        token = self.client.cookies.get(COOKIE)
        with self.Session() as s:
            stored = s.scalars(select(AuthSession.token_hash)).all()
        self.assertEqual(stored, [security.token_hash(token)])
        self.assertNotIn(token, stored)

    def test_duplicate_email_and_short_password_rejected(self):
        self.register()
        fresh = TestClient(self.app)
        r = fresh.post("/register", data={"email": "kestrel@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already exists", r.text)
        r = fresh.post("/register", data={"email": "new@example.com", "password": "short"})
        self.assertEqual(r.status_code, 400)

    def test_login_wrong_password_then_right(self):
        self.register()
        fresh = TestClient(self.app)
        r = fresh.post("/login", data={"email": "kestrel@example.com", "password": "nope nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("do not match", r.text)
        r = fresh.post("/login", data={"email": "kestrel@example.com", "password": PASSWORD,
                                       "next": "/configs"}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/configs"))

    def test_login_next_cannot_redirect_off_site(self):
        self.register()
        fresh = TestClient(self.app)
        for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
            r = fresh.post("/login", data={"email": "kestrel@example.com",
                                           "password": PASSWORD, "next": evil},
                           follow_redirects=False)
            self.assertEqual(r.headers["location"], "/runs", evil)

    def test_logout_invalidates_the_session(self):
        self.register()
        token = self.client.cookies.get(COOKIE)
        r = self.client.post("/logout", data={"csrf": self.csrf()}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        stale = TestClient(self.app, cookies={COOKIE: token})
        self.assertEqual(stale.get("/runs", follow_redirects=False).status_code, 303)

    def test_pages_redirect_and_api_401_when_signed_out(self):
        r = self.client.get("/runs/new", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?next=/runs/new")
        self.assertEqual(self.client.get("/api/runs").status_code, 401)

    def test_password_hash_round_trip(self):
        stored = security.hash_password(PASSWORD)
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(security.verify_password(PASSWORD, stored))
        self.assertFalse(security.verify_password(PASSWORD + "x", stored))
        self.assertFalse(security.verify_password(PASSWORD, "garbage"))


class TestRuns(WebCase):
    def test_upload_queues_then_worker_completes(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv"),
                                         config="sample"))
        self.assertEqual(self.row(run_id).status, QUEUED)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").json()["status"], QUEUED)

        self.drain()
        run = self.row(run_id)
        self.assertEqual(run.status, DONE, run.error)

        # The same counts the CLI path produces for the same inputs.
        expected = run_checks(
            load(os.path.join(SAMPLES, "aetherbound_ui.csv")),
            Config.load(os.path.join(SAMPLES, "aetherbound.locqa.yml")),
        )
        summary = self.client.get(f"/api/runs/{run_id}").json()["summary"]
        self.assertEqual(summary["total"], len(expected))

        page = self.client.get(f"/runs/{run_id}")
        self.assertIn(f'src="/runs/{run_id}/report"', page.text)

    def test_report_is_served_sandboxed(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        self.drain()
        r = self.client.get(f"/runs/{run_id}/report")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<!doctype html>", r.text)
        self.assertTrue(r.headers["content-security-policy"].startswith("sandbox allow-scripts"))
        r = self.client.get(f"/runs/{run_id}/report?download=1")
        self.assertIn("attachment", r.headers["content-disposition"])

    def test_findings_json_download(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ch1.xlf", _sample("aetherbound_ch1.xlf")))
        self.drain()
        body = self.client.get(f"/runs/{run_id}/findings.json").json()
        checks = {f["check"] for f in body["findings"]}
        self.assertIn("placeholder.parity", checks)
        self.assertEqual(body["summary"]["total"], len(body["findings"]))

    def test_renpy_with_translation_file(self):
        self.register()
        run_id = self.run_id(self.upload(
            "aetherbound.rpy", _sample("aetherbound.rpy"), config="sample",
            translation=("aetherbound_ja.rpy", _sample("aetherbound_ja.rpy")),
        ))
        self.drain()
        summary = json.loads(self.row(run_id).summary_json)
        self.assertTrue(summary["has_graph"])
        self.assertIn("graph.unreachable", summary["by_check"])

    def test_sample_button(self):
        self.register()
        r = self.client.post("/runs/sample", data={"csrf": self.csrf(), "which": "sheet"},
                             follow_redirects=False)
        run_id = self.run_id(r)
        self.drain()
        self.assertEqual(self.row(run_id).status, DONE)

    def test_runs_are_private_to_their_owner(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        self.drain()
        other = self.other_client()
        for path in (f"/runs/{run_id}", f"/runs/{run_id}/report",
                     f"/runs/{run_id}/findings.json", f"/api/runs/{run_id}"):
            self.assertEqual(other.get(path).status_code, 404, path)
        self.assertEqual(other.get("/api/runs").json(), [])
        r = other.post(f"/runs/{run_id}/delete", data={"csrf": self.csrf(other)})
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(self.row(run_id))

    def test_csrf_token_required(self):
        self.register()
        r = self.client.post("/runs", data={"config": "default"},
                             files={"file": ("a.csv", b"key,en,ja\n", "text/csv")})
        self.assertEqual(r.status_code, 403)
        r = self.upload("a.csv", b"source,target\nHi,\xe3\x81\x82\n", csrf="forged")
        self.assertEqual(r.status_code, 403)

    def test_unsupported_extension_rejected_on_the_form(self):
        self.register()
        r = self.upload("strings.xlsx", b"PK\x03\x04")
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot read .xlsx", r.text)
        with self.Session() as s:
            self.assertEqual(s.scalars(select(Run)).all(), [])

    def test_translation_only_for_renpy(self):
        self.register()
        r = self.upload("ui.csv", _sample("aetherbound_ui.csv"),
                        translation=("ja.rpy", b"translate japanese x:\n"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("only applies to Ren", r.text)

    def test_unparseable_file_fails_without_retry_and_can_be_retried(self):
        self.register()
        run_id = self.run_id(self.upload("broken.xlf", b"<xliff version='2.0'><unit"))
        self.drain()
        run = self.row(run_id)
        self.assertEqual((run.status, run.attempts), (FAILED, 1))
        self.assertIn("not well-formed", run.error)
        self.assertIn("not well-formed", self.client.get(f"/runs/{run_id}").text)

        r = self.client.post(f"/runs/{run_id}/retry", data={"csrf": self.csrf()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual((self.row(run_id).status, self.row(run_id).attempts), (QUEUED, 0))

    def test_xliff_doctype_is_refused(self):
        self.register()
        evil = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
                b'<xliff version="2.0"/>')
        run_id = self.run_id(self.upload("evil.xlf", evil))
        self.drain()
        self.assertIn("DOCTYPE", self.row(run_id).error)

    def test_non_utf8_sheet_gets_a_plain_message(self):
        self.register()
        run_id = self.run_id(self.upload("sjis.csv", "key,en,ja\nA,Hi,こんにちは\n".encode("cp932")))
        self.drain()
        self.assertIn("not UTF-8", self.row(run_id).error)

    def test_delete_run(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        r = self.client.post(f"/runs/{run_id}/delete", data={"csrf": self.csrf()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(self.row(run_id))

    def test_security_headers_on_pages(self):
        r = self.client.get("/")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_healthz(self):
        self.assertTrue(self.client.get("/healthz").json()["ok"])

    def test_html_404_page(self):
        self.register()
        r = self.client.get("/runs/999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("No such run", r.text)
        self.assertIn("text/html", r.headers["content-type"])


class TestUploadLimit(WebCase):
    settings_overrides = {"max_upload_bytes": 1024}

    def test_file_over_the_limit_rejected_on_the_form(self):
        # Over the per-file limit, under the request-size guard.
        self.register()
        r = self.upload("big.csv", b"source,target\n" + b"a,b\n" * 400)
        self.assertEqual(r.status_code, 400)
        self.assertIn("larger than", r.text)

    def test_huge_request_refused_before_it_is_read(self):
        self.register()
        r = self.upload("huge.csv", b"a,b\n" * 5000)
        self.assertEqual(r.status_code, 413)
        self.assertIn("too large", r.text)
        with self.Session() as s:
            self.assertEqual(s.scalars(select(Run)).all(), [])


class TestConfigs(WebCase):
    def save(self, name, yaml_text):
        return self.client.post("/configs", data={
            "csrf": self.csrf(), "name": name, "yaml_text": yaml_text,
        })

    def test_invalid_configs_are_rejected(self):
        self.register()
        for yaml_text, message in [
            ("charset: [", "not valid YAML"),
            ("glosary: {}", "unknown config key"),
            ("severity_overrides:\n  placeholder.parit: info", "did you mean"),
            ("min_reach: 5", "min_reach"),
        ]:
            r = self.save("Broken", yaml_text)
            self.assertEqual(r.status_code, 400, yaml_text)
            self.assertIn(message, r.text, yaml_text)

    def test_saved_config_is_offered_and_snapshotted(self):
        self.register()
        self.assertEqual(self.save("Strict", "charset: shift_jis\n").status_code, 200)
        page = self.client.get("/runs/new").text
        choice = re.search(r'<option value="(profile:\d+)">Strict</option>', page).group(1)

        run_id = self.run_id(self.upload("ui.csv", _sample("aetherbound_ui.csv"), config=choice))
        self.save("Strict", "charset: cp932\n")             # edited after the run was made
        run = self.row(run_id)
        self.assertEqual((run.config_name, run.config_yaml), ("Strict", "charset: shift_jis\n"))

    def test_someone_elses_config_cannot_be_used(self):
        self.register()
        self.save("Mine", "charset: shift_jis\n")
        choice = re.search(r'value="(profile:\d+)"', self.client.get("/runs/new").text).group(1)
        other = self.other_client()
        r = self.upload("ui.csv", _sample("aetherbound_ui.csv"), config=choice, client=other)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unknown config", r.text)


class TestQueue(WebCase):
    def make_run(self) -> int:
        self.register()
        return self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))

    def test_a_run_is_claimed_once(self):
        run_id = self.make_run()
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 1))
        self.assertIsNone(jobs.claim(self.Session, self.settings))

    def test_lapsed_lease_is_reclaimed_and_the_stale_result_discarded(self):
        run_id = self.make_run()
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 1))
        # Worker 1 goes silent; its lease runs out.
        with self.Session.begin() as s:
            s.execute(update(Run).where(Run.id == run_id)
                      .values(locked_until=utcnow() - timedelta(seconds=1)))
        self.assertEqual(jobs.claim(self.Session, self.settings), (run_id, 2))

        # Worker 1 wakes up and tries to write its result: fenced off.
        self.assertEqual(jobs.execute(self.Session, self.settings, run_id, 1), "superseded")
        self.assertEqual(self.row(run_id).status, RUNNING)
        self.assertEqual(jobs.execute(self.Session, self.settings, run_id, 2), DONE)

    def test_run_failed_after_max_attempts_of_lost_workers(self):
        run_id = self.make_run()
        with self.Session.begin() as s:
            s.execute(update(Run).where(Run.id == run_id).values(
                status=RUNNING, attempts=self.settings.max_attempts,
                locked_until=utcnow() - timedelta(seconds=1),
            ))
        self.assertIsNone(jobs.claim(self.Session, self.settings))
        run = self.row(run_id)
        self.assertEqual(run.status, FAILED)
        self.assertIn("stopped responding", run.error)

    def test_oldest_run_first(self):
        first = self.make_run()
        second = self.run_id(self.upload("aetherbound_ch1.xlf", _sample("aetherbound_ch1.xlf")))
        self.assertEqual(jobs.claim(self.Session, self.settings)[0], first)
        self.assertEqual(jobs.claim(self.Session, self.settings)[0], second)


class TestTimeout(WebCase):
    # Shorter than a Python interpreter takes to start, so every run times out.
    settings_overrides = {"job_timeout_seconds": 0.01}

    def test_slow_run_is_killed_and_failed(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        started = time.monotonic()
        self.drain()
        self.assertLess(time.monotonic() - started, 10)
        run = self.row(run_id)
        self.assertEqual((run.status, run.attempts), (FAILED, 1))
        self.assertIn("longer than", run.error)


class TestInlineWorker(WebCase):
    settings_overrides = {"inline_worker": True, "poll_interval_seconds": 0.05}

    def test_background_thread_finishes_the_run(self):
        self.register()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/runs/{run_id}").json()["status"]
            if status in (DONE, FAILED):
                break
            time.sleep(0.1)
        self.assertEqual(status, DONE)


class TestWakeup(WebCase):
    # A 30-second poll: if the run finishes in a fraction of that, it was the
    # upload that woke the worker, not the poll.
    settings_overrides = {"inline_worker": True, "poll_interval_seconds": 30.0,
                          "idle_poll_max_seconds": 30.0}

    def test_upload_wakes_the_idle_inline_worker(self):
        self.register()
        time.sleep(0.3)                         # let the worker go idle on its 30s wait
        started = time.monotonic()
        run_id = self.run_id(self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv")))
        status = None
        while time.monotonic() - started < 15:
            status = self.client.get(f"/api/runs/{run_id}").json()["status"]
            if status in (DONE, FAILED):
                break
            time.sleep(0.05)
        self.assertEqual(status, DONE)
        self.assertLess(time.monotonic() - started, 15)


@unittest.skipIf(TestClient is None, "web extra not installed")
class TestBackoff(unittest.TestCase):
    """The idle loop, with a fake queue and a fake clock."""

    class Waiter:
        def __init__(self, stop, wake_at=None, waits=8):
            self.stop, self.delays, self.wake_at, self.waits = stop, [], wake_at, waits

        def wait(self, timeout, stop):
            self.delays.append(timeout)
            if len(self.delays) >= self.waits:
                self.stop.set()
            return len(self.delays) == self.wake_at

    def run_loop(self, waiter, stop):
        import threading as _threading  # noqa: F401 - stop is a threading.Event

        settings = Settings(poll_interval_seconds=1.0, idle_poll_max_seconds=30.0)
        original = jobs.run_once
        jobs.run_once = lambda *_: None          # an empty queue, forever
        try:
            jobs.work(None, settings, stop, waiter)
        finally:
            jobs.run_once = original
        return waiter.delays

    def test_idle_worker_backs_off_to_the_cap(self):
        import threading

        stop = threading.Event()
        delays = self.run_loop(self.Waiter(stop), stop)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0])

    def test_a_wakeup_resets_the_backoff(self):
        import threading

        stop = threading.Event()
        delays = self.run_loop(self.Waiter(stop, wake_at=4, waits=6), stop)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 1.0, 2.0])


class TestConcurrentClaims(WebCase):
    """The property the queue exists for. On SQLite this exercises the
    compare-and-set alone; on Postgres, SKIP LOCKED as well."""

    RUNS, WORKERS = 8, 4

    def test_every_run_claimed_exactly_once(self):
        import threading

        self.register()
        expected = {
            self.run_id(self.upload(f"r{i}.csv", _sample("aetherbound_ui.csv")))
            for i in range(self.RUNS)
        }
        claimed: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(self.WORKERS)
        deadline = time.monotonic() + 30

        def worker():
            barrier.wait()
            while time.monotonic() < deadline:
                with lock:
                    if len(claimed) >= self.RUNS:
                        return
                got = jobs.claim(self.Session, self.settings)
                if got is None:
                    time.sleep(0.005)            # lost a race, or SQLite was busy
                    continue
                with lock:
                    claimed.append(got[0])

        threads = [threading.Thread(target=worker) for _ in range(self.WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(claimed), sorted(expected))     # all, and no repeats
        with self.Session() as s:
            statuses = set(s.scalars(select(Run.status)))
            attempts = set(s.scalars(select(Run.attempts)))
        self.assertEqual((statuses, attempts), ({RUNNING}, {1}))


@unittest.skipUnless(TestClient is not None and ON_POSTGRES,
                     "needs LOCQA_TEST_DATABASE_URL pointing at Postgres")
class TestPostgresNotify(WebCase):
    def test_listener_hears_a_new_run(self):
        import threading

        self.register()
        listener = jobs.listener_for(self.app.state.engine)
        self.addCleanup(listener.close)
        self.assertIsInstance(listener, jobs.PgListener)
        self.assertFalse(listener.wait(0.2, threading.Event()))    # connects, hears nothing

        heard = {}
        stop = threading.Event()
        t = threading.Thread(target=lambda: heard.setdefault("woke", listener.wait(10, stop)))
        t.start()
        time.sleep(0.3)
        started = time.monotonic()
        self.upload("aetherbound_ui.csv", _sample("aetherbound_ui.csv"))
        t.join(15)
        self.assertTrue(heard.get("woke"))
        self.assertLess(time.monotonic() - started, 5)


class TestMigrations(WebCase):
    def test_migrations_match_the_models(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        from locqa_web.db import Base

        with self.app.state.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        self.assertEqual(diff, [], "models changed without a migration: "
                                   "alembic revision --autogenerate -m '…'")

    def test_init_is_idempotent(self):
        from locqa_web.db import init_db

        init_db(self.app.state.engine)
        init_db(self.app.state.engine)

    def test_database_from_before_migrations_is_stamped_and_upgraded(self):
        from sqlalchemy import inspect, text as sql

        from locqa_web.db import Base, init_db, make_engine
        from locqa_web.models import AuthSession, ConfigProfile, User

        legacy_url = (normalize_db_url(TEST_DB) if TEST_DB
                      else f"sqlite:///{self._tmp.name}/legacy.db")
        self.app.state.engine.dispose()
        if TEST_DB:
            _reset_database(TEST_DB)
        engine = make_engine(legacy_url)
        try:
            # What `create_all` built at the first web commit: four tables,
            # no alembic_version.
            Base.metadata.create_all(engine, tables=[
                User.__table__, AuthSession.__table__, ConfigProfile.__table__, Run.__table__,
            ])
            with engine.begin() as conn:
                conn.execute(sql("INSERT INTO users (email, password_hash, created_at) "
                                 "VALUES ('old@example.com', 'x', CURRENT_TIMESTAMP)"))
            init_db(engine)
            with engine.connect() as conn:
                tables = set(inspect(conn).get_table_names())
                version = conn.execute(sql("SELECT version_num FROM alembic_version")).scalar()
                kept = conn.execute(sql("SELECT email FROM users")).scalar()
        finally:
            engine.dispose()          # before tearDown deletes the file (Windows)
        self.assertTrue({"rate_events", "password_resets"} <= tables)
        self.assertEqual((version, kept), ("0002", "old@example.com"))


class TestRateLimits(WebCase):
    def login(self, password, client=None):
        return (client or TestClient(self.app)).post(
            "/login", data={"email": "kestrel@example.com", "password": password},
            follow_redirects=False,
        )

    def test_sign_in_locks_after_five_failures(self):
        self.register()
        for _ in range(5):
            self.assertEqual(self.login("wrong password").status_code, 400)
        r = self.login(PASSWORD)                      # right password, still refused
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed sign-ins", r.text)

    def test_success_clears_the_count(self):
        self.register()
        for _ in range(4):
            self.login("wrong password")
        self.assertEqual(self.login(PASSWORD).status_code, 303)
        for _ in range(4):
            self.assertEqual(self.login("wrong password").status_code, 400)

    def test_lockout_ends_when_the_window_passes(self):
        self.register()
        for _ in range(5):
            self.login("wrong password")
        with self.Session.begin() as s:
            s.execute(update(RateEvent).values(created_at=utcnow() - timedelta(minutes=16)))
        self.assertEqual(self.login(PASSWORD).status_code, 303)

    def test_account_creation_limited_per_ip(self):
        fresh = TestClient(self.app)
        for i in range(10):
            r = fresh.post("/register", data={"email": f"u{i}@example.com",
                                              "password": PASSWORD}, follow_redirects=False)
            self.assertEqual(r.status_code, 303, i)
        r = fresh.post("/register", data={"email": "u10@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 429)


class TestPasswordReset(WebCase):
    def outbox(self):
        return self.app.state.mailer.outbox

    def request_reset(self, email="kestrel@example.com"):
        return TestClient(self.app).post("/forgot", data={"email": email})

    def link_token(self) -> str:
        return re.search(r"/reset\?token=([\w-]+)", self.outbox()[-1].text).group(1)

    def test_reset_flow(self):
        self.register()
        other_device = TestClient(self.app, cookies={COOKIE: self.client.cookies.get(COOKIE)})
        self.assertEqual(other_device.get("/runs", follow_redirects=False).status_code, 200)

        r = self.request_reset()
        self.assertIn("a reset link is on its way", r.text)
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.outbox()[0].to, "kestrel@example.com")
        token = self.link_token()

        browser = TestClient(self.app)
        self.assertEqual(browser.get(f"/reset?token={token}").status_code, 200)
        r = browser.post("/reset", data={"token": token, "password": "a new passphrase"},
                         follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/runs"))
        self.assertEqual(browser.get("/runs", follow_redirects=False).status_code, 200)

        # Signed out everywhere else; old password dead, new one works.
        self.assertEqual(other_device.get("/runs", follow_redirects=False).status_code, 303)
        fresh = TestClient(self.app)
        self.assertEqual(fresh.post("/login", data={"email": "kestrel@example.com",
                                                    "password": PASSWORD}).status_code, 400)
        r = fresh.post("/login", data={"email": "kestrel@example.com",
                                       "password": "a new passphrase"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_link_works_once(self):
        self.register()
        self.request_reset()
        token = self.link_token()
        fresh = TestClient(self.app)
        fresh.post("/reset", data={"token": token, "password": "a new passphrase"})
        r = fresh.post("/reset", data={"token": token, "password": "another one!"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.text)

    def test_expired_link_refused(self):
        self.register()
        self.request_reset()
        token = self.link_token()
        with self.Session.begin() as s:
            s.execute(update(PasswordReset).values(expires_at=utcnow() - timedelta(seconds=1)))
        self.assertEqual(TestClient(self.app).get(f"/reset?token={token}").status_code, 400)

    def test_new_request_retires_the_old_link(self):
        self.register()
        self.request_reset()
        first = self.link_token()
        self.request_reset()
        second = self.link_token()
        self.assertNotEqual(first, second)
        fresh = TestClient(self.app)
        self.assertEqual(fresh.get(f"/reset?token={first}").status_code, 400)
        self.assertEqual(fresh.get(f"/reset?token={second}").status_code, 200)

    def test_unknown_email_gets_the_same_page_and_no_mail(self):
        self.register()
        known = self.request_reset("kestrel@example.com").text
        unknown = self.request_reset("nobody@example.com").text
        strip = lambda html: html.replace("kestrel@example.com", "X").replace(  # noqa: E731
            "nobody@example.com", "X")
        self.assertEqual(strip(known), strip(unknown))
        self.assertEqual([m.to for m in self.outbox()], ["kestrel@example.com"])

    def test_tokens_are_stored_hashed(self):
        self.register()
        self.request_reset()
        token = self.link_token()
        with self.Session() as s:
            stored = s.scalars(select(PasswordReset.token_hash)).all()
        self.assertEqual(stored, [security.token_hash(token)])

    def test_reset_requests_are_rate_limited(self):
        self.register()
        for _ in range(3):
            self.assertEqual(self.request_reset().status_code, 200)
        self.assertEqual(self.request_reset().status_code, 429)
        self.assertEqual(len(self.outbox()), 3)


@unittest.skipIf(TestClient is None, "web extra not installed")
class TestSettings(unittest.TestCase):
    def test_env_files_load_without_overriding_the_environment(self):
        from unittest import mock

        from locqa_web.settings import load_env_files

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env.local"), "w", encoding="utf-8") as fh:
                fh.write('# from neon env pull\nDATABASE_URL="postgresql://a@pooler/db"\n'
                         "export LOCQA_JOB_TIMEOUT=7\nALREADY=from-file\n")
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as fh:
                fh.write("DATABASE_URL=postgresql://b@other/db\nONLY_IN_DOTENV='yes'\n")
            with mock.patch.dict(os.environ, {"ALREADY": "from-platform"}, clear=True):
                read = load_env_files(tmp)
                self.assertEqual(len(read), 2)
                self.assertEqual(os.environ["DATABASE_URL"], "postgresql://a@pooler/db")
                self.assertEqual(os.environ["LOCQA_JOB_TIMEOUT"], "7")
                self.assertEqual(os.environ["ALREADY"], "from-platform")
                self.assertEqual(os.environ["ONLY_IN_DOTENV"], "yes")

    def test_neon_unpooled_url_is_preferred(self):
        from unittest import mock

        from locqa_web.settings import database_url_from_env

        both = {"DATABASE_URL": "postgresql://u@ep-x-pooler.neon.tech/db",
                "DATABASE_URL_UNPOOLED": "postgresql://u@ep-x.neon.tech/db"}
        with mock.patch.dict(os.environ, both, clear=True):
            self.assertEqual(database_url_from_env(), "postgresql://u@ep-x.neon.tech/db")
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u@h/db"}, clear=True):
            self.assertEqual(database_url_from_env(), "postgresql://u@h/db")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(database_url_from_env().startswith("sqlite"))

    def test_resend_needs_a_key_and_hides_it(self):
        with self.assertRaises(ValueError):
            Settings(mail_backend="resend")
        settings = Settings(mail_backend="resend", resend_api_key="re_secret")
        self.assertNotIn("re_secret", repr(settings))
        with self.assertRaises(ValueError):
            Settings(mail_backend="carrier-pigeon")

    def test_postgres_urls_use_psycopg3(self):
        self.assertEqual(normalize_db_url("postgres://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(normalize_db_url("postgresql://u@h/db"), "postgresql+psycopg://u@h/db")
        self.assertEqual(normalize_db_url("sqlite:///x.db"), "sqlite:///x.db")

    def test_settings_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Settings().inline_worker = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
