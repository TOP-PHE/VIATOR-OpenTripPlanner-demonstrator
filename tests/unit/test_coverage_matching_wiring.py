"""#304 — the coverage runner's wiring of the matching engine.

The `COVERAGE_MATCH_*` settings read from the platform config and frozen with a run's other knobs
(an older snapshot keeps the defaults); the profile they give; a cell's score, tier and detail
written on its row, with ÖBB's journeys kept as ÖBB answered them; the stations of a check's
journeys placed per session, off without the station module, never failing a cell.

No database and no station module: a stand-in session, stand-in resolvers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import config_service, station_module
from app.journey.canonical import CanonicalJourney, CanonicalStopPlace
from app.journey.canonical.station_resolution import ResolvedStation, StopKey
from app.journey.matching import COVERAGE_ALIGNMENT
from app.network_coverage import runner
from tests.unit.matching_journeys import journey, ride


def test_the_default_profile_is_the_built_in_coverage_one() -> None:
    assert runner.match_profile(runner.CoverageConfig()) == COVERAGE_ALIGNMENT


def test_the_frozen_settings_are_laid_over_the_profile() -> None:
    cfg = runner.CoverageConfig(
        match_train_number_conflict="veto",
        match_position_fallback_m=500,
        match_same_service_min_pct=85,
        match_unresolved_endpoints="sometimes",  # not a policy: the built-in one stays
    )

    profile = runner.match_profile(cfg)

    assert profile.train_number_conflict == "veto"
    assert profile.position_fallback_m == 500
    assert profile.same_service_min == 0.85
    assert profile.unresolved_endpoints == "reject"


def test_the_settings_are_read_from_the_platform_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_service,
        "get_all",
        lambda _db, **_kw: {
            "COVERAGE_MATCH_DEPARTURE_TOLERANCE_S": 180,
            "COVERAGE_MATCH_TRAIN_NUMBER_CONFLICT": "penalty",
        },
    )

    cfg = runner._load_coverage_config(MagicMock())

    assert (cfg.match_departure_tolerance_s, cfg.match_train_number_conflict) == (180, "penalty")
    assert cfg.match_arrival_tolerance_s == COVERAGE_ALIGNMENT.arrival_tolerance_s


def test_a_snapshot_from_before_304_keeps_the_defaults() -> None:
    before = {
        k: v for k, v in asdict(runner.CoverageConfig()).items() if not k.startswith("match_")
    }

    cfg = runner._config_from_snapshot(before)

    assert cfg is not None
    assert runner.match_profile(cfg) == COVERAGE_ALIGNMENT
    frozen = runner._config_from_snapshot(
        asdict(runner.CoverageConfig(match_position_fallback_m=0))
    )
    assert frozen is not None
    assert frozen.match_position_fallback_m == 0


# ── A cell's score, tier and detail ───────────────────────────────────────────────────────


def _pair() -> tuple[CanonicalJourney, CanonicalJourney]:
    viator = journey("motis:0", ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540"))
    oebb = journey(
        "hafas:0",
        ride("Wien Hbf", "Linz Hbf", "08:00", "09:15", published="RJ 540"),
        provider="hafas",
    )
    return viator, oebb


def _placed(journey_: CanonicalJourney, ids: tuple[str, str]) -> CanonicalJourney:
    """The journey with its one ride's two stops placed at `ids`."""
    (leg,) = journey_.legs
    ends = []
    for end, vid in ((leg.origin, ids[0]), (leg.destination, ids[1])):
        place = end.stop_place
        assert place is not None
        ends.append(
            end.model_copy(update={"stop_place": place.model_copy(update={"station_id": vid})})
        )
    return journey_.model_copy(
        update={"legs": [leg.model_copy(update={"origin": ends[0], "destination": ends[1]})]}
    )


