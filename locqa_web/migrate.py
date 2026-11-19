"""Bring the schema to the latest migration. Called by every process at start.

The web process and the worker both call this, and on a real deployment they
start at the same moment. On Postgres the whole upgrade runs inside one
transaction holding an advisory lock, so the second process waits, then finds
the schema already at head and does nothing. (Postgres DDL is transactional,
so a migration that fails part-way leaves nothing behind.)

SQLite has no advisory locks. It is the local-development database, where
two processes starting in the same instant is rare; when it does happen the
loser sees "table already exists", and simply retrying finds the work done.

    python -m locqa_web.migrate        # run it by hand
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

log = logging.getLogger("locqa_web.migrate")

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
# The schema that `create_all` built before migrations existed. A database
# with these tables and no alembic_version table is stamped here, then
# upgraded normally.
BASELINE = "0001"
# Arbitrary, but fixed: every locqa_web process must agree on it.
_LOCK_KEY = 0x10CA_0A01


def alembic_config(connection=None) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def _upgrade_once(engine: Engine) -> None:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        cfg = alembic_config(connection)
        inspector = inspect(connection)
        if inspector.has_table("runs") and not inspector.has_table("alembic_version"):
            log.info("pre-migration database found; stamping it at %s", BASELINE)
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")


def upgrade(engine: Engine, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        try:
            _upgrade_once(engine)
            return
        except DBAPIError:
            if engine.dialect.name != "sqlite" or attempt == attempts:
                raise
            log.warning("migration raced another process; retrying")
            time.sleep(0.5 * attempt)


def main() -> None:
    from .db import make_engine
    from .settings import Settings

    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    engine = make_engine(Settings.from_env().migration_url)     # direct, never pooled
    upgrade(engine)
    log.info("schema is at head")


if __name__ == "__main__":
    main()
