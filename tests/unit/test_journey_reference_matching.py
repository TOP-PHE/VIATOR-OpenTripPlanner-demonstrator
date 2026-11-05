"""#304, step 2 — `/fanout` compares VIATOR's journeys with each reference's by the matching engine.

The comparisons of `app.api.journey` (profile `journey_reference`, the `JOURNEY_MATCH_*`
settings), the stations placed for them only, what the page receives (`comparisons`), OJP's tags
in their old shapes, and the settings on the config page. No FastAPI, no Postgres, no station
module: stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.api import journey as api
from app.config_schema import CONFIG_SCHEMA
from app.journey.canonical import CanonicalJourney, CanonicalStopPlace
from app.journey.canonical.station_resolution import ResolvedStation, StopKey
from app.journey.matching import JOURNEY_REFERENCE, compare, profile_from_config
from app.journey.matching import reasons as r
from tests.unit.matching_journeys import journey, ride, stop, walk

CONFIG_PAGE = Path(__file__).parents[2] / "app" / "templates" / "admin" / "config.html"

WIEN = stop("Wien Hbf", uic="8101003")
LINZ = stop("Linz Hbf", uic="8100013")


def _viator(journey_id: str, departure: str, arrival: str, **fields: Any) -> CanonicalJourney:
    found = journey(journey_id, ride(WIEN, LINZ, departure, arrival, **fields))
    return found.model_copy(update={"source": found.source.model_copy(update={"session_id": "s1"})})


def _oebb(journey_id: str, departure: str, arrival: str, **fields: Any) -> CanonicalJourney:
    return journey(journey_id, ride(WIEN, LINZ, departure, arrival, **fields), provider="hafas")


def _slots(*journeys: CanonicalJourney) -> list[dict[str, Any]]:
    return [{"best": found} for found in journeys]


def _answer(*journeys: CanonicalJourney) -> dict[str, Any]:
    return {"status": "ok", "trips": list(journeys)}


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def _module_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The station module is off unless a test says otherwise."""
    monkeypatch.setattr(api.station_module, "enabled", lambda: False)


# ── The comparisons ───────────────────────────────────────────────────────────────────────


def test_only_references_that_answered_are_compared() -> None:
    slots = _slots(_viator("motis:s1:0", "08:00", "09:15"))
    refs: dict[str, dict[str, Any] | None] = {
        "ojp": None,
        "hafas": {"status": "error", "trips": []},
    }

    assert _run(api._compare_with_references(MagicMock(), {}, [], slots, refs)) == {}


def test_each_reference_gets_its_own_comparison() -> None:
    slots = _slots(_viator("motis:s1:0", "08:00", "09:15", number="540"))
    oebb = _answer(_oebb("hafas:oebb:0", "08:00", "09:15", number="540"))
    ojp = _answer(journey("ojp:ch:0", ride(WIEN, LINZ, "10:00", "11:15"), provider="ojp"))

    found = _run(
        api._compare_with_references(MagicMock(), {}, [], slots, {"ojp": ojp, "hafas": oebb})
    )

    assert set(found) == {"ojp", "hafas"}
    assert [(p.left_id, p.right_id) for p in found["hafas"].pairs] == [
        ("motis:s1:0", "hafas:oebb:0")
    ]
    assert found["hafas"].profile == "journey_reference"
    assert found["ojp"].pairs == []
    assert [item.journey_id for item in found["ojp"].right_only] == ["ojp:ch:0"]


