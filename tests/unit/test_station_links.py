"""The two lists of screen A (unit 8): unmatched stops, and stops whose code
contradicts the reference. Both are read from `station_ref_link`.

The filters are checked here by compiling them, and the two searches by
running them on a SQLite stand-in; the lists run against Postgres in
tests/integration/test_station_panel.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.master import station_links as api
from app.models import StationRefLink
from app.security import require_content_manager
from tests.station_fixtures import sqlite_stand_in

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "station_nap.html"


def where(clauses: list[Any]) -> str:
    """The WHERE of a link query, with its parameters inlined.

    The driver's doubling of `%` and of the backslash is undone, so the text
    reads as Postgres receives it.
    """
    statement = select(StationRefLink.id).where(*clauses)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    sql = re.sub(r"\s+", " ", str(compiled)).split(" WHERE ", 1)[1]
    return re.sub(r"\\+", r"\\", re.sub(r"%+", "%", sql))


# ── unmatched stops ────────────────────────────────────────────────────


def test_unmatched_means_a_link_without_a_station() -> None:
    assert "station_ref_link.station_id IS NULL" in where(api.unmatched_clauses())


def test_the_unmatched_list_defaults_to_rail_and_multimodal() -> None:
    # The urban rows are tram and bus stops that were never expected to match
    # a railway location.
    assert api.DEFAULT_LABELS == ("Rail", "Multimodal")
    default = where(api.unmatched_clauses())
    assert "station_ref_link.label IN ('Rail', 'Multimodal')" in default
    assert default == where(api.unmatched_clauses(labels=[]))


def test_the_label_filter_can_be_widened_or_narrowed() -> None:
    urban = where(api.unmatched_clauses(labels=["Urban"]))
    assert "station_ref_link.label IN ('Urban')" in urban
    both = where(api.unmatched_clauses(labels=["Rail", "unknown"]))
    assert "station_ref_link.label IN ('Rail', 'unknown')" in both
    # `all` lifts the filter altogether.
    assert "label" not in where(api.unmatched_clauses(labels=["all"]))
    assert api.label_clause(["all"]) is None


def test_unmatched_filters_by_feed_country_and_name() -> None:
    sql = where(api.unmatched_clauses(feed="DE_DELFI", country="zz", q="West"))
    assert "station_ref_link.iso2 = 'ZZ'" in sql
    assert "station_ref_link.stop_name ILIKE '%West%'" in sql
    assert "station_ref_link.feed_key = 'DE_DELFI'" in sql


def test_a_feed_matches_inside_a_list_of_feeds() -> None:
    # An unmatched stop can be listed for several feeds: `DE_DELFI|ZZ_FEED`.
    sql = where([api.feed_clause("ZZ_FEED")])
    assert "station_ref_link.feed_key = 'ZZ_FEED'" in sql
    assert "LIKE 'ZZ\\_FEED|%'" in sql  # first of several
    assert "LIKE '%|ZZ\\_FEED'" in sql  # last of several
    assert "LIKE '%|ZZ\\_FEED|%'" in sql  # in the middle
    # The underscore of a feed name is a character, not a wildcard.
    assert "LIKE 'ZZ_FEED" not in sql


def test_the_feed_facet_counts_a_stop_once_per_feed_it_is_listed_for() -> None:
    rows = [("ZZ_FEED", 3), ("DE_DELFI|ZZ_FEED", 1), ("DE_DELFI", 2), (None, 4), ("", 1)]
    assert api.split_feed_counts(rows) == [
        {"value": "DE_DELFI", "count": 3},
        {"value": "ZZ_FEED", "count": 4},
    ]
    assert api.split_feed_counts([]) == []


# ── stops whose code contradicts the reference ─────────────────────────


def test_a_contradiction_is_a_matched_link_not_asserted_despite_a_code() -> None:
    sql = where(api.contradiction_clauses())
    assert "station_ref_link.station_id IS NOT NULL" in sql
    assert "station_ref_link.asserted IS false" in sql
    assert "station_ref_link.code_value IS NOT NULL" in sql


def test_contradictions_filter_by_feed_and_by_the_reference_rows_country() -> None:
    sql = where(api.contradiction_clauses(feed="DE_DELFI", country="zz", q="9900002"))
    assert "station_ref_link.feed_key = 'DE_DELFI'" in sql
    # A matched link has no country of its own: the reference row's is used,
    # through a subquery rather than a join.
    assert "station_ref_link.station_id IN (SELECT station_ref.id FROM station_ref" in sql
    assert "station_ref.iso2 = 'ZZ'" in sql
    assert "station_ref_link.code_value = '9900002'" in sql
    assert "station_ref_link.stop_key = '9900002'" in sql


# ── the filters, executed: what a term with a wildcard in it matches ───


def _link(stop: str, name: str, **kw: Any) -> dict[str, Any]:
    """One link row; every row carries the same keys, as a bulk INSERT needs."""
    row = {"station_id": None, "label": "Rail", "code_value": None, "stop_key": None}
    return {**row, "offline_station_id": stop, "stop_name": name, "asserted": False, **kw}


@pytest.fixture
def links() -> Iterator[Session]:
    """The columns the two lists search, in SQLite, with invented stops. One
    stop of each list carries a LIKE wildcard in its own name."""
    engine = sqlite_stand_in(StationRefLink)
    code = {"station_id": 2, "code_value": "9900002"}  # matched, with a code, not asserted
    rows = [
        _link("NAPST0102", "Sampleton West"),
        _link("NAPST0105", "Sampleton _est", label="Urban"),
        _link("NAPST0002", "Sampleton Nord", stop_key="de:99:2", **code),
        _link("NAPST0006", "Sampleton 100% Nord", stop_key="de:99:6", **code),
    ]
    with Session(engine) as db:
        db.execute(insert(StationRefLink.__table__), rows)
        yield db
    engine.dispose()


def _stops(db: Session, clauses: list[Any]) -> list[str]:
    stop = StationRefLink.offline_station_id
    return list(db.execute(select(stop).where(*clauses).order_by(stop)).scalars())


@pytest.mark.parametrize(
    ("term", "stops"),
    [
        ("west", ["NAPST0102"]),  # by substring, whatever the case
        # A wildcard typed in the term is a character, not a pattern.
        ("Sampleton _est", ["NAPST0105"]),  # not Sampleton West as well
        ("%", []),  # not every stop
    ],
)
def test_a_wildcard_in_an_unmatched_search_is_a_character(
    links: Session, term: str, stops: list[str]
) -> None:
    assert _stops(links, api.unmatched_clauses(labels=["all"], q=term)) == stops


@pytest.mark.parametrize(
    ("term", "stops"),
    [
        ("nord", ["NAPST0002", "NAPST0006"]),
        ("de:99:2", ["NAPST0002"]),  # the stop key, by equality
        ("9900002", ["NAPST0002", "NAPST0006"]),  # the code, by equality
        ("Sampleton _ord", []),  # not Sampleton Nord
        ("%", ["NAPST0006"]),  # the one stop with a percent sign in its name
        ("de:99:_", []),  # a key is compared whole, never as a pattern
    ],
)
def test_a_wildcard_in_a_contradiction_search_is_a_character(
    links: Session, term: str, stops: list[str]
) -> None:
    assert _stops(links, api.contradiction_clauses(q=term)) == stops


# ── gates and registration ─────────────────────────────────────────────


def test_every_route_requires_a_content_manager() -> None:
    routes = [r for r in api.router.routes if getattr(r, "dependant", None) is not None]
    assert len(routes) == 3
    for route in routes:
        gates = {dep.call for dep in route.dependant.dependencies}
        assert require_content_manager in gates, f"{route.path} declares no gate"


def test_the_router_is_registered_and_read_only() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    for name in ("summary", "unmatched", "contradictions"):
        assert set(paths[f"/api/master/station-links/{name}"]) == {"get"}


# ── the screen ─────────────────────────────────────────────────────────


def test_the_screen_is_the_two_lists_with_their_filters() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('data-view="unmatched"') < html.index('data-view="contradictions"')
    assert "Stops we could not match" in html
    assert "Stops whose code contradicts the reference" in html
    for name in ("feed", "country"):
        assert f'<select name="{name}">' in html
    # The default label filter comes from the API, not from the template.
    assert "summary.default_labels.includes(item.value)" in html
    # The hint an operator works from: the nearest reference row and its distance.
    assert "Nearest reference row" in html
    assert "metres(r.nearest_distance_m)" in html
    # A table always has its header row, before any script runs.
    assert re.search(r'<thead id="nap-head"><tr>\s*<th scope="col">Stop</th>', html)
