"""Alembic environment.

Two ways in:

- `locqa_web.migrate.upgrade(engine)`, called at startup, hands over an open
  connection in `config.attributes["connection"]` — already inside the
  transaction that holds the migration lock. Nothing here may commit it.
- The `alembic` CLI (alembic.ini at the repo root), for writing new
  revisions: `alembic revision --autogenerate -m "…"`. It builds its own
  engine from DATABASE_URL, exactly as the app would.
"""

from __future__ import annotations

from alembic import context

from locqa_web import models  # noqa: F401 - registers every table on Base
from locqa_web.db import Base, make_engine
from locqa_web.settings import Settings

config = context.config
target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things; batch mode rebuilds the table.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = make_engine(Settings.from_env().database_url)
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not set up for locqa_web.")
run_migrations_online()
