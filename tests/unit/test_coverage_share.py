"""Tests for the public, unauthenticated coverage-run share link.

`app/api/coverage_share.py` deliberately has no `require_platform_admin`
(or any) auth dependency — the run id itself (a `gen_random_uuid()`, 128
bits of randomness) is the access control. These tests exercise the real
route through a `TestClient` (not a bare function call) specifically to
prove the "no login required" property behaviourally, not just by
reading the source: a request with zero auth headers/cookies must
succeed.

`_build_export_context` and the Jinja template itself are already
covered by test_coverage_export.py; here we only monkeypatch the DB-
touching helpers (`_resolve_hubs`, `runner.get_run_with_results`,
`_build_cell_trips_response`) and let the real context-building +
real template render run, so a wiring mistake between this module and
the shared export helpers would still be caught.
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

from app.api import coverage_share
from app.db import get_db
from app.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_limiter():
    """The limiter is a process-wide singleton (shared with the real app);
    without this, hit counts from other test modules could — in theory —
    push a test over the 60/minute budget."""
    limiter.reset()
    yield
    limiter.reset()


class _StubResult:
    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _StubRun:
    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _stub_run(**overrides) -> _StubRun:
    base = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "session_id": "eu11-transit-motis",
        "mode": "single_session",
        "direction": "both",
        "depart_at": datetime(2026, 6, 29, 6, 0, 0),
        "started_at": datetime(2026, 6, 29, 5, 55, 12),
        "status": "completed",
        "total_pairs": 1,
        "completed_pairs": 1,
        "ok_pairs": 1,
        "no_route_pairs": 0,
        "error_pairs": 0,
        "countries": None,
        "verify_externally": False,
        "summary": None,
    }
    base.update(overrides)
    return _StubRun(**base)


def _stub_hub_info(**overrides):
    from app.api.admin.network_coverage import HubInfo

    base = {
        "id": "p-nord",
        "name": "Paris Nord",
        "short": "P-Nord",
        "region": "ile-de-france",
        "country": "FR",
        "tier": "main",
        "lat": 48.8809,
        "lon": 2.3553,
        "is_active": True,
        "sort_order": 0,
    }
    base.update(overrides)
    return HubInfo(**base)


def _make_app(db_factory=None) -> FastAPI:
    """A minimal app carrying only the share router + the same slowapi
    wiring `app.main` sets up for real — enough for `@limiter.limit(...)`
    to actually execute, without importing the whole application (DB
    engine, sessions orchestrator, etc.).

    `db_factory` lets cell-trips tests inject a stub with a working
    `.get()`; the page tests keep the bare `object()` since they
    monkeypatch every DB-touching helper anyway."""
    app = FastAPI()
    app.state.limiter = limiter

    def _rate_limit_handler(request, exc):
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(coverage_share.router)
    app.dependency_overrides[get_db] = db_factory or (lambda: object())
    return app


def test_share_link_succeeds_with_no_auth_headers(monkeypatch):
    """The whole point of this endpoint: an anonymous request (no
    Authorization header, no session cookie) must succeed."""
    run = _stub_run()
    results = [
        _StubResult(
            origin_hub_id="p-nord",
            dest_hub_id="p-nord",
            status="ok",
            response_ms=100,
            num_itineraries=3,
            best_duration_seconds=None,
            best_num_transfers=None,
            best_operators=None,
            error_message=None,
            # A REAL search id, deliberately: with journey_search_id=None
            # the cell renders "trips": [] no matter what the route does,
            # and a regression that re-embeds trips (re-adding a
            # _fetch_trips_by_search call on this path) would go
            # undetected. With a real id, the assertion below only holds
            # while the route truly embeds nothing.
            journey_search_id=uuid.uuid4(),
            session_ids=None,
        ),
    ]
    monkeypatch.setattr(
        coverage_share.runner, "get_run_with_results", lambda db, run_id: (run, results)
    )
    monkeypatch.setattr(coverage_share, "_resolve_hubs", lambda db: [_stub_hub_info()])

    client = TestClient(_make_app())
    resp = client.get(f"/share/coverage/{run.id}")

    assert resp.status_code == 200
    assert "eu11-transit-motis" in resp.text
    # Share pages are lazy — trips are fetched per cell on click, never
    # embedded, so page size stays constant regardless of run size. The
    # cell has a search id and claims 3 itineraries, yet its embedded
    # trips must still be empty.
    assert '"lazy_trips": true' in resp.text
    assert '"trips": []' in resp.text


def test_share_link_404_for_unknown_run(monkeypatch):
    monkeypatch.setattr(
        coverage_share.runner, "get_run_with_results", lambda db, run_id: (None, [])
    )

    client = TestClient(_make_app())
    resp = client.get(f"/share/coverage/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_share_link_has_no_content_disposition_header(monkeypatch):
    """Unlike the authenticated download endpoint, this one must render
    inline — a Content-Disposition: attachment would force a download
    instead of opening in the recipient's browser."""
    run = _stub_run()
    monkeypatch.setattr(coverage_share.runner, "get_run_with_results", lambda db, run_id: (run, []))
    monkeypatch.setattr(coverage_share, "_resolve_hubs", lambda db: [_stub_hub_info()])

    client = TestClient(_make_app())
    resp = client.get(f"/share/coverage/{run.id}")

    assert resp.status_code == 200
    assert "content-disposition" not in resp.headers