def test_the_journey_match_settings_are_read() -> None:
    """Eight minutes apart: different under the default 120 s, paired under 600 s."""
    slots = _slots(_viator("motis:s1:0", "08:00", "09:15", number="540"))
    oebb = _answer(_oebb("hafas:oebb:0", "08:08", "09:23", number="540"))
    refs: dict[str, dict[str, Any] | None] = {"hafas": oebb}

    default = _run(api._compare_with_references(MagicMock(), {}, [], slots, refs))
    wider = _run(
        api._compare_with_references(
            MagicMock(),
            {
                "JOURNEY_MATCH_DEPARTURE_TOLERANCE_S": 600,
                "JOURNEY_MATCH_ARRIVAL_TOLERANCE_S": 600,
                "JOURNEY_MATCH_PROBABLE_MIN_PCT": 10,
            },
            [],
            slots,
            refs,
        )
    )

    assert default["hafas"].pairs == []
    assert len(wider["hafas"].pairs) == 1


# ── The stations: placed for the comparison only ──────────────────────────────────────────

WEST = stop("Wien Westbahnhof")
HBF = stop("Wien Hbf")
ST_POELTEN = stop("St. Pölten Hbf")


class _Placing:
    """A resolver placing stops by name; `wait` seconds before answering."""

    def __init__(self, by_name: Mapping[str, str], wait: float = 0.0) -> None:
        self._by_name = by_name
        self._wait = wait
        self.asked: list[str] = []

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        if self._wait:
            await asyncio.sleep(self._wait)
        self.asked += [place.name or "" for place in stops]
        return {
            (place.source, place.ref): ResolvedStation(station_id=self._by_name[place.name])
            for place in stops
            if place.name in self._by_name
        }


STATIONS = {"Wien Hbf": "ZZ-00001", "Wien Westbahnhof": "ZZ-00002", "St. Pölten Hbf": "ZZ-00003"}


def _codeless() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any] | None]]:
    """VIATOR from Wien Hbf, ÖBB from Wien Westbahnhof, same minutes, no codes: the same
    service unless the stations say otherwise."""
    viator = journey("motis:s1:0", ride(HBF, ST_POELTEN, "08:00", "08:30", number="540"))
    viator = viator.model_copy(
        update={"source": viator.source.model_copy(update={"session_id": "s1"})}
    )
    oebb = journey(
        "hafas:oebb:0", ride(WEST, ST_POELTEN, "08:00", "08:30", number="540"), provider="hafas"
    )
    return _slots(viator), {"hafas": _answer(oebb)}


def _module_on(
    monkeypatch: pytest.MonkeyPatch, viator: _Placing, oebb: _Placing
) -> list[tuple[Any, ...]]:
    steps: list[tuple[Any, ...]] = []

    async def db_step(fn: Any, *args: Any) -> Any:
        steps.append((fn, *args))
        return fn(*args)

    monkeypatch.setattr(api.station_module, "enabled", lambda: True)
    monkeypatch.setattr(api, "db_step", db_step)
    monkeypatch.setattr(api, "_station_resolvers", lambda db, sessions, cfg: ({"s1": viator}, oebb))
    return steps


def test_without_the_module_the_codeless_journeys_pair() -> None:
    slots, refs = _codeless()

    found = _run(api._compare_with_references(MagicMock(), {}, [], slots, refs))

    assert len(found["hafas"].pairs) == 1


def test_the_module_s_stations_decide_and_are_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    viator, oebb = _Placing(STATIONS), _Placing(STATIONS)
    steps = _module_on(monkeypatch, viator, oebb)
    slots, refs = _codeless()

    found = _run(api._compare_with_references(MagicMock(), {}, [], slots, refs))

    assert found["hafas"].pairs == []
    codes = {item.code for item in found["hafas"].left_only[0].reasons}
    assert r.ENDPOINT_STATION_DIFFERS in codes
    assert "Wien Hbf" in viator.asked
    assert "Wien Westbahnhof" in oebb.asked
    assert len(steps) == 1  # one database step: the resolvers
    # The journeys sent to the page keep no station id (decision 68).
    sent = json.dumps([api._journey_json(slot["best"]) for slot in slots])
    sent += json.dumps(api._reference_json(refs["hafas"] or {}))
    assert "ZZ-0000" not in sent


