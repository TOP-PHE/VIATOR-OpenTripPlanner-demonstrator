"""Make journey_search_executions.graph_snapshot_id nullable.

The original schema required every search execution to point at a
graph_snapshots row. The fanout endpoint relied on a `_placeholder_snapshot_id`
helper returning the all-zero UUID for sessions that don't yet have a
snapshot recorded — but that UUID isn't in graph_snapshots, so the FK
fires:

  IntegrityError: insert or update on table "journey_search_executions"
    violates foreign key constraint
    "fk_journey_search_executions_graph_snapshot_id_graph_snapshots"

Two paths considered:

  A) Have the worker write a graph_snapshots row after every successful
     build (proper Phase-3 wiring; ties trip_signature canonicalisation
     to the actual graph version).
  B) Allow NULL in the column so executions that ran without a recorded
     snapshot don't crash the fanout endpoint.

This migration does B (a small, reversible change). Path A becomes a
straightforward follow-up — the worker writes the row, the API uses
that ID, and most rows are non-null again. NULL remains the right
answer for failed executions where we couldn't determine the snapshot.

Revision ID: 20260429_0700_exec_snap_nullable
Revises: 20260427_2200_initial
Create Date: 2026-04-29 07:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260429_0700_exec_snap_nullable"
down_revision: str | Sequence[str] | None = "20260427_2200_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "journey_search_executions",
        "graph_snapshot_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Note: this will fail if any rows currently have NULL — the operator
    # would need to clean those up first or backfill from graph_snapshots.
    op.alter_column(
        "journey_search_executions",
        "graph_snapshot_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
