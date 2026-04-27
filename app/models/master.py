"""Master data: stations, route aliases, carriers, and pending-drift mirrors. See spec §7."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MasterStation(Base):
    """UIC-keyed registry of European passenger stations.

    Bootstrap source: Trainline-eu/stations (ODbL).
    """

    __tablename__ = "master_stations"
    __table_args__ = (
        CheckConstraint(
            "source IN ('trainline','sncf','manual','merits','other')",
            name="source_valid",
        ),
        Index("ix_master_stations_country", "country_iso"),
        Index("ix_master_stations_trigramme_sncf", "trigramme_sncf"),
    )

    uic: Mapped[str] = mapped_column(String, primary_key=True)
    uic8_sncf: Mapped[str | None] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str | None] = mapped_column(String)
    country_iso: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    parent_uic: Mapped[str | None] = mapped_column(String, ForeignKey("master_stations.uic"))
    is_main_station: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    is_suggestable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )

    # Operator-specific identifiers
    trigramme_sncf: Mapped[str | None] = mapped_column(String)
    db_code: Mapped[str | None] = mapped_column(String)
    trenitalia_code: Mapped[str | None] = mapped_column(String)
    renfe_code: Mapped[str | None] = mapped_column(String)
    atoc_code: Mapped[str | None] = mapped_column(String)
    other_codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    name_translations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    source: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'trainline'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RouteAlias(Base):
    """Service-name equivalences (e.g. TGV ⇄ TGV INOUI). Editable by content managers."""

    __tablename__ = "route_aliases"
    __table_args__ = (
        # Effective uniqueness: one (alias, canonical_name, scope) tuple at a time.
        UniqueConstraint(
            "alias",
            "canonical_name",
            "scope_country",
            "scope_carrier",
            name="alias_canonical_scope_unique",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    applies_from: Mapped[date | None] = mapped_column(Date)
    applies_until: Mapped[date | None] = mapped_column(Date)
    scope_country: Mapped[str | None] = mapped_column(String(2))
    scope_carrier: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(String)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MasterCarrier(Base):
    """RICS code dictionary."""

    __tablename__ = "master_carriers"
    __table_args__ = (CheckConstraint("source IN ('uic','manual')", name="source_valid"),)

    rics_code: Mapped[str] = mapped_column(String, primary_key=True)
    short_name: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String)
    country_iso: Mapped[str | None] = mapped_column(String(2))
    legacy_codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'uic'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MasterStationPendingDrift(Base):
    """Trainline values that differ from our local edits — surfaced in admin UI."""

    __tablename__ = "master_stations_pending_drift"

    uic: Mapped[str] = mapped_column(String, ForeignKey("master_stations.uic"), primary_key=True)
    trainline_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fields_differing: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MasterCarrierPendingDrift(Base):
    """RICS upstream values that differ from our local edits."""

    __tablename__ = "master_carriers_pending_drift"

    rics_code: Mapped[str] = mapped_column(
        String, ForeignKey("master_carriers.rics_code"), primary_key=True
    )
    upstream_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fields_differing: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
