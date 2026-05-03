"""User-owned API credentials for authenticated provider URLs (v0.1.10).

Adds the `user_credentials` table. Each row stores one encrypted secret
(API key, bearer token, basic-auth pair) belonging to one user, attachable
to any session's provider URLs by referencing the row's UUID.

See `app/models/credentials.py` for the design rationale and
`app/credentials.py` for the AES-256-GCM crypto module that produces
the `ciphertext` + `nonce` columns.

Revision ID: 20260503_0700_user_credentials
Revises: 20260429_0700_exec_snap_nullable
Create Date: 2026-05-03 07:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260503_0700_user_credentials"
down_revision: str | Sequence[str] | None = "20260429_0700_exec_snap_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("auth_type", sa.String(16), nullable=False),
        # param_name: URL key for query auth, header name for header auth.
        # Null for bearer / basic.
        sa.Column("param_name", sa.String(80)),
        # AES-256-GCM ciphertext + 12-byte nonce.
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("note", sa.String(280)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Auth-type whitelist — keep in sync with app/models/credentials.py.
        sa.CheckConstraint(
            "auth_type IN ('bearer','basic','query','header')",
            name="ck_user_credentials_auth_type",
        ),
        # param_name presence by auth_type.
        sa.CheckConstraint(
            "(auth_type IN ('bearer','basic') AND param_name IS NULL) "
            "OR (auth_type IN ('query','header') AND param_name IS NOT NULL "
            "AND length(param_name) > 0)",
            name="ck_user_credentials_param_name_required",
        ),
        # No two credentials of the same user can share a friendly name.
        sa.UniqueConstraint("user_id", "name", name="uq_user_credentials_user_id_name"),
    )

    # Index for the "list this user's credentials" query (per-user picker).
    op.create_index(
        "ix_user_credentials_user_id",
        "user_credentials",
        ["user_id"],
    )


def downgrade() -> None:
    # Dropping the table cascades the index automatically.
    # Note: this destroys all stored credentials irrecoverably (encrypted
    # blobs without their key derivation context have no value to keep).
    op.drop_table("user_credentials")
