"""Rate limits on the account endpoints.

A sliding window over rows in `rate_events`. Each rule is (scope, limit,
window): "no more than 5 failed sign-ins for one email in 15 minutes". A
request is refused while any of its rules is at its limit, and told when the
oldest counted event will age out.

Limits are per email *and* per IP, because they stop different attacks. Per
email stops guessing one account's password from many addresses; per IP stops
one address trying a password against many accounts. The cost of the per-email
rule is that anyone can lock an account out for 15 minutes by failing on
purpose — the standard trade, and the reason the window is short.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import RateEvent

MINUTE = timedelta(minutes=1)

RULES: dict[str, tuple[tuple[str, int, timedelta], ...]] = {
    # Failed sign-ins only; a success clears the email's count.
    "login": (("email", 5, 15 * MINUTE), ("ip", 20, 15 * MINUTE)),
    # Every account creation, successful or not.
    "register": (("ip", 10, 60 * MINUTE),),
    # Every reset request, whether or not the email has an account — counting
    # only real accounts would reveal which emails do.
    "reset": (("email", 3, 60 * MINUTE), ("ip", 10, 60 * MINUTE)),
}
RETENTION = timedelta(days=1)          # longer than any window above


def _keys(kind: str, values: dict[str, str]) -> list[tuple[str, int, timedelta]]:
    return [
        (f"{scope}:{values[scope]}", limit, window)
        for scope, limit, window in RULES[kind]
        if values.get(scope)
    ]


def retry_after(s: Session, kind: str, **values: str) -> timedelta | None:
    """How long until this request would be allowed, or None if it is now."""
    now = utcnow()
    wait: timedelta | None = None
    for key, limit, window in _keys(kind, values):
        since = now - window
        count, oldest = s.execute(
            select(func.count(), func.min(RateEvent.created_at)).where(
                RateEvent.kind == kind, RateEvent.key == key, RateEvent.created_at > since,
            )
        ).one()
        if count >= limit:
            remaining = (oldest + window) - now
            wait = max(wait, remaining) if wait else remaining
    return wait


def record(s: Session, kind: str, **values: str) -> None:
    now = utcnow()
    for key, _, _ in _keys(kind, values):
        s.add(RateEvent(kind=kind, key=key, created_at=now))
    s.execute(delete(RateEvent).where(RateEvent.created_at < now - RETENTION))


def clear(s: Session, kind: str, **values: str) -> None:
    keys = [key for key, _, _ in _keys(kind, values)]
    if keys:
        s.execute(delete(RateEvent).where(RateEvent.kind == kind, RateEvent.key.in_(keys)))


def describe(wait: timedelta) -> str:
    minutes = max(1, -(-int(wait.total_seconds()) // 60))     # round up
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
