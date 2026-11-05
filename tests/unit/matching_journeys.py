"""Builders of canonical journeys for the matching engine's tests (#304).

Times are `HH:MM` on Monday 9 November 2026, UTC. Names and codes are invented or public
station names; ids are of the module's form (`ZZ-99...`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.journey.canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalOperator,
    CanonicalService,
    CanonicalStopPlace,
    CanonicalTiming,
    GeoPosition,
    JourneySource,
    ProductCategory,
    TransportMode,
)

DAY = datetime(2026, 11, 9, tzinfo=UTC)
RAIL = TransportMode.RAIL
TRAM = TransportMode.TRAM
BUS = TransportMode.BUS


def at(hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return DAY + timedelta(hours=int(hours), minutes=int(minutes))


def stop(
    name: str,
    *,
    uic: str | None = None,
    station_id: str | None = None,
    parent_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    token: str | None = None,
    source: str = "motis:zz",  # DEFAULT_SOURCE
) -> CanonicalLocation:
    """A stop; without a code, an id or a position it is unknown to every comparison."""
    place = GeoPosition(lat=lat, lon=lon) if lat is not None and lon is not None else None
    return CanonicalLocation(
        kind="stop",
        name=name,
        stop_place=CanonicalStopPlace(
            ref=f"{source}:{name}",
            source=source,
            name=name,
            uic=uic,
            unresolved_token=token,
            position=place,
            station_id=station_id,
            station_parent_id=parent_id,
        ),
    )


def service(
    *,
    mode: TransportMode = RAIL,
    line: str | None = None,
    number: str | None = None,
    published: str | None = None,
    category: str | None = None,
    operator: str | None = None,
) -> CanonicalService:
    return CanonicalService(
        mode=mode,
        line=line,
        train_number=number,
        published_name=published,
        product_category=ProductCategory(code=category) if category else None,
        operator=CanonicalOperator(name=operator) if operator else None,
    )


def ride(
    origin: CanonicalLocation | str,
    destination: CanonicalLocation | str,
    departure: str,
    arrival: str,
    **fields: Any,
) -> CanonicalLeg:
    """A timed leg; `fields` are those of `service`."""
    start, end = at(departure), at(arrival)
    return CanonicalLeg(
        kind="timed",
        origin=stop(origin) if isinstance(origin, str) else origin,
        destination=stop(destination) if isinstance(destination, str) else destination,
        departure=CanonicalTiming(timetabled=start),
        arrival=CanonicalTiming(timetabled=end),
        duration_seconds=int((end - start).total_seconds()),
        service=service(**fields),
        source_mode=str(fields.get("mode", RAIL)),
    )


def walk(origin: str, destination: str, departure: str, arrival: str) -> CanonicalLeg:
    start, end = at(departure), at(arrival)
    return CanonicalLeg(
        kind="transfer",
        origin=stop(origin),
        destination=stop(destination),
        departure=CanonicalTiming(timetabled=start),
        arrival=CanonicalTiming(timetabled=end),
        duration_seconds=int((end - start).total_seconds()),
        continuous_mode="WALK",
    )


DEFAULT_SOURCE = "motis:zz"


def _of_provider(location: CanonicalLocation, provider: str) -> CanonicalLocation:
    """A stop built with the default source takes the journey's provider as its source, as
    an adapter's would: two sources' stops without a code never share a fingerprint key."""
    place = location.stop_place
    if place is None or place.source != DEFAULT_SOURCE or provider == "motis":
        return location
    source = f"{provider}:zz"
    return location.model_copy(
        update={
            "stop_place": place.model_copy(
                update={"source": source, "ref": f"{source}:{place.name}"}
            )
        }
    )


def journey(journey_id: str, *legs: CanonicalLeg, provider: str = "motis") -> CanonicalJourney:
    legs = tuple(
        leg.model_copy(
            update={
                "origin": _of_provider(leg.origin, provider),
                "destination": _of_provider(leg.destination, provider),
            }
        )
        for leg in legs
    )
    times = [leg.departure for leg in legs if leg.departure] + [
        leg.arrival for leg in legs if leg.arrival
    ]
    start = min(t.timetabled for t in times) if times else DAY
    end = max(t.timetabled for t in times) if times else DAY
    return CanonicalJourney(
        id=journey_id,
        source=JourneySource(
            provider=provider,  # type: ignore[arg-type]
            role="viator" if provider in ("motis", "otp") else "reference",
            adapter_version=1,
        ),
        legs=list(legs),
        departure=CanonicalTiming(timetabled=start) if times else None,
        arrival=CanonicalTiming(timetabled=end) if times else None,
        duration_seconds=int((end - start).total_seconds()),
    )
