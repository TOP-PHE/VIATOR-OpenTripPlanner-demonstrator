"""The registers screen (unit 7): the version delta, and the shape of the lists.

The delta is pure: two lists of rows in, five kinds of change out. The SQL of
the lists runs in tests/integration/test_station_panel.py; their search also
runs here, on a SQLite stand-in.

Every row is invented; the PLC prefix `ZZ` does not exist.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.master import station_registers as api
from app.master import station_delta as sd
from app.security import require_content_manager
from tests.station_fixtures import sqlite_stand_in

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "station_registers.html"
)


def row(
    key: str, name: str | None = None, lat: float | None = None, lon: float | None = None
) -> sd.RegisterRow:
    return sd.RegisterRow(key, name, lat, lon)


OLD = [
    row("ZZ00001", "Exampleville Central", 50.0, 4.0),
    row("ZZ00002", "Sampleton", 50.1, 4.1),
    row("ZZ00003", "Testbury Yard", 50.2, 4.2),
    row("ZZ00004", "Mockford Halt"),  # no position
    row("ZZ00005", "Oldtown", 50.5, 4.5),
    row("ZZ00006", "Renumbered Junction", 50.6, 4.6),
]
NEW = [
    row("ZZ00001", "Exampleville Central", 50.0, 4.0),  # unchanged
    row("ZZ00002", "Sampleton Hbf", 50.1, 4.1),  # renamed
    row("ZZ00003", "Testbury Yard", 50.21, 4.2),  # moved by about 1.1 km
    row("ZZ00004", "Mockford Halt", 50.4, 4.4),  # a position appears: not a move
    # ZZ00005 is gone
    row("ZZ00016", "Renumbered Junction", 50.6, 4.6),  # ZZ00006 under a new code
    row("ZZ00007", "Newville", 50.7, 4.7),  # created
]


# ── distance ───────────────────────────────────────────────────────────


def test_haversine() -> None:
    assert sd.haversine_m(50.0, 4.0, 50.0, 4.0) == 0.0
    # One degree of latitude is about 111.2 km everywhere.
    assert sd.haversine_m(50.0, 4.0, 51.0, 4.0) == pytest.approx(111_195, rel=1e-3)
    # One degree of longitude shrinks with the cosine of the latitude.
    assert sd.haversine_m(60.0, 4.0, 60.0, 5.0) == pytest.approx(55_597, rel=1e-3)


def test_distance_needs_both_positions() -> None:
    assert sd.distance_m(row("a", lat=50.0, lon=4.0), row("b")) is None
    assert sd.distance_m(row("a"), row("b", lat=50.0, lon=4.0)) is None
    assert sd.distance_m(row("a", lat=50.0, lon=4.0), row("b", lat=50.0, lon=4.0)) == 0.0


# ── the delta ──────────────────────────────────────────────────────────


def test_the_delta_sorts_changes_into_five_kinds() -> None:
    delta = sd.register_delta(OLD, NEW)
    assert delta.counts() == {
        "created": 1,
        "removed": 1,
        "renamed": 1,
        "moved": 1,
        "renumbered": 1,
        "unchanged": 2,
    }
    assert [r.key for r in delta.created] == ["ZZ00007"]
    assert [r.key for r in delta.removed] == ["ZZ00005"]
    assert [(old.name, new.name) for old, new in delta.renamed] == [("Sampleton", "Sampleton Hbf")]
    ((old, new, apart),) = delta.moved
    assert (old.key, new.key) == ("ZZ00003", "ZZ00003")
    assert apart == pytest.approx(1112, rel=1e-2)
    assert [(old.key, new.key) for old, new in delta.renumbered] == [("ZZ00006", "ZZ00016")]


def test_a_renumbered_location_is_neither_created_nor_removed() -> None:
    delta = sd.register_delta(OLD, NEW)
    assert "ZZ00006" not in {r.key for r in delta.removed}
    assert "ZZ00016" not in {r.key for r in delta.created}


def test_a_position_that_appears_is_not_a_move() -> None:
    delta = sd.register_delta(
        [row("ZZ00004", "Mockford Halt")], [row("ZZ00004", "Mockford Halt", 50.4, 4.4)]
    )
    assert delta.moved == []
    assert delta.unchanged == 1


def test_the_threshold_decides_what_counts_as_moved() -> None:
    old = [row("ZZ00003", "Testbury Yard", 50.2, 4.2)]
    new = [row("ZZ00003", "Testbury Yard", 50.2005, 4.2)]  # about 56 m north
    assert sd.register_delta(old, new).moved == []  # under the default 100 m
    assert len(sd.register_delta(old, new, threshold_m=50).moved) == 1
    assert sd.DEFAULT_THRESHOLD_M == 100.0


def test_a_location_can_be_renamed_and_moved_at_once() -> None:
    old = [row("ZZ00002", "Sampleton", 50.1, 4.1)]
    new = [row("ZZ00002", "Sampleton Hbf", 50.2, 4.1)]
    delta = sd.register_delta(old, new)
    assert delta.counts() == {
        "created": 0,
        "removed": 0,
        "renamed": 1,
        "moved": 1,
        "renumbered": 0,
        "unchanged": 0,
    }


def test_renumbering_needs_the_same_name_at_the_same_place() -> None:
    old = [row("ZZ00006", "Junction", 50.6, 4.6)]
    # Same name, 11 km away: two different places that share a name.
    far = sd.register_delta(old, [row("ZZ00016", "Junction", 50.7, 4.6)])
    assert far.renumbered == []
    assert (len(far.removed), len(far.created)) == (1, 1)
    # Same place, another name: not proposed either.
    other = sd.register_delta(old, [row("ZZ00016", "Crossing", 50.6, 4.6)])
    assert other.renumbered == []
    # Without a position there is nothing to confirm the pairing with.
    blind = sd.register_delta([row("ZZ00006", "Junction")], [row("ZZ00016", "Junction")])
    assert blind.renumbered == []
    # Case and surrounding spaces do not matter for the name.
    same = sd.register_delta(old, [row("ZZ00016", " JUNCTION ", 50.6, 4.6)])
    assert len(same.renumbered) == 1


def test_each_location_is_paired_once() -> None:
    old = [row("ZZ00006", "Junction", 50.6, 4.6)]
    new = [row("ZZ00016", "Junction", 50.6, 4.6), row("ZZ00026", "Junction", 50.6, 4.6)]
    delta = sd.register_delta(old, new)
    assert [(o.key, n.key) for o, n in delta.renumbered] == [("ZZ00006", "ZZ00016")]
    assert [r.key for r in delta.created] == ["ZZ00026"]
    assert delta.removed == []


def test_identical_versions_have_an_empty_delta() -> None:
    delta = sd.register_delta(OLD, OLD)
    assert delta.counts() == {
        "created": 0,
        "removed": 0,
        "renamed": 0,
        "moved": 0,
        "renumbered": 0,
        "unchanged": 6,
    }


def test_a_missing_name_equals_an_empty_one() -> None:
    delta = sd.register_delta([row("ZZ00001", None)], [row("ZZ00001", "")])
    assert delta.renamed == []


def test_a_name_that_appears_or_disappears_is_not_a_rename() -> None:
    named = [row("ZZ00001", "Exampleville Central", 50.0, 4.0)]
    for bare in (row("ZZ00001"), row("ZZ00001", "  ")):
        gone = sd.register_delta(named, [bare])
        assert (gone.renamed, gone.moved, gone.unchanged) == ([], [], 1)
        back = sd.register_delta([bare], named)
        assert (back.renamed, back.moved, back.unchanged) == ([], [], 1)
    # Named in both versions, and differently: that is still a rename.
    other = sd.register_delta(named, [row("ZZ00001", "Exampleville Hbf", 50.0, 4.0)])
    assert len(other.renamed) == 1


def test_a_location_crd_retires_between_two_versions_is_not_renamed() -> None:
    """Once a location is retired in CRD the file carries ERA's name and
    position for it (name_src, pos_src), so its register row has none. CRD
    changed the validity, not the name: the delta must not call it renamed."""
    from app.master import station_files as sf
    from app.master import station_parse as sp
    from tests.station_fixtures import crd_location_rows

    own = crd_location_rows()[0]
    retired = {
        **own,
        "spine_source": "ERA_retired_in_CRD",
        "crd_end": "2022-12-10",
        "name_src": "ERA",
        "pos_src": "ERA",
    }

    def delta_rows(source: dict[str, str]) -> list[sd.RegisterRow]:
        line = {column: source.get(column, "") for column in sf.FILE_SHAPES[sf.CRD_LOCATIONS]}
        return api.crd_rows(
            (loc["plc"], loc["name"], loc["lat"], loc["lon"], loc["start_validity"])
            for loc in sp.parse_crd_locations([line]).locations
        )

    older, newer = delta_rows(own), delta_rows(retired)
    assert [(r.key, r.name, r.lat, r.lon) for r in older] == [
        ("ZZ00001", "Exampleville Central", 50.0, 4.0)
    ]
    assert [(r.key, r.name, r.lat, r.lon) for r in newer] == [("ZZ00001", None, None, None)]
    # Retired, and reinstated: neither direction is a rename or a move.
    for delta in (sd.register_delta(older, newer), sd.register_delta(newer, older)):
        assert delta.counts() == {
            "created": 0,
            "removed": 0,
            "renamed": 0,
            "moved": 0,
            "renumbered": 0,
            "unchanged": 1,
        }


# ── register rows for the delta ────────────────────────────────────────


def test_a_plc_with_several_validity_periods_is_one_delta_row() -> None:
    rows = api.crd_rows(
        [
            ("ZZ00001", "Exampleville (old name)", 50.0, 4.0, "2010-12-12"),
            ("ZZ00001", "Exampleville Central", 50.0, 4.0, "2019-12-15"),
            ("ZZ00002", "Sampleton", 50.1, 4.1, None),
        ]
    )
    assert [(r.key, r.name) for r in rows] == [
        ("ZZ00001", "Exampleville Central"),  # the latest start of validity
        ("ZZ00002", "Sampleton"),
    ]


def test_era_rows_are_keyed_on_plc_and_operational_point() -> None:
    rows = api.era_rows(
        [
            ("ZZ00003", "ZZOP03A", "Testbury Yard A", 50.2, 4.2),
            ("ZZ00003", "ZZOP03B", "Testbury Yard B", 50.2, 4.2),
        ]
    )
    assert [r.key for r in rows] == ["ZZ00003 / ZZOP03A", "ZZ00003 / ZZOP03B"]


def test_delta_payload_caps_the_lists_and_keeps_the_counts() -> None:
    old = [row(f"ZZ{i:05d}", f"Station {i}") for i in range(10)]
    payload = api.delta_payload(sd.register_delta(old, []), limit=3)
    assert payload["counts"]["removed"] == 10  # complete
    assert len(payload["removed"]) == 3  # capped
    assert payload["truncated"] == ["removed"]
    assert payload["removed"][0] == {
        "key": "ZZ00000",
        "name": "Station 0",
        "lat": None,
        "lon": None,
    }

    full = api.delta_payload(sd.register_delta(OLD, NEW))
    assert full["truncated"] == []
    assert full["moved"][0]["distance_m"] == 1112
    assert full["renumbered"][0]["old"]["key"] == "ZZ00006"
    assert full["renamed"][0]["new"]["name"] == "Sampleton Hbf"


# ── the list query ─────────────────────────────────────────────────────


def sql(statement: Any) -> str:
    return re.sub(r"\s+", " ", str(statement.compile(dialect=postgresql.dialect())))


def _where(register: str, **kw: Any) -> str:
    import uuid

    table = api._REGISTERS[register].table
    clauses = api.list_clauses(register, uuid.uuid4(), kw.get("q"), kw.get("country"))
    return sql(select(table.plc).where(*clauses))


def test_a_list_reads_one_version_only() -> None:
    assert "WHERE crd_location.source_version_id = " in _where("crd")
    assert "WHERE era_operational_point.source_version_id = " in _where("era")


def test_the_search_is_by_name_plc_and_the_registers_own_code() -> None:
    crd = _where("crd", q="x", country="zz")
    assert "crd_location.name ILIKE" in crd
    assert "crd_location.plc ILIKE" in crd
    assert "crd_location.location_code = " in crd
    assert "crd_location.country = " in crd
    era = _where("era", q="x", country="zz")
    assert "era_operational_point.uopid = " in era
    assert "era_operational_point.iso2 = " in era


VERSION = uuid.uuid4()
# (PLC, name, the register's own code). One name carries a LIKE wildcard.
REGISTER_ROWS = [
    ("ZZ00002", "Sampleton", "00002"),
    ("ZZ00005", "Sam_leton Sidings", "00005"),
    ("ZZ00006", "Quarterbury 25% Halt", "00006"),
]


@pytest.fixture
def registers() -> Iterator[Session]:
    """The columns the register search reads, in SQLite, with invented rows:
    one version of each register, and one row of another version."""
    engine = sqlite_stand_in(api.CrdLocation, api.EraOperationalPoint)
    other = uuid.uuid4()
    versions = [*[(VERSION, row) for row in REGISTER_ROWS], (other, ("ZZ00009", "Sampleton", "0"))]
    with Session(engine) as db:
        for table, code in ((api.CrdLocation, "location_code"), (api.EraOperationalPoint, "uopid")):
            db.execute(
                insert(table.__table__),
                [
                    {"source_version_id": version, "plc": plc, "name": name, code: own}
                    for version, (plc, name, own) in versions
                ],
            )
        yield db
    engine.dispose()


@pytest.mark.parametrize("register", ["crd", "era"])
@pytest.mark.parametrize(
    ("term", "plcs"),
    [
        ("sampl", ["ZZ00002"]),  # by substring, whatever the case, in one version only
        ("zz0000", ["ZZ00002", "ZZ00005", "ZZ00006"]),  # in the PLC
        ("00005", ["ZZ00005"]),  # the PLC again, and the register's own code
        # A wildcard typed in the term is a character, not a pattern.
        ("Sam_leton", ["ZZ00005"]),  # not Sampleton as well
        ("%", ["ZZ00006"]),  # not every row
        ("ZZ0000_", []),  # not every PLC
        ("0000_", []),  # nor by its digits; and a code is compared whole
    ],
)
def test_a_wildcard_in_a_register_search_is_a_character_not_a_pattern(
    registers: Session, register: str, term: str, plcs: list[str]
) -> None:
    plc = api._REGISTERS[register].table.plc
    clauses = api.list_clauses(register, VERSION, term, None)
    assert registers.execute(select(plc).where(*clauses).order_by(plc)).scalars().all() == plcs


def test_the_two_registers_map_to_the_two_offline_shapes() -> None:
    from app.master import station_files as sf

    assert api._REGISTERS["crd"].fmt == sf.CRD_LOCATIONS
    assert api._REGISTERS["era"].fmt == sf.ERA_TELREF
    assert set(api._REGISTERS) == {"crd", "era"}


# ── gates and registration ─────────────────────────────────────────────


def test_every_route_requires_a_content_manager() -> None:
    routes = [r for r in api.router.routes if getattr(r, "dependant", None) is not None]
    assert len(routes) == 3
    for route in routes:
        gates = {dep.call for dep in route.dependant.dependencies}
        assert require_content_manager in gates, f"{route.path} declares no gate"


def test_the_router_is_registered() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    for path in ("/{register}", "/{register}/versions", "/{register}/delta"):
        assert "get" in paths["/api/master/station-registers" + path]
        # Read-only: a register changes by uploading a new version of its source.
        assert set(paths["/api/master/station-registers" + path]) == {"get"}


# ── the screen ─────────────────────────────────────────────────────────


def test_the_screen_has_two_tabs_crd_first_and_a_licence_banner() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('data-register="crd"') < html.index('data-register="era"')
    assert "await show('crd');" in html  # CRD is the default tab
    assert 'class="sp-banner licence"' in html
    assert "These rows stay inside VIATOR" in html
    # The step 1 limitation is stated plainly on the CRD tab.
    assert "does not parse the CRD XML export yet" in html
    for kind in ("created", "removed", "renumbered", "renamed", "moved"):
        assert f"'{kind}'" in html
