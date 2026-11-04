"""`CanonicalJourneyToOjpMapper`: CanonicalJourney → OJP 2.0 `TripResult` (XML and JSON).

Element names and order follow the OJP 2.0 `OJPTripDelivery` the Swiss endpoint returns (the
shape `ojp_client` parses). Nothing VIATOR-specific is added: `TripResult` has no extension point
(architecture.md ch. 12). Validation against the OJP XSD is not done here: the schema files are
not in this repository (#306 brings them).

The JSON form is the same tree, element names as keys, `{"Text": …}` kept for international
texts, so a later OJP JSON binding can be derived from it mechanically.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # building only; parsing untrusted XML uses defusedxml
from collections.abc import Sequence
from typing import Any

from ..canonical_journey import CanonicalJourney
from ..canonical_leg import CanonicalLeg
from ..canonical_location import CanonicalLocation
from ..canonical_service import CanonicalService, TransportMode
from ..canonical_timing import CanonicalTiming
from . import iso_duration, iso_instant

_NS_SCHEME = "http:"  # namespace names, never fetched
OJP_NS = f"{_NS_SCHEME}//www.vdv.de/ojp"
SIRI_NS = f"{_NS_SCHEME}//www.siri.org.uk/siri"

_PT_MODE = {
    TransportMode.RAIL: "rail",
    TransportMode.BUS: "bus",
    TransportMode.COACH: "coach",
    TransportMode.TRAM: "tram",
    TransportMode.SUBWAY: "metro",
    TransportMode.FERRY: "water",
    TransportMode.CABLEWAY: "telecabin",
    TransportMode.FUNICULAR: "funicular",
    TransportMode.AIR: "air",
    TransportMode.TAXI: "taxi",
    TransportMode.OTHER: "unknown",
}


def pt_mode(mode: TransportMode) -> str:
    return _PT_MODE[mode]


def _stop_ref(end: CanonicalLocation) -> str | None:
    """The UIC when known (what OJP's `StopPlaceRef` carries for a station), else the source's
    own ref."""
    stop = end.stop_place
    if stop is None:
        return None
    return stop.uic or stop.ref


# ── JSON tree ────────────────────────────────────────────────────────────────────────────


def _text(value: str | None) -> dict[str, str] | None:
    return {"Text": value} if value else None


def _compact(node: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if value not in (None, {}, [])}


def _geo(end: CanonicalLocation) -> dict[str, float] | None:
    if end.position is None:
        return None
    return {"siri:Longitude": end.position.lon, "siri:Latitude": end.position.lat}


def _call_json(
    end: CanonicalLocation, time: CanonicalTiming | None, *, board: bool, order: int
) -> dict[str, Any]:
    service_time = "ServiceDeparture" if board else "ServiceArrival"
    return _compact(
        {
            "siri:StopPointRef": _stop_ref(end),
            "StopPointName": _text(end.name),
            service_time: {"TimetabledTime": iso_instant(time.timetabled)} if time else None,
            "Order": order,
        }
    )


def _service_json(service: CanonicalService) -> dict[str, Any]:
    category = service.product_category
    return _compact(
        {
            "JourneyRef": service.journey_ref,
            "PublicCode": service.line,
            "siri:LineRef": service.line_ref,
            "Mode": {"PtMode": pt_mode(service.mode)},
            "ProductCategory": _compact(
                {"Name": _text(category.name), "ShortName": _text(category.code)}
            )
            if category
            else None,
            "PublishedServiceName": _text(service.published_name),
            "TrainNumber": service.train_number,
            "siri:OperatorRef": service.operator.ref if service.operator else None,
            "DestinationText": _text(service.destination_text),
        }
    )


def _end_json(end: CanonicalLocation) -> dict[str, Any]:
    return _compact(
        {"siri:StopPointRef": _stop_ref(end), "GeoPosition": _geo(end), "Name": _text(end.name)}
    )


def _leg_json(leg: CanonicalLeg, index: int) -> dict[str, Any]:
    body: dict[str, Any]
    if leg.is_timed and leg.service is not None:
        body = {
            "TimedLeg": {
                "LegBoard": _call_json(leg.origin, leg.departure, board=True, order=1),
                "LegAlight": _call_json(leg.destination, leg.arrival, board=False, order=2),
                "Service": _service_json(leg.service),
            }
        }
    elif leg.kind == "transfer":
        body = {
            "TransferLeg": _compact(
                {
                    "TransferType": "walk",
                    "LegStart": _end_json(leg.origin),
                    "LegEnd": _end_json(leg.destination),
                    "Duration": iso_duration(leg.duration_seconds),
                }
            )
        }
    else:
        body = {
            "ContinuousLeg": _compact(
                {
                    "LegStart": _end_json(leg.origin),
                    "LegEnd": _end_json(leg.destination),
                    "Service": {"PersonalMode": "foot"},
                    "Duration": iso_duration(leg.duration_seconds),
                    "Length": round(leg.distance_meters)
                    if leg.distance_meters is not None
                    else None,
                }
            )
        }
    return {"Id": str(index + 1), "Duration": iso_duration(leg.duration_seconds), **body}


def to_ojp_json(journey: CanonicalJourney) -> dict[str, Any]:
    """One `TripResult` as a JSON tree."""
    trip = _compact(
        {
            "Id": journey.id,
            "Duration": iso_duration(journey.duration_seconds),
            "StartTime": iso_instant(journey.departure.timetabled) if journey.departure else None,
            "EndTime": iso_instant(journey.arrival.timetabled) if journey.arrival else None,
            "Transfers": journey.transfer_count,
            "Leg": [_leg_json(leg, index) for index, leg in enumerate(journey.legs)],
        }
    )
    return {"Id": journey.id, "Trip": trip}


# ── XML ──────────────────────────────────────────────────────────────────────────────────


def _qualified(key: str) -> str:
    if key.startswith("siri:"):
        return f"{{{SIRI_NS}}}{key[5:]}"
    return f"{{{OJP_NS}}}{key}"


def _append(parent: ET.Element, key: str, value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _append(parent, key, item)
        return
    child = ET.SubElement(parent, _qualified(key))
    if isinstance(value, dict):
        for sub_key, sub_value in value.items():
            _append(child, sub_key, sub_value)
    else:
        child.text = str(value)


def to_ojp_xml(journeys: Sequence[CanonicalJourney]) -> str:
    """An `OJPTripDelivery` holding one `TripResult` per journey, as XML text."""
    ET.register_namespace("", OJP_NS)
    ET.register_namespace("siri", SIRI_NS)
    delivery = ET.Element(_qualified("OJPTripDelivery"))
    for journey in journeys:
        _append(delivery, "TripResult", to_ojp_json(journey))
    return ET.tostring(delivery, encoding="unicode")


class CanonicalJourneyToOjpMapper:
    """The mapper #303 names, over the two functions above."""

    def to_json(self, journey: CanonicalJourney) -> dict[str, Any]:
        return to_ojp_json(journey)

    def to_xml(self, journeys: Sequence[CanonicalJourney]) -> str:
        return to_ojp_xml(journeys)
