"""JWTs (for sessions) and verification tokens (for email magic links).

Verification tokens are stored **hashed** (sha256) in the DB, so a leaked DB
dump does not yield usable tokens.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt as jose_jwt

from ..settings import settings


# ────────────────────────────── JWT ──────────────────────────────


def issue_jwt(
    user_id: uuid.UUID,
    email: str,
    role: str,
    *,
    ttl_seconds: int | None = None,
) -> str:
    """Mint a session JWT. Caller is responsible for setting it as a cookie."""
    now = datetime.now(timezone.utc)
    ttl = ttl_seconds if ttl_seconds is not None else settings.jwt_ttl_seconds
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    return jose_jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_jwt(token: str) -> dict[str, Any]:
    """Decode + verify signature + check expiry. Raises jose.JWTError on failure."""
    return jose_jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])


# ──────────────────── Verification tokens ────────────────────


# 32 bytes URL-safe base64 → 43 characters; ample entropy.
_VERIFICATION_TOKEN_BYTES = 32


def make_verification_token() -> tuple[str, bytes]:
    """Return (raw_token_for_email, sha256_hash_for_db_storage).

    Caller emails the raw value; only the hash is persisted.
    """
    raw = secrets.token_urlsafe(_VERIFICATION_TOKEN_BYTES)
    return raw, hash_verification_token(raw)


def hash_verification_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


# ─────────── helper for callers wiring the cookie / TTL ───────────


def jwt_cookie_max_age() -> int:
    return settings.jwt_ttl_seconds
