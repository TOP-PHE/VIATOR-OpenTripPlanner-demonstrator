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
     headers tuple to pass to httpx. Covers all four auth schemes; the
     fifth (`none`) just returns the inputs unchanged.

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

import logging
import os
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from .models import UserCredential

log = logging.getLogger(__name__)


# ─────────────────────────────── auth types ───────────────────────────────

AuthType = Literal["none", "bearer", "basic", "query", "header"]
AUTH_TYPES: Final[tuple[AuthType, ...]] = ("none", "bearer", "basic", "query", "header")
AUTH_TYPES_REQUIRING_PARAM_NAME: Final[frozenset[str]] = frozenset({"query", "header"})


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
    return v  # type: ignore[return-value]


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
