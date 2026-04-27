"""JWT issuance/decode + verification-token hashing."""

from __future__ import annotations

import time
import uuid

import pytest
from jose import JWTError

from app.auth import tokens


def test_jwt_round_trip() -> None:
    uid = uuid.uuid4()
    jwt = tokens.issue_jwt(uid, "alice@example.org", "platform_admin")
    claims = tokens.decode_jwt(jwt)
    assert claims["sub"] == str(uid)
    assert claims["email"] == "alice@example.org"
    assert claims["role"] == "platform_admin"
    assert claims["exp"] > claims["iat"]


def test_jwt_tamper_detection() -> None:
    jwt = tokens.issue_jwt(uuid.uuid4(), "x@y.z", "end_user")
    # Flip a character in the payload section.
    head, payload, sig = jwt.split(".")
    tampered = (
        f"{head}.{payload[:-1]}A.{sig}" if payload[-1] != "A" else f"{head}.{payload[:-1]}B.{sig}"
    )
    with pytest.raises(JWTError):
        tokens.decode_jwt(tampered)


def test_jwt_expired_rejected() -> None:
    jwt = tokens.issue_jwt(uuid.uuid4(), "x@y.z", "end_user", ttl_seconds=1)
    time.sleep(1.5)
    with pytest.raises(JWTError):
        tokens.decode_jwt(jwt)


def test_verification_token_hash_is_stable() -> None:
    raw, hashed = tokens.make_verification_token()
    assert tokens.hash_verification_token(raw) == hashed
    # 32-byte digest
    assert len(hashed) == 32


def test_verification_token_is_high_entropy() -> None:
    samples = {tokens.make_verification_token()[0] for _ in range(20)}
    assert len(samples) == 20  # no collisions over 20 draws
    # url-safe base64, ≥ 32 chars
    for s in samples:
        assert len(s) >= 32
        assert all(c.isalnum() or c in "-_" for c in s)
