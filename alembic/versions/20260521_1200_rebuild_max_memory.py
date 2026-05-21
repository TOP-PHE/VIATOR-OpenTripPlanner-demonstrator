"""Per-job max-memory rebuild flag (v0.1.38).

Adds `rebuild_jobs.max_memory` — set when the operator ticks the
"max-memory rebuild" checkbox next to Rebuild. The worker reads it (the
worker is a separate process, so the flag must be persisted with the job)
and, when true, stops serving sessions + the observability stack, sizes the
build heap to host RAM, then restarts them. Defaults FALSE so existing rows
and the normal rebuild path are unchanged.

Revision ID: 20260521_1200_rebuild_maxmem
Revises: 20260520_1300_upload_provider
Create Date: 2026-05-21 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic. (Kept <=30 chars — the default
# alembic_version.version_num column is VARCHAR(32).)
revision: str = "20260521_1200_rebuild_maxmem"
down_revision: str | Sequence[str] | None = "20260520_1300_upload_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rebuild_jobs",
        sa.Column("max_memory", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )


def downgrade() -> None:
    op.drop_column("rebuild_jobs", "max_memory")
