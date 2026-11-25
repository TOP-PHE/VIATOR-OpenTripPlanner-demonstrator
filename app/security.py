"""Authentication / authorization dependencies.

Two authentication surfaces coexist:

- **`authed`** — HTTP basic auth, kept for the Phase-1 upload UI (`/`, `/upload`).
  The single basic-auth credential corresponds to the operator's `.env` config,
  and exists only when BOTH `ADMIN_USER` and `ADMIN_PASSWORD` are set — an empty
  setting switches the surface off.

- **`current_user_jwt`** + **`require_*`** — JWT (cookie or Bearer header), used
  by everything new (auth API, admin API, future journey UI). This is the
  permanent contract; basic auth is on borrowed time.

The `require_*` dependencies enforce role-based access. Routes use:

    user = Depends(require_platform_admin)

That signature is permanent — body changes between phases are invisible to callers.
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jwt import PyJWTError

from .auth import tokens
from .settings import settings

_basic = HTTPBasic(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal for a request."""

    id: uuid.UUID | None  # None for the basic-auth shadow user
    username: str  # email for JWT users; basic-auth username otherwise
    role: str  # 'platform_admin' | 'content_manager' | 'end_user'


# ────────────────────────── basic auth (Phase-1) ──────────────────────────


def _basic_configured() -> bool:
    """The legacy credential exists only when both halves are set."""
    return bool(settings.admin_user and settings.admin_password)


def _check_basic(creds: HTTPBasicCredentials) -> None:
    ok_user = secrets.compare_digest(creds.username, settings.admin_user)
    ok_pwd = secrets.compare_digest(creds.password, settings.admin_password)
    if not (_basic_configured() and ok_user and ok_pwd):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def authed(creds: Annotated[HTTPBasicCredentials | None, Depends(_basic)]) -> str:
    """Phase-1 basic auth — preserved for the upload UI on `/` and `/upload`.

    Always 401 while `ADMIN_USER` or `ADMIN_PASSWORD` is empty (Phase-2 mode).
    """
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    _check_basic(creds)
    return creds.username


def authed_or_none(
    creds: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str | None:
    """Like `authed`, but returns None when admin_user is empty (Phase-2 mode).

    Allows the root index route to redirect Phase-2 users to `/login`
    instead of triggering the browser's native basic-auth challenge — the
    main UX papercut on a fresh Phase-2 deployment where someone hits the
    bare hostname out of habit.
    """
    if not settings.admin_user:
        return None
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    _check_basic(creds)
    return creds.username


# ────────────────────────────── JWT decode ──────────────────────────────


def _extract_jwt(request: Request) -> str | None:
    """Cookie first (browser flow), then Authorization: Bearer (API clients)."""
    cookie = request.cookies.get(settings.jwt_cookie_name)
    if cookie:
        return cookie
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        return authz[7:]
    return None


def _decode_to_user(token: str | None) -> CurrentUser | None:
    if not token:
        return None
    try:
        claims = tokens.decode_jwt(token)
    except PyJWTError:
        return None
    sub = claims.get("sub")
    email = claims.get("email")
    role = claims.get("role")
    if not (sub and email and role):
        return None
    try:
        user_id = uuid.UUID(str(sub))
    except ValueError:
        return None
    return CurrentUser(id=user_id, username=str(email), role=str(role))


def current_user_jwt(request: Request) -> CurrentUser:
    """Resolve the authenticated user from JWT. 401 on missing/invalid."""
    user = _decode_to_user(_extract_jwt(request))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ────────────────────────── role-based gates ──────────────────────────


def _require_role(*allowed: str):  # type: ignore[no-untyped-def]
    """Build a FastAPI dependency that gates on `role in allowed`."""

    def _dep(user: Annotated[CurrentUser, Depends(current_user_jwt)]) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {sorted(allowed)}",
            )
        return user

    return _dep


require_logged_in = _require_role("platform_admin", "content_manager", "end_user")
require_content_manager = _require_role("platform_admin", "content_manager")
require_platform_admin = _require_role("platform_admin")


# ────────────────────────── client IP helper ──────────────────────────


# The header nginx writes itself on every request it passes to web
# (`proxy_set_header X-Real-IP $remote_addr;` in docker/nginx/nginx.conf): the
# address of the connection nginx accepted, in place of whatever the client
# sent under that name.
PROXY_CLIENT_ADDRESS_HEADER = "X-Real-IP"


def _as_ip(raw: str | None) -> str | None:
    """`raw` in its canonical form when it is an IP address, else None."""
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str | None:
    """The client's address as nginx saw it: the one written in audit events
    and the one the rate limiter counts by (`rate_limit.client_key`).

    It is nginx's `X-Real-IP`, and the address of the connection itself for a
    request that carries none (a call made inside the Docker network). It is
    never read from `X-Forwarded-For`: that header is a list the client can
    start with any value it likes.

    **What this relies on.** web publishes no port (docker-compose.yml), so the
    only way in from outside is nginx, which always sets the header. A
    container of the Docker network can call web directly and send any
    `X-Real-IP`: those containers are trusted here. If web ever becomes
    reachable without nginx, or behind a second proxy, this header is the
    client's to choose again (docs/architecture.md, "Invariants & traps").

    Returns None when neither value is an IP address: `request.client.host`
    is the literal `"testclient"` under FastAPI's TestClient, and
    `audit_events.actor_ip` is `inet NULL`, so a value that is not an address
    is left out rather than failing the audit insert and losing the event.
    """
    peer = request.client.host if request.client else None
    return _as_ip(request.headers.get(PROXY_CLIENT_ADDRESS_HEADER)) or _as_ip(peer)
