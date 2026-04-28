"""Admin user-management endpoints. See spec §9.2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import audit
from ...auth import passwords
from ...db import get_db
from ...models import User
from ...models.identity import UserRole
from ...security import CurrentUser, client_ip, require_platform_admin

router = APIRouter(prefix="/api/users", tags=["admin", "users"])


_VALID_ROLES = {r.value for r in UserRole}


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    last_login_at: str | None
    created_at: str

    @classmethod
    def from_orm_user(cls, u: User) -> UserResponse:
        return cls(
            id=str(u.id),
            email=u.email,
            name=u.name,
            role=u.role,
            is_active=u.is_active,
            last_login_at=_iso(u.last_login_at),
            created_at=_iso(u.created_at) or "",
        )


class UserCreate(BaseModel):
    """Direct user creation by a platform_admin.

    The admin sets the initial password and shares it out-of-band. The user
    can then change it via the password-reset flow. Use this when SMTP isn't
    configured yet, or when you don't want to wait on email delivery.
    """

    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    role: str
    password: str = Field(min_length=passwords.MIN_PASSWORD_LENGTH, max_length=200)


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


@router.get("", response_model=list[UserResponse])
def list_users(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[UserResponse]:
    users = db.execute(select(User).order_by(User.created_at)).scalars().all()
    return [UserResponse.from_orm_user(u) for u in users]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> UserResponse:
    """Create a user directly. Platform-admin only.

    Validates: role is one of the three allowed values; email is unique.
    Pydantic validates: email syntax, password ≥ MIN_PASSWORD_LENGTH.

    The new user is active immediately (`is_active=True`) and can log in with
    the password supplied here. The audit row records the creation; the
    password itself is never logged.
    """
    if payload.role not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid role: {payload.role}")

    existing = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "A user with this email already exists")

    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=passwords.hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.flush()  # populate user.id

    audit.record(
        db,
        action="user.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="user",
        target_id=str(user.id),
        metadata={"email": str(user.email), "role": user.role, "name": user.name},
    )
    db.commit()
    return UserResponse.from_orm_user(user)


@router.patch("/{user_id}", response_model=UserResponse)
def patch_user(
    user_id: uuid.UUID,
    payload: UserPatch,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> UserResponse:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")

    changes: dict[str, dict[str, object]] = {}

    if payload.role is not None:
        if payload.role not in _VALID_ROLES:
            raise HTTPException(400, f"Invalid role: {payload.role}")
        if target.id == actor.id and payload.role != "platform_admin":
            raise HTTPException(400, "Cannot demote yourself from platform_admin")
        if target.role != payload.role:
            changes["role"] = {"from": target.role, "to": payload.role}
            target.role = payload.role

    if payload.is_active is not None:
        if target.id == actor.id and not payload.is_active:
            raise HTTPException(400, "Cannot deactivate yourself")
        if target.is_active != payload.is_active:
            changes["is_active"] = {"from": target.is_active, "to": payload.is_active}
            target.is_active = payload.is_active

    if changes:
        audit.record(
            db,
            action="user.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="user",
            target_id=str(target.id),
            metadata={"changes": changes},
        )
    db.commit()
    return UserResponse.from_orm_user(target)
