"""Cross-session (fanout) coverage runs — PR #36.

Adds a `mode` column to `network_coverage_runs` so an operator can run an
all-pairs coverage matrix against every fanout-enabled session in parallel
instead of one specific session. The fanout mode reuses the same merge-
by-trip-signature logic the live `/api/journey/fanout` endpoint already
uses; the result is one row per pair carrying the list of session ids
that returned trips for that pair (so the matrix UI can colour-badge
"FR + EU" vs "EU only" vs "0 sessions").

Two columns added:

  network_coverage_runs.mode           VARCHAR(16) NOT NULL DEFAULT 'single_session'
      CHECK mode IN ('single_session','fanout')

  network_coverage_results.session_ids TEXT[] NULL
      The sessions that returned at least one itinerary for this pair
      in fanout mode. NULL on single-session rows (the run's session_id
      is the authoritative answer there).

Why no NOT-NULL invariant on `network_coverage_runs.session_id` even for
mode='single_session' rows: the existing `ON DELETE SET NULL` on the FK
keeps run history alive past session deletion. Enforcing
"mode='single_session' ⇒ session_id NOT NULL" via DB would break that —
we validate the combination at write time in the API layer instead.

See docs/audit-2026-05.md row #36.

Revision ID: 20260529_1400_coverage_fanout
Revises: 20260521_1200_rebuild_maxmem
Create Date: 2026-05-29 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision: str = "20260529_1400_coverage_fanout"
# Chains AFTER the worker-rebuild-max-memory migration which landed on
# main between PR #36 being drafted and rebased. Both had originally
# targeted 20260520_1300_upload_provider as their parent, creating two
# alembic heads — re-parent this one onto the now-merged sibling so the
# revision graph stays linear.
down_revision: str | Sequence[str] | None = "20260521_1200_rebuild_maxmem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add `mode` to runs with a default so existing rows backfill cleanly.
    #    `server_default` populates pre-existing rows; the column is NOT NULL
    #    immediately because the default covers every row at write time.
    op.add_column(
        "network_coverage_runs",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'single_session'"),
        ),
    )
    op.create_check_constraint(
        "coverage_run_mode_valid",
        "network_coverage_runs",
        "mode IN ('single_session','fanout')",
    )

    # 2. Add `session_ids` text-array to results. Nullable: single-session
    #    rows leave it NULL (the run's session_id is the unique answer);
    #    fanout rows store the list of sessions that returned trips.
    op.add_column(
        "network_coverage_results",
        sa.Column(
            "session_ids",
            sa.dialects.postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("network_coverage_results", "session_ids")
    op.drop_constraint("coverage_run_mode_valid", "network_coverage_runs", type_="check")
    op.drop_column("network_coverage_runs", "mode")