def test_ojp_s_stops_are_never_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    viator, oebb = _Placing(STATIONS), _Placing(STATIONS)
    _module_on(monkeypatch, viator, oebb)
    slots, _ = _codeless()
    ojp = _answer(journey("ojp:ch:0", ride(WEST, ST_POELTEN, "08:00", "08:30"), provider="ojp"))

    _run(api._compare_with_references(MagicMock(), {}, [], slots, {"ojp": ojp}))

    assert oebb.asked == []


def test_a_slow_module_is_given_up_and_the_codes_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    viator, oebb = _Placing(STATIONS, wait=5.0), _Placing(STATIONS)
    _module_on(monkeypatch, viator, oebb)
    monkeypatch.setattr(api, "_STATION_BUDGET_S", 0.05)
    slots, refs = _codeless()

    found = _run(api._compare_with_references(MagicMock(), {}, [], slots, refs))

    assert len(found["hafas"].pairs) == 1


def test_a_failed_resolver_step_is_given_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(fn: Any, *args: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(api.station_module, "enabled", lambda: True)
    monkeypatch.setattr(api, "db_step", failing)
    slots, refs = _codeless()

    found = _run(api._compare_with_references(MagicMock(), {}, [], slots, refs))

    assert len(found["hafas"].pairs) == 1


def test_each_session_s_resolver_is_built_from_its_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[Any, ...]] = []

    def resolver_for_session(config: Any, uploads: Any, series: Any) -> str:
        built.append((config, uploads, series))
        return f"resolver of {config.get('name')}"

    # The upload sender's module needs `fcntl`: a stand-in, on every platform.
    monkeypatch.setitem(
        sys.modules,
        "app.station_extract_upload",
        SimpleNamespace(latest_upload_sha256=lambda db, sid: {"p": f"sha of {sid}"}),
    )
    monkeypatch.setattr(api.station_resolver, "resolver_for_session", resolver_for_session)
    sessions = [
        SimpleNamespace(id="s1", config={"name": "one"}),
        SimpleNamespace(id="s2", config=None),
    ]

    resolvers, oebb = api._station_resolvers(
        MagicMock(),
        sessions,
        {"COVERAGE_OEBB_STOP_SERIES": "DELFI_stop_key"},  # type: ignore[arg-type]
    )

    assert resolvers == {"s1": "resolver of one", "s2": "resolver of None"}
    assert built[0] == ({"name": "one"}, {"p": "sha of s1"}, "DELFI_stop_key")
    assert built[1][0] == {}
    assert isinstance(oebb, api.station_resolver.ModuleStationResolver)


# ── What the page receives ────────────────────────────────────────────────────────────────


def test_the_page_gets_pairs_unpaired_journeys_and_sentences() -> None:
    comparison = compare(
        [
            _viator("motis:s1:0", "08:00", "09:15", number="540", line="RJX"),
            _viator("motis:s1:1", "12:00", "13:15", number="548"),
        ],
        [
            _oebb("hafas:oebb:0", "08:00", "09:15", number="540", line="RJ"),
            _oebb("hafas:oebb:1", "12:09", "13:24", number="548"),
        ],
        JOURNEY_REFERENCE,
    )

    sent = api._comparison_json(comparison, "ÖBB")

    assert (sent["left"], sent["right"], sent["profile"]) == ("VIATOR", "ÖBB", "journey_reference")
    assert [(p["left_id"], p["right_id"]) for p in sent["pairs"]] == [
        ("motis:s1:0", "hafas:oebb:0")
    ]
    pair = sent["pairs"][0]
    assert pair["verdict"] in ("same_service", "probably_same")
    assert all(isinstance(note, str) and note for note in pair["notes"])
    assert [item["journey_id"] for item in sent["left_only"]] == ["motis:s1:1"]
    assert [item["journey_id"] for item in sent["right_only"]] == ["hafas:oebb:1"]
    assert "departures 9 min apart" in sent["left_only"][0]["notes"]
    assert sent["summary"]["left_only"] == 1
    json.dumps(sent)  # plain JSON


