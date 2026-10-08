"""Session-JWT behaviour pinned independently of the JWT library (#319).

These tests build tokens by hand with the standard library (hmac, hashlib,
base64, json), in the exact compact form the app has always issued: header
`{"alg":"HS256","typ":"JWT"}` (keys sorted, no spaces), payload claims in the
order sub, email, role, iat, exp, HMAC-SHA256 signature, base64url without
padding. They pin what the app accepts and refuses, so that swapping the
JWT library cannot silently change it, and so that a session cookie issued
before the swap still decodes after it.

The signing secret is generated at run time; nothing here is a real secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt.api_jwt
import pytest
from fastapi import HTTPException, Request
from jwt import PyJWTError

from app import security
from app.auth import tokens

_HS256_HEADER = {"alg": "HS256", "typ": "JWT"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json(obj: Any, *, sort_keys: bool = False) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=sort_keys).encode("utf-8")


def _sign(signing_input: str, secret: str, digest: Any = hashlib.sha256) -> str:
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), digest)
    return _b64url(mac.digest())


def _token(
    claims: dict[str, Any],
    secret: str,
    header: dict[str, Any] | None = None,
    digest: Any = hashlib.sha256,
) -> str:
    """A compact JWS in the byte-exact form the app issues for HS256."""
    head = _b64url(_json(header if header is not None else _HS256_HEADER, sort_keys=True))
    body = _b64url(_json(claims))
    signing_input = f"{head}.{body}"
    return f"{signing_input}.{_sign(signing_input, secret, digest)}"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "email": "zz.user@example.invalid",
        "role": "content_manager",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    value = secrets.token_urlsafe(32)
    monkeypatch.setattr(tokens.settings, "jwt_secret", value)
    monkeypatch.setattr(tokens.settings, "jwt_alg", "HS256")
    return value


def _refused(token: str) -> None:
    """Refused at both layers: decode_jwt raises, the request has no user."""
    with pytest.raises(PyJWTError):
        tokens.decode_jwt(token)
    assert security._decode_to_user(token) is None


def _bearer_request(token: str) -> Request:
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))]}
    )


# ───────────────────────── issuing: byte format ─────────────────────────


def test_issued_token_is_byte_identical_to_the_hand_built_form(
    secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed

    monkeypatch.setattr(tokens, "datetime", _Frozen)
    uid = uuid.UUID(int=0x99)
    issued = tokens.issue_jwt(uid, "zz.admin@example.invalid", "platform_admin", ttl_seconds=600)
    expected = _token(
        {
            "sub": str(uid),
            "email": "zz.admin@example.invalid",
            "role": "platform_admin",
            "iat": int(fixed.timestamp()),
            "exp": int(fixed.timestamp()) + 600,
        },
        secret,
    )
    assert issued == expected


def test_issued_claims_are_strings_and_integers(secret: str) -> None:
    claims = tokens.decode_jwt(tokens.issue_jwt(uuid.uuid4(), "zz@example.invalid", "end_user"))
    assert set(claims) == {"sub", "email", "role", "iat", "exp"}
    assert isinstance(claims["sub"], str)
    assert type(claims["iat"]) is int
    assert type(claims["exp"]) is int
    assert claims["exp"] - claims["iat"] == tokens.settings.jwt_ttl_seconds


# ─────────────────── accepting: tokens issued earlier ───────────────────


def test_token_issued_before_the_library_change_still_decodes(secret: str) -> None:
    claims = _claims()
    token = _token(claims, secret)
    assert tokens.decode_jwt(token) == claims
    user = security._decode_to_user(token)
    assert user == security.CurrentUser(
        id=uuid.UUID(claims["sub"]), username=claims["email"], role=claims["role"]
    )


def test_token_without_exp_is_accepted(secret: str) -> None:
    # Today's behaviour: exp is checked when present, not required.
    claims = _claims()
    del claims["exp"]
    assert tokens.decode_jwt(_token(claims, secret)) == claims


def test_token_with_iat_in_the_future_is_refused(secret: str) -> None:
    # Changed by #319: python-jose only checked that iat was a number; PyJWT
    # also refuses an iat more than the leeway (1 s) ahead of now. issue_jwt
    # sets iat to the current whole second, so no token the app issued is
    # affected; only a hand-made one signed with our secret could be.
    _refused(_token(_claims(iat=int(time.time()) + 3600), secret))


def test_token_with_nbf_in_the_future_is_refused(secret: str) -> None:
    _refused(_token(_claims(nbf=int(time.time()) + 3600), secret))


def test_expiry_leeway_is_one_second() -> None:
    assert tokens._EXP_LEEWAY_SECONDS == 1


@pytest.fixture
def frozen_pyjwt_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    """Freeze PyJWT's clock 0.9 s into a whole second; return that second.

    0.9 s in is where a leeway below one second would already refuse a
    token whose exp is that second, so the boundary tests below fail for
    any leeway other than 1.
    """
    second = int(time.time())
    fixed = datetime.fromtimestamp(second + 0.9, tz=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed

    monkeypatch.setattr(jwt.api_jwt, "datetime", _Frozen)
    return second


def test_exp_equal_to_the_current_second_is_still_accepted(
    secret: str, frozen_pyjwt_clock: int
) -> None:
    token = _token(_claims(iat=frozen_pyjwt_clock - 60, exp=frozen_pyjwt_clock), secret)
    assert security._decode_to_user(token) is not None


def test_exp_one_second_in_the_past_is_refused(secret: str, frozen_pyjwt_clock: int) -> None:
    token = _token(_claims(iat=frozen_pyjwt_clock - 60, exp=frozen_pyjwt_clock - 1), secret)
    _refused(token)


# ─────────────────────────────── refusing ───────────────────────────────


def test_expired_token_is_refused(secret: str) -> None:
    now = int(time.time())
    _refused(_token(_claims(iat=now - 7200, exp=now - 60), secret))


def test_tampered_signature_is_refused(secret: str) -> None:
    head, body, sig = _token(_claims(), secret).split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    _refused(f"{head}.{body}.{flipped}")


def test_tampered_payload_is_refused(secret: str) -> None:
    token = _token(_claims(role="end_user"), secret)
    head, _body, sig = token.split(".")
    forged = _b64url(_json(_claims(role="platform_admin")))
    _refused(f"{head}.{forged}.{sig}")


def test_wrong_secret_is_refused(secret: str) -> None:
    _refused(_token(_claims(), secrets.token_urlsafe(32)))


@pytest.mark.parametrize("signature", ["", "AAAA"], ids=["empty-sig", "junk-sig"])
def test_alg_none_is_refused(secret: str, signature: str) -> None:
    head = _b64url(_json({"alg": "none", "typ": "JWT"}, sort_keys=True))
    body = _b64url(_json(_claims()))
    _refused(f"{head}.{body}.{signature}")


@pytest.mark.parametrize("alg", ["RS256", "ES256", "PS256"])
def test_other_alg_header_is_refused_even_when_hmac_signed(secret: str, alg: str) -> None:
    # Algorithm confusion: the header claims another algorithm while the
    # signature is an HMAC with our secret. Only HS256 is ever accepted.
    _refused(_token(_claims(), secret, header={"alg": alg, "typ": "JWT"}))


@pytest.mark.parametrize(
    ("alg", "digest"),
    [("HS384", hashlib.sha384), ("HS512", hashlib.sha512)],
    ids=["HS384", "HS512"],
)
def test_other_hmac_alg_is_refused_by_the_allow_list(secret: str, alg: str, digest: Any) -> None:
    # Correctly signed for its own algorithm with our secret, so only
    # `algorithms=[settings.jwt_alg]` can refuse it.
    _refused(_token(_claims(), secret, header={"alg": alg, "typ": "JWT"}, digest=digest))


def test_non_string_sub_is_refused(secret: str) -> None:
    _refused(_token(_claims(sub=12345), secret))


def test_audience_claim_is_refused(secret: str) -> None:
    # No audience is configured, so a token naming one does not match.
    _refused(_token(_claims(aud="zz-other-service"), secret))


@pytest.mark.parametrize(
    "token",
    [
        "x",
        "a.b",
        "a.b.c.d",
        "..",
        "!!!.???.***",
        "eyJhbGciOiJIUzI1NiJ9.e30",
    ],
    ids=["one-part", "two-parts", "four-parts", "empty-parts", "not-base64", "no-signature"],
)
def test_malformed_tokens_give_no_user(secret: str, token: str) -> None:
    _refused(token)


def test_payload_that_is_not_an_object_is_refused(secret: str) -> None:
    head = _b64url(_json(_HS256_HEADER, sort_keys=True))
    body = _b64url(_json(["zz", "list"]))
    signing_input = f"{head}.{body}"
    _refused(f"{signing_input}.{_sign(signing_input, secret)}")


def test_missing_identity_claim_gives_no_user(secret: str) -> None:
    # decode succeeds, but security.py insists on sub, email and role.
    claims = _claims()
    del claims["role"]
    token = _token(claims, secret)
    assert tokens.decode_jwt(token) == claims
    assert security._decode_to_user(token) is None


@pytest.mark.parametrize("claim", ["exp", "iat"])
@pytest.mark.parametrize(
    "value",
    [None, [1, 2], float("inf")],
    ids=["null", "list", "infinity"],
)
def test_malformed_time_claim_is_a_401_not_a_crash(secret: str, claim: str, value: Any) -> None:
    # Fixed by #319: python-jose raised TypeError / OverflowError on these
    # (signed with our secret), which escaped security.py as an HTTP 500.
    token = _token(_claims(**{claim: value}), secret)
    _refused(token)
    request = _bearer_request(token)
    with pytest.raises(HTTPException) as caught:
        security.current_user_jwt(request)
    assert caught.value.status_code == 401
