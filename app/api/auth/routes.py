"""Auth API — register / confirm / login / logout / me / password-reset / bootstrap.

See spec §3 (User management) and §9.1 (Auth API).

Generic-204 semantics for /register-request and /password-reset-request prevent
email enumeration. Failure modes are still audit-logged; only the HTTP shape is
indistinguishable.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ... import audit, config_service
from ...auth import email as email_sender
from ...auth import passwords, tokens
from ...db import get_db
from ...models import PasswordResetToken, User, VerificationToken
from ...rate_limit import limiter
from ...security import CurrentUser, client_ip, current_user_jwt
from ...settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ─────────────────────── request / response schemas ───────────────────────


class RegisterRequestBody(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)


class RegisterConfirmBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH, max_length=200)


class CheckTokenResponse(BaseModel):
    email: str
    name: str
    expires_at: str


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    jwt: str
    role: str
    name: str


class PasswordResetRequestBody(BaseModel):
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH, max_length=200)


class BootstrapBody(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH, max_length=200)


class MeResponse(BaseModel):
    id: str
    email: str
    role: str


# ────────────────────────── helpers ──────────────────────────


def _set_jwt_cookie(response: Response, jwt: str) -> None:
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=jwt,
        httponly=True,
        secure=settings.jwt_cookie_secure,
        samesite="lax",
        path="/",
        max_age=tokens.jwt_cookie_max_age(),
    )


def _now() -> datetime:
    return datetime.now(UTC)


# ────────────────────────── register ──────────────────────────


@router.post("/register-request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def register_request(
    request: Request,
    body: RegisterRequestBody,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Initiate self-registration. Always returns 204 (no enumeration).

    Behaviour:
    - If registration is closed → no-op (still 204).
    - If a user already exists for this email → no-op (still 204).
    - Otherwise: create / refresh a verification token, send email magic link.
    """
    cfg = config_service.get_all(db)
    if not cfg["REGISTRATION_OPEN"]:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    existing = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()
    if existing is not None:
        # Quietly no-op. Audit so admins see the attempt.
        audit.record(
            db,
            action="register.requested.already_exists",
            actor_ip=client_ip(request),
            target_kind="email",
            target_id=str(body.email),
        )
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Drop any prior pending tokens for this email.
    db.execute(delete(VerificationToken).where(VerificationToken.email == body.email))

    raw, hashed = tokens.make_verification_token()
    db.add(
        VerificationToken(
            token_hash=hashed,
            email=body.email,
            name=body.name,
            expires_at=_now() + timedelta(hours=24),
        )
    )
    audit.record(
        db,
        action="register.requested",
        actor_ip=client_ip(request),
        target_kind="email",
        target_id=str(body.email),
        metadata={"name": body.name},
    )
    db.commit()

    magic_link = f"{settings.public_base_url.rstrip('/')}/confirm/{raw}"
    await email_sender.send_verification_email(
        to_email=str(body.email), name=body.name, magic_link=magic_link
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/check-token", response_model=CheckTokenResponse)
async def check_token(
    t: str,
    db: Annotated[Session, Depends(get_db)],
) -> CheckTokenResponse:
    """Validate a verification token before showing the password form."""
    row = db.get(VerificationToken, tokens.hash_verification_token(t))
    if row is None or row.consumed_at is not None:
        raise HTTPException(404, "Invalid or already-used token")
    if row.expires_at < _now():
        raise HTTPException(404, "Token expired")
    return CheckTokenResponse(email=row.email, name=row.name, expires_at=row.expires_at.isoformat())


@router.post("/register-confirm")
@limiter.limit("10/hour")
async def register_confirm(
    request: Request,
    body: RegisterConfirmBody,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Set the password and create the user. Returns JWT + sets cookie."""
    row = db.get(VerificationToken, tokens.hash_verification_token(body.token))
    if row is None or row.consumed_at is not None:
        raise HTTPException(400, "Invalid or already-used token")
    if row.expires_at < _now():
        raise HTTPException(400, "Token expired")

    cfg = config_service.get_all(db)
    role = cfg["REGISTRATION_DEFAULT_ROLE"]
    user = User(
        email=row.email,
        name=row.name,
        password_hash=passwords.hash_password(body.password),
        role=role,
    )
    db.add(user)
    db.flush()  # populate user.id
    db.delete(row)

    audit.record(
        db,
        action="register.confirmed",
        actor_user_id=user.id,
        actor_ip=client_ip(request),
        target_kind="user",
        target_id=str(user.id),
    )
    db.commit()

    jwt = tokens.issue_jwt(user.id, user.email, user.role)
    _set_jwt_cookie(response, jwt)
    return {"jwt": jwt, "role": user.role, "id": str(user.id)}


# ────────────────────────── login / logout / me ──────────────────────────


@router.post("/login", response_model=LoginResponse)
@limiter.limit("20/15minute")
async def login(
    request: Request,
    body: LoginBody,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    user = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()

    if user is None or not passwords.verify_password(body.password, user.password_hash):
        audit.record(
            db,
            action="login.failed",
            actor_ip=client_ip(request),
            target_kind="email",
            target_id=str(body.email),
        )
        db.commit()
        raise HTTPException(401, "Invalid credentials")

    if not user.is_active:
        audit.record(
            db,
            action="login.inactive",
            actor_user_id=user.id,
            actor_ip=client_ip(request),
        )
        db.commit()
        raise HTTPException(403, "Account is disabled")

    user.last_login_at = _now()
    audit.record(
        db,
        action="login.success",
        actor_user_id=user.id,
        actor_ip=client_ip(request),
    )
    db.commit()

    jwt = tokens.issue_jwt(user.id, user.email, user.role)
    _set_jwt_cookie(response, jwt)
    return LoginResponse(jwt=jwt, role=user.role, name=user.name)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    # Build the response we'll actually return, then drop the cookie on *that*
    # one. The auto-injected `response: Response` parameter pattern is a trap
    # here: any cookies set on it are discarded the moment we return a fresh
    # Response object instead of None.
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.jwt_cookie_name, path="/")
    return response


@router.get("/me", response_model=MeResponse)
async def me(
    user: Annotated[CurrentUser, Depends(current_user_jwt)],
) -> MeResponse:
    return MeResponse(
        id=str(user.id) if user.id else "",
        email=user.username,
        role=user.role,
    )


# ────────────────────────── password reset ──────────────────────────


@router.post("/password-reset-request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def password_reset_request(
    request: Request,
    body: PasswordResetRequestBody,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()
    if user is not None and user.is_active:
        # Drop any prior pending tokens for this user.
        db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
        raw, hashed = tokens.make_verification_token()
        db.add(
            PasswordResetToken(
                token_hash=hashed,
                user_id=user.id,
                expires_at=_now() + timedelta(hours=2),
            )
        )
        audit.record(
            db,
            action="password_reset.requested",
            actor_user_id=user.id,
            actor_ip=client_ip(request),
        )
        db.commit()
        magic_link = f"{settings.public_base_url.rstrip('/')}/reset/{raw}"
        await email_sender.send_password_reset_email(
            to_email=user.email, name=user.name, magic_link=magic_link
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password-reset-confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour")
async def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmBody,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    row = db.get(PasswordResetToken, tokens.hash_verification_token(body.token))
    if row is None or row.consumed_at is not None:
        raise HTTPException(400, "Invalid or already-used token")
    if row.expires_at < _now():
        raise HTTPException(400, "Token expired")

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(400, "Invalid token")

    user.password_hash = passwords.hash_password(body.password)
    db.delete(row)

    audit.record(
        db,
        action="password_reset.confirmed",
        actor_user_id=user.id,
        actor_ip=client_ip(request),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ────────────────────── first-platform-admin bootstrap ──────────────────────


@router.post("/bootstrap-platform-user")
@limiter.limit("3/hour")
async def bootstrap_platform_user(
    request: Request,
    body: BootstrapBody,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Create the first platform admin. Refuses once any platform admin exists.

    Set `BOOTSTRAP_TOKEN` in env on first deploy, hit this endpoint once,
    then unset the env var (or leave it — it stops working anyway).
    """
    existing = db.execute(
        select(User).where(User.role == "platform_admin").limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(403, "Bootstrap is closed: a platform admin already exists")

    if not settings.bootstrap_token:
        raise HTTPException(403, "Bootstrap is disabled (BOOTSTRAP_TOKEN unset)")
    if not secrets.compare_digest(body.token, settings.bootstrap_token):
        raise HTTPException(403, "Invalid bootstrap token")

    user = User(
        email=body.email,
        name=body.name,
        password_hash=passwords.hash_password(body.password),
        role="platform_admin",
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        action="bootstrap.platform_admin",
        actor_user_id=user.id,
        actor_ip=client_ip(request),
        target_kind="user",
        target_id=str(user.id),
    )
    db.commit()

    jwt = tokens.issue_jwt(user.id, user.email, user.role)
    _set_jwt_cookie(response, jwt)
    return {"jwt": jwt, "id": str(user.id), "role": user.role}
