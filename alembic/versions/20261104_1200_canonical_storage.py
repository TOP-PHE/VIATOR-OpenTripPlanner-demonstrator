"""Journeys stored as canonical JSON (#303 PR 4a, design D1). DELETES DATA.

The owner decided on 2026-11-04 to keep no recorded journey and no coverage run from before
this revision: they restart the journey tests and the coverage runs from scratch. So the upgrade

1. deletes every row of `network_coverage_runs` (their results go by cascade), then every row of
   `journey_searches` (executions and trips go by cascade). Hubs, sessions, settings and users are
   kept;
2. replaces `journey_trips.legs` (today's leg dicts) with `journey`, the journey as
   `CanonicalJourney.model_dump(mode="json")` (docs/schemas/canonical-journey-v1.schema.json);
3. replaces `network_coverage_results.external_itineraries` (the ÖBB check's `VerifyItinerary`
   list) with `external_journeys`, ÖBB's answer as canonical journeys.

Stop every coverage run before deploying: a run the worker holds would be deleted mid-run.

The downgrade puts the old columns back, empty: it cannot bring back what the upgrade deleted
(`legs` comes back nullable, since the rows written since have none).

Revision ID: 20261104_1200_canonical_storage
Revises: 20261027_1200_verify_progress
Create Date: 2026-11-04 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "20261104_1200_canonical_storage"
down_revision: str | Sequence[str] | None = "20261027_1200_verify_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Runs first: their results point at the searches (journey_search_id).
    op.execute("DELETE FROM network_coverage_runs")
    op.execute("DELETE FROM journey_searches")
    op.add_column("journey_trips", sa.Column("journey", JSONB(), nullable=False))
    op.drop_column("journey_trips", "legs")
    op.add_column(
        "network_coverage_results", sa.Column("external_journeys", JSONB(), nullable=True)
    )
    op.drop_column("network_coverage_results", "external_itineraries")


def downgrade() -> None:
    op.add_column(
        "network_coverage_results", sa.Column("external_itineraries", JSONB(), nullable=True)
    )
    op.drop_column("network_coverage_results", "external_journeys")
    op.add_column("journey_trips", sa.Column("legs", JSONB(), nullable=True))
    op.drop_column("journey_trips", "journey")
