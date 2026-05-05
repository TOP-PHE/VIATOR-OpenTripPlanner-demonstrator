"""ORM models for the v0.1.27 network-coverage feature.

Three tables:

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

  network_coverage_hubs     — v0.1.31: editable hub catalog. Was a
                              hard-coded NamedTuple list in
                              app/network_coverage/hubs.py through
                              v0.1.30; that file remains as legacy
                              fallback / seed source for the migration.
                              The runner reads active hubs from this
                              table at run-creation time so adding
                              cross-border stations (London St Pancras,
                              Brussels-Midi, ...) doesn't require a
                              code release.

The journey_search_id link reuses the existing journey_searches /
journey_trips infrastructure — no duplication, and the v0.1.26 trip-
card UI works as the click-cell drilldown out of the box.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
    direction: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'both'"))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # State machine: pending → running → (completed | failed | cancelled)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))

    # Counters updated by the runner as it progresses. The UI uses these
    # for the live progress bar without having to aggregate over the
    # results table.
    total_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ok_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    no_route_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_pairs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Catch-all for any post-run summary the runner wants to attach
    # (e.g. average response time, slowest pair, list of timed-out pairs).
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class NetworkCoverageResult(Base):
    __tablename__ = "network_coverage_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "origin_hub_id",
            "dest_hub_id",
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


class NetworkCoverageHub(Base):
    """v0.1.31 — operator-editable hub catalog.

    Replaces the hard-coded NamedTuple list in
    `app/network_coverage/hubs.py` (which remains as the seed source for
    the alembic migration and a legacy fallback for environments that
    haven't run the migration yet).

    Why a table instead of a config-file constant: cross-border
    expansion (v0.1.30 EU sessions) needs to add London St Pancras,
    Brussels-Midi, Amsterdam Centraal, Cologne Hbf, Frankfurt Hbf,
    Milano Centrale, Zurich HB, Madrid-Atocha, Vienna Hbf etc. as the
    operator iterates — and waiting on a code release + Docker build
    + tag push for every "I want to add Köln to the matrix" is a
    velocity tax we don't need.

    Soft-delete via `is_active`: deleted hubs stay in the table so old
    coverage runs (whose result rows reference hub_id as a string)
    continue to render correctly. The matrix UI filters to is_active
    when fetching the current hub axis; old runs use the per-result
    snapshot of hub coords on the row itself.

    Country / region / tier are three independent metadata dimensions:
      country  ISO 3166-1 alpha-2 (FR, UK, BE, NL, DE, IT, CH, ES, AT,
               LU, ...). Used for grouping in the manage-hubs UI. The
               editor convention is uppercase 2-letter; we don't enforce
               via CHECK because future codes (e.g. "EU" for cross-
               border placeholders) shouldn't break the schema.
      region   free-form string. Used by the matrix CSS to colour
               row/column headers — keep the existing
               paris/NE/CE/SE/SW/W/Center vocabulary for FR hubs so
               the historical matrix aesthetic is preserved.
      tier     'main' | 'regional'. Operator-meaningful split: main =
               TGV/IC headline city, regional = TER halt or smaller
               commuter city used for stress-testing. Surfaces in the
               UI as separate sections under each country header.
    """

    __tablename__ = "network_coverage_hubs"
    __table_args__ = (
        CheckConstraint("tier IN ('main','regional')", name="tier_valid"),
        Index("ix_network_coverage_hubs_country_tier", "country", "tier", "is_active"),
        Index("ix_network_coverage_hubs_active_sort", "is_active", "sort_order"),
    )

    # Slug as PK — same convention used by the existing
    # network_coverage_results.origin_hub_id / dest_hub_id columns
    # (which are intentionally not FK-constrained against this table
    # so old runs survive a hub-deletion). Matching slug semantics
    # means the matrix render works the same way for runs that pre-
    # date this table.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    short: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str | None] = mapped_column(String(40))
    tier: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'main'"))
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    # Soft-delete flag. UI hides is_active=false by default; admin can
    # toggle "show inactive" to restore one if it was removed by mistake.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Lower number sorts first within (country, tier). Default 100 so
    # new entries don't disrupt the curated FR ordering. Operator can
    # override via the edit form to push a hub up/down the matrix axis.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
