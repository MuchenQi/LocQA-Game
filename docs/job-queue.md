# Running a slow linter without blocking the request

locqa's web front end lets someone upload a game script and get back a QA
report. The checker is not fast: a large branching script means parsing,
building a dialogue graph, and running 26 checks, some of which walk every
path through it. A file can take seconds, and a pathological file can take
forever. This is how the web layer runs that work without ever making an
HTTP request wait for it, and without adding a single service beyond the
database it already needed.

The code is in [`locqa_web/jobs.py`](../locqa_web/jobs.py); the tests that pin
each property down are in [`tests/test_web.py`](../tests/test_web.py).

## The shape

```
browser ── POST /runs ──▶ web: store upload, INSERT run (queued), 303 ──▶ run page
                                              │                          │ polls
                                              ▼                          ▼
                                worker: claim → run in child → write result
```

The request handler validates the upload, inserts a row with
`status = 'queued'`, and redirects. A worker claims the row, runs the checker,
and writes the result back to the same row. The run page polls
`GET /api/runs/{id}` until the status is `done` or `failed`. The user can
close the tab; the run lands in their history either way.

## Why the queue is a table

The usual answer is a broker: Redis with RQ or Celery. It was rejected for
two reasons.

1. **One more service to keep alive.** The deployment target is a free tier:
   one web service and one Postgres. A broker is a third thing to provision,
   pay for, and monitor, for a queue that sees a handful of jobs a minute.
2. **Two sources of truth.** With a broker, the job lives in Redis and its
   result lives in Postgres. A worker that crashes between finishing the work
   and acknowledging the message leaves them disagreeing. When the job *is*
   the row, "queued", "running", and "done" are one row's state, and every
   transition is a single `UPDATE`.

The cost is that Postgres now does a broker's job, so the hard parts a broker
handles have to be handled here. There are four.

## 1. One run, one worker: compare-and-set

Two workers polling at the same moment will both see the same oldest queued
row. The claim must hand it to exactly one of them.

```python
run_id = s.execute(
    select(Run.id)
    .where(claimable(now), Run.attempts < max_attempts)
    .order_by(Run.created_at, Run.id)
    .limit(1)
    .with_for_update(skip_locked=True)        # Postgres: skip rows another worker holds
).scalar()

result = s.execute(
    update(Run)
    .where(Run.id == run_id, claimable(now))  # repeat the condition: compare-and-set
    .values(status=RUNNING, attempts=Run.attempts + 1, locked_until=now + lease)
)
if result.rowcount != 1:
    return None                               # another worker won
```

The `UPDATE` repeats the "still claimable" condition, so if two workers race
for one row, the second one's `UPDATE` matches nothing and it gets
`rowcount == 0`. That alone is correct on any database, which is what lets the
same code run on SQLite in development. On Postgres the `SELECT` also takes
`FOR UPDATE SKIP LOCKED`, so the losing worker does not wait at all. It skips
to the next row, and four workers drain a queue in parallel instead of
queueing behind each other.

`TestConcurrentClaims` starts four threads against eight queued runs and
asserts every run is claimed exactly once. CI runs it on SQLite and on a real
Postgres 17.

## 2. A dead worker does not strand a run: leases

If a worker claims a run and then dies (the platform restarts it, or it runs
out of memory), the row says `running` forever. So a claim is not ownership.
It is a lease: `locked_until` is set to the job timeout plus a margin. A row
whose lease has lapsed counts as claimable again:

```python
or_(Run.status == QUEUED,
    and_(Run.status == RUNNING, Run.locked_until < now))
```

Each claim increments `attempts`. After `max_attempts` lost workers, the run
is failed with a message saying so, rather than retried forever. A file that
reliably crashes the checker must not keep a worker busy indefinitely.

## 3. A stale worker cannot overwrite a newer attempt: fencing

Leases introduce a subtler bug. Worker A claims a run and stalls: a long GC
pause, or a network partition to the database. Its lease lapses, worker B
reclaims the run and starts again, and then A wakes up and writes its result.
If B finishes later, B's result overwrites A's, or the other way round. One
of them is writing a result for an attempt that no longer exists.

The attempt number is a fencing token. A worker remembers which attempt it
claimed, and the result is written only if the row is still on that attempt:

```python
update(Run).where(Run.id == run_id, Run.status == RUNNING, Run.attempts == attempt)
```

Worker A's write matches nothing and is discarded, and the log says so.
`test_lapsed_lease_is_reclaimed_and_the_stale_result_discarded` plays this
sequence out step by step.

