"""Saved NAP catalogue endpoints (v0.1.12).

Adds the `nap_catalogues` table. Each row names a NAP catalogue endpoint
(URL + optional credential + default country/modes) so the Import-from-NAP
modal can offer a dropdown of pre-configured catalogues instead of a
free-text URL input.

See `app/models/nap_catalogues.py` for design rationale.

Revision ID: 20260503_0900_nap_catalogues
Revises: 20260503_0700_user_credentials
Create Date: 2026-05-03 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260503_0900_nap_catalogues"
down_revision: str | Sequence[str] | None = "20260503_0700_user_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nap_catalogues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("default_country", sa.String(2)),
        sa.Column("default_modes", sa.String(80)),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_credentials.id", ondelete="SET NULL"),
        ),
        sa.Column("note", sa.String(280)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_nap_catalogues_name"),
    )

    # Seed the canonical France NAP so a fresh install has at least one
    # catalogue in the picker — operator can edit/delete if they want.
    # Other NAPs (German, Swiss, etc.) are added via the admin UI.
    op.execute(
        sa.text(
            """
            INSERT INTO nap_catalogues (name, url, default_country, default_modes, note)
            VALUES (
                'France NAP (transport.data.gouv.fr)',
                'https://transport.data.gouv.fr/api/datasets',
                'FR',
                'rail',
                'Public, no auth required. Default seed in v0.1.12.'
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_table("nap_catalogues")
