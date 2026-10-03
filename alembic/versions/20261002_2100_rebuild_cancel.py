"""Let an operator cancel a running rebuild.

`rebuild_jobs.cancel_requested_at` is set by the web app (Cancel button); the
worker, which owns the build containers, watches it while a build runs, kills
the build container and records the job as `cancelled`. A pending job is
cancelled directly by the web app and never needs the column.

Revision ID: 20261002_2100_rebuild_cancel
Revises: 20261001_0900_cred_oauth2_login
Create Date: 2026-10-02 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261002_2100_rebuild_cancel"
down_revision: str | Sequence[str] | None = "20261001_0900_cred_oauth2_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rebuild_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rebuild_jobs", "cancel_requested_at")
