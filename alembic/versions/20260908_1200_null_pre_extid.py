"""Invalidate alignment scores computed before the endpoint-identity fix.

Every `external_alignment_score` written before this migration was computed by
a matcher comparing **longitudes**, not station ids. `extract_uic` ran its
7-8 digit regex with `.search()` over the whole HAFAS lid, and a real lid
orders its fields `A= @ O= @ X= @ Y= @ U= @ L=` — so it returned the X
coordinate in micro-degrees and never reached the station id in `L=`. The
endpoint guard therefore compared two coordinates and rejected essentially
every pair, pushing genuine matches into `no_overlap` / `disagree`.

**Invalidate, do not repair.** A longitude does not encode the `L=` value, so
there is nothing to rewrite the old tokens *to*. The scores are unrecoverable
without a fresh sweep; the honest state is "unscored".

NULL rather than a literal `'no_data'`: `api/admin/network_coverage.py`
already documents that the matrix JS maps a NULL score to the `no_data` tier,
so this reuses the existing legacy-row semantic instead of inventing a second
one.

The `external_itineraries` JSONB is deliberately left alone. Rewriting the leg
tokens inside it would be a heavy pass over every leg of every itinerary of
every cell across all historical runs, for display-only benefit on rows that
are now flagged unscored and need a re-sweep regardless. **Residual:** those
cells' detail modals still show legacy `UIC:<longitude>` endpoint tokens.

No DDL — `external_itineraries` is JSONB and the six additive `VerifyLeg`
provenance fields need no column change. This is data-only.

Revision ID: 20260908_1200_null_pre_extid
Revises: 20260704_1000_hub_modes
Create Date: 2026-09-08 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260908_1200_null_pre_extid"
down_revision: str | Sequence[str] | None = "20260704_1000_hub_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `now()` is bound at upgrade time. Code and migration deploy together, so
    # any row verified before this runs cannot have been written by the fixed
    # extractor — the predicate is self-describing and needs no hardcoded date.
    op.execute(
        sa.text(
            """
            UPDATE network_coverage_results
               SET external_alignment_score = NULL,
                   external_alignment_tier  = NULL
             WHERE external_verified_at IS NOT NULL
               AND external_verified_at < now()
            """
        )
    )


def downgrade() -> None:
    """Deliberate no-op.

    The scores this migration cleared were computed from coordinates and were
    never valid. There is no prior state worth restoring, and no information
    left to restore it from — re-running the sweep is the only way back to a
    populated column.
    """
