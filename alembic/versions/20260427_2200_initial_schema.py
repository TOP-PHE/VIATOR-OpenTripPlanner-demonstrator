"""Initial schema — full VIATOR data model.

Lays down: identity (users, verification_tokens, password_reset_tokens),
sessions, ingestion (uploads, rebuild_jobs), graph_snapshots, search
(journey_searches + executions + trips + provenance view), master data
(stations, route_aliases, carriers, *_pending_drift), runtime (mct_overrides,
stations_xref), audit_events, platform_config.

Revision ID: 20260427_2200_initial
Revises:
Create Date: 2026-04-27 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260427_2200_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------- Postgres extensions ----------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")  # gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS citext;")  # case-insensitive emails
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")  # autocomplete on station names

    # ---------- Identity ----------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "role IN ('platform_admin','content_manager','end_user')",
            name="ck_users_role_valid",
        ),
    )

    op.create_table(
        "verification_tokens",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )

    # ---------- Sessions ----------
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "include_in_fanout", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "category IN ('NAP','MERITS','MANUAL','EXPERIMENTAL')",
            name="ck_sessions_category_valid",
        ),
        sa.CheckConstraint(
            "state IN ('created','configured','populated','graph_built',"
            "'serving','archived','deleted')",
            name="ck_sessions_state_valid",
        ),
    )
    op.create_index(
        "ix_sessions_fanout",
        "sessions",
        ["state", "include_in_fanout"],
        postgresql_where=sa.text("state = 'serving' AND include_in_fanout"),
    )

    # ---------- Ingestion: uploads, rebuild_jobs ----------
    op.create_table(
        "uploads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Nullable in Phase 1 — becomes NOT NULL in steps 3 (auth) and 7 (sessions).
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("declared_kind", sa.String(), nullable=False),
        sa.Column("detected_kind", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_path", sa.String(), nullable=False),
        sa.Column("version_label", sa.String()),
        sa.Column(
            "triggered_rebuild", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_uploads_session_created", "uploads", ["session_id", "created_at"])

    op.create_table(
        "rebuild_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("log", sa.Text()),
        sa.Column("graph_path", sa.String()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','cancelled')",
            name="ck_rebuild_jobs_status_valid",
        ),
    )
    op.create_index("ix_rebuild_jobs_session_created", "rebuild_jobs", ["session_id", "created_at"])

    # ---------- Graph snapshots (timetable versioning) ----------
    op.create_table(
        "graph_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column(
            "rebuild_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rebuild_jobs.id")
        ),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("graph_path", sa.String(), nullable=False),
        sa.Column("source_uploads", postgresql.JSONB(), nullable=False),
        sa.Column("feed_signature", sa.String(64), nullable=False),
        sa.Column("timetable_main_version", sa.String(), nullable=False),
        sa.Column(
            "timetable_update_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("service_period_start", sa.Date(), nullable=False),
        sa.Column("service_period_end", sa.Date(), nullable=False),
        sa.Column(
            "main_version_source", sa.String(), nullable=False, server_default=sa.text("'auto'")
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "main_version_source IN ('auto','manual_override')",
            name="ck_graph_snapshots_main_version_source_valid",
        ),
        sa.UniqueConstraint(
            "session_id",
            "timetable_main_version",
            "timetable_update_version",
            name="uq_graph_snapshots_unique_update_within_main",
        ),
    )
    op.create_index(
        "ix_graph_snapshots_session_built", "graph_snapshots", ["session_id", "built_at"]
    )
    op.create_index(
        "ix_graph_snapshots_main_version",
        "graph_snapshots",
        ["session_id", "timetable_main_version", "timetable_update_version"],
    )
    op.create_index(
        "uq_graph_snapshots_one_current_per_session",
        "graph_snapshots",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    # ---------- Master data ----------
    op.create_table(
        "master_stations",
        sa.Column("uic", sa.String(), primary_key=True),
        sa.Column("uic8_sncf", sa.String()),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String()),
        sa.Column("country_iso", sa.CHAR(2)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("parent_uic", sa.String(), sa.ForeignKey("master_stations.uic")),
        sa.Column("is_main_station", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("is_suggestable", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("trigramme_sncf", sa.String()),
        sa.Column("db_code", sa.String()),
        sa.Column("trenitalia_code", sa.String()),
        sa.Column("renfe_code", sa.String()),
        sa.Column("atoc_code", sa.String()),
        sa.Column(
            "other_codes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "name_translations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source", sa.String(), nullable=False, server_default=sa.text("'trainline'")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "source IN ('trainline','sncf','manual','merits','other')",
            name="ck_master_stations_source_valid",
        ),
    )
    op.create_index("ix_master_stations_country", "master_stations", ["country_iso"])
    op.create_index("ix_master_stations_trigramme_sncf", "master_stations", ["trigramme_sncf"])
    # Trigram index for autocomplete (GIN, requires pg_trgm extension above).
    op.execute(
        "CREATE INDEX ix_master_stations_name_trgm "
        "ON master_stations USING gin (name gin_trgm_ops);"
    )

    op.create_table(
        "route_aliases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("applies_from", sa.Date()),
        sa.Column("applies_until", sa.Date()),
        sa.Column("scope_country", sa.CHAR(2)),
        sa.Column("scope_carrier", sa.String()),
        sa.Column("notes", sa.String()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "alias",
            "canonical_name",
            "scope_country",
            "scope_carrier",
            name="uq_route_aliases_alias_canonical_scope_country_scope_carrier",
        ),
    )

    op.create_table(
        "master_carriers",
        sa.Column("rics_code", sa.String(), primary_key=True),
        sa.Column("short_name", sa.String(), nullable=False),
        sa.Column("full_name", sa.String()),
        sa.Column("country_iso", sa.CHAR(2)),
        sa.Column(
            "legacy_codes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source", sa.String(), nullable=False, server_default=sa.text("'uic'")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("source IN ('uic','manual')", name="ck_master_carriers_source_valid"),
    )

    op.create_table(
        "master_stations_pending_drift",
        sa.Column(
            "uic",
            sa.String(),
            sa.ForeignKey("master_stations.uic", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trainline_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("fields_differing", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "master_carriers_pending_drift",
        sa.Column(
            "rics_code",
            sa.String(),
            sa.ForeignKey("master_carriers.rics_code", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("upstream_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("fields_differing", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # ---------- Runtime: MCT, stations xref ----------
    op.create_table(
        "mct_overrides",
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("station_code", sa.String(), primary_key=True),
        sa.Column("carrier_a", sa.String(), primary_key=True),
        sa.Column("carrier_b", sa.String(), primary_key=True),
        sa.Column("min_minutes", sa.Integer(), nullable=False),
    )
    op.create_table(
        "stations_xref",
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("stop_id", sa.String(), primary_key=True),
        sa.Column("uic", sa.String(), sa.ForeignKey("master_stations.uic")),
        sa.Column("trigramme", sa.String()),
        sa.Column("insee", sa.String()),
        sa.Column("rics", sa.String()),
    )
    op.create_index("ix_stations_xref_uic", "stations_xref", ["uic"])

    # ---------- Search: searches, executions, trips ----------
    op.create_table(
        "journey_searches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("ip", postgresql.INET()),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("origin_lat", sa.Float(), nullable=False),
        sa.Column("origin_lon", sa.Float(), nullable=False),
        sa.Column("origin_label", sa.String()),
        sa.Column("dest_lat", sa.Float(), nullable=False),
        sa.Column("dest_lon", sa.Float(), nullable=False),
        sa.Column("dest_label", sa.String()),
        sa.Column("requested_time_kind", sa.String(), nullable=False),
        sa.Column("requested_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modes", sa.String(), nullable=False),
        sa.Column("total_response_ms", sa.Integer()),
        sa.Column("total_trips_unique", sa.Integer()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "replay_of_search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journey_searches.id"),
        ),
        sa.CheckConstraint(
            "endpoint IN ('plan','compare','fanout')", name="ck_journey_searches_endpoint_valid"
        ),
        sa.CheckConstraint(
            "requested_time_kind IN ('depart_at','arrive_by')",
            name="ck_journey_searches_requested_time_kind_valid",
        ),
        sa.CheckConstraint(
            "status IN ('ok','partial','no_route','error','timeout')",
            name="ck_journey_searches_status_valid",
        ),
    )
    op.create_index("ix_journey_searches_ts", "journey_searches", ["ts"])
    op.create_index("ix_journey_searches_user_ts", "journey_searches", ["user_id", "ts"])

    op.create_table(
        "journey_search_executions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journey_searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column(
            "graph_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("graph_snapshots.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("num_itineraries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("response_ms", sa.Integer()),
        sa.Column("raw_response", postgresql.JSONB()),
        sa.Column("error_message", sa.String()),
        sa.CheckConstraint(
            "status IN ('ok','no_route','error','timeout')",
            name="ck_journey_search_executions_status_valid",
        ),
    )
    op.create_index("ix_journey_executions_search", "journey_search_executions", ["search_id"])
    op.create_index(
        "ix_journey_executions_session_snap",
        "journey_search_executions",
        ["session_id", "graph_snapshot_id"],
    )

    op.create_table(
        "journey_trips",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journey_search_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trip_signature", sa.String(16), nullable=False),
        sa.Column("rank_in_response", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("num_transfers", sa.Integer(), nullable=False),
        sa.Column("departure_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrival_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modes", sa.String(), nullable=False),
        sa.Column("legs", postgresql.JSONB(), nullable=False),
        sa.Column("fare", postgresql.JSONB()),
    )
    op.create_index(
        "ix_journey_trips_execution_rank", "journey_trips", ["execution_id", "rank_in_response"]
    )
    op.create_index("ix_journey_trips_signature", "journey_trips", ["trip_signature"])

    # ---------- Cross-session provenance VIEW ----------
    op.execute(
        """
        CREATE OR REPLACE VIEW journey_trip_provenance AS
        SELECT
          e.search_id,
          t.trip_signature,
          array_agg(DISTINCT e.session_id ORDER BY e.session_id)        AS found_in_sessions,
          count(DISTINCT e.session_id)                                  AS num_sessions_with_trip,
          array_agg(DISTINCT e.graph_snapshot_id)                       AS graph_snapshot_ids,
          min(t.duration_seconds)                                       AS best_duration_seconds,
          min(t.departure_at)                                           AS earliest_departure_at,
          max(t.arrival_at)                                             AS latest_arrival_at
        FROM journey_search_executions e
        JOIN journey_trips t ON t.execution_id = e.id
        GROUP BY e.search_id, t.trip_signature;
    """
    )

    # ---------- Audit ----------
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("actor_ip", postgresql.INET()),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String()),
        sa.Column("target_id", sa.String()),
        sa.Column("metadata", postgresql.JSONB()),
    )
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"])
    op.create_index("ix_audit_events_actor_ts", "audit_events", ["actor_user_id", "ts"])

    # ---------- Platform configuration ----------
    op.create_table(
        "platform_config",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
    )


def downgrade() -> None:
    """Drops everything. Use only against an empty / disposable database."""
    op.execute("DROP VIEW IF EXISTS journey_trip_provenance;")

    op.drop_table("platform_config")
    op.drop_index("ix_audit_events_actor_ts", table_name="audit_events")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_journey_trips_signature", table_name="journey_trips")
    op.drop_index("ix_journey_trips_execution_rank", table_name="journey_trips")
    op.drop_table("journey_trips")
    op.drop_index("ix_journey_executions_session_snap", table_name="journey_search_executions")
    op.drop_index("ix_journey_executions_search", table_name="journey_search_executions")
    op.drop_table("journey_search_executions")
    op.drop_index("ix_journey_searches_user_ts", table_name="journey_searches")
    op.drop_index("ix_journey_searches_ts", table_name="journey_searches")
    op.drop_table("journey_searches")

    op.drop_index("ix_stations_xref_uic", table_name="stations_xref")
    op.drop_table("stations_xref")
    op.drop_table("mct_overrides")

    op.drop_table("master_carriers_pending_drift")
    op.drop_table("master_stations_pending_drift")
    op.drop_table("master_carriers")
    op.drop_table("route_aliases")
    op.execute("DROP INDEX IF EXISTS ix_master_stations_name_trgm;")
    op.drop_index("ix_master_stations_trigramme_sncf", table_name="master_stations")
    op.drop_index("ix_master_stations_country", table_name="master_stations")
    op.drop_table("master_stations")

    op.drop_index("uq_graph_snapshots_one_current_per_session", table_name="graph_snapshots")
    op.drop_index("ix_graph_snapshots_main_version", table_name="graph_snapshots")
    op.drop_index("ix_graph_snapshots_session_built", table_name="graph_snapshots")
    op.drop_table("graph_snapshots")

    op.drop_index("ix_rebuild_jobs_session_created", table_name="rebuild_jobs")
    op.drop_table("rebuild_jobs")
    op.drop_index("ix_uploads_session_created", table_name="uploads")
    op.drop_table("uploads")

    op.drop_index("ix_sessions_fanout", table_name="sessions")
    op.drop_table("sessions")

    op.drop_table("password_reset_tokens")
    op.drop_table("verification_tokens")
    op.drop_table("users")

    # We deliberately do NOT drop extensions on downgrade — they may be used
    # by other databases on the same Postgres cluster.
