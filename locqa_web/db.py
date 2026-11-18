"""Engine and session factory.

One function builds both, so the web process, the worker process, and the
tests each get their own engine from their own URL rather than sharing a
module-level global that the tests would have to monkeypatch.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC. SQLite drops tzinfo on the way in, so storing aware datetimes
    would make every comparison between a fresh value and a loaded one raise."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            # The inline worker thread and the request threads share the file.
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):     # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            # WAL lets the worker write a finished run while a request reads.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Migrate the schema to head. Safe to call from every process at once."""
    from .migrate import upgrade

    upgrade(engine)
