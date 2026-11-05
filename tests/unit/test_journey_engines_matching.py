"""#304 — `/fanout` pairs OTP's journeys with MOTIS's by the matching engine (`journey_engines`).

The sides of the comparison (one journey per merged slot and engine, `engine_journey_ids`), the
profile (the built-in `journey_engines`: 60 s, the `JOURNEY_MATCH_*` settings left out), the
stations placed for it, what the page receives, and the merge that keeps each session's journey
without sending it. No FastAPI, no Postgres, no station module: stand-ins.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.api import journey as api
from app.journey.canonical import CanonicalJourney, CanonicalStopPlace
from app.journey.canonical.station_resolution import ResolvedStation, StopKey
from tests.unit.matching_journeys import journey, ride, stop

WIEN = stop("Wien Hbf", uic="8101003")
LINZ = stop("Linz Hbf", uic="8100013")


def _in_session(found: CanonicalJourney, sid: str) -> CanonicalJourney:
    return found.model_copy(update={"source": found.source.model_copy(update={"session_id": sid})})


def _otp(journey_id: str, departure: str, arrival: str, **fields: Any) -> CanonicalJourney:
    found = journey(journey_id, ride(WIEN, LINZ, departure, arrival, **fields), provider="otp")
    return _in_session(found, "otp1")


def _motis(journey_id: str, departure: str, arrival: str, **fields: Any) -> CanonicalJourney:
    return _in_session(journey(journey_id, ride(WIEN, LINZ, departure, arrival, **fields)), "m1")


def _slot(**journeys: CanonicalJourney) -> dict[str, Any]:
    """A merged slot found by the sessions named (session id → its journey of the slot)."""
    best = min(journeys.values(), key=lambda found: found.duration_seconds)
    return {"best": best, "found_in_sessions": list(journeys), "_journeys": dict(journeys)}


EXECUTIONS = [
    {"session_id": "otp1", "engine": "otp", "status": "ok"},
    {"session_id": "otp2", "engine": "otp", "status": "ok"},
    {"session_id": "m1", "engine": "motis", "status": "ok"},
]


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def _module_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.station_module, "enabled", lambda: False)


# ── The sides ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "executions",
    [
        [{"session_id": "otp1", "engine": "otp", "status": "ok"}],
        [
            {"session_id": "otp1", "engine": "otp", "status": "ok"},
            {"session_id": "m1", "engine": "motis", "status": "error"},
        ],
    ],
)
def test_no_engine_comparison_without_both_engines(executions: list[dict[str, Any]]) -> None:
    slots = [_slot(otp1=_otp("otp:otp1:0", "08:00", "09:15"))]

    assert api._engine_sides(slots, executions) is None
    assert "engine_journey_ids" not in slots[0]


def test_each_slot_gives_each_engine_its_shortest_journey() -> None:
    fast = _otp("otp:otp2:0", "08:00", "09:10")
    slots = [
        _slot(
            otp1=_otp("otp:otp1:0", "08:00", "09:15"),
            otp2=fast,
            m1=_motis("motis:m1:0", "08:00", "09:15"),
        ),
        _slot(m1=_motis("motis:m1:1", "12:00", "13:15")),
    ]

    sides = api._engine_sides(slots, EXECUTIONS)

    assert sides is not None
    assert [j.id for j in sides["otp"]] == ["otp:otp2:0"]
    assert [j.id for j in sides["motis"]] == ["motis:m1:0", "motis:m1:1"]
    assert slots[0]["engine_journey_ids"] == {"otp": "otp:otp2:0", "motis": "motis:m1:0"}
    assert slots[1]["engine_journey_ids"] == {"motis": "motis:m1:1"}


# ── The comparison ────────────────────────────────────────────────────────────────────────


def _engines_of(*slots: dict[str, Any]) -> tuple[list[dict[str, Any]], Any]:
    merged = list(slots)
    return merged, api._engine_sides(merged, EXECUTIONS)


def test_the_same_train_on_both_engines_is_one_pair() -> None:
    merged, sides = _engines_of(
        _slot(otp1=_otp("otp:otp1:0", "08:00", "09:15", number="540")),
        _slot(m1=_motis("motis:m1:0", "08:00", "09:15", number="540")),
    )

    found = _run(api._compare_journeys(MagicMock(), {}, [], merged, {}, sides))

    assert set(found) == {"engines"}
    assert [(p.left_id, p.right_id) for p in found["engines"].pairs] == [
        ("otp:otp1:0", "motis:m1:0")
    ]
    assert found["engines"].profile == "journey_engines"


def test_the_engines_use_60_s_whatever_the_journey_settings() -> None:
    """Two minutes apart at the start: the references' 120 s would pair them, the same feeds'
    60 s does not, and a wider `JOURNEY_MATCH_*` setting changes nothing here."""
    merged, sides = _engines_of(
        _slot(otp1=_otp("otp:otp1:0", "08:00", "09:15", number="540")),
        _slot(m1=_motis("motis:m1:0", "08:02", "09:15", number="540")),
    )
    cfg = {"JOURNEY_MATCH_DEPARTURE_TOLERANCE_S": 600, "JOURNEY_MATCH_ARRIVAL_TOLERANCE_S": 600}

    found = _run(api._compare_journeys(MagicMock(), cfg, [], merged, {}, sides))

    assert found["engines"].pairs == []
    codes = {item.code for item in found["engines"].left_only[0].reasons}
    assert "departure_outside_tolerance" in codes


def test_engines_and_a_reference_are_compared_in_one_search() -> None:
    otp = _otp("otp:otp1:0", "08:00", "09:15", number="540")
    merged, sides = _engines_of(
        _slot(otp1=otp, m1=_motis("motis:m1:0", "08:00", "09:15", number="540"))
    )
    oebb = journey(
        "hafas:oebb:0", ride(WIEN, LINZ, "08:00", "09:15", number="540"), provider="hafas"
    )

    found = _run(
        api._compare_journeys(
            MagicMock(), {}, [], merged, {"hafas": {"status": "ok", "trips": [oebb]}}, sides
        )
    )

    assert set(found) == {"hafas", "engines"}
    assert len(found["hafas"].pairs) == 1
    assert len(found["engines"].pairs) == 1


# ── The stations ──────────────────────────────────────────────────────────────────────────


class _Placing:
    def __init__(self, by_name: Mapping[str, str]) -> None:
        self._by_name = by_name

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        return {
            (place.source, place.ref): ResolvedStation(station_id=self._by_name[place.name])
            for place in stops
            if place.name in self._by_name
        }


def test_the_module_s_stations_decide_for_the_engines_too(monkeypatch: pytest.MonkeyPatch) -> None:
    stations = {
        "Wien Hbf": "ZZ-00001",
        "Wien Westbahnhof": "ZZ-00002",
        "St. Pölten Hbf": "ZZ-00003",
    }
    otp = _in_session(
        journey(
            "otp:otp1:0",
            ride(stop("Wien Hbf"), stop("St. Pölten Hbf"), "08:00", "08:30"),
            provider="otp",
        ),
        "otp1",
    )
    motis = _in_session(
        journey(
            "motis:m1:0", ride(stop("Wien Westbahnhof"), stop("St. Pölten Hbf"), "08:00", "08:30")
        ),
        "m1",
    )

    async def db_step(fn: Any, *args: Any) -> Any:
        return fn(*args)

    merged, sides = _engines_of(_slot(otp1=otp), _slot(m1=motis))
    without = _run(api._compare_journeys(MagicMock(), {}, [], merged, {}, sides))

    monkeypatch.setattr(api.station_module, "enabled", lambda: True)
    monkeypatch.setattr(api, "db_step", db_step)
    placing = _Placing(stations)
    monkeypatch.setattr(
        api,
        "_station_resolvers",
        lambda db, sessions, cfg: ({"otp1": placing, "m1": placing}, placing),
    )
    merged, sides = _engines_of(_slot(otp1=otp), _slot(m1=motis))
    placed = _run(api._compare_journeys(MagicMock(), {}, [], merged, {}, sides))

    assert len(without["engines"].pairs) == 1
    assert placed["engines"].pairs == []
    # The journeys sent keep no station id.
    assert "ZZ-0000" not in json.dumps([api._slot_json(slot) for slot in merged])


# ── What the page receives ────────────────────────────────────────────────────────────────


def test_the_page_gets_the_engines_by_name() -> None:
    merged, sides = _engines_of(
        _slot(otp1=_otp("otp:otp1:0", "08:00", "09:15", line="S7")),
        _slot(m1=_motis("motis:m1:0", "08:00", "09:15", line="S8")),
    )
    found = _run(api._compare_journeys(MagicMock(), {}, [], merged, {}, sides))

    sent = api._comparison_json(found["engines"], *api._COMPARISON_SIDES["engines"])

    assert (sent["left"], sent["right"]) == ("OTP", "MOTIS")
    assert sent["pairs"][0]["verdict"] == "probably_same"
    assert "line labels differ: S7 / S8" in sent["pairs"][0]["notes"]


def test_a_slot_is_sent_without_its_sessions_journeys() -> None:
    slot = _slot(
        otp1=_otp("otp:otp1:0", "08:00", "09:15"), m1=_motis("motis:m1:0", "08:00", "09:15")
    )
    slot["engine_journey_ids"] = {"otp": "otp:otp1:0", "motis": "motis:m1:0"}

    sent = api._slot_json(slot)

    assert "_journeys" not in sent
    assert sent["engine_journey_ids"] == {"otp": "otp:otp1:0", "motis": "motis:m1:0"}
    assert sent["best"]["id"] in ("otp:otp1:0", "motis:m1:0")
    json.dumps(sent)
    assert "_journeys" in slot  # the slot itself is left as it was


def test_the_merge_keeps_each_session_s_journey(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.journey.signature as signature

    monkeypatch.setattr(signature, "trip_signature", lambda db, session_id, legs: "same")
    otp = _otp("otp:otp1:0", "08:00", "09:15")
    motis = _motis("motis:m1:0", "08:00", "09:10")
    by_signature: dict[str, dict[str, Any]] = {}

    api._merge_by_signature(MagicMock(), by_signature, "otp1", [otp])
    api._merge_by_signature(MagicMock(), by_signature, "m1", [motis])

    (slot,) = by_signature.values()
    assert slot["_journeys"] == {"otp1": otp, "m1": motis}
    assert slot["best"] is motis
