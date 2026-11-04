"""#303 PR 4a — ÖBB's answer in a coverage cell as canonical journeys (`oebb_journeys`).

The check's own HAFAS parser is gone; its answer goes through the HAFAS adapter. These tests pin
what the scorer reads from those journeys: the values the old parser gave (design D5), and the
two fixes the adapter brings (times past midnight, dateless connections). The pages read the
journeys themselves since PR 4b (`app/static/js/canonical_view.js`).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.journey.canonical import CanonicalJourney
from app.journey.canonical_adapters import hafas_adapter
from app.network_coverage import oebb_journeys
from app.network_coverage.external_verify import HafasTripPayload, VerifyResult
from tests.unit import canonical_fixtures as fx


def _leg(**overrides: Any) -> dict[str, Any]:
    leg = {
        "mode": "RAIL",
        "from_uic": "UIC:8103000",
        "to_uic": "UIC:8100002",
        "dep": "2026-06-30T08:00",
        "arr": "2026-06-30T10:30",
        "route": "RJ 1141",
    }
    leg.update(overrides)
    return leg


def _journey(*legs: dict[str, Any]) -> CanonicalJourney:
    (journey,) = fx.oebb_journeys(list(legs))
    return journey


# ─────────────── the scorer's legs: today's values ───────────────


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("UIC:8103000", "UIC:8103000"),  # normalised into the shared namespace
        ("OEBB:904050", "OEBB:904050"),  # a real ÖBB id outside it (Wien Hbf's bus terminal)
        (None, oebb_journeys.UNKNOWN_ENDPOINT),  # no id at all: equal to nothing
    ],
)
def test_each_end_is_the_stop_token_never_the_lid(token: str | None, expected: str) -> None:
    (leg,) = oebb_journeys.scoring_legs(_journey(_leg(from_uic=token)))
    assert leg["from_stop_id"] == expected
    assert "@" not in leg["from_stop_id"]  # a lid would carry the coordinates first


def test_the_lid_alone_is_read_by_field_name_not_scanned() -> None:
    """No `extId`: the token comes from the lid's `L=` field. A scan of the lid would return the
    longitude `X=16375400` (CLAUDE.md §3)."""
    section = {
        "type": "JNY",
        "dep": {"locX": 0, "dTimeS": "080000"},
        "arr": {"locX": 1, "aTimeS": "103000"},
        "jny": {"prodX": 0},
    }
    connection = {"date": "20260630", "dur": "023000", "chg": 0, "secL": [section]}
    connection.update(dep=section["dep"], arr=section["arr"])
    payload = fx._hafas_payload(
        [connection],
        {
            "locL": [
                {"lid": "A=1@O=Wien Hbf@X=16375400@Y=48185900@U=81@L=8103000@"},
                {"lid": "A=1@O=Salzburg Hbf@X=13046700@Y=47812700@U=81@L=8100002@"},
            ],
            "prodL": [{"name": "RJ 1141", "prodCtx": {"catOut": "RJ"}}],
        },
    )
    (journey,) = hafas_adapter.journeys_from_trip_search(payload)
    (leg,) = oebb_journeys.scoring_legs(journey)
    assert (leg["from_stop_id"], leg["to_stop_id"]) == ("UIC:8103000", "UIC:8100002")


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("RJ", "RAIL"),
        ("S", "RAIL"),
        ("CJX", "RAIL"),  # unknown category: RAIL here (the adapter passes it through)
        ("Bus", "BUS"),
        ("STR", "TRAM"),
        ("U", "SUBWAY"),
        ("Ship", "FERRY"),
        (4, "RAIL"),  # HAFAS sometimes sends a number (#213)
        (None, "TRANSIT"),
        ("", "TRANSIT"),
    ],
)
def test_oebb_mode_keeps_the_checks_category_rule(category: Any, expected: str) -> None:
    assert oebb_journeys.oebb_mode(category) == expected


def test_a_ride_takes_its_category_mode_and_a_walk_stays_a_walk() -> None:
    journey = _journey(
        _leg(cat="CJX", to_uic="UIC:8100100", arr="2026-06-30T09:00"),
        _leg(mode="WALK", from_uic="UIC:8100100", to_uic="UIC:8100100", dep="2026-06-30T09:00"),
        _leg(mode="BUS", from_uic="UIC:8100100", dep="2026-06-30T09:10"),
    )
    assert [leg["mode"] for leg in oebb_journeys.scoring_legs(journey)] == ["RAIL", "WALK", "BUS"]


def test_the_route_name_is_the_label_hafas_shows_else_its_line() -> None:
    (shown,) = oebb_journeys.scoring_legs(_journey(_leg(route="RJ 1141", line="RJ")))
    (line_only,) = oebb_journeys.scoring_legs(_journey(_leg(route=None, line="S7")))
    assert shown["route_short_name"] == "RJ 1141"
    assert line_only["route_short_name"] == "S7"


def test_times_are_utc_and_a_time_past_midnight_is_the_next_day() -> None:
    """The old parser read `01024500` (02:45 the next day) as 01:02 on the connection's day."""
    journey = _journey(_leg(dep="2026-06-30T23:50", arr="2026-07-01T02:45"))
    (leg,) = oebb_journeys.scoring_legs(journey)
    assert leg["departure"] == "2026-06-30T21:50:00+00:00"
    assert leg["arrival"] == "2026-07-01T00:45:00+00:00"


def test_a_connection_without_a_date_is_dropped() -> None:
    payload = fx.oebb_payload([_leg()])
    payload["svcResL"][0]["res"]["outConL"][0].pop("date")
    assert hafas_adapter.journeys_from_trip_search(payload) == []


# ─────────────── journeys_from_answer ───────────────


def _answer(*, ok: bool, payload: dict[str, Any] | None) -> HafasTripPayload:
    verdict = VerifyResult(source="fahrplan.oebb.at", ok=ok, num_connections=1 if ok else 0)
    return HafasTripPayload(verdict=verdict, payload=payload)


def test_an_answer_with_connections_gives_its_journeys() -> None:
    journeys = oebb_journeys.journeys_from_answer(
        _answer(ok=True, payload=fx.oebb_payload([_leg()]))
    )
    assert [journey.source.provider for journey in journeys] == ["hafas"]


@pytest.mark.parametrize("payload", [None, {}])
def test_no_payload_gives_no_journey(payload: dict[str, Any] | None) -> None:
    assert oebb_journeys.journeys_from_answer(_answer(ok=True, payload=payload)) == []


def test_a_negative_or_failed_verdict_gives_no_journey() -> None:
    answer = _answer(ok=False, payload=fx.oebb_payload([_leg()]))
    assert oebb_journeys.journeys_from_answer(answer) == []
