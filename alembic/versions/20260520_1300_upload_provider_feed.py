"""Link an upload to a provider (v0.1.37, Phase 1).

Adds `uploads.provider_feed_id` — the OTP feedId (e.g. "SNCF-XB") of the
provider an uploaded file satisfies, so the admin UI can show which file
backs which provider. Nullable: the generic per-session upload path (no
provider selected) leaves it NULL, preserving the pre-v0.1.37 behaviour.

A `(session_id, provider_feed_id)` index supports the "latest file for
this provider" lookup the provider card needs.

See docs/provider-source-modes-design.md.

Revision ID: 20260520_1300_upload_provider
Revises: 20260505_2200_coverage_hubs
Create Date: 2026-05-20 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic. (Kept <=30 chars — the default
# alembic_version.version_num column is VARCHAR(32); see the v0.1.32.1 note
# in 20260505_2200_coverage_hubs.)
revision: str = "20260520_1300_upload_provider"
down_revision: str | Sequence[str] | None = "20260505_2200_coverage_hubs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("provider_feed_id", sa.String(), nullable=True))
    op.create_index(
        "ix_uploads_session_provider",
        "uploads",
        ["session_id", "provider_feed_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_uploads_session_provider", "uploads")
    op.drop_column("uploads", "provider_feed_id")
