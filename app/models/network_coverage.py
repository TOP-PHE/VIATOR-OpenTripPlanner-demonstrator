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
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
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
        # PR #36 — mode controls whether each pair is queried against one
        # session (the run's session_id) or fanned out across every
        # fanout-enabled session at execute time. The validity of the
        # (mode, session_id) pair at WRITE time is enforced by the API
        # layer — keeping the DB constraint loose lets historical
        # single-session rows survive a `ON DELETE SET NULL` of their
        # session FK without violating a NOT-NULL.
        CheckConstraint("mode IN ('single_session','fanout')", name="coverage_run_mode_valid"),
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
    # For fanout-mode runs the label is the placeholder "fanout" so the
    # sidebar / matrix UI can distinguish them at a glance.
    session_label: Mapped[str] = mapped_column(String, nullable=False)

    # PR #36 — 'single_session' (the legacy behaviour) or 'fanout' (run
    # each pair against every serving + include_in_fanout session, merge
    # results by trip_signature). Default mirrors the pre-PR-#36 shape so
    # existing rows after the alembic backfill stay correct.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'single_session'")
    )

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

    # Optional country-subset filter (ISO 3166-1 alpha-2 codes). NULL =
    # no filter, the whole active hub list participates (legacy behaviour).
    # When set, both matrix axes are restricted to hubs whose `country`
    # is in the list — drops a 50-hub by 50-hub by 2-direction matrix
    # down to a ~100-pair smoke test for cross-border work without
    # having to tag hubs into named groups.
    countries: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # PR-E — operator opt-in to run external-planner verification (ÖBB
    # HAFAS) automatically at run-completion time on every no_route /
    # timeout / error cell. Default False keeps legacy behaviour. The
    # Phase-3 sweep in `runner.execute_run` reads this flag and, if true,
    # populates the `NetworkCoverageResult.external_*` columns inside the
    # same txn that flips `run.status='completed'`.
    verify_externally: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # PR-3 — per-run day-window override (origin-local time-of-day slice).
    # NULL means "use COVERAGE_DEFAULT_WINDOW_START/END from platform
    # config at execute time". Stored as TIME (no date / no tz); the
    # runner combines (reference_date, window_*_local, window_timezone)
    # into the K-slot UTC grid.
    #
    # `window_end_local` accepts "24:00" on the API as a sentinel for
    # end-of-day; the API layer stores it as 00:00 (i.e. midnight of the
    # NEXT day) so the DB-side TIME constraint isn't violated. The
    # runner detects the "end == start" round-trip case and adds a day.
    window_start_local: Mapped[time | None] = mapped_column(Time(), nullable=True)
    window_end_local: Mapped[time | None] = mapped_column(Time(), nullable=True)

    # IANA timezone name (e.g. "Europe/Vienna", "UTC"). NULL = fall back
    # to COVERAGE_DEFAULT_TIMEZONE. Free-form TEXT in the DB so a future
    # operator-typed zone doesn't require a migration; the create-run
    # API gates against the COVERAGE_DEFAULT_TIMEZONE choices list.
    window_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Calendar day (in window_timezone) on which the K time-slots are
    # anchored. NULL = "tomorrow at run-create time in window_timezone"
    # — resolved by `runner.create_run` so the persisted depart_at and
    # the reference_date stay consistent on the row.
    reference_date: Mapped[date | None] = mapped_column(Date(), nullable=True)


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

    # PR #36 — the session ids that returned at least one itinerary for
    # this pair in fanout mode. NULL on single-session rows (the run's
    # session_id is the unique answer there). Matrix UI uses this to
    # badge each cell with which network covered it (e.g. "nap-fr-rail
    # + nap-eu-corridors" for Paris→Madrid).
    session_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    error_message: Mapped[str | None] = mapped_column(String)

    # FK to the journey_searches row this result came from, so the
    # click-cell drilldown can reuse the existing v0.1.26 trip-card UI.
    journey_search_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journey_searches.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # PR-E — external-planner verdict for this cell. NULL on every
    # legacy row and on any row whose owning run had verify_externally
    # = False. When populated, the matrix UI renders a coloured dot:
    #   external_ok=True (green)  — ÖBB found a connection, likely a
    #                               VIATOR data gap to investigate.
    #   external_ok=False, error=None (blue) — ÖBB also returned zero,
    #                                          real "no service" gap.
    #   external_error non-NULL (yellow) — ÖBB couldn't answer (timeout,
    #                                      auth, station-not-found etc).
    # See `app/network_coverage/external_verify.py` for the verdict
    # semantics and `runner._run_external_verify_sweep` for the worker
    # loop that writes these columns.
    external_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    external_num_connections: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_best_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_best_transfers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_error: Mapped[str | None] = mapped_column(String, nullable=True)

    # PR-196a — per-cell ÖBB itinerary capture + alignment heatmap. The
    # sweep persists the full normalised itinerary list so the cell-
    # trips modal can render the ÖBB side-by-side against VIATOR
    # without re-querying HAFAS. `external_alignment_score` is 0.0-1.0
    # (NULL when not computable: one or both sides empty in the no_data
    # tier). `external_alignment_tier` is the human-readable bucket the
    # matrix uses to colour cells: agree / mostly_agree / partial /
    # disagree / no_overlap / one_sided_viator / one_sided_oebb /
    # no_service / no_data. See app/network_coverage/alignment.py for
    # the scoring rules.
    external_itineraries: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    external_alignment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_alignment_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)


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
