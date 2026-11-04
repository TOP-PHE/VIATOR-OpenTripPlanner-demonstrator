"""CanonicalJourney → today's trip dict (architecture.md ch. 4, "the canonical trip dict").

**Bridge, kept until #304 replaces the rules written on these dicts.** Since #303 PR 4a storage
holds canonical journeys; the rules still written on the old dicts read them from here, so their
results do not change (design D5): `trip_signature` and the summary columns (the recorder),
`transit_fingerprint` (the `/journey` comparison, the federated dedup), the coverage scorer's VIATOR
side and the coverage modal's legs (until PR 4b). It is also what proves the
adapters lose nothing: `to_trip_dict(adapter(raw))` equals the client's own normaliser, key for
key (`tests/unit/test_canonical_adapters.py`), times compared as instants.

The old dicts differ by provider (design §1.3); this is the one place those differences are
written down, which is why it lives with the adapters.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..canonical import CanonicalJourney, CanonicalLeg, CanonicalLocation, CanonicalTiming
from ..trip_normalize import first_transit_leg_departure_utc
from .motis_adapter import feed_id_from_motis_id

_SOURCE_FEED = {"hafas": "fahrplan.oebb.at", "ojp": "OJP"}


def _iso(value: CanonicalTiming | None) -> str | None:
    if value is None:
        return None
    return value.timetabled.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _end(prefix: str, end: CanonicalLocation, provider: str) -> dict[str, Any]:
    stop = end.stop_place
    fields: dict[str, Any] = {
        f"{prefix}_name": end.name,
        f"{prefix}_lat": end.position.lat if end.position is not None else None,
        f"{prefix}_lon": end.position.lon if end.position is not None else None,
        f"{prefix}_stop_id": stop.ref if stop is not None else None,
    }
    if provider == "motis":
        fields[f"{prefix}_stop_code"] = stop.codes.get("GTFS_STOP_CODE") if stop else None
        fields[f"{prefix}_parent_id"] = stop.parent_ref if stop else None
    return fields


def _route_names(leg: CanonicalLeg, provider: str) -> tuple[str | None, str | None, str | None]:
    """`(route_short_name, route_long_name, route_id)` with each provider's old meaning."""
    service = leg.service
    if service is None:
        return None, None, None
    if provider == "hafas":
        short = service.line or service.published_name
        return short, service.published_name if service.line else None, service.journey_ref
    if provider == "ojp":
        category = service.product_category.name if service.product_category else None
        return service.published_name or service.line, category, service.line_ref
    return service.line, service.route_long_name, service.line_ref


def _feed_id(leg: CanonicalLeg, provider: str) -> str | None:
    if provider in _SOURCE_FEED:
        return _SOURCE_FEED[provider]
    if provider == "motis":
        stop = leg.origin.stop_place
        return feed_id_from_motis_id(stop.ref if stop is not None else None)
    return leg.service.feed if leg.service is not None else None


def _leg_dict(leg: CanonicalLeg, provider: str) -> dict[str, Any]:
    service = leg.service
    operator = service.operator if service is not None else None
    short, long_name, route_id = _route_names(leg, provider)
    # MOTIS's client copied its time strings as given, "" when missing.
    missing_time = "" if provider == "motis" else None
    departure = _iso(leg.departure)
    arrival = _iso(leg.arrival)
    return {
        "mode": leg.source_mode,
        "departure": departure if departure is not None else missing_time,
        "arrival": arrival if arrival is not None else missing_time,
        "duration_seconds": leg.duration_seconds,
        "distance_meters": leg.distance_meters or 0.0,
        **_end("from", leg.origin, provider),
        **_end("to", leg.destination, provider),
        "route_short_name": short,
        "route_long_name": long_name,
        "route_id": route_id,
        "agency_name": operator.name if operator is not None else None,
        "agency_id": operator.ref if operator is not None else None,
        "agency_url": operator.url if operator is not None else None,
        "feed_id": _feed_id(leg, provider),
        "trip_id": service.journey_ref if service is not None else None,
        "trip_headsign": service.destination_text if service is not None else None,
    }


def _num_transfers(journey: CanonicalJourney, legs: list[dict[str, Any]], provider: str) -> int:
    if journey.source_transfer_count is not None:
        return journey.source_transfer_count
    if provider == "otp":
        return max(0, len([lg for lg in legs if lg["mode"] not in (None, "WALK")]) - 1)
    # MOTIS without `transfers`: every leg that is not a walk.
    return max(0, sum(1 for lg in legs if lg["mode"] != "WALK") - 1)


def _modes(legs: list[dict[str, Any]], provider: str) -> str:
    """OTP and OJP list WALK among the modes; MOTIS and HAFAS leave it out."""
    if provider in ("otp", "ojp"):
        return ",".join(sorted({lg["mode"] for lg in legs if lg["mode"]}))
    return ",".join(sorted({lg["mode"] for lg in legs if lg["mode"] and lg["mode"] != "WALK"}))


def to_trip_dict(journey: CanonicalJourney) -> dict[str, Any]:
    provider = journey.source.provider
    legs = [_leg_dict(leg, provider) for leg in journey.legs]
    return {
        "duration_seconds": journey.duration_seconds,
        "num_transfers": _num_transfers(journey, legs, provider),
        "departure_at": _iso(journey.departure) or "",
        "arrival_at": _iso(journey.arrival) or "",
        "modes": _modes(legs, provider),
        "legs": legs,
        "first_transit_leg_departure_utc": first_transit_leg_departure_utc(legs),
    }


def to_trip_dicts(journeys: Sequence[CanonicalJourney]) -> list[dict[str, Any]]:
    return [to_trip_dict(journey) for journey in journeys]
