"""Coverage runs — country subset filter.

Adds a nullable `countries` JSONB column to `network_coverage_runs` so an
operator can restrict a run's matrix to a subset of countries (e.g. FR
only, or FR+CH for a fast cross-border smoke). NULL = no filter (legacy
behaviour: all active hubs participate, same as every pre-this-migration
run).

Why JSONB and not TEXT[]: keeps the API/UI layer symmetric with the
existing `summary` JSONB blob the runner already attaches to runs, and
sidesteps needing a separate `text[]`/`array_to_string` cast path in the
matrix renderer. The list is short (≤ 20 ISO codes) so the on-disk
overhead vs TEXT[] is negligible.

Revision ID: 20260628_1100_coverage_country
Revises: 20260618_0900_session_engine
Create Date: 2026-06-28 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20260628_1100_coverage_country"
down_revision: str | Sequence[str] | None = "20260618_0900_session_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "network_coverage_runs",
        sa.Column("countries", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("network_coverage_runs", "countries")
