"""Screen C, the Trainline codes panel: the three fixes of unit 9.

1. `GET /api/master/stations` reads the drift table's key only, not its rows.
2. `page` is optional: omitted, a context search lands on its first match;
   given, it is the page shown, 0 included. The panel sends it only when the
   operator asked for a page by number.
3. `.flag` and `.hint` are defined in `_base.html`, where every page finds them.

No database: the route is called with a session that answers each statement
in turn and keeps what it was asked.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import Response
from sqlalchemy.dialects import postgresql

from app.api import pages
from app.api.master import stations
from app.models import MasterStation
from tests.station_fixtures import inline_scripts, page_request, run_node_program

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
PANEL = TEMPLATES / "admin" / "master_stations.html"
LIST_PATH = "/api/master/stations"


class FakeResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> Any:
        return self.value


class FakeDb:
    """Answers each `execute` in turn and keeps the statements it was given."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.answers.pop(0))


def station(uic: str, name: str) -> MasterStation:
    return MasterStation(
        uic=uic, name=name, country_iso="ZZ", is_main_station=False, source="trainline"
    )


def sql(statement: Any) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return re.sub(r"\s+", " ", str(compiled))


def call(db: FakeDb, **overrides: Any) -> tuple[list[stations.StationResponse], Response]:
    """The route as FastAPI calls it, every parameter resolved."""
    params: dict[str, Any] = {
        "q": None,
        "country": None,
        "page": None,
        "size": 50,
        "mode": "filter",
    }
    response = Response()
    rows = stations.list_stations(response, db, object(), **(params | overrides))  # type: ignore[arg-type]
    return rows, response


ECHO = station("9900005", "Echo (synthetic)")


def context_search(page: int | None) -> tuple[FakeDb, Response]:
    """A context search whose first match sits after 260 rows: on page 5."""
    db = FakeDb(900, 3, ECHO, 260, [ECHO], [])
    response = call(db, q="echo", mode="context", page=page)[1]
    return db, response


# ── fix 2: `page` is optional ──────────────────────────────────────────


def test_a_context_search_that_pins_no_page_lands_on_its_first_match() -> None:
    db, response = context_search(None)
    assert "LIMIT 50 OFFSET 250" in sql(db.statements[4])
    assert response.headers["X-Match-Page"] == "5"
    assert response.headers["X-Match-Count"] == "3"
    assert response.headers["X-Total-Count"] == "900"


@pytest.mark.parametrize(("page", "offset"), [(0, 0), (1, 50), (3, 150), (5, 250)])
def test_a_page_the_caller_asked_for_is_the_page_shown(page: int, offset: int) -> None:
    # Page 0 used to double as "no page asked for": going back to the first
    # page during a context search silently returned the match page again.
    db, response = context_search(page)
    assert f"LIMIT 50 OFFSET {offset}" in sql(db.statements[4])
    # Where the first match is does not depend on the page that was asked for.
    assert response.headers["X-Match-Page"] == "5"


def test_the_default_mode_is_still_filter_and_starts_on_the_first_page() -> None:
    # What the journey typeahead sends: `q` and `size`, nothing else.
    db = FakeDb(1, 1, [ECHO], [])
    rows, response = call(db, q="echo", size=20)
    assert len(db.statements) == 4  # no search for the page of the first match
    listed = sql(db.statements[2])
    assert "ILIKE '%%echo%%'" in listed
    assert "LIMIT 20 OFFSET 0" in listed
    assert response.headers["X-Total-Count"] == "1"
    assert response.headers["X-Match-Page"] == "0"
    assert [row.is_match for row in rows] == [True]


def test_a_listing_without_a_query_starts_on_the_first_page() -> None:
    db = FakeDb(900, [ECHO], [])
    response = call(db)[1]
    assert "LIMIT 50 OFFSET 0" in sql(db.statements[1])
    assert "X-Match-Page" not in response.headers

    paged = FakeDb(900, [ECHO], [])
    call(paged, page=4, mode="context")
    assert "LIMIT 50 OFFSET 200" in sql(paged.statements[1])


@pytest.mark.parametrize(("page", "expected"), [(None, 0), (0, 0), (7, 7)])
def test_pinned_page(page: int | None, expected: int) -> None:
    assert stations.pinned_page(page) == expected


def test_page_is_optional_in_the_signature_and_the_mode_default_is_untouched() -> None:
    parameters = inspect.signature(stations.list_stations).parameters
    assert parameters["page"].annotation == "int | None"
    assert parameters["page"].default.default is None
    assert parameters["mode"].default.default == "filter"


def test_the_published_contract() -> None:
    from app.main import app

    published = {p["name"]: p for p in app.openapi()["paths"][LIST_PATH]["get"]["parameters"]}
    page = published["page"]
    assert page["required"] is False
    assert "default" not in page["schema"]
    assert {"type": "integer", "minimum": 0} in page["schema"]["anyOf"]
    assert published["mode"]["schema"]["default"] == "filter"


# ── fix 1: the drift column, not the drift rows ────────────────────────


def test_the_drift_lookup_reads_the_key_only() -> None:
    db = FakeDb(["9900005", "9900007"])
    assert stations.drift_uics_of(db) == {"9900005", "9900007"}  # type: ignore[arg-type]
    (statement,) = db.statements
    assert sql(statement) == (
        "SELECT master_stations_pending_drift.uic FROM master_stations_pending_drift"
    )


def test_the_list_marks_drift_from_the_projected_keys() -> None:
    golf = station("9900007", "Golf (synthetic)")
    db = FakeDb(2, [ECHO, golf], ["9900007"])
    rows = call(db)[0]
    assert [(row.uic, row.has_drift) for row in rows] == [("9900005", False), ("9900007", True)]
    assert "trainline_snapshot" not in sql(db.statements[-1])


