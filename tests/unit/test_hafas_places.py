"""The places of ÖBB's answers, as the coverage runs store them, for ÖBB's stop list (the station
module's design 22.20, decision 70 way a; `app/network_coverage/hafas_places.py`).

ÖBB's answers are built as HAFAS sends them and go through the real adapter, then into the JSON
the coverage check stores (`external_journeys`), which is what the collection reads. Every value
is invented: `ZZ` names, numbers with 99, positions in the sea of numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.journey.canonical_adapters import hafas_adapter
from app.network_coverage import hafas_places
from app.network_coverage.hafas_places import SeenPlace, add_journeys, station_end, train_leg
from tests.unit import canonical_fixtures as fx

U = "U\u00d7"  # the separator HAFAS writes in a `gidL` entry: U, MULTIPLICATION SIGN
AT = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
LATER = AT + timedelta(days=1)

RAIL = {"name": "ZZ 99", "cls": 4, "prodCtx": {"catOut": "RJ", "num": "99"}, "oprX": 0}
NEW_CATEGORY = {"name": "ZY 99", "cls": 1, "prodCtx": {"catOut": "ZY"}, "oprX": 1}
BUS = {"name": "Bus 99", "cls": 64, "prodCtx": {"catOut": "Bus"}, "oprX": 0}
OPERATORS = [{"name": "ZZ Bahn"}, {"name": "ZZ Fern"}]


def place(
    name: str,
    ext_id: str | None,
    *,
    kind: int = 1,
    stated: str | None = None,
    x: int = 9_990_000,
    y: int = 49_990_000,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "lid": f"A={kind}@O={name}@X={x}@Y={y}@U=99@L={ext_id or ''}@",
        "crd": {"x": x, "y": y},
    }
    if ext_id is not None:
        entry["extId"] = ext_id
    if stated is not None:
        entry["gidL"] = [f"{U}{stated}", "B\u00d799ZZ"]
    return entry


NORD = place("ZZ Nord", "9900001", stated="9900001", x=9_990_001, y=49_990_001)
SUD = place("ZZ Sued", "9900002", stated="9911112", x=9_990_002, y=49_990_002)
OST = place("ZZ Ost", "9900004", x=9_990_004, y=49_990_004)
BUS_STOP = place("ZZ Nord (Bus)", "9900003", x=9_990_003, y=49_990_003)
ADDRESS = place("ZZ Strasse 9", "9900005", kind=2)


def stored(rides: list[tuple[int, int, int]], locations: list[dict[str, Any]]) -> list[Any]:
    """ÖBB's answer of one connection a ride (origin, destination, product), through the real
    adapter, in the JSON coverage stores."""
    connections = []
    for origin, destination, product in rides:
        section = {
            "type": "JNY",
            "dep": {"locX": origin, "dTimeS": "090900"},
            "arr": {"locX": destination, "aTimeS": "103900"},
            "jny": {"prodX": product},
        }
        connection = {"date": "20990909", "dur": "013000", "chg": 0, "secL": [section]}
        connection.update(dep=section["dep"], arr=section["arr"])
        connections.append(connection)
    common = {"locL": locations, "prodL": [RAIL, NEW_CATEGORY, BUS], "opL": OPERATORS}
    payload = fx._hafas_payload(connections, common)
    journeys = hafas_adapter.journeys_from_trip_search(payload)
    return [journey.model_dump(mode="json") for journey in journeys]


def test_the_adapter_keeps_the_uic_oebb_states_even_when_it_is_not_the_number() -> None:
    (journey,) = stored([(0, 1, 0)], [NORD, SUD])
    origin = journey["legs"][0]["origin"]["stop_place"]
    destination = journey["legs"][0]["destination"]["stop_place"]

    assert origin["codes"] == {"OEBB_EXT": "9900001", "HAFAS_UIC": "9900001"}
    assert origin["uic"] == "9900001"
    # A stated UIC that is not the number is evidence for the module's matching, never `uic`.
    assert destination["codes"] == {"OEBB_EXT": "9900002", "HAFAS_UIC": "9911112"}
    assert destination["uic"] is None


def test_each_end_of_a_train_leg_is_a_place_with_its_number_name_position_and_uic() -> None:
    places: dict[str, SeenPlace] = {}

    calls = add_journeys(places, stored([(0, 1, 0)], [NORD, SUD]), AT)

    assert calls == 2
    assert set(places) == {"9900001", "9900002"}
    nord, sud = places["9900001"], places["9900002"]
    assert (nord.name, nord.lat, nord.lon, nord.uic) == ("ZZ Nord", 49.990001, 9.990001, "9900001")
    assert (sud.name, sud.uic) == ("ZZ Sued", "9911112")
    assert nord.operators == {"ZZ Bahn"}
    assert (nord.calls, nord.first_seen_at, nord.seen_at) == (1, AT, AT)


def test_a_bus_stop_and_a_place_that_is_no_station_are_never_listed() -> None:
    """A bus stop beside a station is a HAFAS station too (`A=1`): only a train leg lists a
    place, so it is never placed on the rail station (22.20, point 2). An address (`A=2`) is no
    station, even at the end of a train leg."""
    places: dict[str, SeenPlace] = {}

    add_journeys(places, stored([(0, 1, 2), (2, 3, 0)], [OST, BUS_STOP, ADDRESS, NORD]), AT)

    assert set(places) == {"9900001"}


def test_a_place_seen_again_adds_its_calls_and_keeps_the_latest_answer() -> None:
    places: dict[str, SeenPlace] = {}
    renamed = {**NORD, "name": "ZZ Nord Hbf", "crd": {"x": 9_990_009, "y": 49_990_009}}
    renamed.pop("gidL")

    add_journeys(places, stored([(0, 1, 0)], [NORD, SUD]), LATER)
    add_journeys(places, stored([(0, 1, 1), (0, 1, 0)], [renamed, OST]), AT)

    nord = places["9900001"]
    assert nord.calls == 3
    # The later answer's name and position; a UIC once seen is kept.
    assert (nord.name, nord.lat, nord.uic) == ("ZZ Nord", 49.990001, "9900001")
    assert (nord.first_seen_at, nord.seen_at) == (AT, LATER)
    assert nord.operators == {"ZZ Bahn", "ZZ Fern"}


def test_a_run_before_456_lists_a_train_its_mode_table_did_not_know() -> None:
    """Before #456 a category the mode table lacked (FR, OGV, RB) was stored as OTHER: such a
    leg is a train when the coverage's rule (`oebb_mode`) says so, never a bus or no category."""
    journey = stored([(0, 1, 1)], [NORD, SUD])[0]
    leg = journey["legs"][0]
    leg["service"]["mode"] = "OTHER"
    assert train_leg(leg)
    for category in ({"code": "Bus"}, {"code": "STR"}, {"code": ""}, None):
        leg["service"]["product_category"] = category
        assert not train_leg(leg)
    leg["service"]["mode"] = "BUS"
    leg["service"]["product_category"] = {"code": "ZY"}
    assert not train_leg(leg)


