"""ORM models for the v0.1.27 network-coverage feature.

Two tables:

  network_coverage_runs     — one row per "Run" click. Carries the
                              session_id, departure datetime, and overall
                              run state (running / completed / failed).
                              Multiple runs against the same session +
                              datetime are allowed and useful for
                              before/after testing (rebuild a graph,
                              re-run, compare).

  network_coverage_results  — one row per (run, origin, destination)
                              triple. Persists the per-pair outcome
                              (status / num_itineraries / shortest
                              duration / response time / journey_search
                              FK for click-cell drilldown).

The journey_search_id link reuses the existing journey_searches /
journey_trips infrastructure — no duplication, and the v0.1.26 trip-
card UI works as the click-cell drilldown out of the box.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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


class NetworkCoverageRun(Base):
    __tablename__ = "network_coverage_runs"
    __table_args__ = (
        Index("ix_network_coverage_runs_started_at", "started_at"),
        Index("ix_network_coverage_runs_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    # FK to sessions.id (string PK). When the operator deletes a session
    # we keep the run history — coverage is more useful as a long-term
    # comparison record than as a strict-FK constraint. ON DELETE SET
    # NULL on the FK keeps history but unlinks the dead session.
    session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.id", ondelete="SET NULL")
    )
    # Snapshot of the session id at run time, for human display when the
    # FK has gone NULL (so the operator still sees "ran against
    # nap-fr-rail-experimental" even after deleting that session).
    session_label: Mapped[str] = mapped_column(String, nullable=False)

    # Departure datetime the run is searching from. Stored timezone-aware;
    # OTP interprets it in the session's transitModelTimeZone (v0.1.21+).
    depart_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Hub-set identifier — for v0.1.27 always "fr-major-23". Stored so
    # future releases that add Eurostar / Brussels / Frankfurt can run
    # comparable matrices without conflating the data.
    hub_set: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'fr-major-23'")
    )
    # Direction mode: "both" runs A→B and B→A separately; "single" only
    # runs A→B for A < B (half the work, loses asymmetry detection).
    direction: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'both'")
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # State machine: pending → running → (completed | failed | cancelled)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )

    # Counters updated by the runner as it progresses. The UI uses these
    # for the live progress bar without having to aggregate over the
    # results table.
    total_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_pairs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ok_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    no_route_pairs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_pairs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # Catch-all for any post-run summary the runner wants to attach
    # (e.g. average response time, slowest pair, list of timed-out pairs).
    summary: Mapped[dict | None] = mapped_column(JSONB)


class NetworkCoverageResult(Base):
    __tablename__ = "network_coverage_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "origin_hub_id", "dest_hub_id",
            name="unique_result_per_run_pair",
        ),
        Index("ix_network_coverage_results_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_coverage_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Hub slugs from app/network_coverage/hubs.py (e.g. "paris-gdl",
    # "marseille-stc"). Not FK-constrained so a future hub-set update
    # that retires a slug doesn't orphan history rows.
    origin_hub_id: Mapped[str] = mapped_column(String, nullable=False)
    dest_hub_id: Mapped[str] = mapped_column(String, nullable=False)

    # Outcome — same vocabulary as journey_searches.status so the matrix
    # can render with the same colour scheme used elsewhere:
    #   ok        → at least one itinerary returned
    #   no_route  → OTP returned 0 itineraries (legitimately no service)
    #   timeout   → FANOUT_TIMEOUT_MS or otp_api_timeout fired
    #   error     → HTTP / connection / unexpected error
    #   skipped   → run was cancelled before reaching this pair
    status: Mapped[str] = mapped_column(String, nullable=False)

    response_ms: Mapped[int | None] = mapped_column(Integer)
    num_itineraries: Mapped[int | None] = mapped_column(Integer)
    # Best (shortest) itinerary's duration in seconds. The matrix
    # heatmap can colour cells by this when it's set.
    best_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # Number of transfers in the best itinerary (0 = direct). Useful
    # for sorting "good" coverage from "technically possible but 6
    # changes long".
    best_num_transfers: Mapped[int | None] = mapped_column(Integer)
    # Comma-joined list of feed_ids seen across the best itinerary's
    # legs (e.g. "SNCF,IDFM"). Lets the matrix tooltip show "SNCF + 2
    # transfers" without having to load the full journey_trip row.
    best_operators: Mapped[str | None] = mapped_column(String)

    error_message: Mapped[str | None] = mapped_column(String)

    # FK to the journey_searches row this result came from, so the
    # click-cell drilldown can reuse the existing v0.1.26 trip-card UI.
    journey_search_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journey_searches.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
