"""Station panel, part 4 of 4: `rebuild_jobs.kind`.

The worker dispatched every job to a graph builder. A station reference build
has no session and no build container, so the job row has to say what it is:
`'graph'` (the default, and every existing row) or `'station_build'`. The
worker branches on it before the engine lookup, and enqueueing coalesces on
`(status, session_id, kind)` so a station job, whose session is NULL, can no
longer be mistaken for the legacy session-less graph job.

See docs/station-panel-design.md section 5.

Revision ID: 20261003_1230_rebuild_kind
Revises: 20261003_1220_station_ref
Create Date: 2026-10-03 12:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261003_1230_rebuild_kind"
down_revision: str | Sequence[str] | None = "20261003_1220_station_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rebuild_jobs",
        sa.Column("kind", sa.String(32), nullable=False, server_default=sa.text("'graph'")),
    )


def downgrade() -> None:
    op.drop_column("rebuild_jobs", "kind")