def test_a_run_stored_before_this_change_gives_the_uic_it_kept() -> None:
    """Before `HAFAS_UIC`, a stop kept a UIC only where `gidL` stated its own number (`uic`)."""
    journey = stored([(0, 1, 0)], [NORD, SUD])[0]
    for side in ("origin", "destination"):
        journey["legs"][0][side]["stop_place"]["codes"].pop("HAFAS_UIC")
    places: dict[str, SeenPlace] = {}

    add_journeys(places, [journey], AT)

    assert places["9900001"].uic == "9900001"
    assert places["9900002"].uic is None


def test_an_end_without_a_number_name_or_position_is_passed_over() -> None:
    no_number = place("ZZ West", None)
    no_number["lid"] = "A=1@O=ZZ West@L=zz99@"
    no_position = {key: value for key, value in OST.items() if key != "crd"}
    no_position["lid"] = "A=1@O=ZZ Ost@L=9900004@"
    places: dict[str, SeenPlace] = {}

    calls = add_journeys(places, stored([(0, 1, 0), (2, 1, 0)], [no_number, NORD, no_position]), AT)

    assert set(places) == {"9900001"}
    assert calls == 2


def test_what_does_not_read_is_passed_over() -> None:
    places: dict[str, SeenPlace] = {}
    leg = stored([(0, 1, 0)], [NORD, SUD])[0]["legs"][0]
    broken = [None, 9, {"legs": None}, {"legs": [None, {"kind": "timed"}, leg]}]

    assert add_journeys(places, None, AT) == 0
    assert add_journeys(places, {"legs": []}, AT) == 0
    assert add_journeys(places, broken, AT) == 2
    assert station_end({"stop_place": {"source": "hafas:oebb", "ref": "A=1@", "codes": 9}}) is None
    assert station_end({"stop_place": {"source": "motis:zz", "ref": "A=1@L=9900001@"}}) is None


def test_a_place_keeps_a_bounded_set_of_operators() -> None:
    nord = SeenPlace("9900001", "ZZ Nord", 49.99, 9.99, AT, AT)
    for n in range(hafas_places.OPERATORS_MAX + 5):
        nord.see("ZZ Nord", 49.99, 9.99, None, f"ZZ {n:02d}", AT)

    assert len(nord.operators) == hafas_places.OPERATORS_MAX
    assert nord.calls == hafas_places.OPERATORS_MAX + 5


def test_the_merge_of_operators_in_the_database_keeps_the_same_bound() -> None:
    assert f"LIMIT {hafas_places.OPERATORS_MAX})" in str(hafas_places._MERGED_OPERATORS)


def test_the_totals_say_where_the_places_are_without_naming_one() -> None:
    totals = hafas_places.Totals(
        places=3,
        calls=9,
        outside_de_at=2,
        unknown_country=0,
        with_uic=1,
        outside_de_at_with_uic=1,
        runs=1,
        by_country={"AT": 1, "CH": 1, "FR": 1},
    )

    lines = totals.lines()

    assert "outside Germany and Austria: 2 (country unknown: 0)" in lines
    assert "by country: AT 1, CH 1, FR 1" in lines
    assert not any("99" in line for line in lines)
