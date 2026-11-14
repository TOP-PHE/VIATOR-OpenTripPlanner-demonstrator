"""ÖBB's stop number is its own (an EVA number), a UIC code only where ÖBB says so.

HAFAS gives every stop an `extId`. Outside Germany, Switzerland and a few other countries it is not
the station's UIC code, and VIATOR's French and Italian feeds carry the true one. Read as a UIC, the
two numbers made the matching engine see two different stations, and the same train at the same
minute was vetoed ("different first or last station"): coverage run f7165bd7 scored 1 % agreement
on the cells touching France. The forms below were measured on ÖBB's TripSearch on 2026-11-12.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.journey.canonical import CanonicalJourney, station_key
from app.journey.canonical_adapters import hafas_adapter
from app.journey.matching import COVERAGE_ALIGNMENT, compare
from app.journey.matching import reasons as r
from tests.unit import canonical_fixtures as fx
from tests.unit.matching_journeys import journey, ride, stop

U = "U\u00d7"  # the separator HAFAS writes in a `gidL` entry: U, MULTIPLICATION SIGN


def _location(name: str, ext_id: str | None, gid_l: Any, x: int, y: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "lid": f"A=1@O={name}@X={x}@Y={y}@U=80@L={ext_id or ''}@",
        "crd": {"x": x, "y": y},
    }
    if ext_id is not None:
        entry["extId"] = ext_id
    if gid_l is not None:
        entry["gidL"] = gid_l
    return entry


def _one_ride(
    origin: dict[str, Any], destination: dict[str, Any], *, product: dict[str, Any] | None = None
) -> CanonicalJourney:
    """One ÖBB journey of one ride, 9 Nov 2026 08:46 → 10:40 Vienna time (CET), through the
    real adapter."""
    section = {
        "type": "JNY",
        "dep": {"locX": 0, "dTimeS": "084600"},
        "arr": {"locX": 1, "aTimeS": "104000"},
        "jny": {"prodX": 0},
    }
    connection = {"date": "20261109", "dur": "015400", "chg": 0, "secL": [section]}
    connection.update(dep=section["dep"], arr=section["arr"])
    common = {
        "locL": [origin, destination],
        "prodL": [
            product
            or {"name": "FR 6645", "cls": 1, "prodCtx": {"catOut": "FR      ", "num": "6645"}}
        ],
    }
    (oebb,) = hafas_adapter.journeys_from_trip_search(fx._hafas_payload([connection], common))
    return oebb


ZURICH = _location("Zürich HB", "8503000", [f"{U}8503000", "B\u00d785Zue"], 8_540_192, 47_378_177)
BASEL = _location("Basel SBB", "008500010", [f"{U}8500010", "B\u00d785Bs"], 7_589_563, 47_547_408)
PARIS_GDL = _location("Paris Gare de Lyon", "8700012", None, 2_373_481, 48_844_945)
LYON_PD = _location("Lyon Part Dieu", "8700152", [f"{U}8772219"], 4_859_435, 45_760_585)
MILANO = _location("Milano Centrale", "8300046", [f"{U}8301700"], 9_204_828, 45_486_347)
MULHOUSE = _location(
    "Mulhouse Ville", "8700031", [f"{U}8718200", "B\u00d787MSE"], 7_342_423, 47_741_853
)


@pytest.mark.parametrize(
    ("loc", "uic"),
    [
        pytest.param(ZURICH, "8503000", id="the number ÖBB states as the UIC: kept"),
        pytest.param(BASEL, "8500010", id="zero padding absorbed, then compared"),
        pytest.param(PARIS_GDL, None, id="no UIC stated (Paris Gare de Lyon)"),
        pytest.param(LYON_PD, None, id="a different UIC stated (Lyon Part-Dieu)"),
        pytest.param(MILANO, None, id="a different UIC stated (Milano Centrale)"),
        pytest.param(MULHOUSE, None, id="a different UIC stated (Mulhouse Ville)"),
    ],
)
def test_a_stop_is_given_a_uic_only_when_oebb_states_its_own_number_as_one(
    loc: dict[str, Any], uic: str | None
) -> None:
    oebb = _one_ride(loc, ZURICH if loc is not ZURICH else BASEL)
    place = oebb.legs[0].origin.stop_place

    assert place is not None
    assert place.uic == uic
    # The number stays where the station module looks for it, and never becomes a token whose
    # 7 digits could be read back as a UIC downstream (`_oebb_opaque_payload`'s invariant).
    assert place.codes == {"OEBB_EXT": loc["extId"]}
    assert place.unresolved_token is None
    expected_key = (
        f"UIC:{uic}" if uic else f"GEO:{loc['crd']['y'] / 1e6:.3f},{loc['crd']['x'] / 1e6:.3f}"
    )
    assert station_key(oebb.legs[0].origin) == expected_key


@pytest.mark.parametrize(
    "gid_l",
    [None, "U\u00d78503000", [], ["B\u00d785Zue"], [8503000], [f"{U}850300"], ["X\u00d78503000"]],
)
def test_nothing_but_a_well_formed_u_entry_states_a_uic(gid_l: Any) -> None:
    assert hafas_adapter.stated_uic(gid_l) is None


def test_an_eight_digit_stated_uic_drops_its_check_digit_like_the_number() -> None:
    assert hafas_adapter.stated_uic([f"{U}87686006"]) == "8768600"


def test_a_number_from_the_lid_alone_is_kept_for_the_station_module() -> None:
    no_ext = _location("Somewhere", None, None, 2_373_481, 48_844_945)
    no_ext["lid"] = "A=1@O=Somewhere@X=2373481@Y=48844945@U=80@L=8700012@"

    place = _one_ride(no_ext, ZURICH).legs[0].origin.stop_place

    assert place is not None
    assert (place.uic, place.codes) == (None, {"OEBB_EXT": "8700012"})


def test_the_same_train_paris_lyon_now_pairs_instead_of_being_vetoed() -> None:
    """Run f7165bd7, cell Paris Gare de Lyon → Lyon Part-Dieu: VIATOR's Trenitalia France feed
    and ÖBB both give FR 6645, 08:46 → 10:40. VIATOR's stops carry SNCF's UIC codes; ÖBB's carry
    8700012 and 8700152. Before, the engine compared 8768600 with 8700012 as two UICs and vetoed."""
    oebb = _one_ride(PARIS_GDL, LYON_PD)
    viator = journey(
        "viator",
        ride(
            stop("Paris-Gare-de-Lyon", uic="8768600", lat=48.844945, lon=2.373481),
            stop("Lyon-Part-Dieu", uic="8772319", lat=45.760585, lon=4.859435),
            "07:46",
            "09:40",
            line="0083_FR",
            operator="TRENITALIA",
        ),
    )

    result = compare([viator], [oebb], COVERAGE_ALIGNMENT)

    assert len(result.pairs) == 1, result.reason_counts()
    assert result.pairs[0].verdict in {"same_service", "probably_same"}
    assert r.ENDPOINT_STATION_DIFFERS not in result.reason_counts()
    assert r.STATIONS_BY_POSITION in result.reason_counts()


@pytest.mark.parametrize(
    ("cat_out", "cls", "mode"),
    [
        # measured 2026-11-12 (catOut padded with spaces as ÖBB sends it)
        pytest.param("FR      ", 1, "RAIL", id="Frecciarossa"),
        pytest.param("OGV     ", 1, "RAIL", id="OUIGO Grande Vitesse"),
        pytest.param("IRY     ", 1, "RAIL", id="iryo"),
        pytest.param("RB      ", 16, "RAIL", id="Regionalbahn"),
        pytest.param("RER     ", 16, "RAIL", id="RER"),
        pytest.param("DB", 32, "RAIL", id="S-Bahn Muenchen filed as DB"),
        pytest.param("TGV     ", 1, "RAIL", id="TGV, known by name"),
        pytest.param("Bus     ", 2, "BUS", id="rail replacement bus keeps its category"),
        pytest.param("FR", "16", "RAIL", id="a class sent as digits"),
        pytest.param("XYZ", 64, "OTHER", id="an unknown category outside the train classes"),
        pytest.param("XYZ", None, "OTHER", id="an unknown category without a class"),
        pytest.param("XYZ", "n/a", "OTHER", id="a class that is not a number"),
    ],
)
def test_a_train_class_makes_an_unknown_category_rail(cat_out: str, cls: Any, mode: str) -> None:
    product: dict[str, Any] = {
        "name": f"{cat_out.strip()} 1",
        "prodCtx": {"catOut": cat_out, "num": "1"},
    }
    if cls is not None:
        product["cls"] = cls

    oebb = _one_ride(BASEL, ZURICH, product=product)

    assert oebb.legs[0].service is not None
    assert oebb.legs[0].service.mode == mode


def test_two_stops_oebb_states_as_uics_still_compare_by_code() -> None:
    """Where ÖBB's number is the UIC (Basel SBB, Zürich HB), the code still decides: a VIATOR
    stop with another UIC is another station, however close."""
    oebb = _one_ride(
        BASEL, ZURICH, product={"name": "IC 61", "prodCtx": {"catOut": "IC", "num": "61"}}
    )
    other_station = journey(
        "viator",
        ride(
            stop("Basel Bad Bf", uic="8000026", lat=47.547408, lon=7.589563),
            stop("Zürich HB", uic="8503000", lat=47.378177, lon=8.540192),
            "07:46",
            "09:40",
            number="61",
        ),
    )

    result = compare([other_station], [oebb], COVERAGE_ALIGNMENT)

    assert result.pairs == []
    assert r.ENDPOINT_STATION_DIFFERS in result.reason_counts()