## 4. A slow or hostile file cannot hang the worker: isolation

Uploads come from strangers. A file that sends a regular expression into
catastrophic backtracking, or simply never finishes, must not take the worker
with it. So the checker never runs in the worker's own process. Each job runs
in a fresh `python -m locqa_web.analysis` subprocess, with the inputs pickled
over stdin and the result pickled back over stdout, under
`communicate(timeout=…)`. On timeout the child is killed.

The first version used `multiprocessing` and was replaced. Spawn-mode
multiprocessing re-imports the parent's `__main__` in the child, so the
child's behaviour depended on whatever had launched the worker. A child that
died during start-up also went undetected until the full timeout expired. A
plain subprocess runs one known module, and `communicate` returns the moment
the child exits, so a crash is reported immediately, not 60 seconds later.

## What is retried, and what is not

Retries exist for one failure: a lost worker. Everything else is a property
of the input and would fail identically the second time:

| failure | outcome |
|---|---|
| worker died mid-run | lease lapses; retried up to `max_attempts` |
| file cannot be parsed, or the config is invalid | failed once, with the parser's message |
| run exceeded the timeout | failed once, saying so |
| unexpected exception in the checker | failed once: "a bug in the checker, not your file" |

The distinction matters to the person reading the error. A message they can
act on ("this file is not UTF-8") is useful. Three identical retries followed
by "job failed" is not.

## Not polling every second

The first version of the worker polled the queue once a second, forever.
That works, and on a serverless Postgres (the deployment uses Neon) it is
expensive. A query every second keeps the database awake around the clock,
so it never scales to zero, and that spends its compute allowance on nothing.

So an idle worker backs off, waiting 1 s, 2 s, 4 s and so on up to 30 s, and new
work does not wait for the poll:

- **Worker inside the web process** (the free-tier setup): the upload request
  sets a `threading.Event` the worker is sleeping on.
- **Standalone worker on Postgres:** the upload's transaction runs
  `pg_notify('locqa_runs', id)`. The worker holds a `LISTEN` connection and
  wakes when the transaction commits. NOTIFY is transactional, so a worker
  can never be woken for a row that was rolled back.

Polling stays as the safety net, because a lapsed lease sends no
notification. Polling is what finds a run whose worker died. Measured on
Neon (us-west-2), a standalone worker that had been idle long enough to back
off claimed a new run 0.67 s after it was created. A `NOTIFY` sent partway
through a 20-second wait woke the listener after 1.54 s, and that figure
includes opening a fresh TLS connection to send it.

The `LISTEN` connection has to be a direct one. Neon's default
`DATABASE_URL` points at a PgBouncer pooler in transaction mode, which cannot
carry a session-level `LISTEN`. So the app splits its connections the way
Neon recommends. Ordinary queries, claims included, go through the pooler.
The claim's `SELECT … FOR UPDATE` and `UPDATE` run inside one transaction,
which is exactly what a transaction pooler preserves. The `LISTEN`
connection and schema migrations use `DATABASE_URL_UNPOOLED`. The `NOTIFY`
itself can go through the pooler, because it is part of the upload's
transaction and is delivered at commit.

## Schema changes, too

The same "two processes start at once" problem applies to migrations. The
web process and the worker both run `alembic upgrade head` at start-up. On
Postgres the upgrade runs inside one transaction holding
`pg_advisory_xact_lock`, so the second process waits, then finds the schema
already at head and does nothing. Postgres runs schema changes
transactionally, so a migration that fails part-way leaves nothing behind.

## Where this stops scaling

A table-backed queue is the right size for this tool. It is not the right
size for every tool. The limits:

- **Throughput.** Every claim is a write to a table that also serves the
  run-history pages. At thousands of jobs a second, `SKIP LOCKED` still
  works, but the table churns heavily, and a dedicated queue, or at least a
  separate jobs table, starts paying for itself.
- **Fan-out.** One run is one job. A workflow of dependent steps would want
  a real job framework rather than more columns on `runs`.
- **Payloads in the database.** Uploads and reports are stored in Postgres,
  because free tiers give containers an ephemeral filesystem. With large
  files or long retention, they belong in object storage, with only a key in
  the row.

None of these bind at the scale a localization QA tool sees: dozens of runs a
day, each a few megabytes. The point of the design is to be correct first,
with one fewer service to deploy, and to know which line it would be redrawn
at.
