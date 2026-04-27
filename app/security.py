"""Authentication / authorization dependencies.

Two authentication surfaces coexist:

- **`authed`** — HTTP basic auth, kept for the Phase-1 upload UI (`/`, `/upload`).
  The single basic-auth credential corresponds to the operator's `.env` config.

- **`current_user_jwt`** + **`require_*`** — JWT (cookie or Bearer header), used
  by everything new (auth API, admin API, future journey UI). This is the
  permanent contract; basic auth is on borrowed time.

The `require_*` dependencies enforce role-based access. Routes use:

    user = Depends(require_platform_admin)

That signature is permanent — body changes between phases are invisible to callers.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jose import JWTError

from .auth import tokens
from .settings import settings


_basic = HTTPBasic(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal for a request."""

    id: uuid.UUID | None       # None for the basic-auth shadow user
    username: str              # email for JWT users; basic-auth username otherwise
    role: str                  # 'platform_admin' | 'content_manager' | 'end_user'


# ────────────────────────── basic auth (Phase-1) ──────────────────────────


def _check_basic(creds: HTTPBasicCredentials) -> None:
    ok_user = secrets.compare_digest(creds.username, settings.admin_user)
    ok_pwd = secrets.compare_digest(creds.password, settings.admin_password)
    if not (ok_user and ok_pwd):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def authed(creds: Annotated[HTTPBasicCredentials | None, Depends(_basic)]) -> str:
    """Phase-1 basic auth — preserved for the upload UI on `/` and `/upload`."""
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
    except JWTError:
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


def client_ip(request: Request) -> str | None:
    """Best-effort client IP — respects X-Forwarded-For when present."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None
