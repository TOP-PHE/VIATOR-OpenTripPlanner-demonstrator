"""Saved NAP catalogue endpoints (v0.1.12).

Why this exists:

  Pre-v0.1.12, the "Import from NAP" modal had a free-text URL field
  pre-filled with `https://transport.data.gouv.fr/api/datasets`. Operators
  using other NAPs (German `mobilithek.info`, Swiss `data.ch`, Italian
  `trasportiamo.it`) had to remember/look-up the URL each time, AND
  there was no place to attach an authentication credential — most
  non-public NAPs require an API key.

  v0.1.12 introduces a small admin-managed catalogue: each row names a
  NAP (e.g. "France NAP", "Germany Mobilithek") with its endpoint URL,
  optional default country/modes (pre-filled in the import modal so the
  operator doesn't re-pick them), and an optional credential reference
  for authenticated NAPs.

Design choices:

  - **Platform-wide, not user-scoped.** A NAP is shared infrastructure;
    every operator imports from the same France NAP. (The credential
    attached to it is still owned by whoever set it up — see
    `app/credentials.py`'s threat-model docstring.)
  - **Soft credential reference.** `credential_id` is nullable + uses
    `ON DELETE SET NULL`. If a user deletes their credential, the
    catalogue stays but the next NAP fetch falls back to anonymous (and
    will probably fail for non-public NAPs — surfaced as a clean
    "401 Unauthorized" rather than a foreign-key error).
  - **`default_modes` as text** (not JSON). Modes are a small fixed set
    {rail, urban, bus, bike}; comma-joined text is simpler than JSONB
    here and renders directly in the picker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class NapCatalogue(TimestampMixin, Base):
    """One pre-configured NAP catalogue endpoint."""

    __tablename__ = "nap_catalogues"
    __table_args__ = (
        # Friendly names must be unique so the picker dropdown is unambiguous.
        UniqueConstraint("name", name="uq_nap_catalogues_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # User-facing label, e.g. "France NAP", "Germany Mobilithek".
    # 80 chars matches the user_credentials.name limit — same width in pickers.
    name: Mapped[str] = mapped_column(String(80), nullable=False)

    # The DCAT-AP /datasets-or-equivalent endpoint URL.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Pre-fill defaults for the import modal. None = no pre-fill.
    default_country: Mapped[str | None] = mapped_column(String(2))
    # Comma-joined subset of {rail, urban, bus, bike} — e.g. "rail,urban".
    default_modes: Mapped[str | None] = mapped_column(String(80))

    # Optional credential to apply when fetching this NAP. Null = anonymous
    # fetch. The reference points at user_credentials; SET NULL on delete
    # so revoking a key doesn't cascade-delete the catalogue.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_credentials.id", ondelete="SET NULL"),
    )

    # Free-form admin note (e.g. "rate-limited 60 req/h", "API key
    # expires 2027-01"). Never sent to the NAP.
    note: Mapped[str | None] = mapped_column(String(280))
