"""Coverage hubs — transport-mode classification (R/T/M/B/C).

Adds a nullable `modes` column to `network_coverage_hubs` so the matrix
UI can render a "what serves this station" band alongside the existing
country grouping. Stores a compact, already-joined code like `"R+M"`
(Rail + Metro) rather than a list — the only consumer is display, and
building the join once at write time avoids parsing it back apart on
every render.

NULL means "not yet classified" (renders as `?` in the UI), which is
the state every existing hub starts in — classification is populated
by a separate follow-up (GTFS/NeTEx route_type cross-reference, with a
fallback to inspecting historical coverage-run results), not by this
migration.

Revision ID: 20260704_1000_hub_modes
Revises: 20260701_0900_oebb_alignment
Create Date: 2026-07-04 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260704_1000_hub_modes"
down_revision: str | Sequence[str] | None = "20260701_0900_oebb_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "network_coverage_hubs",
        sa.Column("modes", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("network_coverage_hubs", "modes")
