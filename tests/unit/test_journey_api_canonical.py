"""#303 — `/fanout` and `/plan` answer in canonical journeys (AC3, D3).

Pure helpers of `app.api.journey`, no FastAPI / Postgres: the end-to-end fanout runs in the
integration tests (`test_web_loop_health`, `test_federated_fanout`).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from app.api.journey import (
    _build_comparison,
    _instant_json,
    _journey_json,
    _merge_by_signature,
    _reference_json,
)
from app.journey.canonical import CanonicalJourney
from app.journey.canonical_adapters import hafas_adapter, motis_adapter, ojp_adapter
from app.journey.trip_normalize import renumbered
from tests.unit import canonical_fixtures as fx

# Keys of today's trip and leg dicts, and of the engines' own payloads: none may reach the page.
_PROVIDER_KEYS = (
    "_raw_itinerary",
    "route_short_name",
    "first_transit_leg_departure_utc",
    "feed_id",
    "from_stop_id",
    "gtfsId",
    "secL",
    "TripResult",
)


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for value in node for k in _keys(value)}
    return set()


def _fake_db() -> MagicMock:
    """`trip_signature`'s lookups find nothing (no stations_xref, no route alias)."""
    db = MagicMock()
    db.get.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def test_a_journey_is_sent_as_its_canonical_json_only() -> None:
    for journey in motis_adapter.journeys_from_plan(fx.MOTIS_PLAN, session_id="eu19"):
        sent = _journey_json(journey)
        assert sent["schema_version"] == "1"
        assert not (_keys(sent) & set(_PROVIDER_KEYS))
        assert CanonicalJourney.model_validate_json(json.dumps(sent)) == journey


def test_instants_are_written_like_the_journey_json() -> None:
    journey = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)[0]
    assert _instant_json(journey.departure) == "2026-06-28T06:00:00Z"
    assert _instant_json(None) is None


def test_merge_by_signature_keeps_the_shortest_journey_of_a_slot() -> None:
    db = _fake_db()
    by_signature: dict[str, dict[str, Any]] = {}
    s7 = motis_adapter.journeys_from_plan(fx.MOTIS_PLAN, session_id="eu19")[0]
    slower = s7.model_copy(update={"duration_seconds": s7.duration_seconds + 60})
    _merge_by_signature(db, by_signature, "eu19-b", [slower])
    _merge_by_signature(db, by_signature, "eu19", [s7])
    (slot,) = by_signature.values()
    assert slot["found_in_sessions"] == ["eu19-b", "eu19"]
    assert slot["best"] is s7
    assert slot["by_session"]["eu19"] == {
        "duration_seconds": 3660,
        "departure_at": "2026-11-03T08:10:00Z",
        "arrival_at": "2026-11-03T09:11:00Z",
    }


def test_a_reference_answer_keeps_its_fields_and_sends_its_journeys_as_json() -> None:
    journeys = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)
    sent = _reference_json({"status": "ok", "trips": journeys, "pages": 2})
    assert sent["status"] == "ok"
    assert sent["pages"] == 2
    assert [trip["id"] for trip in sent["trips"]] == [j.id for j in journeys]
    assert not (_keys(sent) & set(_PROVIDER_KEYS))


def test_the_ojp_comparison_tags_reference_journeys_by_id() -> None:
    ojp = ojp_adapter.journeys_from_trip_delivery(fx.OJP_TRIP_DELIVERY)
    merged = [{"best": ojp[0].model_copy(update={"id": "otp:ch:0"})}]
    reference: dict[str, Any] = {"status": "ok", "trips": ojp}
    summary = _build_comparison(merged, reference)
    assert merged[0]["comparison"] == "common"
    assert reference["comparison"][ojp[0].id] == "common"
    assert summary is not None
    assert summary["common"] == 1


def test_pages_of_a_reference_get_ids_unique_in_their_union() -> None:
    page = hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH)
    union = renumbered([*page, *page])
    assert [j.id for j in union] == [f"hafas:oebb:{i}" for i in range(len(union))]
    assert union[3].legs == page[0].legs
