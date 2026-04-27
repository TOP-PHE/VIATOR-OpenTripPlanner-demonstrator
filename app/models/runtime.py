"""Per-session runtime data: MCT overrides, station cross-reference."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class McTOverride(Base):
    """Minimum connection times — per-session, per-station, per-carrier-pair."""

    __tablename__ = "mct_overrides"

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.id"), primary_key=True
    )
    station_code: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_a: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_b: Mapped[str] = mapped_column(String, primary_key=True)
    min_minutes: Mapped[int] = mapped_column(Integer, nullable=False)


class StationXref(Base):
    """Per-session bridge: feed-specific stop_id → master_stations.uic + extras."""

    __tablename__ = "stations_xref"
    __table_args__ = (
        ForeignKeyConstraint(["uic"], ["master_stations.uic"], name="fk_xref_uic"),
    )

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.id"), primary_key=True
    )
    stop_id: Mapped[str] = mapped_column(String, primary_key=True)
    uic: Mapped[str | None] = mapped_column(String)
    trigramme: Mapped[str | None] = mapped_column(String)
    insee: Mapped[str | None] = mapped_column(String)
    rics: Mapped[str | None] = mapped_column(String)
