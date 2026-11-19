"""The places ÖBB's HAFAS planner answered in VIATOR's coverage runs (the station module's design
22.20, its decision 70, way a): the cumulative list VIATOR sends the module as a stop list
(`app/hafas_stop_list.py`).

  hafas_places      — one row per place of HAFAS type station (`A=1` in its lid) at which a
                      train of ÖBB's answers starts or ends a leg: its number (`extId`), name and
                      position as HAFAS last gave them, the UIC code its `gidL` states (when
                      one was seen), how many such calls were seen, when first and last, and the
                      operators of those legs. Never shrinks: a run that asked other pairs adds
                      to it (a list that shrinks would be held by the module's guard, its 22.4).

  hafas_place_runs  — one row per coverage run whose ÖBB journeys were added, so that a run is
                      counted once (`app/network_coverage/hafas_places.py`).

No row of either holds a person's data; the places are ÖBB's (decision 69: kept).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# A HAFAS number as the list keeps it: digits only (ÖBB's `extId`), at most 15.
EXT_ID_CHECK = "ext_id ~ '^[0-9]{1,15}$'"
UIC_CHECK = "uic IS NULL OR uic ~ '^[0-9]{7}$'"


class HafasPlace(Base):
    __tablename__ = "hafas_places"
    __table_args__ = (
        CheckConstraint(EXT_ID_CHECK, name="ext_id_digits"),
        CheckConstraint(UIC_CHECK, name="uic_digits"),
        CheckConstraint("times_seen >= 1", name="times_seen_positive"),
        CheckConstraint("lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180", name="position"),
    )

    ext_id: Mapped[str] = mapped_column(String(15), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    uic: Mapped[str | None] = mapped_column(String(7), nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operators: Mapped[list[str]] = mapped_column(
        ARRAY(String(200)), nullable=False, server_default=text("'{}'")
    )


class HafasPlaceRun(Base):
    __tablename__ = "hafas_place_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_coverage_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The train calls at a station of the list the run's ÖBB journeys held, and their places.
    calls: Mapped[int] = mapped_column(Integer, nullable=False)
    places: Mapped[int] = mapped_column(Integer, nullable=False)
