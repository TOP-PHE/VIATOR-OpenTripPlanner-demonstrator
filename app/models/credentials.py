"""User-owned API credentials for authenticated provider URLs (v0.1.10).

Why this exists:

  Many transit data feeds we want to consume (SNCF GTFS-RT, Swiss SBB,
  some German VRRs, most Italian regional feeds, private operator-provided
  feeds) require an HTTP authentication header or API-key query string.
  VIATOR's `httpx.AsyncClient(...)` calls in the refresh path went out
  unauthenticated until v0.1.10, so only fully-public feeds (France NAP)
  worked.

Design choices:

  - **User-scoped, not session-scoped.** A content_manager who has an
    SNCF API key wants to reuse it across every session they configure
    that uses an SNCF feed. Storing per-session would force re-entering
    the same key for each session.

  - **Reference by id, not by name.** A session's provider config stores
    the credential's UUID. The credential itself can be renamed without
    breaking session references. Picker UI surfaces the user's own
    credentials by their friendly `name`.

  - **Encrypted at rest with AES-256-GCM.** Key is derived from
    `JWT_SECRET` via HKDF (see `app/credentials.py`). Anyone with DB
    backup access cannot read secrets without `JWT_SECRET`. Anyone with
    code execution can decrypt — by design. This protects against
    accidental data leaks (backups, devops snapshots), not against
    insider-attacker scenarios.

  - **Cross-user usage of credentials is allowed but auditable.** When
    a user attaches credential X to a session, any subsequent operator
    editing the same session can keep using it (HTTP requests succeed)
    or detach it (back to anonymous). The credential's owner can revoke
    by deleting; that surfaces as a clear "credential X not found" error
    on the next refresh, not a silent failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

# Auth types we support. Mirrors `app/credentials.py::AUTH_TYPES`.
# Migration's CHECK constraint must list the same values.
AUTH_TYPE_NONE = "none"
AUTH_TYPE_BEARER = "bearer"
AUTH_TYPE_BASIC = "basic"
AUTH_TYPE_QUERY = "query"
AUTH_TYPE_HEADER = "header"


class UserCredential(TimestampMixin, Base):
    """One user-owned, encrypted-at-rest API credential.

    Storage layout (per row):
        ciphertext = AES-256-GCM(plaintext, key=HKDF(JWT_SECRET))
        nonce      = 12-byte random GCM nonce, regenerated on every update

    Plaintext shape depends on `auth_type`:
        bearer   plaintext is the bearer token (no "Bearer " prefix)
        basic    plaintext is "user:pass" (httpx encodes to b64)
        query    plaintext is the value; param_name holds the URL key
        header   plaintext is the value; param_name holds the header name
        none     unused (we don't create rows for `none`)

    The plaintext is short (typically < 200 chars), but we use LargeBinary
    so we never have to grow the column for a longer secret.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        # CHECK keeps the auth_type set in sync with the application enum.
        # Bump both when adding a new auth scheme.
        CheckConstraint(
            "auth_type IN ('bearer','basic','query','header')",
            name="ck_user_credentials_auth_type",
        ),
        # `param_name` is required for query+header (URL key / header name).
        # Bearer + basic don't use it (the scheme is the "name"). Enforced
        # by app code on POST/PATCH; CHECK here is a belt-and-braces.
        CheckConstraint(
            "(auth_type IN ('bearer','basic') AND param_name IS NULL) "
            "OR (auth_type IN ('query','header') AND param_name IS NOT NULL "
            "AND length(param_name) > 0)",
            name="ck_user_credentials_param_name_required",
        ),
        # A user can't have two credentials with the same friendly name
        # (would make the picker ambiguous).
        UniqueConstraint("user_id", "name", name="uq_user_credentials_user_id_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # User-facing label, e.g. "SNCF prod key", "My Trenitalia token".
    # Limited to 80 chars — fits in a one-line dropdown without truncation.
    name: Mapped[str] = mapped_column(String(80), nullable=False)

    auth_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # For query: the URL parameter name (e.g. "apikey").
    # For header: the HTTP header name (e.g. "X-API-Key").
    # Null for bearer / basic.
    param_name: Mapped[str | None] = mapped_column(String(80))

    # AES-256-GCM ciphertext + 12-byte nonce. Both regenerated on every update.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Free-text note shown to the operator. Never sent to the provider.
    # Use case: "expires 2027-01"; "shared with Patrick"; etc.
    note: Mapped[str | None] = mapped_column(String(280))

    # When this credential was last used in a successful HTTP request.
    # Populated by `app/credentials.py::touch_used`. Lets the user see
    # "this credential hasn't been used in 90 days, maybe drop it".
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
