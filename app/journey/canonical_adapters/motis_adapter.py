"""MOTIS `/api/v6/plan` → CanonicalJourney.

Field names as `motis_client._leg_to_canonical` documents them (Renfe AVE spike, eu19
measurements): `lat`/`lon`, top-level `routeShortName` / `agencyName`, `<feed>_<local>` stop ids
at platform level with the station in `parentId`, a UIC in `stopCode` on some feeds only.
MOTIS gives no leg distance and no train number field the client reads.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from ..motis_client import _feed_id_from_motis_id
from ..trip_normalize import clean_operator_name
from ._common import (
    ADAPTER_VERSION,
    continuous_mode,
    is_timed_mode,
    location,
    mark_transfers,
    parse_instant,
    position,
    text_or_none,
    timing,
    viator_stop_place,
)


def journeys_from_plan(
    raw: Mapping[str, Any], *, session_id: str | None = None
) -> list[CanonicalJourney]:
    """One journey per itinerary of a `plan` answer, in the engine's order."""
    source = JourneySource(
        provider="motis",
        role="viator",
        session_id=session_id,
        engine="motis",
        adapter_version=ADAPTER_VERSION,
    )
    out: list[CanonicalJourney] = []
    for rank, itinerary in enumerate(raw.get("itineraries") or []):
        if not isinstance(itinerary, Mapping):
            continue
        legs = [_leg(leg) for leg in itinerary.get("legs") or [] if isinstance(leg, Mapping)]
        transfers = itinerary.get("transfers")
        out.append(
            CanonicalJourney(
                id=f"motis:{session_id or '-'}:{rank}",
                source=source,
                legs=mark_transfers(legs),
                departure=timing(parse_instant(itinerary.get("startTime"))),
                arrival=timing(parse_instant(itinerary.get("endTime"))),
                duration_seconds=int(itinerary.get("duration") or 0),
                source_transfer_count=int(transfers) if transfers is not None else None,
            )
        )
    return out


def _end(place: Mapping[str, Any]) -> CanonicalLocation:
    name = text_or_none(place.get("name"))
    at = position(place.get("lat"), place.get("lon"))
    ref = place.get("stopId")
    if not ref:
        return location(None, name, at)
    stop = viator_stop_place(
        source=f"motis:{_feed_id_from_motis_id(str(ref)) or '-'}",
        ref=str(ref),
        name=name,
        at=at,
        stop_code=place.get("stopCode"),
        parent_ref=text_or_none(place.get("parentId")),
    )
    return location(stop, name, at)


def _operator(leg: Mapping[str, Any]) -> CanonicalOperator | None:
    ref = text_or_none(leg.get("agencyId"))
    name = clean_operator_name(leg.get("agencyName"))
    url = text_or_none(leg.get("agencyUrl"))
    if ref is None and name is None and url is None:
        return None
    return CanonicalOperator(ref=ref, name=name, url=url)


def _leg(leg: Mapping[str, Any]) -> CanonicalLeg:
    mode = leg.get("mode")
    origin_place = leg.get("from") or {}
    duration = leg.get("duration")
    timed = is_timed_mode(mode)
    service = None
    if timed:
        line = text_or_none(leg.get("routeShortName"))
        service = CanonicalService(
            mode=mode_family(str(mode)),
            line=line,
            published_name=line,
            route_long_name=text_or_none(leg.get("routeLongName")),
            line_ref=text_or_none(leg.get("routeId")),
            journey_ref=text_or_none(leg.get("tripId")),
            destination_text=text_or_none(leg.get("headsign")),
            operator=_operator(leg),
            feed=_feed_id_from_motis_id(text_or_none(origin_place.get("stopId"))),
        )
    return CanonicalLeg(
        kind="timed" if timed else "continuous",
        origin=_end(origin_place),
        destination=_end(leg.get("to") or {}),
        departure=timing(parse_instant(leg.get("startTime"))),
        arrival=timing(parse_instant(leg.get("endTime"))),
        duration_seconds=int(duration) if duration is not None else 0,
        distance_meters=None,
        service=service,
        continuous_mode=None if timed else continuous_mode(mode),
        source_mode=text_or_none(mode),
    )
