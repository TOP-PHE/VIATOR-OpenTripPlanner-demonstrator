"""The VIATOR station reference API and its two pages (unit 6).

What can be decided without a database is decided here: the shape of the list
query (paginate the reference, never join the codes), the pivot, the badge of
a PLC that carries several operational points, the correction rules and the
role gate on every route. The same routes run against Postgres in
tests/integration/test_station_panel.py.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api import pages
from app.api.master import station_ref as api
from app.master import station_overrides as so
from app.security import require_content_manager
from tests.station_fixtures import assert_scripts_parse, page_request

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin"
ACTOR = SimpleNamespace(id=uuid.uuid4(), username="cm@example.org", role="content_manager")
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


def sql(statement: Any) -> str:
    return re.sub(r"\s+", " ", str(statement.compile(dialect=postgresql.dialect())))


# ── small pure helpers ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "separation", "badge"),
    [
        (7, 0, "PLC carries 7 operational points, 0 m apart"),
        (2, 140, "PLC carries 2 operational points, 140 m apart"),
        (3, None, "PLC carries 3 operational points"),
        (1, 0, None),  # one operational point: nothing to tell apart
        (None, None, None),
    ],
)
def test_op_badge(count: int | None, separation: int | None, badge: str | None) -> None:
    assert api.op_badge(count, separation) == badge


def test_pivot_codes_turns_long_rows_into_one_column_per_provider() -> None:
    rows = [
        (1, "nap_CH_SBB", "9900001"),
        (1, "nap_CH_SBB", "9900001:0:1"),
        (1, "nap_ES_regional", "ZZ_FEED#MC"),
        (2, "nap_DE_DELFI", "de:99:2"),
    ]
    assert api.pivot_codes(rows) == {
        1: {"nap_CH_SBB": ["9900001", "9900001:0:1"], "nap_ES_regional": ["ZZ_FEED#MC"]},
        2: {"nap_DE_DELFI": ["de:99:2"]},
    }
    assert api.pivot_codes([]) == {}


def test_a_search_term_cannot_inject_like_wildcards() -> None:
    assert api.like_pattern("100%_sure\\") == "%100\\%\\_sure\\\\%"
    assert api.like_pattern("Sampleton") == "%Sampleton%"


# ── the list query: paginate first, never join the codes ───────────────


def test_the_uncollapsed_page_is_cut_from_station_ref_alone() -> None:
    query = sql(api.page_query(api.filter_clauses(), collapse=False).offset(100).limit(50))
    assert " JOIN " not in query
    assert "FROM station_ref ORDER BY station_ref.plc, station_ref.era_uopid" in query
    assert "LIMIT" in query


def test_no_filter_ever_joins_the_code_table() -> None:
    clauses = api.filter_clauses(
        q="9900001", country="zz", confidence="high", flag="swap_partner", has_code=True
    )
    for collapse in (True, False):
        query = sql(api.page_query(clauses, collapse))
        # The codes are reached through subqueries only: a join would multiply
        # the reference rows by their codes before the page is cut.
        assert "JOIN station_ref_code" not in query
        assert "JOIN station_ref_flag" not in query
        assert "IN (SELECT station_ref_code.station_id FROM station_ref_code" in query
        assert re.search(r"EXISTS \(SELECT .{1,40} FROM station_ref_code", query)
        assert "IN (SELECT station_ref_flag.station_id FROM station_ref_flag" in query


def test_collapsed_keeps_one_row_per_plc_by_a_fixed_rule() -> None:
    query = sql(api.page_query(api.filter_clauses(), collapse=True))
    # The operational point named like its PLC first, else the first in byte order.
    assert re.search(
        r"row_number\(\) OVER \(PARTITION BY station_ref\.plc ORDER BY "
        r"\(?station_ref\.era_uopid = station_ref\.plc\)? DESC, station_ref\.era_uopid\)",
        query,
    )
    assert "rank = " in query
    # The only join is the reference with a ranking of itself.
    assert query.count(" JOIN ") == 1
    assert "JOIN (SELECT station_ref.id AS id" in query


def test_the_total_counts_plcs_when_collapsed_and_rows_otherwise() -> None:
    clauses = api.filter_clauses(country="ZZ")
    assert "count(DISTINCT station_ref.plc)" in sql(api.count_query(clauses, True))
    assert "count(*)" in sql(api.count_query(clauses, False))


def test_filter_clauses() -> None:
    assert api.filter_clauses() == []
    none = sql(api.page_query(api.filter_clauses(confidence="none"), False))
    assert "station_ref.uic_merits_confidence IS NULL" in none
    uncoded = sql(api.page_query(api.filter_clauses(has_code=False), False))
    assert re.search(r"NOT \(?EXISTS \(SELECT .{1,40} FROM station_ref_code", uncoded)
    plc = api.filter_clauses(plc="ZZ00003", country="zz")
    assert len(plc) == 2
    assert plc[0].right.value == "ZZ"  # the country is upper-cased


def test_search_covers_name_plc_and_every_code() -> None:
    query = sql(api.page_query(api.filter_clauses(q="x"), False))
    for column in ("name", "alt_name_text", "plc"):
        assert f"station_ref.{column} ILIKE" in query
    for column in ("era_uopid", "previous_plc", "uic_merits", "eva", "rl100"):
        assert f"station_ref.{column} = " in query


# ── the row ────────────────────────────────────────────────────────────


def _station(**kw: Any) -> SimpleNamespace:
    base = {
        "id": 3,
        "plc": "ZZ00003",
        "era_uopid": "ZZOP03A",
        "name": "Testbury Yard A",
        "iso2": "ZZ",
        "is_passenger": False,
        "is_current": False,
        "complex_id": None,
        "complex_role": None,
        "uic_merits": None,
        "uic_merits_origin": None,
        "uic_merits_confidence": None,
        "warning_level": "OK",
        "best_tier": "none",
        "n_nap_feeds": 0,
        "n_op_with_plc": 2,
        "plc_op_max_sep_m": 140,
        "last_built_build_id": 7,
        "lat": 50.2,
        "rl100": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_row_shows_the_operational_point_and_the_badge() -> None:
    row = api.reference_row(
        _station(),  # type: ignore[arg-type]
        codes={"nap_CH_SBB": ["9900001"]},
        flags=["shares_plc_with"],
        complex_label=None,
        has_override=True,
        latest_build_id=7,
    )
    assert (row.plc, row.era_uopid) == ("ZZ00003", "ZZOP03A")
    assert row.op_badge == "PLC carries 2 operational points, 140 m apart"
    assert row.codes == {"nap_CH_SBB": ["9900001"]}
    assert row.flags == ["shares_plc_with"]
    assert row.has_override is True
    assert row.in_latest_build is True


def test_a_row_absent_from_the_latest_build_says_so() -> None:
    kwargs: dict[str, Any] = {
        "codes": {},
        "flags": [],
        "complex_label": None,
        "has_override": False,
    }
    stale = api.reference_row(_station(last_built_build_id=6), latest_build_id=7, **kwargs)  # type: ignore[arg-type]
    assert stale.in_latest_build is False
    no_build = api.reference_row(_station(), latest_build_id=None, **kwargs)  # type: ignore[arg-type]
    assert no_build.in_latest_build is False


# ── every route is gated ───────────────────────────────────────────────


def test_every_route_requires_a_content_manager() -> None:
    routes = [r for r in api.router.routes if getattr(r, "dependant", None) is not None]
    assert len(routes) == 7
    for route in routes:
        gates = {dep.call for dep in route.dependant.dependencies}
        assert require_content_manager in gates, f"{route.path} declares no gate"


def test_the_router_is_registered_and_summary_is_not_shadowed() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/api/master/station-ref"]
    assert "get" in paths["/api/master/station-ref/summary"]
    assert "get" in paths["/api/master/station-ref/{station_id}"]
    assert "post" in paths["/api/master/station-ref/{station_id}/overrides"]
    assert "post" in paths["/api/master/station-ref/complexes"]
    # `/summary` must be declared before `/{station_id}`, or the integer
    # path parameter would swallow it and answer 422.
    declared = [r.path for r in api.router.routes]
    assert declared.index("/api/master/station-ref/summary") < declared.index(
        "/api/master/station-ref/{station_id}"
    )


# ── corrections, against a scripted database ───────────────────────────


class _FakeDb:
    def __init__(self, station: Any, *scalars: Any) -> None:
        self.station = station
        self._scalars = list(scalars)
        self.added: list[Any] = []
        self.objects: dict[int, Any] = {}
        self.committed = False

    def get(self, model: Any, key: int) -> Any:
        if model is api.StationRef:
            return self.station if self.station is not None and key == self.station.id else None
        return self.objects.get(key)

    def execute(self, _stmt: object) -> Any:
        value = self._scalars.pop(0) if self._scalars else None
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        for index, obj in enumerate(self.added, start=100):
            if getattr(obj, "id", None) is None:
                obj.id = index

    def commit(self) -> None:
        self.committed = True

    def refresh(self, _obj: Any) -> None:
        return None


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: events.append(kw))
    return events


def _set(db: _FakeDb, field: str, value: str | None, station_id: int = 3) -> dict[str, Any]:
    body = api.OverrideBody(field_name=field, value=value, reason="checked on site")
    return api.set_override(station_id, body, REQUEST, db, ACTOR)  # type: ignore[arg-type]


def test_a_correction_changes_the_field_and_remembers_the_computed_value(
    recorded: list[dict[str, Any]],
) -> None:
    station = _station()
    db = _FakeDb(station, None)  # no earlier correction of this field
    out = _set(db, "name", "Testbury Depot")

    assert station.name == "Testbury Depot"
    (override,) = db.added
    assert override.value == "Testbury Depot"
    assert override.computed_value_at_set == "Testbury Yard A"
    assert override.computed_value_latest == "Testbury Yard A"
    assert override.set_by == ACTOR.id
    assert out["active"] is True
    assert out["drifted"] is False
    assert db.committed
    assert recorded[0]["action"] == "station_ref.override.set"
    assert recorded[0]["metadata"]["field"] == "name"


def test_a_value_is_cast_to_the_column_type() -> None:
    station = _station()
    _set(_FakeDb(station, None), "lat", "50.25")
    assert station.lat == 50.25
    db = _FakeDb(station, None)
    _set(db, "is_passenger", "yes")
    assert station.is_passenger is True
    assert db.added[0].value == "true"  # stored in its canonical text form
    _set(_FakeDb(station, None), "rl100", "")
    assert station.rl100 is None


def test_correcting_a_corrected_field_replaces_the_earlier_correction(
    recorded: list[dict[str, Any]],
) -> None:
    station = _station(name="Testbury Depot")
    earlier = SimpleNamespace(
        computed_value_latest="Testbury Yard A", computed_value_at_set="Testbury", released_at=None
    )
    db = _FakeDb(station, earlier)
    _set(db, "name", "Testbury Works")

    assert earlier.released_at is not None  # kept, as released
    (override,) = db.added
    # What the build computes is under the earlier correction, not in the column.
    assert override.computed_value_at_set == "Testbury Yard A"
    assert station.name == "Testbury Works"


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("plc", "ZZ00009", "cannot be corrected by hand"),
        ("lat", "north", "lat:"),
        ("iso2", "ZZZ", "two letters"),
    ],
)
def test_a_correction_that_does_not_fit_is_refused(
    recorded: list[dict[str, Any]], field: str, value: str, fragment: str
) -> None:
    station = _station()
    db = _FakeDb(station)
    with pytest.raises(HTTPException) as exc:
        _set(db, field, value)
    assert exc.value.status_code == 400
    assert fragment in exc.value.detail
    assert db.added == []
    assert recorded == []


def test_a_correction_needs_a_station_and_a_reason() -> None:
    db = _FakeDb(None)
    with pytest.raises(HTTPException) as exc:
        _set(db, "name", "x", station_id=99)
    assert exc.value.status_code == 404
    with pytest.raises(ValueError, match="reason"):
        api.OverrideBody(field_name="name", value="x", reason="")


def test_a_merits_correction_goes_through_the_candidates(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    station = _station(uic_merits="9900002", uic_merits_origin="Trainline")
    computed = [
        {"code": "9900002", "origin": "Trainline", "rule": "r", "confidence": "high",
         "sources": None, "check_digit": None, "is_chosen": True},
    ]  # fmt: skip
    written: list[list[dict[str, Any]]] = []

    def replace(_db: Any, target: Any, candidates: list[dict[str, Any]]) -> None:
        written.append(candidates)
        for name, value in so.merits_mirror(candidates).items():
            setattr(target, name, value)

    monkeypatch.setattr(api, "_candidates", lambda _db, _id: computed)
    monkeypatch.setattr(api, "_replace_candidates", replace)
    _set(_FakeDb(station, None), "uic_merits", "9900777")

    assert [(c["code"], c["is_chosen"]) for c in written[0]] == [
        ("9900002", False),
        ("9900777", True),
    ]
    assert station.uic_merits == "9900777"
    assert station.uic_merits_origin == so.ORIGIN_MANUAL


def _release(db: _FakeDb, override_id: int, station_id: int = 3) -> dict[str, Any]:
    return api.release_override(station_id, override_id, REQUEST, db, ACTOR)  # type: ignore[arg-type]


def _override(**kw: Any) -> SimpleNamespace:
    base = {
        "id": 5,
        "station_id": 3,
        "field_name": "name",
        "value": "Testbury Depot",
        "reason": "checked on site",
        "set_at": datetime(2026, 10, 3, tzinfo=UTC),
        "computed_value_at_set": "Testbury Yard A",
        "computed_value_latest": "Testbury Yard Alpha",
        "released_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_releasing_restores_what_the_build_computes_now(recorded: list[dict[str, Any]]) -> None:
    station = _station(name="Testbury Depot")
    override = _override()
    db = _FakeDb(station)
    db.objects[5] = override
    out = _release(db, 5)

    # Not the value from the day the correction was made: the latest one.
    assert station.name == "Testbury Yard Alpha"
    assert override.released_at is not None
    assert out["active"] is False
    assert recorded[0]["action"] == "station_ref.override.released"


def test_release_refusals() -> None:
    station = _station()
    db = _FakeDb(station)
    db.objects[5] = _override(station_id=4)  # another station's correction
    db.objects[6] = _override(id=6, released_at=datetime(2026, 10, 3, tzinfo=UTC))
    with pytest.raises(HTTPException) as other:
        _release(db, 5)
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as unknown:
        _release(db, 77)
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as twice:
        _release(db, 6)
    assert twice.value.status_code == 409


def test_override_dict_flags_a_computed_value_that_moved() -> None:
    out = api._override_dict(_override())  # type: ignore[arg-type]
    assert out["drifted"] is True
    assert out["computed_value_latest"] == "Testbury Yard Alpha"
    assert out["set_at"] == "2026-10-03T00:00:00+00:00"
    steady = api._override_dict(_override(computed_value_latest="Testbury Yard A"))  # type: ignore[arg-type]
    assert steady["drifted"] is False


# ── complexes ──────────────────────────────────────────────────────────


def _complex_body(**kw: Any) -> api.ComplexBody:
    base: dict[str, Any] = {
        "label": "Exampleville",
        "kind": "one_site",
        "station_ids": [1, 2],
        "principal_id": 1,
    }
    base.update(kw)
    return api.ComplexBody(**base)


class _ComplexDb(_FakeDb):
    def __init__(self, stations: list[Any]) -> None:
        super().__init__(None)
        self.stations = stations

    def execute(self, _stmt: object) -> Any:
        return SimpleNamespace(scalars=lambda: self.stations)


def test_grouping_marks_the_principal_and_the_members(recorded: list[dict[str, Any]]) -> None:
    stations = [_station(id=1, plc="ZZ00001"), _station(id=2, plc="ZZ00002")]
    db = _ComplexDb(stations)
    out = api.create_complex(_complex_body(), REQUEST, db, ACTOR)  # type: ignore[arg-type]

    (group,) = db.added
    assert (group.kind, group.source) == ("one_site", "manual")  # never derived when made by hand
    assert [(s.complex_id, s.complex_role) for s in stations] == [
        (group.id, "principal"),
        (group.id, "member"),
    ]
    assert out["station_ids"] == [1, 2]
    assert recorded[0]["action"] == "station_complex.created"


@pytest.mark.parametrize(
    ("body", "stations", "status"),
    [
        ({"kind": "a_vague_feeling"}, [], 400),
        ({"principal_id": 9}, [], 400),  # the principal is not one of the stations
        ({}, [_station(id=1)], 404),  # one of the two does not exist
        ({}, [_station(id=1), _station(id=2, complex_id=4)], 409),  # already grouped
    ],
)
def test_grouping_refusals(
    recorded: list[dict[str, Any]], body: dict[str, Any], stations: list[Any], status: int
) -> None:
    payload = _complex_body(**body)
    db = _ComplexDb(stations)
    with pytest.raises(HTTPException) as exc:
        api.create_complex(payload, REQUEST, db, ACTOR)  # type: ignore[arg-type]
    assert exc.value.status_code == status
    assert db.added == []
    assert recorded == []


def test_a_complex_needs_at_least_two_stations() -> None:
    with pytest.raises(ValueError, match="station_ids"):
        _complex_body(station_ids=[1])
    assert set(api.COMPLEX_KINDS) == {
        "one_site",
        "shared_operational_point",
        "parallel_register",
        "adjacent_treated_as_one",
    }


# ── the pages ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["platform_admin", "content_manager"])
def test_the_detail_page_renders_for_both_roles(role: str, tmp_path: Path) -> None:
    path = "/admin/stations/reference/42"
    response = pages.admin_station_detail_page(page_request(path, role), 42)
    assert response.status_code == 200
    html = response.body.decode()
    assert 'data-station-id="42"' in html
    assert 'href="/admin/stations/reference" aria-current="page"' in html
    assert "Connection times" in html
    assert "Not part of this first step" in html
    assert assert_scripts_parse(html, tmp_path) == 3


def test_the_detail_page_is_guarded() -> None:
    path = "/admin/stations/reference/42"
    assert pages.admin_station_detail_page(page_request(path, "end_user"), 42).status_code == 403
    anonymous = pages.admin_station_detail_page(page_request(path, None), 42)
    assert anonymous.status_code == 303
    # The return address is the list: nothing the request supplied goes into a redirect.
    assert anonymous.headers["location"] == "/login?next=/admin/stations/reference"


def test_the_list_screen_shows_what_the_design_asks_for() -> None:
    html = (TEMPLATES / "station_reference.html").read_text(encoding="utf-8")
    # era_uopid is half the key: it has its own column, next to the PLC.
    for heading in ("PLC", "Operational point", "Name", "Country", "Complex", "MERITS"):
        assert f"'{heading}'" in html
    assert "SP.esc(r.op_badge)" in html  # the badge, on rows collapsed by default
    assert 'name="collapse" checked' in html
    for name in ("q", "country", "confidence", "flag", "has_code"):
        assert f'name="{name}"' in html
    assert "/admin/stations/reference/' + r.id" in html  # each row links to its detail
    assert "Connection times are not part of this first step" in html


def test_the_detail_screen_has_every_section() -> None:
    html = (TEMPLATES / "station_reference_detail.html").read_text(encoding="utf-8")
    for section in (
        "MERITS candidates",
        "Codes",
        "Matched NAP stops",
        "Complex",
        "Flags",
        "Corrections",
        "What the builds changed",
        "Connection times",
    ):
        assert f"<h2>{section}</h2>" in html
    assert "f.related ? ' → ' + stationLink(f.related)" in html  # a flag links to its station
