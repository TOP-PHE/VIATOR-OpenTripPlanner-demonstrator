"""Journey searches, per-session executions, individual trips. See spec §6.3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class JourneySearch(Base):
    """The user's request — one row regardless of how many sessions answer it."""

    __tablename__ = "journey_searches"
    __table_args__ = (
        CheckConstraint("endpoint IN ('plan','compare','fanout')", name="endpoint_valid"),
        CheckConstraint(
            "requested_time_kind IN ('depart_at','arrive_by')",
            name="requested_time_kind_valid",
        ),
        CheckConstraint(
            "status IN ('ok','partial','no_route','error','timeout')",
            name="status_valid",
        ),
        Index("ix_journey_searches_ts", "ts"),
        Index("ix_journey_searches_user_ts", "user_id", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    ip: Mapped[str | None] = mapped_column(INET)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lon: Mapped[float] = mapped_column(Float, nullable=False)
    origin_label: Mapped[str | None] = mapped_column(String)
    dest_lat: Mapped[float] = mapped_column(Float, nullable=False)
    dest_lon: Mapped[float] = mapped_column(Float, nullable=False)
    dest_label: Mapped[str | None] = mapped_column(String)
    requested_time_kind: Mapped[str] = mapped_column(String, nullable=False)
    requested_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    modes: Mapped[str] = mapped_column(String, nullable=False)
    total_response_ms: Mapped[int | None] = mapped_column(Integer)
    total_trips_unique: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False)
    replay_of_search_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journey_searches.id")
    )


class JourneySearchExecution(Base):
    """One row per (search, session) pair. Carries the exact graph snapshot used."""

    __tablename__ = "journey_search_executions"
    __table_args__ = (
        CheckConstraint("status IN ('ok','no_route','error','timeout')", name="status_valid"),
        Index("ix_journey_executions_search", "search_id"),
        Index("ix_journey_executions_session_snap", "session_id", "graph_snapshot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journey_searches.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), nullable=False)
    # NULL when no snapshot was recorded for this session at execution time
    # (e.g. session has run a build but the worker hasn't written a
    # graph_snapshots row yet — Phase-3 wiring). Proper non-NULL operation
    # returns once the worker auto-creates snapshots on successful build.
    graph_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_snapshots.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    num_itineraries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    response_ms: Mapped[int | None] = mapped_column(Integer)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(String)


class JourneyTrip(Base):
    """Each itinerary OTP returned, decomposed into structured columns."""

    __tablename__ = "journey_trips"
    __table_args__ = (
        Index("ix_journey_trips_execution_rank", "execution_id", "rank_in_response"),
        Index("ix_journey_trips_signature", "trip_signature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journey_search_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    trip_signature: Mapped[str] = mapped_column(String(16), nullable=False)
    rank_in_response: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    num_transfers: Mapped[int] = mapped_column(Integer, nullable=False)
    departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    modes: Mapped[str] = mapped_column(String, nullable=False)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    fare: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