def test_router_is_not_nested_under_the_admin_prefix():
    """Locks in the security design: this router must never share a
    prefix with the admin router, so a future admin-wide auth change
    can't accidentally start blocking (or a refactor accidentally start
    exposing) it."""
    assert coverage_share.router.prefix == "/share/coverage"
    assert not coverage_share.router.prefix.startswith("/api/admin")


def test_view_shared_run_has_no_current_user_dependency():
    """Structural guard: the endpoint must never gain a `CurrentUser` /
    `require_platform_admin` parameter — that would silently reintroduce
    a login requirement this route is explicitly designed not to have."""
    sig = inspect.signature(coverage_share.view_shared_run)
    assert "CurrentUser" not in str(sig)


# ─────────────────────── lazy per-cell trips endpoint ───────────────────────


class _StubDb:
    """`shared_cell_trips` only touches `db.get`; everything after the
    run lookup is delegated to `_build_cell_trips_response`, which the
    tests monkeypatch."""

    def __init__(self, run) -> None:
        self._run = run

    def get(self, _model, _run_id):
        return self._run


def test_shared_cell_trips_succeeds_with_no_auth(monkeypatch):
    """The share page's modal fetches this anonymously — same capability
    model as the page itself (the run id in the URL is the token)."""
    from app.api.admin.network_coverage import CellTripsResponse

    run = _stub_run()
    canned = CellTripsResponse(direction="both", outbound=None, return_=None)
    captured = {}

    def fake_build(db, run_arg, origin_id, dest_id):
        captured["args"] = (run_arg, origin_id, dest_id)
        return canned

    monkeypatch.setattr(coverage_share, "_build_cell_trips_response", fake_build)

    client = TestClient(_make_app(db_factory=lambda: _StubDb(run)))
    resp = client.get(f"/share/coverage/{run.id}/cells/p-nord/bxl-mid/trips")

    assert resp.status_code == 200
    assert resp.json()["direction"] == "both"
    assert captured["args"] == (run, "p-nord", "bxl-mid")


def test_shared_cell_trips_404_for_unknown_run():
    client = TestClient(_make_app(db_factory=lambda: _StubDb(None)))
    resp = client.get(f"/share/coverage/{uuid.uuid4()}/cells/a/b/trips")
    assert resp.status_code == 404


def _viator_journey() -> dict:
    from tests.unit import canonical_fixtures as fx

    return fx.journey_from_legs(
        [
            {
                "mode": "RAIL",
                "from_stop_id": "SBB:8507000:0:7",
                "to_stop_id": "SBB:8503000:0:3",
                "from_name": "Bern",
                "to_name": "Zürich HB",
                "from_lat": 46.94883,
                "from_lon": 7.43913,
                "departure": "2026-07-05T06:02:00+00:00",
                "arrival": "2026-07-05T06:58:00+00:00",
                "route_short_name": "IC 1",
            }
        ]
    ).model_dump(mode="json")


def _oebb_journey() -> dict:
    from tests.unit import canonical_fixtures as fx

    (journey,) = fx.oebb_journeys(
        [
            {
                "mode": "RAIL",
                "from_uic": "UIC:8103000",
                "to_uic": "OEBB:904050",
                "dep": "2026-07-05T08:00",
                "arr": "2026-07-05T10:30",
                "route": "RJ 1141",
                "line": "RJ",
            }
        ]
    )
    return journey.model_dump(mode="json")


def _keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for value in node for k in _keys(value)}
    return set()


# What a canonical journey carries but the share page never draws (#303 PR 4b).
_NOT_RENDERED = {
    "stop_place",
    "quay",
    "position",
    "codes",
    "uic",
    "ref",
    "unresolved_token",
    "station_id",
    "journey_ref",
    "line_ref",
    "train_number",
    "distance_meters",
    "source_mode",
    "session_id",
    "adapter_version",
}


