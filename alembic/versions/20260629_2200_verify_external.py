"""Coverage runs — auto-verify externally on completion.

PR-E. Adds an opt-in `verify_externally` boolean to
`network_coverage_runs` and seven nullable `external_*` columns to
`network_coverage_results` so the worker can persist the ÖBB HAFAS
verdict for every no_route / timeout / error cell at run-completion
time. Operator ticks the run-form checkbox; the runner's Phase-3
aggregation block sweeps every qualifying cell, calls
`external_verify.verify_via_oebb_hafas` with the run's depart_at +
hub coords, and writes the verdict columns in the same txn that flips
`run.status='completed'`.

Backfill behaviour:
  - `verify_externally` defaults to `false` (server_default=text('false'))
    so existing rows keep the legacy click-to-verify behaviour.
  - The seven result columns are nullable with no server_default;
    legacy rows have NULL across the board which the matrix UI
    renders as "not verified". Same goes for any new run created
    with `verify_externally=False`.

Schema-design notes:
  - `external_source` is a free-form provider tag (currently
    `"fahrplan.oebb.at"`, future-proof for an alternative HAFAS or
    OJP source) — no FK.
  - No new index. Cells are always filtered by `run_id` which already
    has `ix_network_coverage_results_run_id`. A future "show all
    cells flagged as gaps across runs" view would benefit from an
    index on `external_ok` — defer until that surface exists.
  - `external_verified_at` is `TIMESTAMPTZ` matching the existing
    `created_at` / `started_at` / `finished_at` convention.

Revision ID: 20260629_2200_verify_external
Revises: 20260628_1100_coverage_country
Create Date: 2026-06-29 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260629_2200_verify_external"
down_revision: str | Sequence[str] | None = "20260628_1100_coverage_country"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PR-E — opt-in flag on the run. server_default='false' backfills
    # every existing row to legacy behaviour in a single ALTER.
    op.add_column(
        "network_coverage_runs",
        sa.Column(
            "verify_externally",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # PR-E — verdict columns on each cell row. All nullable with no
    # server_default — legacy rows pre-date the feature and will read
    # as NULL ("not verified") in the matrix UI.
    op.add_column(
        "network_coverage_results",
        sa.Column("external_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_ok", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_num_connections", sa.Integer(), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_best_duration_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_best_transfers", sa.Integer(), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_source", sa.String(), nullable=True),
    )
    op.add_column(
        "network_coverage_results",
        sa.Column("external_error", sa.String(), nullable=True),
    )


def downgrade() -> None:
    # Reverse order: drop result-table columns first, then the run-table
    # column. No check constraints or indexes to drop in this migration.
    op.drop_column("network_coverage_results", "external_error")
    op.drop_column("network_coverage_results", "external_source")
    op.drop_column("network_coverage_results", "external_best_transfers")
    op.drop_column("network_coverage_results", "external_best_duration_seconds")
    op.drop_column("network_coverage_results", "external_num_connections")
    op.drop_column("network_coverage_results", "external_ok")
    op.drop_column("network_coverage_results", "external_verified_at")
    op.drop_column("network_coverage_runs", "verify_externally")
