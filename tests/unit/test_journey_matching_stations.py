"""Station agreement (#304 design §6), and M4's position fallback."""

from __future__ import annotations

import pytest

from app.journey.matching import reasons as r
from app.journey.matching.identifiers import Agreement
from app.journey.matching.stations import (
    StationAgreement,
    compare_stations,
    distance_m,
    position_of,
    with_position_fallback,
)
from tests.unit.matching_journeys import stop

# St Pancras and King's Cross: 247 m apart, two stations (CLAUDE.md §7 item 2 (c)).
ST_PANCRAS = {"lat": 51.53195, "lon": -0.12620}
KINGS_CROSS = {"lat": 51.53078, "lon": -0.12317}


def test_the_module_s_station_decides_first() -> None:
    same = compare_stations(stop("A", station_id="ZZ-99001"), stop("B", station_id="ZZ-99001"))
    other = compare_stations(stop("A", station_id="ZZ-99001"), stop("B", station_id="ZZ-99002"))

    assert same == StationAgreement(Agreement.AGREE)
    assert other.conflict


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            {"station_id": "ZZ-99001", "parent_id": "ZZ-99009"},
            {"station_id": "ZZ-99002", "parent_id": "ZZ-99009"},
            id="two children of one complex",
        ),
        pytest.param(
            {"station_id": "ZZ-99001", "parent_id": "ZZ-99009"},
            {"station_id": "ZZ-99009"},
            id="a child and its parent",
        ),
    ],
)
def test_a_shared_parent_agrees_as_one_complex(left: dict[str, str], right: dict[str, str]) -> None:
    found = compare_stations(stop("Bruxelles-Midi", **left), stop("Midi Eurostar", **right))

    assert found == StationAgreement(Agreement.AGREE, r.SAME_COMPLEX)


def test_the_module_s_ids_win_over_codes() -> None:
    found = compare_stations(
        stop("A", station_id="ZZ-99001", uic="9900001"),
        stop("B", station_id="ZZ-99002", uic="9900001"),
    )

    assert found.conflict


def test_an_end_the_module_has_not_placed_falls_back_to_codes() -> None:
    assert compare_stations(
        stop("A", station_id="ZZ-99001", uic="9900001"), stop("B", uic="9900001")
    ) == StationAgreement(Agreement.AGREE)
    assert compare_stations(stop("A", uic="9900001"), stop("B", uic="9900002")).conflict


def test_positions_in_one_cell_agree_as_location_only_but_not_for_the_endpoint_gate() -> None:
    found = compare_stations(
        stop("A", lat=48.1402, lon=11.5583), stop("B", lat=48.1404, lon=11.5581)
    )

    assert found == StationAgreement(Agreement.AGREE, r.LOCATION_ONLY)
    assert found.for_endpoint_gate() == Agreement.UNKNOWN


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(stop("A", uic="9900001"), stop("B", lat=48.1, lon=11.5), id="code / position"),
        pytest.param(stop("A", uic="9900001"), stop("B", token="OEBB:904050"), id="an ÖBB number"),
        pytest.param(stop("A"), stop("B"), id="nothing"),
        pytest.param(
            stop("A", lat=48.140, lon=11.558), stop("B", lat=48.150, lon=11.558), id="two cells"
        ),
    ],
)
def test_anything_else_is_unknown(left: object, right: object) -> None:
    assert compare_stations(left, right).agreement == Agreement.UNKNOWN  # type: ignore[arg-type]


def test_the_position_fallback_agrees_within_its_distance_only() -> None:
    near = (
        stop("Hauptbahnhof", lat=48.7840, lon=9.1820),
        stop("Stuttgart Hbf", lat=48.7867, lon=9.1820),
    )
    far = (
        stop("Hauptbahnhof", lat=48.7840, lon=9.1820),
        stop("Stuttgart Hbf", lat=48.7975, lon=9.1820),
    )
    unknown = StationAgreement(Agreement.UNKNOWN)

    assert 290 < distance_m(position_of(near[0]), position_of(near[1])) < 310  # type: ignore[arg-type]
    assert with_position_fallback(unknown, *near, 1000) == StationAgreement(
        Agreement.AGREE, r.STATIONS_BY_POSITION
    )
    assert with_position_fallback(unknown, *far, 1000) == unknown
    # 0 turns it off.
    assert with_position_fallback(unknown, *near, 0) == unknown


def test_the_fallback_never_turns_a_conflict_into_an_agreement() -> None:
    pancras = stop("St Pancras", uic="9900101", **ST_PANCRAS)
    kings = stop("King's Cross", uic="9900102", **KINGS_CROSS)
    found = compare_stations(pancras, kings)

    assert 240 < distance_m(position_of(pancras), position_of(kings)) < 255  # type: ignore[arg-type]
    assert found.conflict
    assert with_position_fallback(found, pancras, kings, 5000).conflict
