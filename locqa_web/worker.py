"""Standalone worker process.

    python -m locqa_web.worker

Run as many as you like against one database; claims are safe under
concurrency (see jobs.py). On a single free-tier service, skip this and leave
LOCQA_INLINE_WORKER on, which runs the same loop as a thread in the web
process.
"""

from __future__ import annotations

import logging
import signal
import threading

from . import jobs
from .db import init_db, make_engine, make_sessionmaker
from .settings import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)
    stop = threading.Event()

    def _stop(*_):
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    # A run in progress finishes (or times out) before the loop sees `stop`;
    # if the platform kills the process first, the lease lets another worker
    # pick the run up.
    jobs.work(make_sessionmaker(engine), settings, stop)


if __name__ == "__main__":
    main()