# ── fix 2, in the panel: only a page the operator asked for is sent ────

NODE_PRELUDE = """
const fetched = [];
// An element that accepts any call and any assignment, and keeps what it is given.
function node(props = {}) {
  return new Proxy(props, {
    get: (target, key) => (key in target ? target[key] : () => node()),
    set: (target, key, value) => { target[key] = value; return true; },
  });
}
const fields = {
  q: node({value: 'sample'}),
  country: node({value: ''}),
  'context-mode': node({checked: true}),
};
globalThis.document = {
  getElementById: (id) => fields[id] || node(),
  querySelectorAll: () => [],
};
globalThis.requestAnimationFrame = () => {};
globalThis.fetch = async (url) => {
  fetched.push(url);
  const headers = {get: (name) => (name === 'X-Match-Page' ? '4' : '0')};
  return {ok: true, json: async () => [], headers};
};
"""

NODE_SCENARIO = """
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));  // the page's own first load
  const out = {opened: currentPage};
  await showPage(0);                 // « First, during a context search
  out.first = currentPage;
  await showPage(3);                 // the page-jump box
  out.jumped = currentPage;
  await loadStations({page: 0});     // the Search button
  out.searched = currentPage;
  fields.q.value = 'other';
  await showPage(2);                 // a page control after the query changed
  out.newQuery = currentPage;
  out.urls = fetched.filter((url) => !url.endsWith('/drift'));
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_the_panel_sends_a_page_only_when_the_operator_asked_for_one() -> None:
    ((is_module, script),) = inline_scripts(PANEL.read_text(encoding="utf-8"))
    assert not is_module
    out = run_node_program(NODE_PRELUDE + script + NODE_SCENARIO)
    base = LIST_PATH + "?q=sample&mode=context"
    assert out["urls"] == [
        f"{base}&size=50",  # opening the page: no page pinned
        f"{base}&page=0&size=50",  # « First is sent as page 0 ...
        f"{base}&page=3&size=50",
        f"{base}&size=50",  # a new search pins nothing
        f"{LIST_PATH}?q=other&mode=context&size=50",  # nor does a new query
    ]
    # The server answers an unpinned context search with the match page (4
    # here); a pinned page is the page shown.
    assert out["opened"] == 4
    assert out["first"] == 0  # ... and the panel stays on it
    assert out["jumped"] == 3
    assert out["searched"] == 4
    assert out["newQuery"] == 4


def test_the_pagination_controls_pin_their_page() -> None:
    html = PANEL.read_text(encoding="utf-8")
    assert "if (pinned) params.set('page', String(page));" in html
    assert "params.set('page', String(page));\n  params.set('size'" in html
    assert html.count("showPage(") == 3  # the definition, the buttons, the page-jump box
    assert "loadStations({page: n, flashMatches: false})" in html  # only inside showPage
    assert html.count("flashMatches: false") == 2  # showPage and Clear


# ── fix 3: `.flag` and `.hint` belong to the base template ─────────────


def test_flag_and_hint_are_defined_in_the_base_template() -> None:
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    styles = base[base.index("<style>") : base.index("{% block extra_styles %}")]
    assert re.search(r"^\s*\.flag \{ font-size: 0\.7rem;", styles, re.MULTILINE)
    for variant in ("ALL", "NAP-ONLY", "MERITS-ONLY", "SUBSET"):
        assert re.search(rf"^\s*\.flag\.{variant}\s*\{{", styles, re.MULTILINE), variant
    assert re.search(r"^\s*\.hint \{ color: var\(--rail-steel\); \}", styles, re.MULTILINE)


def test_the_trainline_panel_gets_the_styles_it_uses() -> None:
    request = page_request("/admin/stations/trainline", "content_manager")
    html = pages.admin_station_trainline_page(request).body.decode()
    assert 'class="flag SUBSET"' in html  # the DRIFT badge
    assert 'class="hint"' in html
    assert ".flag.SUBSET" in html
    assert ".hint { color: var(--rail-steel); }" in html


def test_the_journey_page_keeps_its_own_blue_and_amber() -> None:
    journey = (TEMPLATES / "journey.html").read_text(encoding="utf-8")
    # The rules that moved are gone from the page ...
    assert not re.search(r"^\.flag \{", journey, re.MULTILINE)
    assert not re.search(r"^\.flag\.(ALL|NAP-ONLY)\b", journey, re.MULTILINE)
    # ... and the two it shares with .leg-mode and .cmp-count still override the base.
    assert ".flag.MERITS-ONLY{ background: #e8f0fb; color: #1C75BC; }" in journey
    assert ".flag.SUBSET     { background: #fff8e1; color: #b3760e; }" in journey


def test_the_moved_variants_render_as_they_did_on_the_journey_page() -> None:
    base = (TEMPLATES / "_base.html").read_text(encoding="utf-8")
    # journey.html had #1a7e3c and #b30021: the values of these two variables.
    assert "--ok:         #1a7e3c;" in base
    assert "--fail:       #b30021;" in base
    assert ".flag.ALL         { background: #e9f6ec; color: var(--ok); }" in base
    assert ".flag.NAP-ONLY    { background: #fdecef; color: var(--fail); }" in base


def test_no_other_template_defines_an_unscoped_hint_or_flag() -> None:
    for path in sorted(TEMPLATES.rglob("*.html")):
        if path.name == "_base.html":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*\.hint\s*\{", text, re.MULTILINE), path.name
        assert not re.search(r"^\s*\.flag\s*\{", text, re.MULTILINE), path.name
