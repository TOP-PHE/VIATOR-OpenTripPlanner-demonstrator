"""User-credential encryption + httpx integration (v0.1.10).

Three concerns in one module to keep them auditable together:

  1. **Key derivation.** A 32-byte AES key is derived from `JWT_SECRET`
     via HKDF-SHA256 with a domain-separation salt + info string.
     Rotating `JWT_SECRET` invalidates every stored credential — this
     is the documented price of pinning to one bootstrap secret. Operators
     who rotate JWT_SECRET (rarely) re-enter their credentials.

  2. **AES-256-GCM encrypt / decrypt.** Authenticated encryption with a
     fresh 12-byte random nonce per write. Wrong key (or tampered
     ciphertext) raises `cryptography.exceptions.InvalidTag` — surfaced
     to the operator as "credential X cannot be decrypted (key changed?)"
     so they re-enter rather than silently fail the refresh.

  3. **httpx application.** `apply_to_request(...)` takes a stored
     credential + a base URL and returns the (possibly augmented) URL +
     headers tuple to pass to httpx. Covers all four static auth schemes;
     `none` just returns the inputs unchanged.

  4. **Login exchange.** `oauth2_password` stores a login, not a token:
     `{token_url, username, password[, client_id][, scope]}` as JSON. Each
     use posts an OAuth2 password grant to `token_url` and sends the
     returned access token as a Bearer header (`authorize(...)`). This is
     the flow the Austrian NAP (data.mobilitaetsverbuende.at, Keycloak)
     documents for scripted downloads, and the Slovenian NAP's B2B service
     (b2b.nap.si, no client id). Tokens are cached in-process until
     shortly before they expire.

Why the crypto lives next to the http-injection helper: the failure modes
are coupled. If decryption fails, the http call must not silently
proceed with no auth header (which would leak that the credential
*existed* via the response status). Keeping both here means there's one
place where "what we send to the provider" is computed.

Threat model (what this protects, what it does NOT):

  Protects against:
    - Postgres backup file leaking → secrets unreadable without JWT_SECRET
    - DBA reading rows directly → ciphertext+nonce bytes only
    - Read-only DB replica access → same as above

  Does NOT protect against:
    - Anyone with shell access to the web container (they can read
      JWT_SECRET from env)
    - Compromised Python code path (decrypt is just a function call)
    - Supply-chain attack on `cryptography` package itself

  The threat model matches "in-scope at-rest protection, no in-process
  isolation" — same as how Django/Rails store encrypted fields.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    import httpx

    from .models import UserCredential

log = logging.getLogger(__name__)


# ─────────────────────────────── auth types ───────────────────────────────

AuthType = Literal["none", "bearer", "basic", "query", "header", "oauth2_password"]
AUTH_TYPES: Final[tuple[AuthType, ...]] = (
    "none",
    "bearer",
    "basic",
    "query",
    "header",
    "oauth2_password",
)
AUTH_TYPES_REQUIRING_PARAM_NAME: Final[frozenset[str]] = frozenset({"query", "header"})
# Schemes whose secret is a login exchanged for a token at use time. They
# cannot be turned into static headers (OTP router-config, NAP catalogue
# import) — only `authorize()` can apply them.
AUTH_TYPES_NEEDING_LOGIN: Final[frozenset[str]] = frozenset({"oauth2_password"})


# ─────────────────────────────── crypto core ──────────────────────────────

# 12 bytes = AES-GCM standard nonce length. Larger nonces have a small
# perf hit; 96 bits is the sweet spot per NIST SP 800-38D.
_NONCE_BYTES: Final[int] = 12

# Domain-separation strings for HKDF. Changing either invalidates every
# stored credential, so don't.
_HKDF_SALT: Final[bytes] = b"viator-user-credentials-v1"
_HKDF_INFO: Final[bytes] = b"AES-256-GCM key for user_credentials.ciphertext"


def _derive_key(jwt_secret: str | bytes) -> bytes:
    """Derive a 32-byte AES key from `JWT_SECRET` via HKDF-SHA256.

    Why HKDF and not just `hashlib.sha256(JWT_SECRET).digest()`:
    HKDF separates extract (decorrelate input entropy) from expand
    (produce a key for a specific purpose, with `info=` binding). If we
    later want a second derived key (e.g. for different field), we can
    reuse the same input with a different `info=` and not have to
    reason about hash collisions.
    """
    if isinstance(jwt_secret, str):
        jwt_secret = jwt_secret.encode("utf-8")
    if not jwt_secret:
        raise ValueError(
            "JWT_SECRET is empty. Set it in .env before using credentials. See docker/.env.example."
        )
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,  # AES-256 → 32-byte key
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    return hkdf.derive(jwt_secret)


def encrypt(plaintext: str, jwt_secret: str | bytes) -> tuple[bytes, bytes]:
    """Encrypt a credential value. Returns (ciphertext, nonce) bytes.

    The nonce is fresh-random per call. NEVER reuse a (key, nonce) pair
    in GCM — the AESGCM constructor's contract is that the caller
    guarantees nonce uniqueness. Random 12-byte nonces give negligible
    collision probability over realistic DB sizes (< 2^32 writes).
    """
    if not plaintext:
        raise ValueError("credential plaintext cannot be empty")
    key = _derive_key(jwt_secret)
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return ciphertext, nonce


def decrypt(ciphertext: bytes, nonce: bytes, jwt_secret: str | bytes) -> str:
    """Decrypt a credential. Raises CredentialDecryptError on tamper or wrong key.

    Callers should catch CredentialDecryptError and surface a clean
    "credential cannot be decrypted (was JWT_SECRET rotated?)" message
    rather than letting the cryptography exception leak.
    """
    key = _derive_key(jwt_secret)
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise CredentialDecryptError(
            "credential ciphertext failed authentication "
            "(JWT_SECRET rotated, or row was tampered with)"
        ) from exc
    return plaintext.decode("utf-8")


class CredentialDecryptError(RuntimeError):
    """Raised when AES-GCM authentication fails on a stored credential.

    Most common cause: operator rotated JWT_SECRET in `.env`. The fix is
    to delete the affected credentials and have users re-create them —
    we don't carry a backup key.
    """


# ──────────────────────────── input validation ────────────────────────────


def validate_auth_type(value: str) -> AuthType:
    """Normalize + validate an auth type string. Rejects unknown schemes."""
    v = (value or "").strip().lower()
    if v not in AUTH_TYPES:
        raise ValueError(f"auth_type={value!r} unknown. Must be one of {list(AUTH_TYPES)}.")
    # mypy 2.0 narrows `v` to AuthType via the membership check above
    # (`not in AUTH_TYPES` followed by `raise`). The old explicit `cast()`
    # / `# type: ignore[return-value]` was needed only on mypy 1.13.0
    # which couldn't follow this narrowing. Audit-2026-05 follow-up to
    # dependency bump #69.
    return v


def validate_param_name(auth_type: AuthType, raw: str | None) -> str | None:
    """Enforce param_name presence/absence rules per auth type.

    Mirrors the CHECK constraint on user_credentials.
    """
    needs_name = auth_type in AUTH_TYPES_REQUIRING_PARAM_NAME
    name = (raw or "").strip() or None
    if needs_name and not name:
        raise ValueError(
            f"auth_type={auth_type!r} requires param_name "
            f"(URL key for query, header name for header)"
        )
    if not needs_name and name:
        raise ValueError(
            f"auth_type={auth_type!r} must not set param_name (only used for query / header)"
        )
    if name is not None and len(name) > 80:
        raise ValueError("param_name longer than 80 chars")
    return name


_OAUTH2_REQUIRED: Final[tuple[str, ...]] = ("token_url", "username", "password")
# client_id: Keycloak portals (AT) need one; b2b.nap.si (SI) has none.
_OAUTH2_OPTIONAL: Final[tuple[str, ...]] = ("client_id", "scope")


def parse_oauth2_password_secret(plaintext: str) -> dict[str, str]:
    """Parse + validate an `oauth2_password` secret. Raises ValueError.

    Error messages name the faulty field, never its value — the value may
    be the password.
    """
    try:
        raw = json.loads(plaintext)
    except ValueError as exc:
        raise ValueError("oauth2_password secret must be a JSON object") from exc
    if not isinstance(raw, dict):
        raise ValueError("oauth2_password secret must be a JSON object")
    unknown = set(raw) - set(_OAUTH2_REQUIRED) - set(_OAUTH2_OPTIONAL)
    if unknown:
        raise ValueError(f"oauth2_password secret has unknown fields {sorted(unknown)}")
    out: dict[str, str] = {}
    for field in (*_OAUTH2_REQUIRED, *_OAUTH2_OPTIONAL):
        value = raw.get(field)
        if value is None and field in _OAUTH2_OPTIONAL:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"oauth2_password secret needs a non-empty {field!r}")
        # A password may legitimately start or end with a space.
        out[field] = value if field == "password" else value.strip()
    parsed = urlparse(out["token_url"])
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("oauth2_password token_url must be an https URL")
    return out


def validate_secret(auth_type: AuthType, secret: str) -> str:
    """Return the secret to encrypt for `auth_type`. Raises ValueError.

    Static schemes store the value as typed. A login scheme is re-serialised
    so the stored JSON is canonical.
    """
    if auth_type == "oauth2_password":
        return json.dumps(parse_oauth2_password_secret(secret), separators=(",", ":"))
    return secret


# ────────────────────────── httpx integration ─────────────────────────────


def apply_to_request(
    url: str,
    *,
    auth_type: AuthType,
    plaintext: str,
    param_name: str | None,
) -> tuple[str, dict[str, str]]:
    """Compute (final_url, extra_headers) for an authenticated request.

    Caller pattern:
        url, headers = apply_to_request(url, auth_type=..., plaintext=..., param_name=...)
        await client.get(url, headers=headers)

    For `none` we don't pass through here; the call site gates on
    auth_type == "none" and skips this entirely.

    Header conflicts: the returned headers dict is meant to be passed
    *as-is* to httpx (which merges with client defaults). We don't try
    to detect / resolve clashes with caller-supplied headers — if a
    caller already sets `Authorization`, the credential's would
    overwrite it via dict merge order. That's the documented contract.
    """
    if auth_type == "none":
        # Defensive — call sites should skip us entirely for `none`,
        # but if they don't, a no-op is the safest behaviour.
        return url, {}

    if auth_type == "bearer":
        return url, {"Authorization": f"Bearer {plaintext}"}

    if auth_type == "basic":
        # plaintext is "user:pass". httpx-style basic encoding.
        import base64

        token = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        return url, {"Authorization": f"Basic {token}"}

    if auth_type == "header":
        if not param_name:
            # Should be caught by validate_param_name on save.
            raise ValueError("header auth_type requires param_name")
        return url, {param_name: plaintext}

    if auth_type in AUTH_TYPES_NEEDING_LOGIN:
        # A login is not a header. Callers that can log in use authorize().
        raise ValueError(f"auth_type {auth_type!r} needs a login exchange; use authorize()")

    if auth_type == "query":
        if not param_name:
            raise ValueError("query auth_type requires param_name")
        # Append the param to the URL's query string. We preserve any
        # existing params (parse → mutate → re-serialize) so a URL like
        # `https://x/y?format=json` becomes `https://x/y?format=json&apikey=...`
        # rather than overwriting the format.
        parsed = urlparse(url)
        params = list(parse_qsl(parsed.query, keep_blank_values=True))
        # If the operator already set the same param in the URL, we
        # overwrite (their key wins). This matches the principle of
        # least surprise: the credential is what the user picked from
        # the picker, not whatever they accidentally pasted in the URL.
        params = [(k, v) for k, v in params if k != param_name]
        params.append((param_name, plaintext))
        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query)), {}

    # Defensive: AUTH_TYPES is closed, but mypy + future-proofing.
    raise ValueError(f"unsupported auth_type {auth_type!r}")


def apply_credential(
    credential: UserCredential,
    url: str,
    jwt_secret: str | bytes,
) -> tuple[str, dict[str, str]]:
    """Convenience wrapper: decrypt a stored credential and apply to URL.

    Pattern at the call site:
        cred = db.get(UserCredential, credential_id)
        if cred is None:
            log.warning("credential %s not found — falling back to anonymous", credential_id)
            url_to_fetch, headers = url, {}
        else:
            try:
                url_to_fetch, headers = apply_credential(cred, url, settings.jwt_secret)
            except CredentialDecryptError as exc:
                log.error("credential %s cannot be decrypted: %s", credential_id, exc)
                raise   # surface to operator
        await client.get(url_to_fetch, headers=headers)
    """
    plaintext = decrypt(credential.ciphertext, credential.nonce, jwt_secret)
    return apply_to_request(
        url,
        auth_type=credential.auth_type,  # type: ignore[arg-type]
        plaintext=plaintext,
        param_name=credential.param_name,
    )


# ──────────────────────────── login exchange ──────────────────────────────


class CredentialLoginError(RuntimeError):
    """The login exchange failed (bad password, licence not accepted,
    portal down). The message is operator-facing and never holds a secret."""


# (credential id, nonce) -> (access_token, monotonic expiry). The nonce
# changes on every secret rotation, so a rotated login never reuses a token
# minted from the old one.
_token_cache: dict[tuple[str, bytes], tuple[str, float]] = {}
# Refresh this long before the portal's expiry: a long download must not
# start with a token that dies in its first second.
_TOKEN_EXPIRY_MARGIN_S: Final[float] = 30.0
_TOKEN_DEFAULT_TTL_S: Final[float] = 60.0


def _login_error_detail(resp: httpx.Response) -> str:
    """`invalid_grant: Invalid user credentials` from an OAuth2 error body,
    else the bare status. Only the two standard error fields are read, so a
    portal echoing the request back cannot leak the password into a log."""
    detail = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return detail
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        detail += f" {body['error'][:80]}"
        if isinstance(body.get("error_description"), str):
            detail += f": {body['error_description'][:200]}"
    return detail


async def fetch_oauth2_token(client: httpx.AsyncClient, login: dict[str, str]) -> tuple[str, float]:
    """OAuth2 password grant. Returns (access_token, lifetime_seconds)."""
    import httpx as _httpx

    # Late import: app.master pulls in heavier modules than this one needs.
    from .master.nap_importer import _validate_safe_http_url

    try:
        token_url = await asyncio.to_thread(_validate_safe_http_url, login["token_url"])
    except ValueError as exc:
        raise CredentialLoginError(f"token URL refused: {exc}") from exc
    form = {
        "grant_type": "password",
        "username": login["username"],
        "password": login["password"],
    }
    for optional in _OAUTH2_OPTIONAL:
        if login.get(optional):
            form[optional] = login[optional]
    try:
        # No redirects: a token endpoint that redirects is misconfigured,
        # and following it would re-post the password to another URL.
        resp = await client.post(
            token_url,
            data=form,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
    except _httpx.HTTPError as exc:
        raise CredentialLoginError(f"login request to {token_url} failed: {exc}") from exc
    if resp.status_code != 200:
        raise CredentialLoginError(f"login refused by {token_url}: {_login_error_detail(resp)}")
    try:
        body: Any = resp.json()
    except ValueError as exc:
        raise CredentialLoginError(f"login answer from {token_url} is not JSON") from exc
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialLoginError(f"login answer from {token_url} has no access_token")
    expires_in = body.get("expires_in")
    ttl = (
        float(expires_in)
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else _TOKEN_DEFAULT_TTL_S
    )
    return token, ttl


async def authorize(
    client: httpx.AsyncClient,
    credential: UserCredential,
    url: str,
    jwt_secret: str | bytes,
) -> tuple[str, dict[str, str]]:
    """Async `apply_credential` that also handles login schemes.

    Raises CredentialDecryptError (key rotated / tampered row) or
    CredentialLoginError (login refused / portal unreachable).
    """
    plaintext = decrypt(credential.ciphertext, credential.nonce, jwt_secret)
    if credential.auth_type not in AUTH_TYPES_NEEDING_LOGIN:
        return apply_to_request(
            url,
            auth_type=credential.auth_type,  # type: ignore[arg-type]
            plaintext=plaintext,
            param_name=credential.param_name,
        )
    try:
        login = parse_oauth2_password_secret(plaintext)
    except ValueError as exc:
        raise CredentialLoginError(f"stored login is unusable: {exc}") from exc
    key = (str(credential.id), bytes(credential.nonce))
    cached = _token_cache.get(key)
    if cached is None or cached[1] <= time.monotonic():
        token, ttl = await fetch_oauth2_token(client, login)
        expiry = time.monotonic() + max(ttl - _TOKEN_EXPIRY_MARGIN_S, 0.0)
        _token_cache[key] = (token, expiry)
        cached = (token, expiry)
    return url, {"Authorization": f"Bearer {cached[0]}"}
