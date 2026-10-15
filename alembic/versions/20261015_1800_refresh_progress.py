"""The progress of a session's source refreshes (issue #359).

Creates `source_refresh_progress`, one row per session and refresh kind
(`scope`: "providers" for Refresh providers, "osm" for Refresh OSM), written
by `app/refresh_progress.py` while a refresh runs and kept with its end
result until the next refresh of that kind:

- `session_id` (text, foreign key to `sessions.id`, ON DELETE CASCADE: the
  row goes with its session) and `scope` (text): the primary key;
- `run_id` (uuid): the refresh that owns the row; every write names it;
- `state` (text, CHECK: running / finished / failed / interrupted);
- `started_at`, `heartbeat_at` (timestamptz, not null), `finished_at`
  (timestamptz, null while running): the database's clock;
- `detail`, `extract` (JSONB, default `{}`): the counts and the feed being
  downloaded; the station-module follow-up of a provider refresh.

Nothing else changes and nothing is copied: the table starts empty, and the
Sessions page shows no progress block until a first refresh. Re-runnable:
the upgrade creates the table only when it is missing. The downgrade drops it
and loses only these progress records (the refreshes' outcomes stay in the
audit log and in the feed-status panel's store).

Revision ID: 20261015_1800_refresh_progress
Revises: 20261010_1200_edit_archive
Create Date: 2026-10-15 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261015_1800_refresh_progress"
down_revision: str | Sequence[str] | None = "20261010_1200_edit_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "source_refresh_progress"
# The same list as app/models/runtime.py REFRESH_STATE_CHECK, written out: a
# revision must keep working if the model changes later.
STATE_CHECK = "state IN ('running', 'finished', 'failed', 'interrupted')"
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSON),
        sa.Column("extract", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSON),
        sa.CheckConstraint(STATE_CHECK, name="ck_source_refresh_progress_state"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
