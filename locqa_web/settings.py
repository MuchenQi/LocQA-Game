"""Runtime settings, read once from the environment.

Every value has a default that works on a laptop with no setup: SQLite in the
working directory, insecure cookies (so plain http://localhost works), and the
worker running as a thread inside the web process. A deployment overrides the
three that matter — DATABASE_URL, LOCQA_SECURE_COOKIES, LOCQA_INLINE_WORKER.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value and value.strip() else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value and value.strip() else default


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


MAIL_BACKENDS = ("console", "resend", "memory")


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./locqa_web.db"
    secure_cookies: bool = False
    inline_worker: bool = True
    max_upload_bytes: int = 5 * 1024 * 1024
    job_timeout_seconds: float = 60.0
    max_attempts: int = 3
    session_days: int = 14
    # An idle worker checks the queue after poll_interval_seconds, then backs
    # off, doubling up to idle_poll_max_seconds. New runs do not wait for the
    # poll: an upload wakes the worker directly (see jobs.Wakeup).
    poll_interval_seconds: float = 1.0
    idle_poll_max_seconds: float = 30.0

    # Password reset email. "console" logs the link instead of sending it,
    # which is what local development wants; "resend" sends through Resend's
    # HTTP API; "memory" keeps messages in a list, for tests.
    base_url: str = ""                  # e.g. https://locqa.onrender.com, for links in email
    mail_backend: str = "console"
    mail_from: str = "locqa <onboarding@resend.dev>"
    resend_api_key: str = field(default="", repr=False)
    reset_minutes: int = 60

    def __post_init__(self) -> None:
        if self.mail_backend not in MAIL_BACKENDS:
            raise ValueError(
                f"LOCQA_MAIL_BACKEND must be one of {', '.join(MAIL_BACKENDS)}, "
                f"not {self.mail_backend!r}"
            )
        if self.mail_backend == "resend" and not self.resend_api_key:
            raise ValueError("LOCQA_MAIL_BACKEND=resend needs RESEND_API_KEY.")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=normalize_db_url(
                os.environ.get("DATABASE_URL", cls.database_url)
            ),
            secure_cookies=_flag("LOCQA_SECURE_COOKIES", cls.secure_cookies),
            inline_worker=_flag("LOCQA_INLINE_WORKER", cls.inline_worker),
            max_upload_bytes=_int("LOCQA_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            job_timeout_seconds=_float("LOCQA_JOB_TIMEOUT", cls.job_timeout_seconds),
            max_attempts=_int("LOCQA_MAX_ATTEMPTS", cls.max_attempts),
            session_days=_int("LOCQA_SESSION_DAYS", cls.session_days),
            idle_poll_max_seconds=_float("LOCQA_IDLE_POLL_MAX", cls.idle_poll_max_seconds),
            base_url=os.environ.get("LOCQA_BASE_URL", "").rstrip("/"),
            mail_backend=os.environ.get("LOCQA_MAIL_BACKEND", cls.mail_backend).strip().lower(),
            mail_from=os.environ.get("LOCQA_MAIL_FROM", cls.mail_from),
            resend_api_key=os.environ.get("RESEND_API_KEY", ""),
            reset_minutes=_int("LOCQA_RESET_MINUTES", cls.reset_minutes),
        )
