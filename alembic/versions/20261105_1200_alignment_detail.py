"""Why a coverage cell scored what it did (#304, decision M6).

The matching engine (#304) scores a cell's VIATOR journeys against ÖBB's and says why: how many
pairs of each verdict, how many journeys on one side only, how many VIATOR journeys left after
ÖBB's last connection (M9), and how often each reason was met (a line label that differs, a
shorter transfer after a shared train, stations compared by position...). Adds to
`network_coverage_results`:

- `external_alignment_detail` (jsonb, null): `{engine_version, profile, counts: {...},
  reasons: {code: n}}`, written with the score and the tier by the ÖBB check.

Null on every row scored before this revision and on a cell the check has not asked: nothing is
copied. Re-runnable: the column is added only when missing. The downgrade drops it; the score
and the tier stay.

Revision ID: 20261105_1200_alignment_detail
Revises: 20261104_1200_canonical_storage
Create Date: 2026-11-05 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261105_1200_alignment_detail"
down_revision: str | Sequence[str] | None = "20261104_1200_canonical_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "network_coverage_results"
COLUMN = "external_alignment_detail"


def upgrade() -> None:
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN not in present:
        op.add_column(TABLE, sa.Column(COLUMN, postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN in present:
        op.drop_column(TABLE, COLUMN)
