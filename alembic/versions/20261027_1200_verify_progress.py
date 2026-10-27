"""Progress of a coverage run's ÖBB check (the verify sweep).

Once every pair of a run with "Verify externally" is done, the completion
phase asks ÖBB HAFAS about every cell, about one a second: hours on a large
run. The page showed nothing of it, and nothing was saved until the last
cell, so a worker restart started the check again from zero. The check now
saves each cell's answer as it goes and records its progress on the run.
Adds to `network_coverage_runs`:

- `verify_total` (integer, null): the cells the check asks about;
- `verify_done` (integer, null): the cells whose answer is saved;
- `verify_done_at_start` (integer, null): the cells already saved when the
  current attempt started (a resumed check skips them), so the page can
  measure the current attempt's pace;
- `verify_started_at` (timestamptz, null): the database's clock when the
  current attempt started;
- `verify_progress_at` (timestamptz, null): the database's clock at the
  last progress write.

All null on a run whose check never started (no "Verify externally", or
from before this revision): nothing is copied. Re-runnable: each column is
added only when missing. The downgrade drops them; only the progress
bookkeeping is lost, the saved answers stay on the result rows.

Revision ID: 20261027_1200_verify_progress
Revises: 20261023_1200_coverage_claim
Create Date: 2026-10-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261027_1200_verify_progress"
down_revision: str | Sequence[str] | None = "20261023_1200_coverage_claim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "network_coverage_runs"


def _columns() -> list[sa.Column[object]]:
    return [
        sa.Column("verify_total", sa.Integer(), nullable=True),
        sa.Column("verify_done", sa.Integer(), nullable=True),
        sa.Column("verify_done_at_start", sa.Integer(), nullable=True),
        sa.Column("verify_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_progress_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    for column in _columns():
        if column.name not in present:
            op.add_column(TABLE, column)


def downgrade() -> None:
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    for column in reversed(_columns()):
        if column.name in present:
            op.drop_column(TABLE, column.name)
