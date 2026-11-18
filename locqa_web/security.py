"""Passwords, session tokens, CSRF.

Standard library only. scrypt is in `hashlib`, is memory-hard, and is what a
password hash should be; pulling in bcrypt or passlib would add a compiled
dependency for no gain.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# scrypt cost. n=2**14, r=8 is the interactive-login recommendation and costs
# ~16 MiB and tens of milliseconds per hash, which is the point.
_N, _R, _P = 2**14, 8, 1
_DKLEN = 32

MIN_PASSWORD = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt_hex),
        n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2,
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


# A real hash of a throwaway password, verified against when the email is
# unknown, so that "no such user" and "wrong password" take the same time and
# a login form cannot be used to enumerate accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_against_dummy(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
