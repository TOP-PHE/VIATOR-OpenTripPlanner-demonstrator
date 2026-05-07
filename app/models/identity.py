"""Users, verification tokens, password-reset tokens. See spec §3."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


# StrEnum (Python 3.11+) is the canonical replacement for the older
# `class X(str, Enum)` mixin. Behaviour is identical for our usage:
#   - Equality with raw strings still works (`UserRole.END_USER == "end_user"`)
#   - `str(member)` and `f"{member}"` return the value (e.g. "end_user"),
#     which is what templates, JS, and CSS class names already assume
#     (see `app/templates/admin/users.html` + `_base.html`).
# Audit-2026-05 #24 (ruff UP042).
class UserRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    CONTENT_MANAGER = "content_manager"
    END_USER = "end_user"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('platform_admin','content_manager','end_user')",
            name="role_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationToken(Base):
    """Magic-link tokens for email confirmation. Stored hashed (sha256), single-use."""

    __tablename__ = "verification_tokens"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    """Magic-link tokens for password reset. Stored hashed, single-use."""

    __tablename__ = "password_reset_tokens"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        # FK declared in migration to avoid circular import; index follows.
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
