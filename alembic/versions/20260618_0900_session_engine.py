"""Session.engine — MOTIS as a selectable planner backend.

Adds a `engine` column to `sessions` so an operator can pick OTP (default)
or MOTIS per session. Existing rows backfill to 'otp' via `server_default`,
so the column is NOT NULL immediately without a multi-step migration.

The dispatcher (`app/journey/planner_dispatch.py`) reads this column and
routes journey-plan requests to either `otp_client.fetch_plan` or
`motis_client.fetch_plan` (signatures are mirrored, see app/journey/).
The sessions orchestrator (`app/sessions_orchestrator.py`) renders
`otp-<sid>` or `motis-<sid>` compose services based on the same field.

Revision ID: 20260618_0900_session_engine
Revises: 20260529_1400_coverage_fanout
Create Date: 2026-06-18 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260618_0900_session_engine"
down_revision: str | Sequence[str] | None = "20260529_1400_coverage_fanout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "engine",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'otp'"),
        ),
    )
    op.create_check_constraint(
        "engine_valid",
        "sessions",
        "engine IN ('otp','motis')",
    )


def downgrade() -> None:
    op.drop_constraint("engine_valid", "sessions", type_="check")
    op.drop_column("sessions", "engine")
