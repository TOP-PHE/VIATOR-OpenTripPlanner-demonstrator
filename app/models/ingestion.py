"""Uploads and rebuild jobs (per session). See spec §5."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Upload(TimestampMixin, Base):
    __tablename__ = "uploads"
    __table_args__ = (
        Index("ix_uploads_session_created", "session_id", "created_at"),
        # v0.1.37 — "latest file for this provider" lookup on the card.
        Index("ix_uploads_session_provider", "session_id", "provider_feed_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # FIXME(step-7): session_id becomes NOT NULL once the sessions UI lands.
    session_id: Mapped[str | None] = mapped_column(String, ForeignKey("sessions.id"), nullable=True)
    # FIXME(step-3): user_id becomes NOT NULL once auth replaces basic auth.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    declared_kind: Mapped[str] = mapped_column(String, nullable=False)
    detected_kind: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    version_label: Mapped[str | None] = mapped_column(String)
    triggered_rebuild: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    # v0.1.37 — the provider (OTP feedId, e.g. "SNCF-XB") this file was
    # uploaded for, when the upload targeted a specific provider card.
    # NULL for the generic per-session upload path. See
    # docs/provider-source-modes-design.md.
    provider_feed_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RebuildJob(TimestampMixin, Base):
    __tablename__ = "rebuild_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','failed','cancelled')",
            name="status_valid",
        ),
        Index("ix_rebuild_jobs_session_created", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[str | None] = mapped_column(String, ForeignKey("sessions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    log: Mapped[str | None] = mapped_column(Text)
    graph_path: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
