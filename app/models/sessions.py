"""Sessions — first-class isolated OTP instances. See spec §4."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


# See `app/models/identity.py` for the rationale on the StrEnum migration
# (audit-2026-05 #24 — ruff UP042). Behaviour-preserving for our usage:
# the value is what `str(member)` returns in both forms.
class SessionCategory(StrEnum):
    NAP = "NAP"
    MERITS = "MERITS"
    MANUAL = "MANUAL"
    EXPERIMENTAL = "EXPERIMENTAL"


class SessionState(StrEnum):
    CREATED = "created"
    CONFIGURED = "configured"
    POPULATED = "populated"
    GRAPH_BUILT = "graph_built"
    SERVING = "serving"
    ARCHIVED = "archived"
    DELETED = "deleted"


class SessionEngine(StrEnum):
    OTP = "otp"
    MOTIS = "motis"


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "category IN ('NAP','MERITS','MANUAL','EXPERIMENTAL')",
            name="category_valid",
        ),
        CheckConstraint(
            "state IN ('created','configured','populated','graph_built',"
            "'serving','archived','deleted')",
            name="state_valid",
        ),
        CheckConstraint(
            "engine IN ('otp','motis')",
            name="engine_valid",
        ),
        # Partial index: only currently-serving fanout sessions need fast lookup.
        Index(
            "ix_sessions_fanout",
            "state",
            "include_in_fanout",
            postgresql_where=text("state = 'serving' AND include_in_fanout"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)  # slug like 'nap-fr-2026-q2'
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    # P1 MOTIS — which planner backend serves this session. Existing rows
    # backfill to 'otp' via the alembic server_default; new sessions pick
    # explicitly via the admin form. See app/journey/planner_dispatch.py.
    engine: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'otp'"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    include_in_fanout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
