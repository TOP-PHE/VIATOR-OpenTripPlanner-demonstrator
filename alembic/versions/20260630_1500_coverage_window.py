"""Coverage runs — per-run day window + timezone + reference date.

PR-3. Adds four nullable columns to `network_coverage_runs` so each
coverage run can carry its own day-window slice + IANA timezone +
reference date. NULL on any column = "use the platform_config defaults
at execute time" — keeps existing rows working and lets the form's
Advanced section show a pre-filled placeholder rather than forcing the
operator to fill all four fields on every run.

Schema-design notes:
  - `window_start_local` / `window_end_local` are TIME (no date / no tz)
    because the window is a calendar time-of-day slice in the run's own
    timezone. The window's effective UTC instants are computed at
    execute time from (reference_date, window_*_local, window_timezone).
  - `window_end_local` carries a TIME, but the form allows "24:00" as a
    sentinel for end-of-day; the runner translates that to
    `reference_date + 1 day @ 00:00 local` before computing the UTC
    grid. We do NOT enforce 00:00-23:59 in the DB because Postgres'
    TIME type forbids 24:00 and we want to keep the sentinel reachable
    on the API surface (the runner does the translation).
  - `window_timezone` is TEXT (free-form IANA name like 'Europe/Vienna'
    or 'UTC') — the config_schema enforces the choice list at the
    API layer; the DB stays loose so a future operator-typed zone
    doesn't require a schema migration.
  - `reference_date` is DATE (no tz) — the calendar day in
    `window_timezone` on which the K slots are anchored. NULL means
    "tomorrow at run-create time in window_timezone".

Backfill: every legacy row gets NULL across the board, and the runner
resolves NULL by falling back to (config_service defaults / tomorrow).
So the rollout is bit-identical to PR-2 behaviour for runs created
before the form learns about the new fields.

Revision ID: 20260630_1500_coverage_day_window
Revises: 20260629_2200_verify_external
Create Date: 2026-06-30 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260630_1500_coverage_window"
down_revision: str | Sequence[str] | None = "20260629_2200_verify_external"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PR-3 — per-run day window (origin-local time-of-day slice) +
    # timezone + reference date. All nullable so legacy rows survive
    # and the runner can fall back to platform_config defaults.
    op.add_column(
        "network_coverage_runs",
        sa.Column("window_start_local", sa.Time(), nullable=True),
    )
    op.add_column(
        "network_coverage_runs",
        sa.Column("window_end_local", sa.Time(), nullable=True),
    )
    op.add_column(
        "network_coverage_runs",
        sa.Column("window_timezone", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "network_coverage_runs",
        sa.Column("reference_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("network_coverage_runs", "reference_date")
    op.drop_column("network_coverage_runs", "window_timezone")
    op.drop_column("network_coverage_runs", "window_end_local")
    op.drop_column("network_coverage_runs", "window_start_local")
