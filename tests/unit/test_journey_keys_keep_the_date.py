"""The same train on another day is another journey (2026-11-06).

`trip_signature` (the fanout's merge across sessions) and `transit_fingerprint` (a reference
engine's page dedup, the federated planner's dedup) rounded times to HH:MM and dropped the
date: a search answering over several days (Tallinn-Riga, 4 journeys over 8-10 November) had
its days merged into one result showing one of them, and a later day's train on the next page
was dropped as a duplicate. Both keys now keep the date. The fanout's merge also lists a
session once per result, with its shorter journey.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.api.journey import _merge_by_signature
from app.journey.signature import _round_minute, transit_fingerprint, trip_signature
from tests.unit.matching_journeys import journey, ride


def _leg(day: str) -> dict[str, Any]:
    return {
        "mode": "RAIL",
        "from_lat": 59.4399,
        "from_lon": 24.7365,
        "to_lat": 56.9467,
        "to_lon": 24.1213,
        "departure": f"{day}T07:20:00Z",
        "arrival": f"{day}T13:47:00Z",
        "duration_seconds": 23220,
        "route_short_name": "R",
    }


def _db() -> MagicMock:
    """`trip_signature`'s lookups find nothing (no stations_xref, no route alias)."""
    db = MagicMock()
    db.get.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    return db


@pytest.mark.parametrize(
    ("iso", "rounded"),
    [
        ("2026-11-08T10:25:59Z", "2026-11-08T10:25"),
        ("2026-11-08T10:25:00+01:00", "2026-11-08T10:25"),
        ("10:25:00", "10:25"),
        (None, "?"),
        ("", "?"),
    ],
)
def test_a_time_is_rounded_to_the_minute_with_its_date(iso: str | None, rounded: str) -> None:
    assert _round_minute(iso) == rounded


def test_the_fingerprint_tells_two_days_apart() -> None:
    assert transit_fingerprint([_leg("2026-11-08")]) == transit_fingerprint([_leg("2026-11-08")])
    assert transit_fingerprint([_leg("2026-11-08")]) != transit_fingerprint([_leg("2026-11-10")])


def test_the_merge_signature_tells_two_days_apart() -> None:
    def sig(day: str) -> str:
        return trip_signature(_db(), session_id="eu19", legs=[_leg(day)])

    assert sig("2026-11-08") == sig("2026-11-08")
    assert sig("2026-11-08") != sig("2026-11-10")


def test_a_session_is_listed_once_per_result_with_its_shorter_journey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.journey.signature as signature

    monkeypatch.setattr(signature, "trip_signature", lambda db, session_id, legs: "same")
    slower = journey("motis:eu19:0", ride("Tallinn", "Riga", "07:20", "13:50"))
    faster = journey("motis:eu19:1", ride("Tallinn", "Riga", "07:20", "13:47"))
    by_signature: dict[str, dict[str, Any]] = {}

    _merge_by_signature(_db(), by_signature, "eu19", [slower, faster])

    (slot,) = by_signature.values()
    assert slot["found_in_sessions"] == ["eu19"]
    assert slot["_journeys"] == {"eu19": faster}
    assert slot["by_session"]["eu19"]["duration_seconds"] == faster.duration_seconds
    assert slot["best"] is faster
