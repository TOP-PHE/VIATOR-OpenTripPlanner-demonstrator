"""`CanonicalJourneyToOsdmMapper`: CanonicalJourney → an OSDM `Trip` (offer side), JSON.

Proof of concept for #303 AC5. OSDM places refer to stations by UIC (`urn:uic:stn:<code>`), so
a journey with a timed leg at a stop without a UIC is refused (`OsdmMappingError`) rather than
given an invented reference. Field names follow OSDM 3.x `Trip` / `TripLeg` / `TimedLeg` /
`DatedJourney`; they are to be checked against the OSDM JSON Schema the OSCAR project uses, which
is not in this repository (#307).
"""

from __future__ import annotations

from typing import Any

from ..canonical_journey import CanonicalJourney
from ..canonical_leg import CanonicalLeg
from ..canonical_location import CanonicalLocation
from ..canonical_service import CanonicalService, TransportMode
from ..canonical_timing import CanonicalTiming
from . import iso_duration, iso_instant

_PT_MODE = {
    TransportMode.RAIL: "RAIL",
    TransportMode.BUS: "BUS",
    TransportMode.COACH: "COACH",
    TransportMode.TRAM: "TRAM",
    TransportMode.SUBWAY: "METRO",
    TransportMode.FERRY: "WATER",
    TransportMode.CABLEWAY: "TELECABIN",
    TransportMode.FUNICULAR: "FUNICULAR",
    TransportMode.AIR: "AIR",
    TransportMode.TAXI: "TAXI",
    TransportMode.OTHER: "UNKNOWN",
}


class OsdmMappingError(ValueError):
    """The journey cannot be said in OSDM without inventing data."""


def _place(end: CanonicalLocation, *, required: bool) -> dict[str, Any] | None:
    stop = end.stop_place
    if stop is None or stop.uic is None:
        if required:
            raise OsdmMappingError(f"no UIC for the stop {end.name or '(unnamed)'}")
        return None
    reference: dict[str, Any] = {"stopPlaceRef": f"urn:uic:stn:{stop.uic}"}
    if end.name or stop.name:
        reference["name"] = end.name or stop.name
    return reference


def _time(value: CanonicalTiming | None) -> dict[str, str] | None:
    return {"timetabledTime": iso_instant(value.timetabled)} if value is not None else None


def _service(service: CanonicalService) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": {"ptMode": _PT_MODE[service.mode]}}
    if service.published_name:
        out["publishedServiceName"] = service.published_name
    if service.train_number:
        out["vehicleNumbers"] = [service.train_number]
    category = service.product_category
    if category is not None and (category.code or category.name):
        out["productCategory"] = {
            key: value
            for key, value in (("shortName", category.code), ("name", category.name))
            if value
        }
    if service.operator is not None and service.operator.name:
        out["operator"] = {"name": service.operator.name}
    return out


def _leg(leg: CanonicalLeg, index: int) -> dict[str, Any]:
    out: dict[str, Any] = {"id": str(index + 1)}
    if leg.is_timed and leg.service is not None:
        out["timedLeg"] = {
            "start": {
                "stopPlaceRef": _place(leg.origin, required=True),
                "serviceDeparture": _time(leg.departure),
            },
            "end": {
                "stopPlaceRef": _place(leg.destination, required=True),
                "serviceArrival": _time(leg.arrival),
            },
            "service": _service(leg.service),
            "duration": iso_duration(leg.duration_seconds),
        }
        return out
    body: dict[str, Any] = {"duration": iso_duration(leg.duration_seconds)}
    start, end = _place(leg.origin, required=False), _place(leg.destination, required=False)
    if start is not None:
        body["start"] = start
    if end is not None:
        body["end"] = end
    if leg.kind == "transfer":
        out["transferLeg"] = {**body, "transferMode": "WALK"}
    else:
        out["continuousLeg"] = {**body, "continuousMode": leg.continuous_mode or "WALK"}
    return out


def to_osdm_trip(journey: CanonicalJourney) -> dict[str, Any]:
    if journey.departure is None or journey.arrival is None:
        raise OsdmMappingError("a journey without a start or an end time")
    return {
        "id": journey.id,
        "startTime": iso_instant(journey.departure.timetabled),
        "endTime": iso_instant(journey.arrival.timetabled),
        "duration": iso_duration(journey.duration_seconds),
        "legs": [_leg(leg, index) for index, leg in enumerate(journey.legs)],
    }


class CanonicalJourneyToOsdmMapper:
    """The mapper #303 names."""

    def to_trip(self, journey: CanonicalJourney) -> dict[str, Any]:
        return to_osdm_trip(journey)
