"""Swiss OJP 2.0 `OJPTripDelivery` (XML) → CanonicalJourney.

OJP is the model CanonicalJourney is shaped on, so this adapter maps nearly one to one: a
`TimedLeg` is a timed leg, a `TransferLeg` a transfer, a `ContinuousLeg` a continuous leg. The
body is parsed with `defusedxml`, like `ojp_client`. Stop refs are SLOIDs (`ch:1:sloid:7000:4:8`,
the 4-digit Swiss number with `850` dropped) or bare numbers; places carry the coordinates.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from defusedxml.ElementTree import fromstring as _xml_fromstring

from ..canonical import (
    CanonicalJourney,
    CanonicalLeg,
    CanonicalLocation,
    CanonicalOperator,
    CanonicalService,
    JourneySource,
    LegKind,
    ProductCategory,
    mode_family,
)
from ._common import ADAPTER_VERSION, location, parse_instant, position, timing, viator_stop_place

# `xml.etree.ElementTree` is imported only for the `Element` *type* and
# for navigating already-parsed trees (`.find` / `.iter` — safe). The
# one place untrusted bytes are *parsed* uses `defusedxml`'s hardened
# `fromstring` (see `journeys_from_trip_delivery`) — stdlib parsing is XXE-vulnerable.
_NS_SCHEME = "http:"  # spec-mandated namespace scheme — not a fetched URL
_OJP = f"{_NS_SCHEME}//www.vdv.de/ojp"
_SIRI = f"{_NS_SCHEME}//www.siri.org.uk/siri"
_NS = {"ojp": _OJP, "siri": _SIRI}

# `siri:StopPointRef` is pulled from several leg sub-elements; the
# literal lives here once (SonarCloud S1192).
_STOP_POINT_REF = "siri:StopPointRef"

_ISO_DURATION = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)

# The client's logger, which logged this before: log filters and dashboards keep matching.
log = logging.getLogger("app.journey.ojp_client")

STOP_SOURCE = "ojp:ch"
_SLOID_PREFIX = "ch:1:sloid:"


def journeys_from_trip_delivery(xml_text: str) -> list[CanonicalJourney]:
    """One journey per `<Trip>`; a body that is not safe, well-formed XML gives none."""
    try:
        root = _xml_fromstring(xml_text)
    except (ET.ParseError, ValueError):
        # ET.ParseError: malformed XML; defusedxml raises ValueError subclasses
        # (EntitiesForbidden…) on an XML attack. Either way there is no usable trip.
        log.warning("OJP response did not parse as safe XML (%d bytes)", len(xml_text))
        return []
    places = _index_places(root)
    source = JourneySource(provider="ojp", role="reference", adapter_version=ADAPTER_VERSION)
    out: list[CanonicalJourney] = []
    for rank, trip in enumerate(root.iter(f"{{{_OJP}}}Trip")):
        legs = [
            leg
            for element in trip.findall("ojp:Leg", _NS)
            if (leg := _leg(element, places)) is not None
        ]
        out.append(
            CanonicalJourney(
                id=f"ojp:ch:{rank}",
                source=source,
                legs=legs,
                departure=timing(parse_instant(_text(trip, "ojp:StartTime"))),
                arrival=timing(parse_instant(_text(trip, "ojp:EndTime"))),
                duration_seconds=_iso_duration_to_seconds(_text(trip, "ojp:Duration") or ""),
                source_transfer_count=_int_or_zero(_text(trip, "ojp:Transfers") or ""),
            )
        )
    return out


def _text(element: ET.Element, path: str) -> str | None:
    found = element.findtext(path, default=None, namespaces=_NS)
    return found if found else None


def _stop_location(
    ref: str | None, name: str | None, lat: float | None, lon: float | None
) -> CanonicalLocation:
    at = position(lat, lon)
    if not ref:
        return location(None, name, at)
    stop = viator_stop_place(
        source=STOP_SOURCE,
        ref=ref,
        name=name,
        at=at,
        codes={"SLOID": ref} if ref.startswith(_SLOID_PREFIX) else None,
    )
    return location(stop, name, at)


def _call(
    element: ET.Element | None, places: dict[str, dict[str, Any]], time_path: str
) -> tuple[CanonicalLocation, str | None]:
    """A `LegBoard` / `LegAlight`: the location and its timetabled time (as text)."""
    if element is None:
        return location(None, None, None), None
    ref = _text(element, _STOP_POINT_REF)
    name = _text(element, "ojp:StopPointName/ojp:Text") or _place_name(places, ref)
    lat, lon = _place_coords(places, ref)
    return _stop_location(ref, name, lat, lon), _text(element, time_path)


def _service(svc: ET.Element) -> tuple[CanonicalService, str]:
    pt_mode = _text(svc, "ojp:Mode/ojp:PtMode")
    source_mode = pt_mode.upper() if pt_mode else "TRANSIT"
    category_code = _text(svc, "ojp:ProductCategory/ojp:ShortName/ojp:Text") or _text(
        svc, "ojp:Mode/ojp:ShortName/ojp:Text"
    )
    category_name = _text(svc, "ojp:ProductCategory/ojp:Name/ojp:Text")
    operator_ref = _text(svc, "siri:OperatorRef")
    return (
        CanonicalService(
            mode=mode_family(source_mode),
            product_category=ProductCategory(code=category_code, name=category_name)
            if category_code or category_name
            else None,
            line=_text(svc, "ojp:PublicCode"),
            train_number=_text(svc, "ojp:TrainNumber"),
            published_name=_text(svc, "ojp:PublishedServiceName/ojp:Text"),
            line_ref=_text(svc, "siri:LineRef"),
            journey_ref=_text(svc, "ojp:JourneyRef"),
            destination_text=_text(svc, "ojp:DestinationText/ojp:Text"),
            operator=CanonicalOperator(ref=operator_ref) if operator_ref else None,
        ),
        source_mode,
    )


def _timed_leg(timed: ET.Element, duration: int, places: dict[str, dict[str, Any]]) -> CanonicalLeg:
    origin, departure = _call(
        timed.find("ojp:LegBoard", _NS), places, "ojp:ServiceDeparture/ojp:TimetabledTime"
    )
    destination, arrival = _call(
        timed.find("ojp:LegAlight", _NS), places, "ojp:ServiceArrival/ojp:TimetabledTime"
    )
    svc = timed.find("ojp:Service", _NS)
    service, source_mode = _service(svc) if svc is not None else (None, None)
    return CanonicalLeg(
        # A TimedLeg without a Service has no mode: the old dicts treated it as non-transit.
        kind="timed" if service is not None else "continuous",
        origin=origin,
        destination=destination,
        departure=timing(parse_instant(departure)),
        arrival=timing(parse_instant(arrival)),
        duration_seconds=duration,
        service=service,
        source_mode=source_mode,
    )


def _walk_end(element: ET.Element | None, places: dict[str, dict[str, Any]]) -> CanonicalLocation:
    if element is None:
        return location(None, None, None)
    name, lat, lon, ref = _leg_endpoint(element, places)
    return _stop_location(ref, name, lat, lon)


def _walk_leg(
    walk: ET.Element, kind: LegKind, duration: int, places: dict[str, dict[str, Any]]
) -> CanonicalLeg:
    length = _float_or_none(walk.findtext("ojp:Length", namespaces=_NS))
    return CanonicalLeg(
        kind=kind,
        origin=_walk_end(walk.find("ojp:LegStart", _NS), places),
        destination=_walk_end(walk.find("ojp:LegEnd", _NS), places),
        duration_seconds=duration,
        distance_meters=length if length is not None and length >= 0 else None,
        continuous_mode="WALK",
        source_mode="WALK",
    )


def _leg(element: ET.Element, places: dict[str, dict[str, Any]]) -> CanonicalLeg | None:
    duration = _iso_duration_to_seconds(_text(element, "ojp:Duration") or "")
    timed = element.find("ojp:TimedLeg", _NS)
    if timed is not None:
        return _timed_leg(timed, duration, places)
    continuous = element.find("ojp:ContinuousLeg", _NS)
    if continuous is not None:
        return _walk_leg(continuous, "continuous", duration, places)
    transfer = element.find("ojp:TransferLeg", _NS)
    if transfer is not None:
        return _walk_leg(transfer, "transfer", duration, places)
    return None  # an unknown leg variant: skipped, as ojp_client does


# ── parsing helpers (moved from ojp_client, #303 PR 3a) ─────────────────────────────


def _index_places(root: ET.Element) -> dict[str, dict[str, Any]]:
    """Build `{stopRef: {name, lat, lon}}` from `TripResponseContext/Places`.

    Legs reference stops by `siri:StopPointRef` / `StopPlaceRef`; the
    coordinates live once, here, in the place dictionary.
    """
    index: dict[str, dict[str, Any]] = {}
    for place in root.iter(f"{{{_OJP}}}Place"):
        name = place.findtext("ojp:Name/ojp:Text", default=None, namespaces=_NS)
        geo = place.find("ojp:GeoPosition", _NS)
        lat = (
            _float_or_none(geo.findtext("siri:Latitude", namespaces=_NS))
            if geo is not None
            else None
        )
        lon = (
            _float_or_none(geo.findtext("siri:Longitude", namespaces=_NS))
            if geo is not None
            else None
        )
        # A Place is one of StopPlace / StopPoint / TopographicPlace; the
        # first two carry the refs the legs use.
        for ref in (
            place.findtext("ojp:StopPlace/ojp:StopPlaceRef", namespaces=_NS),
            place.findtext("ojp:StopPoint/siri:StopPointRef", namespaces=_NS),
        ):
            if ref:
                index[ref] = {"name": name, "lat": lat, "lon": lon}
    return index


def _leg_endpoint(
    el: ET.Element, places: dict[str, dict[str, Any]]
) -> tuple[str | None, float | None, float | None, str | None]:
    """A LegStart/LegEnd is either an inline GeoPosition or a StopPointRef
    resolved via the Places dictionary. Returns (name, lat, lon, stop_ref)."""
    name = el.findtext("ojp:Name/ojp:Text", default=None, namespaces=_NS)
    ref = el.findtext(_STOP_POINT_REF, default=None, namespaces=_NS)
    geo = el.find("ojp:GeoPosition", _NS)
    if geo is not None:
        return (
            name,
            _float_or_none(geo.findtext("siri:Latitude", namespaces=_NS)),
            _float_or_none(geo.findtext("siri:Longitude", namespaces=_NS)),
            ref,
        )
    if ref:
        lat, lon = _place_coords(places, ref)
        return (name or _place_name(places, ref), lat, lon, ref)
    return (name, None, None, ref)


def _place_name(places: dict[str, dict[str, Any]], ref: str | None) -> str | None:
    return places.get(ref or "", {}).get("name")


def _place_coords(
    places: dict[str, dict[str, Any]], ref: str | None
) -> tuple[float | None, float | None]:
    p = places.get(ref or "", {})
    return p.get("lat"), p.get("lon")


def _iso_duration_to_seconds(value: str | None) -> int:
    """Parse an ISO-8601 duration (`PT1H9M`, `PT4M`, `PT0S`, …) to whole
    seconds. OJP trip/leg durations are always the `PT…` time form, but
    the regex tolerates the date components too. Unparseable → 0."""
    if not value:
        return 0
    m = _ISO_DURATION.match(value.strip())
    if not m:
        return 0
    # Years / months (groups 1-2) are calendar-ambiguous and never
    # appear in a trip/leg duration - ignored rather than guessed.
    _, _, weeks, days, hours, minutes, seconds = m.groups()
    total = 0.0
    total += int(days or 0) * 86400
    total += int(weeks or 0) * 604800
    total += int(hours or 0) * 3600
    total += int(minutes or 0) * 60
    total += float(seconds or 0)
    return int(total)


def _int_or_zero(value: str | None) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None