def test_a_cell_is_scored_on_the_placed_stations_and_keeps_oebb_s_answer() -> None:
    viator, oebb = _pair()
    row = SimpleNamespace()
    ids = ("ZZ-99001", "ZZ-99002")

    runner._persist_alignment_on_row(
        row, [viator], [oebb], resolved=([_placed(viator, ids)], [_placed(oebb, ids)])
    )

    # Placed on both sides, the stations agree: the coverage profile pairs them.
    assert (row.external_alignment_score, row.external_alignment_tier) == (1.0, "agree")
    assert row.external_alignment_detail["counts"]["same_service"] == 1
    assert row.external_alignment_detail["profile"] == "coverage_alignment"
    # ÖBB's journeys are kept as ÖBB answered them: no station id on a stored stop.
    (stored,) = row.external_journeys
    assert stored["legs"][0]["origin"]["stop_place"]["station_id"] is None


def test_without_station_ids_the_coverage_profile_refuses_unknown_stations() -> None:
    viator, oebb = _pair()
    row = SimpleNamespace()

    runner._persist_alignment_on_row(row, [viator], [oebb])

    assert (row.external_alignment_score, row.external_alignment_tier) == (0.0, "no_overlap")
    assert row.external_alignment_detail["reasons"]["endpoint_unresolved"] == 1


# ── The stations of a check ───────────────────────────────────────────────────────────────


class _Resolver:
    """Places every stop it is asked at `vid`, or fails; records the stops of each call."""

    def __init__(self, vid: str, *, fail: bool = False) -> None:
        self.vid = vid
        self.fail = fail
        self.calls: list[list[str]] = []

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        self.calls.append([s.ref for s in stops])
        if self.fail:
            raise RuntimeError("zz")
        return {(s.source, s.ref): ResolvedStation(station_id=self.vid) for s in stops}


def _run(session_id: str | None = "eu19") -> Any:
    return SimpleNamespace(session_id=session_id)


def _of_session(journey_: CanonicalJourney, session_id: str | None) -> CanonicalJourney:
    return journey_.model_copy(
        update={"source": journey_.source.model_copy(update={"session_id": session_id})}
    )


async def test_without_the_station_module_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_module, "enabled", lambda: False)
    db = MagicMock()
    stations = runner._SweepStations(db, _run())
    viator, _ = _pair()

    assert await stations.resolve([viator]) == [viator]
    db.get.assert_not_called()


async def test_each_journey_is_placed_by_its_own_session_s_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    monkeypatch.setattr(
        config_service,
        "get_all",
        lambda _db, **_kw: {"COVERAGE_OEBB_STOP_SERIES": "DELFI_stop_key"},
    )
    resolvers = {"eu19": _Resolver("ZZ-99001"), "nap-fr-rail": _Resolver("ZZ-99002")}
    stations = runner._SweepStations(MagicMock(), _run("eu19"))
    monkeypatch.setattr(stations, "_resolver", lambda sid: resolvers[sid])
    viator, oebb = _pair()
    fanout = _of_session(viator, "nap-fr-rail")

    placed = await stations.resolve([viator, fanout, oebb])

    ids = [j.legs[0].origin.stop_place.station_id for j in placed]  # type: ignore[union-attr]
    # The first and the ÖBB journey carry no session of their own: the run's (eu19).
    assert ids == ["ZZ-99001", "ZZ-99002", "ZZ-99001"]
    assert len(resolvers["eu19"].calls) == 1


async def test_a_failing_resolution_leaves_the_journeys_as_they_were(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    monkeypatch.setattr(config_service, "get_all", lambda _db, **_kw: {})
    stations = runner._SweepStations(MagicMock(), _run())
    monkeypatch.setattr(stations, "_resolver", lambda _sid: _Resolver("ZZ-99001", fail=True))
    viator, _ = _pair()

    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        assert await stations.resolve([viator]) == [viator]

    assert "coverage.stations_unresolved error=RuntimeError" in caplog.text


def test_a_session_unknown_to_the_database_gets_a_resolver_for_oebb_s_stops_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    monkeypatch.setattr(
        config_service, "get_all", lambda _db, **_kw: {"COVERAGE_OEBB_STOP_SERIES": "zz"}
    )
    db = MagicMock()
    db.get.return_value = None
    stations = runner._SweepStations(db, _run("gone"))

    resolver = stations._resolver("gone")

    assert resolver._feeds.motis == {}
    assert resolver._oebb_series == "zz"
    assert stations._resolver("gone") is resolver  # built once
