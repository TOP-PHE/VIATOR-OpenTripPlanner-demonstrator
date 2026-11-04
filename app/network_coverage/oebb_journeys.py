"""ÖBB's answer in a coverage cell, as canonical journeys (#303 PR 4a).

The check asks ÖBB HAFAS once per cell (`external_verify.fetch_oebb_two_step`). Its answer becomes
canonical journeys through the one HAFAS adapter (`canonical_adapters.hafas_adapter`, which makes
Vienna times UTC once), stored in `network_coverage_results.external_journeys`.

Two views of those journeys live here, so that the alignment scorer keeps today's rules (design D5):

- `scoring_legs`: the leg dicts the scorer compares, with the values the check's own parser gave
  before #303: each end's stop TOKEN from `external_verify.oebb_stop_id` (`UIC:…`, `OEBB:…`, or
  `UNKNOWN_ENDPOINT` when ÖBB gave no id; never the lid, CLAUDE.md §3), the coverage mode rule
  (`oebb_mode`: an unknown category is RAIL), the label HAFAS shows as route name, UTC times;
- `itinerary`: the `VerifyItinerary` the cell modal and the export read until PR 4b moves them onto
  canonical journeys (times as Vienna wall clock, as the modal expects).

Two differences from the parser this replaces, both fixes: a time past midnight (`01024500`, 02:45
the next day) was read as 01:02 on the connection's day; and a connection without a date was kept
without times, it is now dropped (the adapter's rule, the same as `/journey`'s).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from ..journey.canonical import CanonicalJourney, CanonicalLeg, CanonicalLocation, CanonicalTiming
from ..journey.canonical_adapters import hafas_adapter
from .external_verify import (
    HafasTripPayload,
    OebbStopId,
    VerifyItinerary,
    VerifyLeg,
    oebb_stop_id,
)

# An ÖBB end with NO id at all. A bare None would reach the fingerprint as the coordinate
# fallback `"?,?"`, exactly what a VIATOR leg with neither id nor position produces: with equal
# mode, minutes and route name the two would hash identically and score a false `agree` on zero
# endpoint evidence. This token is equal to nothing VIATOR emits
# (`test_two_id_less_itineraries_do_not_exact_match`).
UNKNOWN_ENDPOINT = "OEBB:?"

_VIENNA = ZoneInfo(hafas_adapter.REFERENCE_TZ)
_NO_ID = OebbStopId(token=None, raw=None, source=None)


def journeys_from_answer(answer: HafasTripPayload) -> list[CanonicalJourney]:
    """ÖBB's connections as canonical journeys; none when ÖBB found none or could not be asked
    (the verdict says which)."""
    if not answer.verdict.ok or not answer.payload:
        return []
    return hafas_adapter.journeys_from_trip_search(answer.payload)


def oebb_mode(category: str | int | None) -> str:
    """A HAFAS product category ("ICE", "RJ", "S", "Bus") in the mode vocabulary the scorer's
    fingerprint hashes. Kept from the check's own parser, not the adapter's `map_category_to_mode`:
    an unknown category ("CJX") is RAIL here and so still matches an OTP `RAIL` leg (design D5;
    #304 compares mode families instead). HAFAS sometimes sends a number: `str()` first (#213)."""
    cat_upper = str(category or "").upper()
    if not cat_upper:
        return "TRANSIT"
    if "BUS" in cat_upper:
        return "BUS"
    if "TRAM" in cat_upper or cat_upper == "STR":
        return "TRAM"
    if "METRO" in cat_upper or "SUBWAY" in cat_upper or cat_upper in ("U", "U-BAHN"):
        return "SUBWAY"
    if "FERRY" in cat_upper or cat_upper in ("SHIP", "BOAT"):
        return "FERRY"
    return "RAIL"


def scoring_legs(journey: CanonicalJourney) -> list[dict[str, Any]]:
    """The leg dicts `transit_fingerprint` and the fuzzy tier read, walks included (the scorer
    strips them)."""
    return [
        {
            "mode": _mode(leg),
            "from_stop_id": _identity(leg.origin).token or UNKNOWN_ENDPOINT,
            "to_stop_id": _identity(leg.destination).token or UNKNOWN_ENDPOINT,
            "departure": _utc(leg.departure),
            "arrival": _utc(leg.arrival),
            "route_short_name": _route_name(leg),
        }
        for leg in journey.legs
    ]


def itinerary(journey: CanonicalJourney) -> VerifyItinerary:
    """The shape the cell modal's ÖBB column reads (until PR 4b)."""
    return VerifyItinerary(
        legs=[_verify_leg(leg) for leg in journey.legs],
        departure_at=_vienna(journey.departure),
        arrival_at=_vienna(journey.arrival),
        duration_seconds=journey.duration_seconds,
        num_transfers=journey.source_transfer_count,
    )


def stored_itineraries(stored: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """`external_journeys` as read from a result row → the modal's itinerary dicts; None stays
    None (ÖBB not asked yet)."""
    if stored is None:
        return None
    return [
        itinerary(CanonicalJourney.model_validate(item)).model_dump(mode="json") for item in stored
    ]


def _verify_leg(leg: CanonicalLeg) -> VerifyLeg:
    origin = _identity(leg.origin)
    destination = _identity(leg.destination)
    return VerifyLeg(
        mode=_mode(leg),
        from_uic=origin.token,
        to_uic=destination.token,
        dep_utc=_vienna(leg.departure),
        arr_utc=_vienna(leg.arrival),
        route_name=_route_name(leg),
        from_id_raw=origin.raw,
        to_id_raw=destination.raw,
        from_name=leg.origin.name,
        to_name=leg.destination.name,
        from_id_source=origin.source,
        to_id_source=destination.source,
    )


def _mode(leg: CanonicalLeg) -> str:
    """A ride: its category by `oebb_mode`. A walk or transfer section: WALK. Any other section
    (none in ÖBB's answers so far): TRANSIT, which the scorer keeps, as before."""
    if leg.service is not None:
        category = leg.service.product_category
        return oebb_mode(category.code if category is not None else None)
    return "WALK" if leg.source_mode == "WALK" else "TRANSIT"


def _identity(end: CanonicalLocation) -> OebbStopId:
    """The stop token of one end, computed as the check's parser computed it: `oebb_stop_id` on
    ÖBB's `extId` and the lid."""
    stop = end.stop_place
    if stop is None:
        return _NO_ID
    return oebb_stop_id(stop.codes.get("OEBB_EXT"), lid=stop.ref)


def _route_name(leg: CanonicalLeg) -> str | None:
    """The label HAFAS shows ("RJ 1141"), else its line: the train number the fuzzy tier reads."""
    service = leg.service
    if service is None:
        return None
    return service.published_name or service.line


def _utc(value: CanonicalTiming | None) -> str | None:
    return value.timetabled.astimezone(UTC).isoformat() if value is not None else None


def _vienna(value: CanonicalTiming | None) -> str | None:
    """Vienna wall clock to the minute, the form the modal's ÖBB column was written for."""
    if value is None:
        return None
    return value.timetabled.astimezone(_VIENNA).strftime("%Y-%m-%dT%H:%M")