def test_shared_cell_trips_includes_oebb_journeys_for_side_by_side(monkeypatch):
    """Deliberate product decision (2026-07-05): the share page renders
    the same VIATOR-vs-ÖBB side-by-side as the admin matrix modal, so
    the public endpoint passes the verify sweep's persisted ÖBB journeys
    through. (An earlier revision stripped them on the 'page never
    rendered it' argument — that premise no longer holds; the docstrings
    in coverage_share.py document the expanded scope.) Since #303 every
    journey goes out projected to what the page draws."""
    from app.api.admin.network_coverage import CellTripsDirection, CellTripsResponse

    run = _stub_run()
    viator, oebb = _viator_journey(), _oebb_journey()
    loaded = CellTripsResponse(
        direction="both",
        outbound=CellTripsDirection(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            trips=[{"rank": 0, "duration_seconds": 3360, "journey": viator}],
            external_journeys=[oebb],
        ),
        return_=CellTripsDirection(
            origin_hub_id="bxl-mid",
            dest_hub_id="p-nord",
            status="ok",
            external_journeys=[],
        ),
    )
    monkeypatch.setattr(
        coverage_share, "_build_cell_trips_response", lambda db, run_arg, o, d: loaded
    )

    client = TestClient(_make_app(db_factory=lambda: _StubDb(run)))
    resp = client.get(f"/share/coverage/{run.id}/cells/p-nord/bxl-mid/trips")

    assert resp.status_code == 200
    body = resp.json()
    assert body["outbound"]["external_journeys"] == [coverage_share.shared_journey(oebb)]
    assert body["return"]["external_journeys"] == []
    (trip,) = body["outbound"]["trips"]
    assert trip["journey"] == coverage_share.shared_journey(viator)
    assert not (_keys(body) & _NOT_RENDERED)


def test_the_share_projection_keeps_what_the_page_draws():
    shared = coverage_share.shared_journey(_oebb_journey())
    assert shared is not None
    (leg,) = shared["legs"]
    assert leg["kind"] == "timed"
    assert leg["origin"] == {"name": "Stop 8103000"}
    assert leg["departure"] == {"timetabled": "2026-07-05T06:00:00Z"}
    assert leg["service"]["published_name"] == "RJ 1141"
    assert leg["service"]["line"] == "RJ"
    assert leg["service"]["mode"] == "RAIL"
    assert shared["source"] == {"provider": "hafas"}
    assert shared["duration_seconds"] == 3600
    assert not (_keys(shared) & _NOT_RENDERED)


def test_the_share_projection_of_no_journey_is_none():
    assert coverage_share.shared_journey(None) is None


def test_a_large_report_trip_without_journey_stays_without():
    """A trip of a large report carries no journey (`journey: None`): nothing to project."""
    from app.api.admin.network_coverage import CellTripsDirection, CellTripsResponse

    response = CellTripsResponse(
        direction="single",
        outbound=CellTripsDirection(
            origin_hub_id="p-nord",
            dest_hub_id="bxl-mid",
            status="ok",
            trips=[{"rank": 0, "duration_seconds": 1, "journey": None}],
            external_journeys=None,
        ),
    )
    shared = coverage_share._shared_response(response)
    assert shared.outbound is not None
    assert shared.outbound.trips == [{"rank": 0, "duration_seconds": 1, "journey": None}]
    assert shared.outbound.external_journeys is None
    assert shared.return_ is None


def test_shared_cell_trips_has_no_current_user_dependency():
    """Same structural guard as the page route — the modal's fetch has
    no way to attach admin credentials, so accidentally inheriting auth
    here would break every share link's drill-down silently."""
    sig = inspect.signature(coverage_share.shared_cell_trips)
    assert "CurrentUser" not in str(sig)


# ─────────────────────── "Copy share link" button (sidebar JS) ───────────────────────

_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "network_coverage.html"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_share_button_present_next_to_download_link(template_text: str):
    assert 'id="cov-share-btn"' in template_text
    assert "data-run-id=" in template_text


def test_share_button_builds_the_public_share_url(template_text: str):
    click_handler = re.search(
        r"shareBtn\.addEventListener\('click', async \(\) => \{.*?\n  \}\);",
        template_text,
        re.DOTALL,
    )
    assert click_handler, "share-button click handler not found"
    body = click_handler.group(0)
    assert "/share/coverage/" in body
    assert "navigator.clipboard.writeText" in body