def test_a_journey_with_nothing_opposite_has_no_note() -> None:
    comparison = compare([_viator("motis:s1:0", "08:00", "09:15")], [], JOURNEY_REFERENCE)

    sent = api._comparison_json(comparison, "ÖBB")

    assert sent["left_only"][0]["reasons"] == [r.NO_CANDIDATE]
    assert sent["left_only"][0]["notes"] == []


def test_hidden_reasons_are_codes_only_and_sentences_appear_once() -> None:
    same = r.reason(r.LINE_LABEL_DIFFERS, "warning", left_value="RJX", right_value="RJ")
    hidden = r.reason(r.LABEL_SPACING_ONLY, "info")

    assert api._notes([same, hidden, same], "ÖBB") == ["line labels differ: RJX / RJ"]


def test_no_station_id_reaches_the_page() -> None:
    placed = stop("Wien Hbf", uic="8101003", station_id="ZZ-00001")
    comparison = compare(
        [journey("motis:s1:0", ride(placed, LINZ, "08:00", "09:15"))],
        [journey("hafas:oebb:0", ride(placed, LINZ, "08:20", "09:35"), provider="hafas")],
        JOURNEY_REFERENCE,
    )

    assert "ZZ-00001" not in json.dumps(api._comparison_json(comparison, "ÖBB"))


# ── OJP's tags, in their shapes ───────────────────────────────────────────────────────────


def test_ojp_s_tags_and_counts_come_from_the_engine() -> None:
    slots = _slots(
        _viator("motis:s1:0", "08:00", "09:15", number="540"),
        _viator("motis:s1:1", "12:00", "13:15", number="548"),
        journey("motis:s1:2", walk("Wien Hbf", "Wien Mitte", "08:00", "08:30")),
    )
    ojp_journeys = [
        journey("ojp:ch:0", ride(WIEN, LINZ, "08:00", "09:15", number="540"), provider="ojp"),
        journey("ojp:ch:1", ride(WIEN, LINZ, "15:00", "16:15", number="562"), provider="ojp"),
    ]
    reference: dict[str, Any] = _answer(*ojp_journeys)
    comparison = compare([slot["best"] for slot in slots], ojp_journeys, JOURNEY_REFERENCE)

    summary = api._tag_ojp_comparison(slots, reference, comparison)

    assert [slot["comparison"] for slot in slots] == ["common", "otp_only", "uncomparable"]
    assert reference["comparison"] == {"ojp:ch:0": "common", "ojp:ch:1": "ojp_only"}
    assert summary == {"common": 1, "otp_only": 1, "ojp_only": 1}


def test_no_ojp_comparison_no_tags() -> None:
    slots = _slots(_viator("motis:s1:0", "08:00", "09:15"))

    assert api._tag_ojp_comparison(slots, None, None) is None
    assert "comparison" not in slots[0]


# ── The settings ──────────────────────────────────────────────────────────────────────────

MATCH_KEYS = [key for key in CONFIG_SCHEMA if key.startswith("JOURNEY_MATCH_")]


def test_the_eight_settings_default_to_the_built_in_profile() -> None:
    assert len(MATCH_KEYS) == 8
    defaults = {key: CONFIG_SCHEMA[key]["default"] for key in MATCH_KEYS}

    assert profile_from_config(defaults, "journey_reference") == JOURNEY_REFERENCE


@pytest.mark.parametrize("key", MATCH_KEYS)
def test_each_setting_is_on_the_config_page(key: str) -> None:
    page = CONFIG_PAGE.read_text(encoding="utf-8")

    assert f"'{key}'" in page
    choices = CONFIG_SCHEMA[key].get("choices")
    if choices:
        listed = f"{key}: [" + ",".join(f"'{choice}'" for choice in choices) + "]"
        assert listed in page
