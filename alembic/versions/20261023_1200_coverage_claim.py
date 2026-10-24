"""Coverage runs claimed by an executor, with a heartbeat (issue #392).

A coverage run no longer runs in the web process: the worker's coverage
executor claims it and keeps a heartbeat, and a run whose heartbeat went
stale (the executor was restarted) is claimed again and resumed. Adds to
`network_coverage_runs`:

- `claimed_by` (text, null): the executor that holds the run (host, pid and
  a random part); kept on a finished run as the last one that held it;
- `heartbeat_at` (timestamptz, null): the database's clock at the holder's
  last heartbeat; null on a run never claimed or released at a shutdown;
- `resume_count` (integer, not null, default 0): how many times the run was
  claimed again after its executor stopped;
- `cancel_requested_at` (timestamptz, null): the Stop click, written by web
  and read by the executor, which then stops the run as before;
- `execution_snapshot` (JSONB, null): the hub ids and runner knobs frozen at
  the first claim, so a resumed run asks the same pairs with the same grid;
- index `ix_network_coverage_runs_status` for the executor's poll.

The uniqueness guard of a result, (run_id, origin_hub_id, dest_hub_id), is
already there (`unique_result_per_run_pair`, 20260505_1500).

Nothing is copied: every existing row gets nulls and `resume_count` 0, so a
finished, failed or orphaned run stays as it is. A run still `running` when
this revision lands has no heartbeat: the executor resumes it. Re-runnable:
each column and the index are added only when missing. The downgrade drops
them; only the claim bookkeeping is lost.

Revision ID: 20261023_1200_coverage_claim
Revises: 20261015_1800_refresh_progress
Create Date: 2026-10-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261023_1200_coverage_claim"
down_revision: str | Sequence[str] | None = "20261015_1800_refresh_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "network_coverage_runs"
INDEX = "ix_network_coverage_runs_status"


def _columns() -> list[sa.Column[object]]:
    return [
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_snapshot", postgresql.JSONB(), nullable=True),
    ]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for column in _columns():
        if column.name not in present:
            op.add_column(TABLE, column)
    if INDEX not in {i["name"] for i in inspector.get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["status"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if INDEX in {i["name"] for i in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for column in reversed(_columns()):
        if column.name in present:
            op.drop_column(TABLE, column.name)
