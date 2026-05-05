"""Network coverage matrix runs (v0.1.27).

Adds two tables:

  network_coverage_runs     — one row per "Run Coverage" click. Carries
                              the session_id + departure datetime + run
                              state. Per-run aggregate counters
                              (total/completed/ok/no_route/error) live
                              here so the UI's progress bar doesn't
                              have to aggregate over the results table.

  network_coverage_results  — one row per (run, origin_hub, dest_hub)
                              triple. Persists the per-pair outcome plus
                              FK to journey_searches so the click-cell
                              drilldown reuses v0.1.26's trip-card UI.

The journey_search_id link is intentionally nullable + ON DELETE SET
NULL: clearing journey_searches for retention shouldn't kill coverage
history, just remove the drilldown link.

Revision ID: 20260505_1500_network_coverage
Revises: 20260503_0900_nap_catalogues
Create Date: 2026-05-05 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260505_1500_network_coverage"
down_revision: str | None = "20260503_0900_nap_catalogues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "network_coverage_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "session_id",
            sa.String,
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("session_label", sa.String, nullable=False),
        sa.Column("depart_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "hub_set",
            sa.String,
            nullable=False,
            server_default=sa.text("'fr-major-23'"),
        ),
        sa.Column(
            "direction",
            sa.String,
            nullable=False,
            server_default=sa.text("'both'"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("total_pairs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("completed_pairs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("ok_pairs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("no_route_pairs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_pairs", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("summary", postgresql.JSONB, nullable=True),
    )
    op.create_index(
        "ix_network_coverage_runs_started_at",
        "network_coverage_runs",
        ["started_at"],
    )
    op.create_index(
        "ix_network_coverage_runs_session_id",
        "network_coverage_runs",
        ["session_id"],
    )

    op.create_table(
        "network_coverage_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("network_coverage_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin_hub_id", sa.String, nullable=False),
        sa.Column("dest_hub_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("response_ms", sa.Integer, nullable=True),
        sa.Column("num_itineraries", sa.Integer, nullable=True),
        sa.Column("best_duration_seconds", sa.Integer, nullable=True),
        sa.Column("best_num_transfers", sa.Integer, nullable=True),
        sa.Column("best_operators", sa.String, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column(
            "journey_search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("journey_searches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "origin_hub_id",
            "dest_hub_id",
            name="unique_result_per_run_pair",
        ),
    )
    op.create_index(
        "ix_network_coverage_results_run_id",
        "network_coverage_results",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_network_coverage_results_run_id",
        table_name="network_coverage_results",
    )
    op.drop_table("network_coverage_results")
    op.drop_index(
        "ix_network_coverage_runs_session_id",
        table_name="network_coverage_runs",
    )
    op.drop_index(
        "ix_network_coverage_runs_started_at",
        table_name="network_coverage_runs",
    )
    op.drop_table("network_coverage_runs")
