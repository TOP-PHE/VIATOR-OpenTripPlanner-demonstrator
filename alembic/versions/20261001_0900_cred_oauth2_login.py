"""Allow the `oauth2_password` credential scheme.

A login (token URL, client id, username, password) exchanged for a Bearer
token on each use — the scripted-download flow of the Austrian NAP
(data.mobilitaetsverbuende.at, Keycloak). Both CHECK constraints on
`user_credentials` list the schemes, so both are widened.

The constraints are found by suffix rather than by name: the table was
created through `op.create_table` under the metadata naming convention, so
the stored name may carry the `ck_user_credentials_` prefix once or twice.
Each is recreated under the name it already had.

Revision ID: 20261001_0900_cred_oauth2_login
Revises: 20260908_1200_null_pre_extid
Create Date: 2026-10-01 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261001_0900_cred_oauth2_login"
down_revision: str | Sequence[str] | None = "20260908_1200_null_pre_extid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTH_TYPE_NEW = "auth_type IN ('bearer','basic','query','header','oauth2_password')"
_AUTH_TYPE_OLD = "auth_type IN ('bearer','basic','query','header')"
_PARAM_NAME_NEW = (
    "(auth_type IN ('bearer','basic','oauth2_password') AND param_name IS NULL) "
    "OR (auth_type IN ('query','header') AND param_name IS NOT NULL "
    "AND length(param_name) > 0)"
)
_PARAM_NAME_OLD = (
    "(auth_type IN ('bearer','basic') AND param_name IS NULL) "
    "OR (auth_type IN ('query','header') AND param_name IS NOT NULL "
    "AND length(param_name) > 0)"
)


def _replace_check(suffix: str, default_name: str, condition: str) -> None:
    # `\_` escapes LIKE's single-character wildcard.
    pattern = "%" + suffix.replace("_", "\\_")
    # Every interpolated value is a module constant above, never input.
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE n text;
            BEGIN
              SELECT conname INTO n FROM pg_constraint
               WHERE conrelid = 'user_credentials'::regclass
                 AND contype = 'c' AND conname LIKE '{pattern}';
              IF n IS NULL THEN n := '{default_name}'; END IF;
              EXECUTE format('ALTER TABLE user_credentials DROP CONSTRAINT IF EXISTS %I', n);
              EXECUTE format('ALTER TABLE user_credentials ADD CONSTRAINT %I CHECK ({condition.replace("'", "''")})', n);
            END $$;
            """  # noqa: S608
        )
    )


def upgrade() -> None:
    _replace_check("auth_type", "ck_user_credentials_auth_type", _AUTH_TYPE_NEW)
    _replace_check(
        "param_name_required", "ck_user_credentials_param_name_required", _PARAM_NAME_NEW
    )


def downgrade() -> None:
    # Rows using the new scheme would violate the old constraints.
    op.execute(sa.text("DELETE FROM user_credentials WHERE auth_type = 'oauth2_password'"))
    _replace_check("auth_type", "ck_user_credentials_auth_type", _AUTH_TYPE_OLD)
    _replace_check(
        "param_name_required", "ck_user_credentials_param_name_required", _PARAM_NAME_OLD
    )
