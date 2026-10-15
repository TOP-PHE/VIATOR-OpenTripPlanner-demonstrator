"""Per-session runtime data: MCT overrides, station cross-reference, the
progress of a session's source refreshes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

SESSIONS_ID = "sessions.id"


class McTOverride(Base):
    """Minimum connection times — per-session, per-station, per-carrier-pair."""

    __tablename__ = "mct_overrides"

    session_id: Mapped[str] = mapped_column(String, ForeignKey(SESSIONS_ID), primary_key=True)
    station_code: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_a: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_b: Mapped[str] = mapped_column(String, primary_key=True)
    min_minutes: Mapped[int] = mapped_column(Integer, nullable=False)


class StationXref(Base):
    """Per-session bridge: feed-specific stop_id → master_stations.uic + extras."""

    __tablename__ = "stations_xref"
    __table_args__ = (ForeignKeyConstraint(["uic"], ["master_stations.uic"], name="fk_xref_uic"),)

    session_id: Mapped[str] = mapped_column(String, ForeignKey(SESSIONS_ID), primary_key=True)
    stop_id: Mapped[str] = mapped_column(String, primary_key=True)
    uic: Mapped[str | None] = mapped_column(String)
    trigramme: Mapped[str | None] = mapped_column(String)
    insee: Mapped[str | None] = mapped_column(String)
    rics: Mapped[str | None] = mapped_column(String)


# The states of a refresh record (app/refresh_progress.py). The CHECK below
# and the revision 20261015_1800_refresh_progress hold the same list.
REFRESH_STATES = ("running", "finished", "failed", "interrupted")
REFRESH_STATE_CHECK = "state IN ('running', 'finished', 'failed', 'interrupted')"


class SourceRefreshProgress(Base):
    """The latest refresh of one kind ("providers" or "osm") of a session.

    One row per session and kind, overwritten by the next refresh of that
    kind: it shows the running refresh, then its end result until the next
    one. `run_id` names the refresh that owns the row: every write is fenced
    by it, so a refresh that lost its lease (its heartbeat stopped for longer
    than `refresh_progress.LEASE_SECONDS`) can no longer change it.
    `detail` holds the counts and the feed being downloaded, `extract` the
    station-module follow-up of a provider refresh. Labels and counts only:
    never a URL, a reason text or a credential.
    """

    __tablename__ = "source_refresh_progress"
    __table_args__ = (
        CheckConstraint(REFRESH_STATE_CHECK, name="ck_source_refresh_progress_state"),
    )

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey(SESSIONS_ID, ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    extract: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
