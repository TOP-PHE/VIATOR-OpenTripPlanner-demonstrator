"""MOTIS `/api/v6/plan` → CanonicalJourney.

Field names verified against a real Renfe AVE answer (Phase-0.5 spike, Madrid Atocha →
Barcelona Sants, 2026-06-19) and the live eu19 session:
- place coordinates are `lat` / `lon`; route and agency fields are top-level on the leg
  (`routeShortName`, `agencyName`), no nested `route` / `agency` object;
- `stopId` is `<feed>_<local>` (underscore, OTP uses `:`), at platform level: Amsterdam
  Centraal comes as three `stopId`s sharing `parentId = nl-nap_stoparea:18188`;
- `stopCode` is the station code when the feed publishes one, measured 2026-09-07 on 30 GTFS
  feeds: only `eurostar` puts a UIC there (`8400058` = ÖBB HAFAS's `extId`), `nl-nap` a local
  short code (`1101`), IDFM/OURA/transilien platform ordinals. So it is kept as a code and
  trusted as a UIC only when UIC-shaped (`signature.stop_token`);
- no leg distance, and no train number field: MOTIS reads only `trainNumbers` / a `TripNr`
  key from NeTEx, and NMBS gives none.
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


def feed_id_from_motis_id(motis_id: str | None) -> str | None:
    """The feed of a MOTIS stop or trip id: `renfe-ld` of `renfe-ld_60000`.

    A NeTEx local id carries `:` (`DE::ScheduledStopPoint:8700014_DBDB`) and may itself contain
    `_`, while a feed tag never contains `:`: so the split is made within the part before the
    first `:` (`db_DE::…` gives `db`). None for an id without the `<feed>_<local>` shape; the
    federated planner then stitches by coordinates."""
    if not motis_id or "_" not in motis_id:
        return None
    head = motis_id.split(":", 1)[0] if ":" in motis_id else motis_id
    if "_" not in head:
        return None
    return head.rsplit("_", 1)[0] or None


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
        source=f"motis:{feed_id_from_motis_id(str(ref)) or '-'}",
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
            feed=feed_id_from_motis_id(text_or_none(origin_place.get("stopId"))),
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
