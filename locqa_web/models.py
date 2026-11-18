"""Tables.

The `runs` table doubles as the job queue. A separate queue (Redis, a broker)
would be one more service to deploy and keep alive on a free tier, and one
more place for a job and its result to disagree. With the queue in the same
table as the result, "queued", "running", and "done" are one row's state, and
the transition between them is one UPDATE.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# Run.status values.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    """A login. The cookie holds a random token; only its hash is stored, so
    a leaked database does not hand out live sessions."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


class ConfigProfile(Base):
    """A saved project config, as the YAML text the user wrote."""

    __tablename__ = "config_profiles"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    yaml_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    """One check run: its inputs, its place in the queue, and its result."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Inputs. Uploads live in the database rather than on disk because free
    # tiers give a web process an ephemeral filesystem: a file written there
    # is gone at the next deploy, and the worker may be another machine.
    filename: Mapped[str] = mapped_column(String(255))
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    translation_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    translation_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # The config is copied, not referenced. Editing or deleting a saved config
    # must not change what an old run says it was checked against.
    config_name: Mapped[str] = mapped_column(String(80))
    config_yaml: Mapped[str] = mapped_column(Text, default="")

    # Queue state.
    status: Mapped[str] = mapped_column(String(16), default=QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Result.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_html: Mapped[str | None] = mapped_column(Text, nullable=True)
