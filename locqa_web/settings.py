"""Runtime settings, read once from the environment.

Every value has a default that works on a laptop with no setup: SQLite in the
working directory, insecure cookies (so plain http://localhost works), and the
worker running as a thread inside the web process. A deployment overrides the
three that matter — DATABASE_URL, LOCQA_SECURE_COOKIES, LOCQA_INLINE_WORKER.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value and value.strip() else default


def normalize_db_url(url: str) -> str:
    """Point SQLAlchemy at psycopg 3 for Postgres URLs.

    Hosting platforms hand out `postgres://…` or `postgresql://…`; SQLAlchemy
    reads the first as an unknown dialect and the second as psycopg2, which
    is not a dependency. Both are rewritten to the driver that is installed.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./locqa_web.db"
    secure_cookies: bool = False
    inline_worker: bool = True
    max_upload_bytes: int = 5 * 1024 * 1024
    job_timeout_seconds: float = 60.0
    max_attempts: int = 3
    session_days: int = 14
    poll_interval_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=normalize_db_url(
                os.environ.get("DATABASE_URL", cls.database_url)
            ),
            secure_cookies=_flag("LOCQA_SECURE_COOKIES", cls.secure_cookies),
            inline_worker=_flag("LOCQA_INLINE_WORKER", cls.inline_worker),
            max_upload_bytes=_int("LOCQA_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            job_timeout_seconds=float(
                os.environ.get("LOCQA_JOB_TIMEOUT") or cls.job_timeout_seconds
            ),
            max_attempts=_int("LOCQA_MAX_ATTEMPTS", cls.max_attempts),
            session_days=_int("LOCQA_SESSION_DAYS", cls.session_days),
        )
