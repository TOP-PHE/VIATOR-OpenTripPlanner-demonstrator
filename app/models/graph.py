"""Graph snapshots — every successful OTP build is a first-class anchor.

Two-level versioning:
  timetable_main_version  (e.g. '2026-W14_2026-W39')  — the calendar period
  timetable_update_version (1, 2, 3, ...)              — sequential within main

See spec §6.6.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GraphSnapshot(Base):
    __tablename__ = "graph_snapshots"
    __table_args__ = (
        CheckConstraint(
            "main_version_source IN ('auto','manual_override')",
            name="main_version_source_valid",
        ),
        # Sequential update-version uniqueness within (session, main_version)
        UniqueConstraint(
            "session_id",
            "timetable_main_version",
            "timetable_update_version",
            name="unique_update_within_main",
        ),
        Index("ix_graph_snapshots_session_built", "session_id", "built_at"),
        Index(
            "ix_graph_snapshots_main_version",
            "session_id",
            "timetable_main_version",
            "timetable_update_version",
        ),
        # At most one current snapshot per session — enforced by partial unique index.
        Index(
            "uq_graph_snapshots_one_current_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), nullable=False)
    rebuild_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rebuild_jobs.id")
    )
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    graph_path: Mapped[str] = mapped_column(String, nullable=False)
    source_uploads: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    feed_signature: Mapped[str] = mapped_column(String(64), nullable=False)

    # Two-level versioning
    timetable_main_version: Mapped[str] = mapped_column(String, nullable=False)
    timetable_update_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    service_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    service_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    main_version_source: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'auto'")
    )

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
