"""OTP `planConnection` (GraphQL) → CanonicalJourney.

Itinerary `start` / `end` are ISO with an offset; leg `startTime` / `endTime` are epoch
milliseconds. Stop ids are `<feed>:<local>` (`SBB:8507000:0:7`); the feed of a leg is the prefix
of its trip's `gtfsId`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalOperator,
    CanonicalService,
    JourneySource,
    mode_family,
)
from ..trip_normalize import clean_operator_name
from ._common import (
    ADAPTER_VERSION,
    continuous_mode,
    is_timed_mode,
    location,
    mark_transfers,
    number_or_none,
    parse_instant,
    position,
    text_or_none,
    timing,
    viator_stop_place,
)


def journeys_from_plan(
    raw: Mapping[str, Any], *, session_id: str | None = None
) -> list[CanonicalJourney]:
    """One journey per `planConnection` edge, in the engine's order."""
    source = JourneySource(
        provider="otp",
        role="viator",
        session_id=session_id,
        engine="otp",
        adapter_version=ADAPTER_VERSION,
    )
    connection = ((raw or {}).get("data") or {}).get("planConnection") or {}
    out: list[CanonicalJourney] = []
    for rank, edge in enumerate(connection.get("edges") or []):
        node = (edge or {}).get("node") or {}
        legs = [_leg(leg) for leg in node.get("legs") or [] if isinstance(leg, Mapping)]
        out.append(
            CanonicalJourney(
                id=f"otp:{session_id or '-'}:{rank}",
                source=source,
                legs=mark_transfers(legs),
                departure=timing(parse_instant(node.get("start"))),
                arrival=timing(parse_instant(node.get("end"))),
                duration_seconds=int(node.get("duration") or 0),
            )
        )
    return out


def _from_epoch_ms(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _end(place: Mapping[str, Any]) -> CanonicalLocation:
    name = text_or_none(place.get("name"))
    at = position(place.get("lat"), place.get("lon"))
    ref = (place.get("stop") or {}).get("gtfsId")
    if not ref:
        return location(None, name, at)
    feed = str(ref).split(":", 1)[0] if ":" in str(ref) else "-"
    stop = viator_stop_place(source=f"otp:{feed}", ref=str(ref), name=name, at=at)
    return location(stop, name, at)


def _operator(agency: Mapping[str, Any]) -> CanonicalOperator | None:
    ref = text_or_none(agency.get("gtfsId"))
    name = clean_operator_name(agency.get("name"))
    url = text_or_none(agency.get("url"))
    if ref is None and name is None and url is None:
        return None
    return CanonicalOperator(ref=ref, name=name, url=url)


def _leg(leg: Mapping[str, Any]) -> CanonicalLeg:
    mode = leg.get("mode")
    route = leg.get("route") or {}
    trip = leg.get("trip") or {}
    trip_id = str(trip.get("gtfsId") or "")
    timed = is_timed_mode(mode)
    service = None
    if timed:
        line = text_or_none(route.get("shortName"))
        service = CanonicalService(
            mode=mode_family(str(mode)),
            line=line,
            published_name=line,
            route_long_name=text_or_none(route.get("longName")),
            line_ref=text_or_none(route.get("gtfsId")),
            journey_ref=trip_id or None,
            destination_text=text_or_none(trip.get("tripHeadsign")),
            operator=_operator(route.get("agency") or {}),
            feed=trip_id.split(":", 1)[0] if ":" in trip_id else None,
        )
    distance = number_or_none(leg.get("distance"))
    return CanonicalLeg(
        kind="timed" if timed else "continuous",
        origin=_end(leg.get("from") or {}),
        destination=_end(leg.get("to") or {}),
        departure=timing(_from_epoch_ms(leg.get("startTime"))),
        arrival=timing(_from_epoch_ms(leg.get("endTime"))),
        duration_seconds=int(leg.get("duration") or 0),
        distance_meters=distance if distance is not None and distance >= 0 else None,
        service=service,
        continuous_mode=None if timed else continuous_mode(mode),
        source_mode=text_or_none(mode),
    )
