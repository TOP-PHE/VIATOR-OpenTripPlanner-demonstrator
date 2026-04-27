"""Sessions — first-class isolated OTP instances. See spec §4."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class SessionCategory(str, enum.Enum):
    NAP = "NAP"
    MERITS = "MERITS"
    MANUAL = "MANUAL"
    EXPERIMENTAL = "EXPERIMENTAL"


class SessionState(str, enum.Enum):
    CREATED = "created"
    CONFIGURED = "configured"
    POPULATED = "populated"
    GRAPH_BUILT = "graph_built"
    SERVING = "serving"
    ARCHIVED = "archived"
    DELETED = "deleted"


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
