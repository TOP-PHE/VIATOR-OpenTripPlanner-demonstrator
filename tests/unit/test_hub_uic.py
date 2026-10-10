"""Unit tests for the coverage hubs' station codes (MSMM step 3).

The three admin routes of app/api/admin/network_coverage.py — POST
/hubs/resolve, /hubs/confirm and /hubs/check — and the code an
administrator types in the hub form. Driven through the real application
with a JWT cookie; no database: the two functions that read the hubs are
replaced by stand-ins over transient `NetworkCoverageHub` objects, and the
session by one that counts its commits (the statements themselves are
proved on PostgreSQL in tests/integration/test_hub_uic_selection.py). The
module is never reached: its calls go through `httpx.MockTransport`.
Invented values only: ZZ names, codes beginning with 99, user ids and the
token drawn at run time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import station_module
from app.api.admin import network_coverage
from app.api.admin.network_coverage import HubCreate, HubUpdate, create_hub, update_hub
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.models import NetworkCoverageHub
from app.settings import settings

BASE = "/api/admin/network-coverage/hubs"
MODULE_URL = "http://msmm.invalid:8000"
REPO = Path(__file__).resolve().parents[2]


def _hub(index: int, *, uic: str | None = None, origin: str | None = None, **extra: Any):
    # Two words, the first too short to be searched alone: the name has no
    # shortened form, so a test makes the shortening happen only by naming
    # its hub otherwise.
    values: dict[str, Any] = {
        "id": f"zz-hub-{index:02d}",
        "name": f"ZZ Hub{index:02d}",
        "short": f"ZZ{index:02d}",
        "country": "ZZ",
        "tier": "main",
        "lat": 45.0 + index / 100,
        "lon": 6.0,
        "is_active": True,
        "sort_order": 100,
        "uic": uic,
        "uic_origin": origin,
        **extra,
    }
    return NetworkCoverageHub(**values)


def _station(name: str, uic: str, lat: float, lon: float, parent: str | None = None):
    return {
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "country_iso": "ZZ",
        "uic": uic,
        "parent_uic": parent,
    }


class FakeDb:
    """Stands in for the session: counts commits, refuses any statement."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no statement expected: the hubs come from the stand-ins")


class Hubs:
    """The hubs the routes see: replaces `_hubs_to_look_at` and `_hubs_by_id`."""

    def __init__(self) -> None:
        self.rows: list[NetworkCoverageHub] = []
        self.skips: list[list[str]] = []

    def to_look_at(self, _db: Any, *, resolved: bool, skip: list[str]) -> list[NetworkCoverageHub]:
        self.skips.append(list(skip))
        return [h for h in self.rows if (h.uic is not None) == resolved and h.id not in set(skip)]

    def by_id(self, _db: Any, ids: list[str]) -> dict[str, NetworkCoverageHub]:
        return {h.id: h for h in self.rows if h.id in ids and h.is_active}


def _near(name: str, uic: str, distance: int, parent: str | None = None) -> dict[str, Any]:
    """A row of a near answer: a station at `distance` metres of the hub."""
    return {**_station(name, uic, 45.0, 6.0, parent), "distance_m": distance}


def _near_answer(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"stations": list(rows)})


class Module:
    """The module: `near` answers by the body, `search` by the text (the
    resolve route must never call it), `lookup` from `served`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.near: Callable[[dict[str, Any]], httpx.Response] = lambda _b: _near_answer()
        self.search: Callable[[str], httpx.Response] = lambda _q: httpx.Response(
            200, json={"stations": []}
        )
        self.served: dict[str, dict[str, Any]] = {}
        self.lookup_response: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/stations/near"):
            return self.near(body)
        if request.url.path.endswith("/stations/search"):
            return self.search(body["q"])
        if self.lookup_response is not None:
            return self.lookup_response
        return httpx.Response(
            200, json={"stations": [self.served[c] for c in body["uics"] if c in self.served]}
        )

    def of(self, kind: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(f"/stations/{kind}")]


@pytest.fixture
def hubs(monkeypatch: pytest.MonkeyPatch) -> Hubs:
    stand_in = Hubs()
    monkeypatch.setattr(network_coverage, "_hubs_to_look_at", stand_in.to_look_at)
    monkeypatch.setattr(network_coverage, "_hubs_by_id", stand_in.by_id)
    return stand_in


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Module]:
    stand_in = Module()
    transport = httpx.MockTransport(stand_in)
    real_async = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))
    station_module.reset()
    yield stand_in
    station_module.reset()


@pytest.fixture
def db() -> Iterator[FakeDb]:
    fake = FakeDb()
    app.dependency_overrides[get_db] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def admin() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def client(db: FakeDb, admin: uuid.UUID) -> TestClient:
    test_client = TestClient(app)
    test_client.cookies.set(settings.jwt_cookie_name, _jwt(admin, "platform_admin"))
    return test_client


def _jwt(user_id: uuid.UUID, role: str) -> str:
    return tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)


def _near_positions(module: Module) -> list[tuple[float, float]]:
    return [(json.loads(r.content)["lat"], json.loads(r.content)["lon"]) for r in module.of("near")]


def _searched(module: Module) -> list[str]:
    return [json.loads(r.content)["q"] for r in module.of("search")]


def _looked_up(module: Module) -> list[list[str]]:
    return [json.loads(r.content)["uics"] for r in module.of("lookup")]


def _limited(retry_after: str | None = "17", code: str = "user_minute") -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(429, json={"detail": "ZZ", "code": code}, headers=headers)


# ───────────────────────────── resolve ─────────────────────────────


def test_one_station_near_the_hub_is_proposed_and_nothing_is_written(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, admin: uuid.UUID
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    module.near = lambda _b: _near_answer(_near("ZZ Central", "9900001", 120))

    answer = client.post(f"{BASE}/resolve", json={})

    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "ok"
    assert body["left"] == 0
    (proposal,) = body["proposals"]
    assert proposal["hub_id"] == hub.id
    assert proposal["state"] == "proposed"
    assert proposal["candidates"] == [
        {
            "name": "ZZ Central",
            "uic": "9900001",
            "country_iso": "ZZ",
            "distance_m": 120,
            "found_by": "position",
            "warning": None,
            "shortened_name": None,
        }
    ]
    assert proposal["name_searched"] is False
    # One near call: the hub's position and the 300 m radius, on behalf of
    # the administrator; no search by the hub's name, since it found one.
    (request,) = module.of("near")
    assert json.loads(request.content) == {"lat": hub.lat, "lon": hub.lon, "radius_m": 300}
    assert request.headers["x-viator-user-id"] == str(admin)
    assert module.of("search") == []
    # Nothing written: no commit, the hub still unresolved, no lookup made.
    assert db.commits == 0
    assert hub.uic is None
    assert hub.uic_origin is None
    assert module.of("lookup") == []


def test_several_stations_near_the_hub_are_to_pick_nearest_first(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.near = lambda _b: _near_answer(
        _near("ZZ North", "9900011", 290), _near("ZZ South", "9900012", 40)
    )

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    assert [(c["uic"], c["distance_m"]) for c in proposal["candidates"]] == [
        ("9900012", 40),
        ("9900011", 290),
    ]


def test_five_stations_near_the_hub_are_all_listed(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    rows = [_near(f"ZZ Stop {i}", f"990002{i}", 10 * i) for i in range(5)]
    module.near = lambda _b: _near_answer(*rows)

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    assert [c["uic"] for c in proposal["candidates"]] == [r["uic"] for r in rows]


def test_no_station_near_the_hub_nor_by_name_is_to_pick_with_none(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal == {
        "hub_id": hub.id,
        "hub_name": hub.name,
        "state": "to_pick",
        "candidates": [],
        "name_searched": True,
        "far_dropped": 0,
        "shortened_searches": 0,
    }
    assert len(module.of("near")) == 1
    assert _searched(module) == [hub.name]


def test_a_candidate_whose_code_the_lookup_would_refuse_is_not_offered(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.near = lambda _b: _near_answer(
        _near("ZZ Short Code", "ZZ", 5), _near("ZZ Good Code", "9900002", 40)
    )

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert [c["uic"] for c in proposal["candidates"]] == ["9900002"]
    assert module.of("search") == []


def test_a_near_answer_with_only_refused_codes_falls_back_to_the_name(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    """No usable candidate by position counts as none: the name is searched."""
    hub = _sea_hub()
    hubs.rows = [hub]
    module.near = lambda _b: _near_answer(_near("ZZ Short Code", "ZZ", 5))
    _found(module, _north("ZZ By Name", "9900003", 0.0018))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert _searched(module) == [hub.name]
    assert proposal["name_searched"] is True
    assert [(c["uic"], c["found_by"]) for c in proposal["candidates"]] == [("9900003", "name")]


def test_one_click_makes_exactly_ten_near_calls_one_after_the_other(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]
    module.near = lambda _b: _near_answer(_near("ZZ Central", "9900001", 120))

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert network_coverage.RESOLVE_BATCH == 10
    assert len(module.of("near")) == 10
    assert _near_positions(module) == [(h.lat, h.lon) for h in hubs.rows[:10]]
    assert module.of("search") == []
    assert [p["hub_id"] for p in body["proposals"]] == [f"zz-hub-{i:02d}" for i in range(10)]
    assert body["left"] == 15
    assert body["status"] == "ok"


def test_the_next_click_skips_the_hubs_already_shown(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]
    shown = [f"zz-hub-{i:02d}" for i in range(10)]
    module.near = lambda _b: _near_answer(_near("ZZ Central", "9900001", 120))

    body = client.post(f"{BASE}/resolve", json={"skip": shown}).json()

    assert hubs.skips == [shown]
    assert _near_positions(module) == [(h.lat, h.lon) for h in hubs.rows[10:20]]
    assert body["left"] == 5


@pytest.mark.parametrize(
    ("code", "near_limited", "said"),
    [
        (
            "near_user_minute",
            True,
            "Your limit of proposals a minute is reached; try again in 17 seconds.",
        ),
        (
            "near_user_day",
            True,
            "Your daily limit of proposals is reached; try again tomorrow (UTC).",
        ),
        (
            "near_all_day",
            True,
            "shared by every administrator is reached; try again tomorrow (UTC).",
        ),
        ("user_minute", False, "The station module's limit is reached; try again in 17 seconds."),
        ("all_day", False, "The station module's limit is reached; try again in 17 seconds."),
    ],
)
def test_a_429_on_the_fourth_near_call_stops_the_click_and_keeps_what_was_found(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    code: str,
    near_limited: bool,
    said: str,
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]
    answers = iter(
        [_near_answer(_near("ZZ One", "9900031", 15))] * 3
        + [_limited("17", code)]
        + [_near_answer()] * 30
    )
    module.near = lambda _b: next(answers)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 4  # no further call, no retry
    assert module.of("search") == []  # a near 429 is no reason to search by name
    assert [p["state"] for p in body["proposals"]] == ["proposed"] * 3
    assert body["status"] == "limited"
    assert body["retry_after"] == 17
    assert said in body["message"]
    # A near window refuses the near call alone: the page keeps Save and Check.
    assert body["near_limited"] is near_limited
    assert ("Saving and checking codes still work." in body["message"]) is near_limited
    assert body["left"] == 22  # the fourth hub was not looked at
    assert not station_module.paused()  # a 429 starts no pause
    assert db.commits == 0
    assert all(h.uic is None for h in hubs.rows)


def test_a_429_without_retry_after_says_a_minute(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.near = lambda _b: _limited(None, "near_user_minute")

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["status"] == "limited"
    assert body["retry_after"] is None
    assert "a minute" in body["message"]


def test_a_404_says_the_module_is_too_old_for_the_near_call(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(3)]
    module.near = lambda _b: httpx.Response(404, json={"detail": "ZZ", "code": "not_found"})

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 1
    assert module.of("search") == []
    assert body["status"] == "unavailable"
    assert "needs MSMM v0.3.4 or later" in body["message"]
    assert body["near_limited"] is False
    assert body["proposals"] == []
    assert not station_module.paused()
    assert db.commits == 0


@pytest.mark.parametrize("status", [500, 503, 422])
def test_another_near_failure_stops_the_click_says_so_and_starts_no_pause(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, status: int
) -> None:
    hubs.rows = [_hub(i) for i in range(5)]
    answers = iter([_near_answer(), httpx.Response(status, json={})])
    module.near = lambda _b: next(answers)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 2
    # The first hub (no station around it) was searched by name; the second,
    # whose near call failed, was not.
    assert _searched(module) == [hubs.rows[0].name]
    assert body["status"] == "unavailable"
    assert body["message"] == "The station module did not answer; nothing was changed."
    assert body["retry_after"] is None
    assert len(body["proposals"]) == 1
    assert body["left"] == 4
    assert db.commits == 0
    # A near failure leaves the typeahead alone: no pause.
    assert not station_module.paused()


def _raise(error: type[httpx.HTTPError]) -> Callable[[dict[str, Any]], httpx.Response]:
    def answer(_body: dict[str, Any]) -> httpx.Response:
        raise error("stand-in failure")

    return answer


@pytest.mark.parametrize(
    ("near", "reason"),
    [
        (_raise(httpx.ReadTimeout), "timeout"),
        (_raise(httpx.ConnectError), "network"),
        (lambda _b: httpx.Response(200, text="<html>ZZ</html>"), "shape"),
        (lambda _b: httpx.Response(200, json={"stations": {"name": "ZZ"}}), "shape"),
        (lambda _b: httpx.Response(503, json={"detail": "ZZ", "code": "busy"}), "busy"),
    ],
    ids=["timeout", "network", "not-json", "wrong-shape", "busy"],
)
def test_a_near_failure_without_an_error_status_stops_the_click_without_a_search(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    caplog: pytest.LogCaptureFixture,
    near: Callable[[dict[str, Any]], httpx.Response],
    reason: str,
) -> None:
    hubs.rows = [_sea_hub(i) for i in range(3)]
    module.near = near

    with caplog.at_level("INFO"):
        body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 1
    assert module.of("search") == []
    assert body["status"] == "unavailable"
    assert body["message"] == "The station module did not answer; nothing was changed."
    assert body["proposals"] == []
    assert body["left"] == 3
    assert f"station_module.near_failed reason={reason}" in caplog.text
    assert not station_module.paused()
    assert db.commits == 0


@pytest.mark.parametrize(
    "position",
    [
        {"lat": None},
        {"lon": None},
        {"lat": math.nan},
        {"lon": math.inf},
        {"lat": 90.5},
        {"lon": -180.5},
    ],
    ids=["no-latitude", "no-longitude", "nan", "infinite", "latitude-out", "longitude-out"],
)
def test_a_hub_without_a_usable_position_gets_no_near_call_but_a_name_search(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, position: dict[str, Any]
) -> None:
    hubs.rows = [_hub(1, **position), _hub(2)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["status"] == "ok"
    assert [(p["hub_id"], p["state"]) for p in body["proposals"]] == [
        ("zz-hub-01", "no_position"),
        ("zz-hub-02", "to_pick"),
    ]
    assert body["proposals"][0]["candidates"] == []
    assert body["proposals"][0]["name_searched"] is True
    assert _near_positions(module) == [(hubs.rows[1].lat, hubs.rows[1].lon)]
    assert _searched(module) == ["ZZ Hub01", "ZZ Hub02"]
    assert body["left"] == 0
    assert db.commits == 0


def test_a_hub_at_the_edges_of_the_ranges_is_looked_up(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, lat=-90.0, lon=180.0), _hub(2, lat=0, lon=0)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert [p["state"] for p in body["proposals"]] == ["to_pick", "to_pick"]
    assert _near_positions(module) == [(-90.0, 180.0), (0.0, 0.0)]


def test_the_resolve_route_uses_the_admin_search_never_the_typeaheads() -> None:
    """The name fallback uses the search that never pauses the typeahead of
    every user, and the near call stays the first call."""
    source = inspect.getsource(network_coverage._look_at)
    by_name = inspect.getsource(network_coverage._look_up_by_name)
    assert source.index("near_outcome") < source.index("_look_up_by_name")
    assert "admin_search_outcome" in by_name
    for module_source in (source, by_name):
        assert "station_module.search_outcome" not in module_source
        assert "station_module.search(" not in module_source
    assert network_coverage.PROPOSAL_RADIUS_M == 300
    assert isinstance(network_coverage.PROPOSAL_RADIUS_M, int)


def test_a_hub_with_a_name_the_search_could_not_take_is_still_looked_up(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, name="ZZ")]
    module.near = lambda _b: _near_answer(_near("ZZ Long Station Name", "9900041", 3))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "proposed"
    assert module.of("search") == []


@pytest.mark.parametrize(
    ("name", "located"),
    [("ZZ", True), ("Z" * 101, True), ("ZZ", False)],
    ids=["short", "long", "short-no-position"],
)
def test_a_name_the_search_would_refuse_is_not_sent(
    client: TestClient, hubs: Hubs, module: Module, name: str, located: bool
) -> None:
    hubs.rows = [_hub(1, name=name, **({} if located else {"lat": math.nan}))]

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert module.of("search") == []
    assert proposal["state"] == ("to_pick" if located else "no_position")
    assert proposal["candidates"] == []
    assert proposal["name_searched"] is False


# ─────────────────────── resolve: the name fallback ───────────────────────
#
# A hub in the open ocean (invented), and stations north of it: 0.001° of
# latitude is about 111 m.
_SEA_LAT = -48.0
_SEA_LON = -123.0


def _sea_hub(index: int = 1, **extra: Any) -> NetworkCoverageHub:
    return _hub(index, lat=_SEA_LAT, lon=_SEA_LON, **extra)


def _north(name: str, uic: str, degrees: float) -> dict[str, Any]:
    """A search row `degrees` of latitude north of the sea hub."""
    return _station(name, uic, _SEA_LAT + degrees, _SEA_LON)


def _found(module: Module, *rows: dict[str, Any]) -> None:
    module.search = lambda _q: httpx.Response(200, json={"stations": list(rows)})


def test_no_station_near_the_hub_falls_back_to_its_name_after_the_near_call(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, admin: uuid.UUID
) -> None:
    hub = _sea_hub(name="  ZZ   Sea  Hub ")
    hubs.rows = [hub]
    _found(module, _north("ZZ Sea Hub Station", "9900051", 0.0018))

    body = client.post(f"{BASE}/resolve", json={}).json()

    # The near call first, then one search with the hub's name, normalised
    # as the module's search takes it, on behalf of the administrator.
    assert [r.url.path.rsplit("/", 1)[1] for r in module.requests] == ["near", "search"]
    (request,) = module.of("search")
    assert json.loads(request.content) == {"q": "ZZ Sea Hub"}
    assert request.headers["x-viator-user-id"] == str(admin)
    (proposal,) = body["proposals"]
    assert body["status"] == "ok"
    assert proposal["name_searched"] is True
    # Within 300 m (about 200 m): no warning, but never proposed, even
    # alone: a result of the name search is always to pick (owner rule).
    assert proposal["state"] == "to_pick"
    (candidate,) = proposal["candidates"]
    assert candidate["found_by"] == "name"
    assert candidate["warning"] is None
    assert 195 <= candidate["distance_m"] <= 205
    # Nothing written.
    assert db.commits == 0
    assert hub.uic is None
    assert hub.uic_origin is None
    assert module.of("lookup") == []


def test_name_results_are_ordered_by_distance_and_those_beyond_300_m_carry_a_warning(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub()]
    _found(
        module,
        _north("ZZ Far", "9900061", 0.0108),  # about 1.2 km
        _north("ZZ Near", "9900062", 0.0018),  # about 200 m
        _north("ZZ Middle", "9900063", 0.0045),  # about 500 m
    )

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    assert [c["uic"] for c in proposal["candidates"]] == ["9900062", "9900063", "9900061"]
    near, middle, far = proposal["candidates"]
    assert near["warning"] is None
    assert middle["warning"] == (
        "Found by name, 501 m from the hub's position — check before saving."
    )
    assert far["warning"] == (
        "Found by name, 1.2 km from the hub's position — check before saving."
    )
    assert all(c["found_by"] == "name" for c in proposal["candidates"])


@pytest.mark.parametrize("degrees", [0.0028, 0.0108, 0.4], ids=["311m", "1.2km", "44km"])
def test_a_single_name_result_beyond_300_m_is_to_pick_never_proposed(
    client: TestClient, hubs: Hubs, module: Module, degrees: float
) -> None:
    hubs.rows = [_sea_hub()]
    _found(module, _north("ZZ Lonely", "9900071", degrees))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    (candidate,) = proposal["candidates"]
    assert candidate["distance_m"] > 300
    assert "check before saving" in candidate["warning"]


@pytest.mark.parametrize(
    ("distance", "warned", "shown"),
    [(300.0, False, 300), (300.0001, True, 301), (299.6, False, 300)],
    ids=["300.0", "300.0001", "299.6"],
)
def test_the_300_m_rule_compares_the_unrounded_distance(
    monkeypatch: pytest.MonkeyPatch, distance: float, warned: bool, shown: int
) -> None:
    monkeypatch.setattr(network_coverage, "_distance_m", lambda *_a: distance)
    row = _north("ZZ Edge", "9900072", 0.0027)

    candidates, far = network_coverage._name_candidates(_sea_hub(), [row], located=True)

    assert far == set()
    (candidate,) = candidates
    assert (candidate.warning is not None) is warned
    assert candidate.distance_m == shown


def test_a_name_result_at_300_4_m_is_beyond_300_m(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    """The module's near call keeps a station at an exact distance of 300.0 m
    or less, so one at 300.4 m reaches the name search: VIATOR must not round
    it back to 300 m and drop the warning. Measured, not patched."""
    hubs.rows = [_sea_hub()]
    degrees = 300.4 / (network_coverage._EARTH_RADIUS_M * math.pi / 180)
    _found(module, _north("ZZ Just Beyond", "9900073", degrees))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    (candidate,) = proposal["candidates"]
    assert proposal["state"] == "to_pick"
    assert candidate["distance_m"] == 301
    assert candidate["warning"] == (
        "Found by name, 301 m from the hub's position — check before saving."
    )


@pytest.mark.parametrize(
    ("distance", "dropped"),
    [(50_000.0, False), (50_000.4, True), (50_001.0, True), (49_999.6, False)],
)
def test_the_50_km_rule_drops_only_what_is_farther(
    monkeypatch: pytest.MonkeyPatch, distance: float, dropped: bool
) -> None:
    assert network_coverage.NAME_FALLBACK_MAX_DISTANCE_M == 50_000
    monkeypatch.setattr(network_coverage, "_distance_m", lambda *_a: distance)

    candidates, far = network_coverage._name_candidates(
        _sea_hub(), [_north("ZZ Edge", "9900074", 0.45)], located=True
    )

    assert far == ({"9900074"} if dropped else set())
    assert len(candidates) == (0 if dropped else 1)


def test_a_name_result_without_a_usable_position_is_shown_last_without_distance(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub()]
    _found(
        module,
        _station("ZZ Nowhere", "9900081", 95.0, _SEA_LON),  # latitude out of range
        _north("ZZ Somewhere", "9900082", 0.0108),
    )

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    somewhere, nowhere = proposal["candidates"]
    assert somewhere["uic"] == "9900082"
    assert nowhere["uic"] == "9900081"
    assert nowhere["distance_m"] is None
    assert nowhere["warning"] == (
        "Found by name; the station module gives no usable position for it, so its "
        "distance from the hub is unknown — check before saving."
    )


def test_a_single_name_result_without_position_is_not_proposed(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub()]
    _found(module, _station("ZZ Nowhere", "9900083", _SEA_LAT, 181.0))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    assert proposal["candidates"][0]["distance_m"] is None


def test_name_results_beyond_50_km_are_dropped_and_counted(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub(), _sea_hub(2)]
    answers = iter(
        [
            # 0.5° is about 56 km; 0.449° about 49.9 km.
            [_north("ZZ Namesake", "9900091", 0.5), _north("ZZ Kept", "9900092", 0.449)],
            [_north("ZZ Namesake", "9900093", 0.5), _north("ZZ Other", "9900094", -2.0)],
        ]
    )
    module.search = lambda _q: httpx.Response(200, json={"stations": next(answers)})

    first, second = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert [c["uic"] for c in first["candidates"]] == ["9900092"]
    assert "49.9 km" in first["candidates"][0]["warning"]
    assert first["far_dropped"] == 1
    assert first["state"] == "to_pick"
    assert second["candidates"] == []
    assert second["far_dropped"] == 2
    assert second["state"] == "to_pick"


def test_at_most_five_name_results_are_shown_the_nearest(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub()]
    rows = [_north(f"ZZ Stop {i}", f"990010{i}", 0.01 * (8 - i)) for i in range(8)]
    _found(module, *rows)

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert network_coverage.NAME_FALLBACK_MAX == 5
    assert [c["uic"] for c in proposal["candidates"]] == [f"990010{i}" for i in (7, 6, 5, 4, 3)]
    assert proposal["far_dropped"] == 0


def test_a_name_result_with_a_refused_or_repeated_code_is_not_offered(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub()]
    _found(
        module,
        _north("ZZ Short Code", "ZZ", 0.001),
        _north("ZZ Twice", "9900111", 0.002),
        _north("ZZ Twice Again", "9900111", 0.0005),
    )

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert [(c["name"], c["uic"]) for c in proposal["candidates"]] == [("ZZ Twice", "9900111")]
    assert proposal["state"] == "to_pick"


def test_a_hub_without_position_lists_its_name_results_with_the_no_position_warning(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, lat=math.nan)]
    _found(module, _north("ZZ One", "9900121", 0.0), _north("ZZ Two", "9900122", 3.0))

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert module.of("near") == []
    assert _searched(module) == ["ZZ Hub01"]
    assert proposal["state"] == "no_position"
    assert proposal["name_searched"] is True
    # No distance can be told: none dropped, each in the module's order,
    # each with the warning, never proposed.
    assert proposal["far_dropped"] == 0
    assert [c["uic"] for c in proposal["candidates"]] == ["9900121", "9900122"]
    for candidate in proposal["candidates"]:
        assert candidate["distance_m"] is None
        assert candidate["found_by"] == "name"
        assert candidate["warning"] == (
            "Found by name; this hub has no usable position, so the distance is unknown "
            "— check before saving."
        )
    assert db.commits == 0


def test_a_click_whose_full_names_are_searched_looks_at_six_hubs(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    # Names with no shortened form: 2 calls a hub (near + full name). The
    # budget of 15 lets a hub start while 10 calls or fewer are made: 6
    # hubs, 12 calls (before the shortening: 10 hubs, 20 calls).
    hubs.rows = [_hub(i) for i in range(25)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 6
    assert _searched(module) == [h.name for h in hubs.rows[:6]]
    assert body["left"] == 19
    assert body["status"] == "ok"


@pytest.mark.parametrize(
    ("code", "retry", "said"),
    [
        ("user_minute", "23", "try again in 23 seconds"),
        ("all_day", None, "try again in a minute"),
    ],
)
def test_a_429_on_the_name_search_stops_the_click_and_keeps_what_was_found(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    code: str,
    retry: str | None,
    said: str,
) -> None:
    hubs.rows = [_hub(i) for i in range(6)]
    nears = iter(
        [_near_answer(_near("ZZ One", "9900131", 15)), _near_answer()] + [_near_answer()] * 9
    )
    module.near = lambda _b: next(nears)
    module.search = lambda _q: _limited(retry, code)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 2
    assert len(module.of("search")) == 1  # no retry, no further hub
    assert body["status"] == "limited"
    assert body["retry_after"] == (int(retry) if retry else None)
    assert said in body["message"]
    # A limit of every call: the page holds every button, not only Propose.
    assert body["near_limited"] is False
    assert [p["hub_id"] for p in body["proposals"]] == ["zz-hub-00"]
    assert body["left"] == 5  # the second hub will be looked at again
    assert not station_module.paused()
    assert db.commits == 0


@pytest.mark.parametrize("status", [500, 503, 404])
def test_another_name_search_failure_stops_the_click_without_a_pause(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    hubs.rows = [_hub(i, name=f"ZZ Secret Hub {i}") for i in range(3)]
    module.search = lambda _q: httpx.Response(status, json={"detail": "ZZ", "code": "zz"})

    with caplog.at_level("DEBUG"):
        body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("near")) == 1
    assert len(module.of("search")) == 1
    assert body["status"] == "unavailable"
    assert body["message"] == "The station module did not answer; nothing was changed."
    assert body["proposals"] == []
    assert body["left"] == 3
    assert body["near_limited"] is False
    # An admin search never pauses the typeahead, and never logs the name.
    assert not station_module.paused()
    _assert_logs_hold_none_of(caplog, "Secret", *_position_texts(hubs.rows[0]))
    assert db.commits == 0


def _position_texts(hub: NetworkCoverageHub) -> list[str]:
    return [str(hub.lat), str(hub.lon)]


def _assert_logs_hold_none_of(caplog: pytest.LogCaptureFixture, *texts: str) -> None:
    """No log record (message, arguments or exception) holds any of `texts`."""
    for record in caplog.records:
        logged = record.getMessage() + repr(record.args) + str(record.exc_info or "")
        for text in texts:
            assert text not in logged, record.name


_SECRET_LAT = -47.987654
_SECRET_LON = -122.876543


@pytest.mark.parametrize(
    "path", ["success", "near-429", "search-429", "shortened", "shortened-429", "shortened-none"]
)
def test_the_resolve_logs_hold_no_name_no_position_and_no_code(
    client: TestClient, hubs: Hubs, module: Module, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    hub = _hub(1, name="Zzhidden Hubname-Zzsecret", lat=_SECRET_LAT, lon=_SECRET_LON)
    hubs.rows = [hub]
    station = _station("ZZ Hidden Station", "9900987", _SECRET_LAT + 0.01, _SECRET_LON)
    if path == "near-429":
        module.near = lambda _b: _limited("17", "near_user_minute")
    elif path == "search-429":
        module.search = lambda _q: _limited("17", "user_minute")
    elif path == "shortened":
        _answers(module, {"Zzhidden": [station]})
    elif path == "shortened-429":
        module.search = lambda q: (
            _limited("17", "user_minute")
            if q == "Zzhidden"
            else httpx.Response(200, json={"stations": []})
        )
    elif path == "shortened-none":
        _answers(module, {})
    else:
        _found(module, station)

    with caplog.at_level("DEBUG"):
        body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["status"] == ("limited" if path.endswith("429") else "ok")
    if path in ("success", "shortened"):
        assert body["proposals"][0]["candidates"][0]["uic"] == "9900987"
    if path.startswith("shortened"):
        assert len(module.of("search")) >= 3
    assert caplog.records  # the calls were logged (httpx), without the values
    _assert_logs_hold_none_of(
        caplog,
        "Hidden",
        "Zzhidden",
        "Hubname",
        "Zzsecret",
        "9900987",
        str(_SECRET_LAT),
        str(_SECRET_LON),
        str(_SECRET_LAT + 0.01),
        "47.98",
        "122.87",
    )


def test_the_distance_is_an_unrounded_haversine() -> None:
    distance = network_coverage._distance_m
    assert distance(0.0, 0.0, 0.0, 0.0) == 0
    # One degree of a great circle on a sphere of 6,371 km: 111,194.93 m.
    assert distance(0.0, 0.0, 1.0, 0.0) == pytest.approx(111_194.93, abs=0.01)
    assert distance(0.0, 179.5, 0.0, -179.5) == pytest.approx(111_194.93, abs=0.01)
    assert distance(-90.0, 0.0, 90.0, 0.0) == pytest.approx(math.pi * 6_371_000, abs=0.01)
    assert distance(_SEA_LAT, _SEA_LON, _SEA_LAT, _SEA_LON + 1) == pytest.approx(74_403, abs=1)
    assert isinstance(distance(0.0, 0.0, 0.0027, 0.0), float)
    assert network_coverage._shown_metres(300.0) == 300
    assert network_coverage._shown_metres(300.01) == 301


@pytest.mark.parametrize(
    ("metres", "text"),
    [(0, "0 m"), (999, "999 m"), (1000, "1.0 km"), (1260, "1.3 km"), (49_900, "49.9 km")],
)
def test_the_distance_text_of_a_warning(metres: int, text: str) -> None:
    assert network_coverage._distance_text(metres) == text


# ───────────────────── resolve: the name shortening ─────────────────────
#
# When the full name keeps no candidate, the name is searched again without
# its last word, then its last two, down to its first word (owner decision
# of 10 Oct), at most three times. Invented names whose words are split at
# spaces, hyphens and apostrophes.
_LONG_NAME = "Zzville Zzsaint-Zzcharles"


def _answers(module: Module, by_query: dict[str, list[dict[str, Any]]]) -> None:
    """The search answers each text from `by_query`, nothing for any other."""
    module.search = lambda q: httpx.Response(200, json={"stations": by_query.get(q, [])})


def _shortened_warning(text: str, distance: str) -> str:
    return (
        f"Found by the shortened name «{text}», a looser match, {distance} from the "
        "hub's position — check before saving."
    )


def test_a_full_name_that_finds_a_station_is_not_shortened(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub(name=_LONG_NAME)]
    _answers(module, {_LONG_NAME: [_north("ZZ Found", "9900201", 0.0108)]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert _searched(module) == [_LONG_NAME]
    assert proposal["shortened_searches"] == 0
    (candidate,) = proposal["candidates"]
    assert candidate["shortened_name"] is None
    assert candidate["warning"] == (
        "Found by name, 1.2 km from the hub's position — check before saving."
    )


def test_a_full_name_that_keeps_nothing_is_shortened_word_by_word(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, admin: uuid.UUID
) -> None:
    hub = _sea_hub(name=_LONG_NAME)
    hubs.rows = [hub]
    _answers(
        module,
        {
            "Zzville": [
                _north("ZZ Faraway", "9900211", 0.5),  # about 56 km: dropped
                _station("ZZ Lost", "9900212", 95.0, _SEA_LON),  # no usable position
                _north("ZZ Beyond", "9900213", 0.0108),  # about 1.2 km
                _north("ZZ Close", "9900214", 0.0018),  # about 200 m
            ]
        },
    )

    body = client.post(f"{BASE}/resolve", json={}).json()

    # The near call, the full name, then the name without its last word
    # (split at the hyphen), then its first word alone, which keeps some.
    assert [r.url.path.rsplit("/", 1)[1] for r in module.requests] == [
        "near",
        "search",
        "search",
        "search",
    ]
    assert _searched(module) == [_LONG_NAME, "Zzville Zzsaint", "Zzville"]
    assert all(r.headers["x-viator-user-id"] == str(admin) for r in module.of("search"))
    (proposal,) = body["proposals"]
    assert body["status"] == "ok"
    assert proposal["state"] == "to_pick"  # never proposed
    assert proposal["name_searched"] is True
    assert proposal["shortened_searches"] == 2
    assert proposal["far_dropped"] == 1
    close, beyond, lost = proposal["candidates"]
    assert [c["uic"] for c in (close, beyond, lost)] == ["9900214", "9900213", "9900212"]
    for candidate in (close, beyond, lost):
        assert candidate["found_by"] == "name"
        assert candidate["shortened_name"] == "Zzville"
    # Within 300 m: no warning (the page still says which text found it).
    assert close["warning"] is None
    assert beyond["warning"] == _shortened_warning("Zzville", "1.2 km")
    assert lost["distance_m"] is None
    assert lost["warning"] == (
        "Found by the shortened name «Zzville», a looser match; the station module "
        "gives no usable position for it, so its distance from the hub is unknown "
        "— check before saving."
    )
    assert db.commits == 0
    assert hub.uic is None
    assert module.of("lookup") == []


def test_the_shortening_stops_at_the_first_form_that_keeps_a_candidate(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub(name="Zzaa Zzbb Zzcc Zzdd Zzee")]
    _answers(module, {"Zzaa Zzbb Zzcc": [_north("ZZ Middle", "9900221", 0.0045)]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert _searched(module) == [
        "Zzaa Zzbb Zzcc Zzdd Zzee",
        "Zzaa Zzbb Zzcc Zzdd",
        "Zzaa Zzbb Zzcc",
    ]
    assert proposal["shortened_searches"] == 2
    (candidate,) = proposal["candidates"]
    assert candidate["warning"] == _shortened_warning("Zzaa Zzbb Zzcc", "501 m")


def test_a_form_that_finds_only_stations_beyond_50_km_does_not_stop_the_shortening(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub(name=_LONG_NAME), _sea_hub(2, name=_LONG_NAME)]
    namesake = _north("ZZ Namesake", "9900231", 0.5)
    other = _north("ZZ Other Namesake", "9900232", -0.6)
    answers = iter(
        [
            # The first hub: far stations only, then one within reach.
            [namesake],
            [namesake, other],
            [namesake, _north("ZZ Kept", "9900233", 0.0108)],
            # The second hub: far stations only, every time.
            [namesake],
            [other, namesake],
            [namesake],
        ]
    )
    module.search = lambda _q: httpx.Response(200, json={"stations": next(answers)})

    first, second = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert _searched(module) == [_LONG_NAME, "Zzville Zzsaint", "Zzville"] * 2
    assert [c["uic"] for c in first["candidates"]] == ["9900233"]
    assert first["candidates"][0]["shortened_name"] == "Zzville"
    # The far stations of every search of the hub, each code once.
    assert first["far_dropped"] == 2
    assert second["candidates"] == []
    assert second["far_dropped"] == 2
    assert second["shortened_searches"] == 2


@pytest.mark.parametrize(
    ("name", "forms"),
    [
        (_LONG_NAME, ["Zzville Zzsaint", "Zzville"]),
        ("Zzville-d'Zzasq", ["Zzville-d", "Zzville"]),
        ("Zzville d\u2019Zzasq", ["Zzville d", "Zzville"]),
        ("Zzville\u2010Zzsud", ["Zzville"]),
        ("Zzville", []),
        # Under 3 characters: never sent.
        ("ZZ Hub01", []),
        ("Z Zzhub Zzend", ["Z Zzhub"]),
        # Read by the module as a query already made: skipped.
        ("Zzville Zzsaint-", ["Zzville"]),
        ("Zzville - Zzsaint", ["Zzville"]),
        ("Zzab Zzcd'", ["Zzab"]),
        # At most three forms, the longest.
        (
            "Zza1 Zzb2 Zzc3 Zzd4 Zze5 Zzf6",
            ["Zza1 Zzb2 Zzc3 Zzd4 Zze5", "Zza1 Zzb2 Zzc3 Zzd4", "Zza1 Zzb2 Zzc3"],
        ),
    ],
    ids=[
        "space-and-hyphen",
        "apostrophe",
        "typographic-apostrophe",
        "unicode-hyphen",
        "one-word",
        "first-word-too-short",
        "single-letter-skipped",
        "trailing-hyphen",
        "spaced-hyphen",
        "trailing-apostrophe",
        "at-most-three",
    ],
)
def test_the_shortened_forms_of_a_name(name: str, forms: list[str]) -> None:
    assert network_coverage.NAME_SHORTEN_MAX == 3
    query = network_coverage.station_suggest.normalise_query(name)
    assert network_coverage._shortened_names(query) == forms


def test_the_shortening_makes_at_most_three_more_searches(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_sea_hub(name="Zza1 Zzb2 Zzc3 Zzd4 Zze5 Zzf6")]

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert _searched(module) == [
        "Zza1 Zzb2 Zzc3 Zzd4 Zze5 Zzf6",
        "Zza1 Zzb2 Zzc3 Zzd4 Zze5",
        "Zza1 Zzb2 Zzc3 Zzd4",
        "Zza1 Zzb2 Zzc3",
    ]
    assert proposal["candidates"] == []
    assert proposal["name_searched"] is True
    assert proposal["shortened_searches"] == 3


def test_a_hub_without_a_usable_position_is_not_searched_by_a_shortened_name(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, name=_LONG_NAME, lat=math.nan)]
    _answers(module, {"Zzville": [_north("ZZ Guess", "9900241", 0.0)]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert module.of("near") == []
    assert _searched(module) == [_LONG_NAME]
    assert proposal["state"] == "no_position"
    assert proposal["candidates"] == []
    assert proposal["shortened_searches"] == 0


@pytest.mark.parametrize(
    ("answer", "status", "message"),
    [
        (_limited("29", "user_minute"), "limited", "try again in 29 seconds"),
        (httpx.Response(503, json={"detail": "ZZ", "code": "zz"}), "unavailable", "did not answer"),
    ],
    ids=["429", "503"],
)
def test_a_failure_during_the_shortening_stops_the_click_and_keeps_what_was_found(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    caplog: pytest.LogCaptureFixture,
    answer: httpx.Response,
    status: str,
    message: str,
) -> None:
    hubs.rows = [_sea_hub(0), _sea_hub(1, name="Zzsecret Zzhidden-Zzname"), _sea_hub(2)]
    nears = iter([_near_answer(_near("ZZ One", "9900251", 15)), _near_answer(), _near_answer()])
    module.near = lambda _b: next(nears)
    module.search = lambda q: (
        answer if q == "Zzsecret Zzhidden" else httpx.Response(200, json={"stations": []})
    )

    with caplog.at_level("DEBUG"):
        body = client.post(f"{BASE}/resolve", json={}).json()

    # No retry, no shorter form, no further hub.
    assert _searched(module) == ["Zzsecret Zzhidden-Zzname", "Zzsecret Zzhidden"]
    assert len(module.of("near")) == 2
    assert body["status"] == status
    assert message in body["message"]
    assert body["retry_after"] == (29 if status == "limited" else None)
    assert body["near_limited"] is False
    assert [p["hub_id"] for p in body["proposals"]] == ["zz-hub-00"]
    assert body["left"] == 2
    assert not station_module.paused()
    assert db.commits == 0
    _assert_logs_hold_none_of(caplog, "Zzsecret", "Zzhidden", "Zzname")


def test_the_worst_click_stays_within_its_budget_of_calls(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i, name=f"Zza{i} Zzb Zzc Zzd Zze") for i in range(25)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    # Each hub: 1 near call + 1 full name + 3 shortened names = 5 calls, so
    # a click of 15 calls looks at 3 hubs. Four such clicks fit in the
    # module's 60 a minute for one person (three of the 20-call worst click
    # before the shortening); with its confirm (at most one code per hub
    # looked at, at most 10) a click stays at 25 calls or fewer.
    assert network_coverage.RESOLVE_HUB_MAX_CALLS == 5
    assert network_coverage.RESOLVE_CALL_BUDGET == 15
    assert len(module.of("near")) == 3
    assert len(module.of("search")) == 12
    assert len(module.requests) == network_coverage.RESOLVE_CALL_BUDGET
    assert [p["hub_id"] for p in body["proposals"]] == [f"zz-hub-{i:02d}" for i in range(3)]
    assert body["status"] == "ok"
    assert body["left"] == 22
    assert 60 // network_coverage.RESOLVE_CALL_BUDGET == 4
    assert network_coverage.RESOLVE_CALL_BUDGET + network_coverage.CONFIRM_BATCH <= 25


def test_a_click_looks_at_a_hub_only_while_its_worst_case_fits_the_budget(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    # Two hubs at 5 calls (10), then hubs whose near call finds a station
    # (1 call each): the third fits (10 + 5 <= 15), the fourth would not
    # (11 + 5 > 15), although it would only cost one call.
    hubs.rows = [_hub(i, name=f"Zza{i} Zzb Zzc Zzd Zze") for i in range(2)] + [
        _hub(i) for i in range(2, 6)
    ]
    nears = iter([_near_answer()] * 2 + [_near_answer(_near("ZZ One", "9900261", 15))] * 4)
    module.near = lambda _b: next(nears)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.requests) == 11
    assert len(body["proposals"]) == 3
    assert body["left"] == 3


# ───────────────────────────── confirm ─────────────────────────────


def _pairs(count: int) -> list[dict[str, str]]:
    return [{"hub_id": f"zz-hub-{i:02d}", "uic": f"99000{i:02d}"} for i in range(count)]


def test_confirm_makes_one_lookup_for_ten_pairs_and_stores_them_as_msmm(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, admin: uuid.UUID
) -> None:
    hubs.rows = [_hub(i) for i in range(10)]
    module.served = {
        p["uic"]: _station(f"ZZ Station {p['uic']}", p["uic"], 45.0, 6.0) for p in _pairs(10)
    }

    body = client.post(f"{BASE}/confirm", json={"pairs": _pairs(10)}).json()

    assert network_coverage.CONFIRM_BATCH == 10
    assert _looked_up(module) == [[p["uic"] for p in _pairs(10)]]
    assert module.of("lookup")[0].headers["x-viator-user-id"] == str(admin)
    assert module.of("search") == []
    assert body["status"] == "ok"
    assert [r["state"] for r in body["results"]] == ["stored"] * 10
    assert body["results"][0]["module_name"] == "ZZ Station 9900000"
    assert [(h.uic, h.uic_origin) for h in hubs.rows] == [
        (f"99000{i:02d}", "msmm") for i in range(10)
    ]
    assert db.commits == 1


def test_confirm_refuses_eleven_pairs_without_a_call(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(11)]

    answer = client.post(f"{BASE}/confirm", json={"pairs": _pairs(11)})

    assert answer.status_code == 422
    assert module.requests == []
    assert db.commits == 0


def test_a_code_the_module_does_not_serve_leaves_its_hub_as_it_was(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    kept = _hub(1, uic="9900091", origin="manual")
    fresh = _hub(2)
    hubs.rows = [kept, fresh]
    module.served = {"9900002": _station("ZZ Two", "9900002", 45.0, 6.0)}

    body = client.post(
        f"{BASE}/confirm",
        json={
            "pairs": [
                {"hub_id": kept.id, "uic": "9900001"},
                {"hub_id": fresh.id, "uic": "9900002"},
            ]
        },
    ).json()

    assert [r["state"] for r in body["results"]] == ["not_served", "stored"]
    assert (kept.uic, kept.uic_origin) == ("9900091", "manual")
    assert (fresh.uic, fresh.uic_origin) == ("9900002", "msmm")


def test_confirm_sends_each_code_once_and_applies_the_answer_to_both_hubs(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1), _hub(2), _hub(3)]
    module.served = {"9900001": _station("ZZ One", "9900001", 45.0, 6.0)}

    body = client.post(
        f"{BASE}/confirm",
        json={
            "pairs": [
                {"hub_id": "zz-hub-01", "uic": "9900001"},
                {"hub_id": "zz-hub-02", "uic": "9900001"},
                {"hub_id": "zz-hub-03", "uic": "9900003"},
            ]
        },
    ).json()

    assert _looked_up(module) == [["9900001", "9900003"]]
    assert [r["state"] for r in body["results"]] == ["stored", "stored", "not_served"]
    assert [h.uic for h in hubs.rows] == ["9900001", "9900001", None]


def test_confirm_does_not_send_the_code_of_an_unknown_hub(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.served = {"9900001": _station("ZZ One", "9900001", 45.0, 6.0)}

    body = client.post(
        f"{BASE}/confirm",
        json={
            "pairs": [
                {"hub_id": "zz-hub-01", "uic": "9900001"},
                {"hub_id": "zz-hub-77", "uic": "9900077"},
            ]
        },
    ).json()

    assert _looked_up(module) == [["9900001"]]
    assert [r["state"] for r in body["results"]] == ["stored", "unknown_hub"]


def test_confirm_with_only_unknown_hubs_makes_no_call(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    body = client.post(
        f"{BASE}/confirm", json={"pairs": [{"hub_id": "zz-hub-77", "uic": "9900077"}]}
    ).json()

    assert module.requests == []
    assert body["results"][0]["state"] == "unknown_hub"


@pytest.mark.parametrize(
    ("response", "status", "retry_after"),
    [
        pytest.param(_limited("23"), "limited", 23, id="429"),
        pytest.param(httpx.Response(503, json={"code": "busy"}), "unavailable", None, id="busy"),
        pytest.param(httpx.Response(404, json={}), "unavailable", None, id="older-module-404"),
        pytest.param(httpx.Response(200, json=[]), "unavailable", None, id="wrong-shape"),
    ],
)
def test_a_failed_confirm_writes_nothing(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    response: httpx.Response,
    status: str,
    retry_after: int | None,
) -> None:
    hubs.rows = [_hub(1)]
    module.lookup_response = response

    body = client.post(f"{BASE}/confirm", json={"pairs": _pairs(2)[1:]}).json()

    assert body["status"] == status
    assert body["retry_after"] == retry_after
    assert body["results"] == []
    assert hubs.rows[0].uic is None
    assert db.commits == 0
    # A lookup failure starts no pause of the client.
    assert not station_module.paused()


@pytest.mark.parametrize(
    "pairs",
    [
        pytest.param([], id="none"),
        pytest.param(
            [{"hub_id": "zz-hub-01", "uic": "9900001"}, {"hub_id": "zz-hub-01", "uic": "9900002"}],
            id="a-hub-twice",
        ),
        pytest.param([{"hub_id": "zz-hub-01", "uic": "99 01"}], id="a-space-in-the-code"),
        pytest.param([{"hub_id": "zz-hub-01", "uic": "ZZ"}], id="a-code-of-two"),
        pytest.param([{"hub_id": "zz-hub-01", "uic": "9" * 21}], id="a-code-of-twenty-one"),
        pytest.param([{"hub_id": "zz-hub-01", "uic": "99\x0001"}], id="a-nul"),
        pytest.param([{"hub_id": "zz-hub-01", "uic": 9900001}], id="a-number"),
        pytest.param([{"hub_id": "zz-hub-01", "uic": "9900001", "zz": 1}], id="an-unknown-field"),
    ],
)
def test_a_confirm_body_out_of_the_rules_is_refused_without_a_call(
    client: TestClient, hubs: Hubs, module: Module, pairs: list[dict[str, Any]]
) -> None:
    hubs.rows = [_hub(1)]

    answer = client.post(f"{BASE}/confirm", json={"pairs": pairs})

    assert answer.status_code == 422
    assert module.requests == []


# ───────────────────────────── check ─────────────────────────────


def test_check_makes_one_lookup_of_twenty_codes_for_twenty_five_hubs(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i, uic=f"99000{i:02d}", origin="msmm") for i in range(25)]
    module.served = {
        h.uic: _station(h.name, h.uic, h.lat, h.lon) for h in hubs.rows if h.uic is not None
    }

    body = client.post(f"{BASE}/check", json={}).json()

    assert network_coverage.CHECK_BATCH == 20
    assert _looked_up(module) == [[f"99000{i:02d}" for i in range(20)]]
    assert body["status"] == "ok"
    assert body["left"] == 5
    assert [r["state"] for r in body["results"]] == ["ok"] * 20
    assert db.commits == 0


def test_check_sends_each_code_once_and_applies_the_answer_to_both_hubs(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [
        _hub(1, uic="9900001", origin="msmm", name="ZZ Hub 01"),
        _hub(2, uic="9900001", origin="manual", name="ZZ Hub 02"),
        _hub(3, uic="9900003", origin="msmm"),
    ]
    module.served = {"9900001": _station("ZZ Hub 01", "9900001", 45.0, 6.0)}

    body = client.post(f"{BASE}/check", json={}).json()

    assert _looked_up(module) == [["9900001", "9900003"]]
    assert [(r["hub_id"], r["state"]) for r in body["results"]] == [
        ("zz-hub-01", "ok"),
        ("zz-hub-02", "name_differs"),
        ("zz-hub-03", "not_served"),
    ]
    assert body["results"][1]["module_name"] == "ZZ Hub 01"
    assert body["results"][1]["uic_origin"] == "manual"


def test_check_shows_a_name_that_differs_and_the_parent_and_writes_nothing(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    same = _hub(1, uic="9900001", origin="msmm", name="ZZ Hub 01")
    other = _hub(2, uic="9900002", origin="msmm")
    hubs.rows = [same, other]
    module.served = {
        "9900001": _station("zz  hub 01", "9900001", 45.0, 6.0, parent="9900009"),
        "9900002": _station("ZZ Elsewhere", "9900002", 45.0, 6.0),
    }

    body = client.post(f"{BASE}/check", json={}).json()

    first, second = body["results"]
    assert (first["state"], first["parent_uic"]) == ("ok", "9900009")
    assert (second["state"], second["module_name"]) == ("name_differs", "ZZ Elsewhere")
    assert (other.uic, other.uic_origin) == ("9900002", "msmm")  # nothing cleared
    assert db.commits == 0


def test_check_shows_a_stored_code_the_module_would_refuse_without_sending_it(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, uic="ZZ", origin="manual"), _hub(2, uic="9900002", origin="msmm")]

    body = client.post(f"{BASE}/check", json={}).json()

    assert _looked_up(module) == [["9900002"]]
    assert [r["state"] for r in body["results"]] == ["not_served", "not_served"]


def test_a_429_on_check_shows_its_retry_after(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i, uic=f"99000{i:02d}", origin="msmm") for i in range(25)]
    module.lookup_response = _limited("31")

    body = client.post(f"{BASE}/check", json={}).json()

    assert body["status"] == "limited"
    assert body["retry_after"] == 31
    assert "31 seconds" in body["message"]
    assert body["results"] == []
    assert body["left"] == 25
    assert len(module.requests) == 1


def test_check_skips_the_hubs_already_shown(client: TestClient, hubs: Hubs, module: Module) -> None:
    hubs.rows = [_hub(i, uic=f"99000{i:02d}", origin="msmm") for i in range(25)]
    shown = [f"zz-hub-{i:02d}" for i in range(20)]

    body = client.post(f"{BASE}/check", json={"skip": shown}).json()

    assert _looked_up(module) == [[f"99000{i:02d}" for i in range(20, 25)]]
    assert body["left"] == 0


# ─────────────────── the module paused, or not configured ───────────────────


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("resolve", {}),
        ("confirm", {"pairs": [{"hub_id": "zz-hub-01", "uic": "9900001"}]}),
        ("check", {}),
    ],
)
def test_with_the_client_paused_the_routes_make_no_call(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, Any],
) -> None:
    hubs.rows = [_hub(1), _hub(2, uic="9900002", origin="msmm")]
    monkeypatch.setattr(station_module, "paused", lambda: True)

    answer = client.post(f"{BASE}/{route}", json=body).json()

    assert module.requests == []
    assert answer["status"] == "unavailable"
    assert answer["message"] == "The station module did not answer; nothing was changed."
    assert db.commits == 0


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("resolve", {}),
        ("confirm", {"pairs": [{"hub_id": "zz-hub-01", "uic": "9900001"}]}),
        ("check", {}),
    ],
)
def test_without_the_module_the_routes_make_no_call(
    client: TestClient,
    db: FakeDb,
    hubs: Hubs,
    module: Module,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, Any],
) -> None:
    hubs.rows = [_hub(1), _hub(2, uic="9900002", origin="msmm")]
    monkeypatch.setattr(settings, "station_module_url", "")

    answer = client.post(f"{BASE}/{route}", json=body).json()

    assert module.requests == []
    assert answer["status"] == "unavailable"
    assert answer["message"] == "The station module is not configured; nothing was changed."
    assert db.commits == 0


def test_a_real_pause_of_the_client_is_seen_by_the_routes(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1), _hub(2)]
    module.search = lambda _q: httpx.Response(500, json={})
    # A typeahead search answered 500 pauses the client.
    assert asyncio.run(station_module.search("ZZ Hub", uuid.uuid4())) is None
    calls = len(module.requests)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.requests) == calls
    assert body["status"] == "unavailable"


def test_a_near_failure_does_not_pause_the_next_click(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1), _hub(2)]
    module.near = lambda _b: httpx.Response(500, json={})
    client.post(f"{BASE}/resolve", json={})

    module.near = lambda _b: _near_answer()
    body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["status"] == "ok"
    assert len(module.of("near")) == 3


# ───────────────────────────── who may ─────────────────────────────


@pytest.mark.parametrize("route", ["resolve", "confirm", "check"])
@pytest.mark.parametrize("role", ["content_manager", "end_user"])
def test_non_administrators_are_refused(
    client: TestClient, hubs: Hubs, module: Module, route: str, role: str
) -> None:
    client.cookies.set(settings.jwt_cookie_name, _jwt(uuid.uuid4(), role))

    answer = client.post(f"{BASE}/{route}", json={"pairs": _pairs(1)})

    assert answer.status_code == 403
    assert module.requests == []


@pytest.mark.parametrize("route", ["resolve", "confirm", "check"])
def test_an_anonymous_caller_is_refused(
    client: TestClient, hubs: Hubs, module: Module, route: str
) -> None:
    client.cookies.clear()

    assert client.post(f"{BASE}/{route}", json={"pairs": _pairs(1)}).status_code == 401
    assert module.requests == []


# ───────────────────────────── a typed code ─────────────────────────────


def _actor() -> MagicMock:
    actor = MagicMock()
    actor.id = uuid.uuid4()
    return actor


def test_a_code_typed_at_create_is_stored_as_manual() -> None:
    db = MagicMock()
    body = HubCreate(
        id="zz-hub", name="ZZ Hub", short="ZZ", country="ZZ", lat=45.0, lon=6.0, uic="9900001"
    )

    info = create_hub(body=body, db=db, _=_actor())

    added = db.add.call_args[0][0]
    assert (added.uic, added.uic_origin) == ("9900001", "manual")
    assert (info.uic, info.uic_origin) == ("9900001", "manual")


def test_a_hub_created_without_a_code_is_unresolved() -> None:
    db = MagicMock()
    body = HubCreate(id="zz-hub", name="ZZ Hub", short="ZZ", country="ZZ", lat=45.0, lon=6.0)

    create_hub(body=body, db=db, _=_actor())

    added = db.add.call_args[0][0]
    assert (added.uic, added.uic_origin) == (None, None)


def _patch(hub: NetworkCoverageHub, body: HubUpdate) -> NetworkCoverageHub:
    db = MagicMock()
    db.get.return_value = hub
    update_hub(hub_id=hub.id, body=body, db=db, _=_actor())
    return hub


def test_a_code_typed_in_patch_is_stored_as_manual() -> None:
    hub = _patch(_hub(1, uic="9900001", origin="msmm"), HubUpdate(uic="9900002"))
    assert (hub.uic, hub.uic_origin) == ("9900002", "manual")


def test_a_null_code_clears_the_code_and_its_origin() -> None:
    hub = _patch(_hub(1, uic="9900001", origin="msmm"), HubUpdate(uic=None))
    assert (hub.uic, hub.uic_origin) == (None, None)


def test_the_stored_code_sent_back_unchanged_keeps_its_origin() -> None:
    hub = _patch(_hub(1, uic="9900001", origin="msmm"), HubUpdate(uic="9900001", short="ZZ1"))
    assert (hub.uic, hub.uic_origin, hub.short) == ("9900001", "msmm", "ZZ1")


def test_a_patch_without_a_code_leaves_it_alone() -> None:
    hub = _patch(_hub(1, uic="9900001", origin="msmm"), HubUpdate(name="ZZ Renamed"))
    assert (hub.uic, hub.uic_origin) == ("9900001", "msmm")


@pytest.mark.parametrize(
    "code",
    ["ZZ", "9" * 21, "99 001", "99\u00a0001", "99\t001", "99\x00001", "99\ud800001", ""],
    ids=[
        "two",
        "twenty-one",
        "space",
        "no-break-space",
        "tab",
        "nul",
        "surrogate",
        "empty",
    ],
)
def test_a_typed_code_out_of_the_rule_is_refused(code: str) -> None:
    with pytest.raises(ValueError, match="uic"):
        HubUpdate(uic=code)
    with pytest.raises(ValueError, match="uic"):
        HubCreate(id="zz-hub", name="ZZ Hub", short="ZZ", country="ZZ", lat=45, lon=6, uic=code)


def test_a_typed_code_out_of_the_rule_is_a_422(client: TestClient) -> None:
    answer = client.post(
        BASE,
        json={
            "id": "zz-hub",
            "name": "ZZ Hub",
            "short": "ZZ",
            "country": "ZZ",
            "lat": 45.0,
            "lon": 6.0,
            "uic": "99 001",
        },
    )
    assert answer.status_code == 422


# ───────────────────────── where the code goes ─────────────────────────


def test_the_hub_shape_carries_the_code_for_the_cell_dialog() -> None:
    info = network_coverage._hub_to_info(_hub(1, uic="9900001", origin="manual"))
    assert (info.uic, info.uic_origin) == ("9900001", "manual")


def test_the_export_and_its_share_page_carry_no_code() -> None:
    info = network_coverage._hub_to_info(_hub(1, uic="9900001", origin="manual"))

    rows, _runs = network_coverage._annotate_hubs_with_country_bands([info])

    assert "uic" not in rows[0]
    assert "uic_origin" not in rows[0]


def test_coverage_runs_never_import_the_station_module_client() -> None:
    """A coverage run routes by the positions stored on the hubs; the
    module's limits apply only to the administrator's clicks."""
    for path in (REPO / "app" / "network_coverage").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bstation_module\b", text), path.name


# ───────────────────────── the manage-hubs panel ─────────────────────────

TEMPLATE = REPO / "app" / "templates" / "admin" / "network_coverage.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _codes_script(text: str) -> str:
    """The script of the station codes, from its banner to the toast helper."""
    start = text.index("// MSMM step 3 — station codes of the hubs")
    end = text.index("// Reuse the toast plumbing", start)
    return text[start:end]


def test_the_buttons_are_shown_only_with_the_module(template_text: str) -> None:
    match = re.search(r"{% if station_module_enabled %}(.*?){% endif %}", template_text, re.S)
    assert match
    block = match.group(1)
    assert 'id="hub-codes-resolve"' in block
    assert 'id="hub-codes-check"' in block
    assert template_text.count('id="hub-codes-resolve"') == 1
    assert template_text.count('id="hub-codes-check"') == 1


def test_module_data_never_goes_through_inner_html(template_text: str) -> None:
    script = _codes_script(template_text)
    code_lines = [line for line in script.splitlines() if not line.strip().startswith("//")]
    assert not any("innerHTML" in line or "insertAdjacentHTML" in line for line in code_lines)
    assert "textContent" in script
    # The candidates' and the check's names and codes are text nodes.
    assert "label.append(input, ` ${candidate.name}" in script
    assert "hubCodesElement('strong', proposal.hub_name)" in script
    assert '`The station module names it "${result.module_name}".`' in script


def test_the_panel_speaks_of_positions_first_then_names(template_text: str) -> None:
    script = _codes_script(template_text)
    assert "This hub has no usable position" in script
    assert "within 300 m of this hub\\'s position" in script
    assert "found by its name instead: check each one before saving" in script
    # Each candidate says where it comes from, as text.
    assert "candidate.found_by === 'name' ? hubNameOrigin(candidate) : 'by position'" in script
    # A candidate found by a shortened name quotes that text.
    assert "`by the shortened name «${candidate.shortened_name}»` : 'by name'" in script
    # The warning is a text node starting with the word, tied to its input.
    assert "hubCodesElement('div', `Warning: ${candidate.warning}`, 'hub-code-warning')" in script
    assert "input.setAttribute('aria-describedby', warning.id);" in script


def test_the_warning_colours_pass_wcag_aa(template_text: str) -> None:
    match = re.search(r"\.hub-code-item \.hub-code-warning \{([^}]*)\}", template_text)
    assert match
    rule = match.group(1)
    colour = re.search(r"(?<!-)color: (#[0-9a-f]{6})", rule)
    background = re.search(r"background: (#[0-9a-f]{6})", rule)
    assert colour
    assert background

    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(colour.group(1)), luminance(background.group(1))), reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5


# The proposal part of the codes script, run in Node with a stand-in page:
# what an administrator reads for each kind of proposal.
_RENDER_PRELUDE = """
function node(tag) {
  const el = { tag, children: [], attrs: {}, dataset: {}, className: '', id: '', textContent: '' };
  el.append = (...kids) => { el.children.push(...kids); };
  el.setAttribute = (k, v) => { el.attrs[k] = v; };
  return el;
}
globalThis.document = { createElement: node };
const HUB_CODES = { warnings: 0 };
function text(el) {
  if (typeof el === 'string') return el;
  return (el.textContent || '') + el.children.map(text).join('');
}
function lines(item) {
  return item.children.map(c => (c.tag === 'label' ? `[${c.children[0].type}]` : '') + text(c));
}
"""

_RENDER_SCENARIO = """
const position = { name: 'ZZ Central', uic: '9900001', country_iso: 'ZZ', distance_m: 120, found_by: 'position', warning: null };
const near = { name: 'ZZ Near', uic: '9900002', country_iso: 'ZZ', distance_m: 200, found_by: 'name', warning: null };
const far = { name: 'ZZ Far', uic: '9900003', country_iso: null, distance_m: 1260, found_by: 'name', warning: 'Found by name, 1.3 km from the hub' };
const lost = { name: 'ZZ Lost', uic: '9900004', country_iso: 'ZZ', distance_m: null, found_by: 'name', warning: 'ZZ unknown' };
const base = { hub_id: 'zz-hub-01', hub_name: 'ZZ Hub 01', name_searched: false, far_dropped: 0 };
const out = {
  byPosition: lines(hubProposalItem({ ...base, state: 'proposed', candidates: [position] })),
  byName: lines(hubProposalItem({ ...base, state: 'to_pick', name_searched: true, far_dropped: 2, candidates: [near, far] })),
  oneFar: lines(hubProposalItem({ ...base, state: 'to_pick', name_searched: true, far_dropped: 1, candidates: [] })),
  none: lines(hubProposalItem({ ...base, state: 'to_pick', name_searched: true, candidates: [] })),
  notSearched: lines(hubProposalItem({ ...base, state: 'to_pick', candidates: [] })),
  noPosition: lines(hubProposalItem({ ...base, state: 'no_position', name_searched: true, candidates: [lost] })),
  noPositionNone: lines(hubProposalItem({ ...base, state: 'no_position', name_searched: true, candidates: [] })),
  noPositionNotSearched: lines(hubProposalItem({ ...base, state: 'no_position', candidates: [] })),
};
// Found by a shortened name: the text is markup-like on purpose, to show it
// stays text.
const cut = '<b>Zz</b> & Zzx';
const shortNear = { name: 'ZZ Close', uic: '9900005', country_iso: 'ZZ', distance_m: 200, found_by: 'name', warning: null, shortened_name: cut };
const shortFar = { name: 'ZZ Beyond', uic: '9900006', country_iso: 'ZZ', distance_m: 1200, found_by: 'name', warning: `Found by the shortened name «${cut}», a looser match, 1.2 km`, shortened_name: cut };
const shortenedItem = hubProposalItem({ ...base, state: 'to_pick', name_searched: true, shortened_searches: 2, candidates: [shortNear, shortFar] });
out.shortened = lines(shortenedItem);
out.shortenedTexts = shortenedItem.children.filter(c => c.tag === 'label').map(c => c.children.slice(1).every(k => typeof k === 'string'));
out.shortenedNone = lines(hubProposalItem({ ...base, state: 'to_pick', name_searched: true, shortened_searches: 3, candidates: [] }));
out.shortenedOne = lines(hubProposalItem({ ...base, state: 'to_pick', name_searched: true, shortened_searches: 1, candidates: [] }));
// Three items with two warned candidates each: every warning has its own id,
// each choice points at the warning right after it, none is hidden.
const items = [[far, lost], [far, lost], [shortFar, lost]].map(cands => hubProposalItem({ ...base, state: 'to_pick', name_searched: true, candidates: cands }));
const described = (c) => c.tag === 'label' && c.children[0].attrs['aria-describedby'];
const pairs = items.flatMap(item => item.children.flatMap((c, i) => (described(c) ? [[described(c), item.children[i + 1]]] : [])));
out.warnings = pairs.length;
out.ownWarning = pairs.every(([ref, next]) => next.className === 'hub-code-warning' && next.id === ref && ref !== '');
out.uniqueIds = new Set(pairs.map(([ref]) => ref)).size === pairs.length;
out.hidden = pairs.some(([, next]) => 'aria-hidden' in next.attrs || next.hidden === true);
console.log(JSON.stringify(out));
"""


def _render_script(text: str) -> str:
    script = _codes_script(text)
    helpers = script[
        script.index("function hubCodesElement(") : script.index("function hubCodesStatus(")
    ]
    start = script.index("// A distance as the warnings write it")
    end = script.index("async function resolveHubCodes(")
    return helpers + script[start:end]


def test_the_page_shows_origin_and_warning_of_each_candidate(template_text: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = _RENDER_PRELUDE + _render_script(template_text) + _RENDER_SCENARIO
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["byPosition"] == [
        "ZZ Hub 01",
        "[checkbox] ZZ Central · 9900001 · ZZ · 120 m · by position",
    ]
    assert out["byName"] == [
        "ZZ Hub 01",
        "No station returned by the position search; found by its name instead: "
        "check each one before saving, or leave it.",
        "2 more stations found by name stand over 50 km from the hub's position and are "
        "not shown: check the position.",
        "[radio] ZZ Near · 9900002 · ZZ · 200 m · by name",
        # The distance is written as the warnings write it.
        "[radio] ZZ Far · 9900003 · ? · 1.3 km · by name",
        "Warning: Found by name, 1.3 km from the hub",
        "[radio] Leave it",
    ]
    assert out["oneFar"][2] == (
        "1 more station found by name stands over 50 km from the hub's position and is "
        "not shown: check the position."
    )
    assert out["none"][1] == (
        "No station of the module within 300 m of this hub's position, and none found by "
        "its name: type the code in the hub form if you know it."
    )
    assert out["notSearched"][1] == (
        "No station of the module within 300 m of this hub's position, and its name was "
        "not searched (it is under 3 or over 100 characters): type the code in the hub form "
        "if you know it."
    )
    assert out["noPosition"] == [
        "ZZ Hub 01",
        "This hub has no usable position: correct its latitude and longitude in the hub form.",
        "Found by its name, at a distance that cannot be told: check each one before saving, "
        "or leave it.",
        "[radio] ZZ Lost · 9900004 · ZZ · distance unknown · by name",
        "Warning: ZZ unknown",
        "[radio] Leave it",
    ]
    assert out["noPositionNone"][2] == (
        "Its position cannot be searched, and none found by its name: type the code in the "
        "hub form if you know it."
    )
    assert "its name was not searched" in out["noPositionNotSearched"][2]
    # Found by a shortened name: the text that found it, quoted, on each
    # candidate and in the note, as text nodes (never markup).
    assert out["shortened"] == [
        "ZZ Hub 01",
        "No station returned by the position search nor by the hub's full name; found by "
        "the shortened name «<b>Zz</b> & Zzx» instead, a looser match: check each one "
        "before saving, or leave it.",
        "[radio] ZZ Close · 9900005 · ZZ · 200 m · by the shortened name «<b>Zz</b> & Zzx»",
        "[radio] ZZ Beyond · 9900006 · ZZ · 1.2 km · by the shortened name «<b>Zz</b> & Zzx»",
        "Warning: Found by the shortened name «<b>Zz</b> & Zzx», a looser match, 1.2 km",
        "[radio] Leave it",
    ]
    assert out["shortenedTexts"] == [True, True, True]
    assert out["shortenedNone"][1] == (
        "No station of the module within 300 m of this hub's position, and none found by "
        "its name nor by 3 shortened forms of it: type the code in the hub form if you "
        "know it."
    )
    assert "nor by 1 shortened form of it:" in out["shortenedOne"][1]
    assert out["warnings"] == 6
    assert out["ownWarning"] is True
    assert out["uniqueIds"] is True
    assert out["hidden"] is False


def test_the_code_on_each_hub_row_is_written_as_text(template_text: str) -> None:
    assert '<div class="hub-code" data-code-for="${escHTML(h.id)}"></div>' in template_text
    fill = _codes_script(template_text)
    assert "el.textContent = h ? hubCodeLabel(h) : '';" in fill
    assert "fillHubCodes(list, hubs);" in template_text


def test_the_form_sends_the_code_only_when_it_changed(template_text: str) -> None:
    assert 'id="hub-form-uic" name="uic" maxlength="20"' in template_text
    assert "if (typedUic !== HUB_EDIT_UIC) {" in template_text
    assert "body.uic = typedUic || null;" in template_text
    assert "HUB_EDIT_UIC = h.uic || '';" in template_text


def test_a_429_holds_the_buttons_for_the_modules_retry_after(template_text: str) -> None:
    script = _codes_script(template_text)
    assert "holdHubCodeButtons(answer.retry_after || 60);" in script
    assert "holdHubProposeButtons(answer.retry_after || 60);" in script
    assert "setHubCodeButtons(true);" in script  # also while a request runs


# The buttons' part of the codes script, run in Node with a stand-in page
# and clock: which buttons a 429 holds, and for how long.
_BUTTON_PRELUDE = """
let now = 1000000;
Date.now = () => now;
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push([now + ms, fn]); };
const buttons = {};
for (const id of ['hub-codes-resolve', 'hub-codes-check', 'hub-codes-confirm', 'hub-codes-next']) {
  buttons[id] = { id, disabled: false };
}
const status = { textContent: '', classList: { toggle() {} } };
globalThis.document = { getElementById: id => buttons[id] || (id === 'hub-codes-status' ? status : null) };
"""

_BUTTON_SCENARIO = """
function disabled() {
  return Object.fromEntries(Object.entries(buttons).map(([id, b]) => [id.slice(10), b.disabled]));
}
function advance(seconds) {
  now += seconds * 1000;
  timers.filter(([at]) => at <= now).forEach(([, fn]) => fn());
}
const out = {};
HUB_CODES.mode = 'resolve';
showHubCodesAnswer({ status: 'limited', near_limited: true, retry_after: 86400, message: 'ZZ' });
out.nearDay = disabled();
HUB_CODES.mode = 'check';
setHubCodeButtons(false);
out.nearDayInCheck = disabled();
HUB_CODES.mode = 'resolve';
setHubCodeButtons(false);
advance(86399);
out.nearDayAlmost = disabled();
advance(1);
out.nearDayOver = disabled();
showHubCodesAnswer({ status: 'limited', near_limited: false, retry_after: 17, message: 'ZZ' });
out.shared = disabled();
advance(17);
out.sharedOver = disabled();
showHubCodesAnswer({ status: 'limited', near_limited: true, message: 'ZZ' });
out.nearNoWait = disabled();
advance(60);
out.nearNoWaitOver = disabled();
console.log(JSON.stringify(out));
"""


def _button_script(text: str) -> str:
    script = _codes_script(text)
    start = script.index("// heldUntil: every button waits")
    end = script.index("function openHubCodes(")
    return script[start:end]


def test_a_near_limit_holds_only_propose_and_next_in_the_page(template_text: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = _BUTTON_PRELUDE + _button_script(template_text) + _BUTTON_SCENARIO
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    held = {"resolve": True, "check": False, "confirm": False, "next": True}
    free = dict.fromkeys(held, False)
    # A daily near limit: Save and Check stay usable for the whole day.
    assert out["nearDay"] == held
    assert out["nearDayInCheck"] == {**held, "next": False}  # "next" checks then
    assert out["nearDayAlmost"] == held
    assert out["nearDayOver"] == free
    # A limit of every call holds the four, for the module's Retry-After.
    assert out["shared"] == dict.fromkeys(held, True)
    assert out["sharedOver"] == free
    assert out["nearNoWait"] == held  # 60 s when the module gave no wait
    assert out["nearNoWaitOver"] == free


def test_the_cell_dialog_link_carries_the_hub_code(template_text: str) -> None:
    assert "const oUic = orig && orig.uic ? encodeURIComponent(orig.uic) : '';" in template_text
    assert "&from_uic=${oUic}" in template_text
    assert "&to_uic=${dUic}" in template_text


# ─────────────── never a 500 on a lone surrogate or a NUL ───────────────

# JSON escapes as a client may send them: a lone surrogate and a NUL.
_SURROGATE = "\\ud800"
_NUL = "\\u0000"


def _raw(client: TestClient, method: str, url: str, body: str) -> httpx.Response:
    return client.request(
        method, url, content=body.encode("ascii"), headers={"Content-Type": "application/json"}
    )


def _assert_fixed_422(answer: httpx.Response) -> None:
    assert answer.status_code == 422
    text = answer.content.decode("utf-8")  # valid UTF-8 JSON
    detail = json.loads(text)["detail"]
    assert isinstance(detail, str)
    assert "99Z" not in text  # the refused value is never echoed
    assert "\\ud800" not in text
    assert "input" not in text


_HUB_JSON = (
    '{"id": "zz-hub", "name": "NAME", "short": "ZZ", "country": "ZZ", '
    '"lat": 45.0, "lon": 6.0, "uic": "CODE"}'
)


@pytest.mark.parametrize("bad", [_SURROGATE, _NUL], ids=["surrogate", "nul"])
@pytest.mark.parametrize("field", ["uic", "name"])
def test_a_hub_create_with_an_unwritable_character_is_a_fixed_422(
    client: TestClient, db: FakeDb, field: str, bad: str
) -> None:
    name, code = ("ZZ Hub", f"99Z{bad}1") if field == "uic" else (f"99Z{bad}", "9900001")

    answer = _raw(client, "POST", BASE, _HUB_JSON.replace("NAME", name).replace("CODE", code))

    _assert_fixed_422(answer)
    assert f"Fields at fault: {field}." in answer.json()["detail"]
    assert db.commits == 0


@pytest.mark.parametrize("bad", [_SURROGATE, _NUL], ids=["surrogate", "nul"])
@pytest.mark.parametrize("field", ["uic", "name", "region"])
def test_a_hub_patch_with_an_unwritable_character_is_a_fixed_422(
    client: TestClient, db: FakeDb, field: str, bad: str
) -> None:
    answer = _raw(client, "PATCH", f"{BASE}/zz-hub-01", f'{{"{field}": "99Z{bad}1"}}')

    _assert_fixed_422(answer)
    assert db.commits == 0


@pytest.mark.parametrize("path", ["zz%00hub", "z" * 65], ids=["nul", "too-long"])
def test_a_hub_patch_on_an_impossible_id_is_a_404_without_a_statement(
    client: TestClient, db: FakeDb, path: str
) -> None:
    # FakeDb has no `get`: reaching the database would be a 500.
    answer = client.patch(f"{BASE}/{path}", json={"name": "ZZ Renamed"})

    assert answer.status_code == 404
    assert db.commits == 0


@pytest.mark.parametrize("bad", [_SURROGATE, _NUL], ids=["surrogate", "nul"])
@pytest.mark.parametrize("field", ["uic", "hub_id"])
def test_a_confirm_with_an_unwritable_character_is_a_fixed_422(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, field: str, bad: str
) -> None:
    hubs.rows = [_hub(1)]
    pair = {"hub_id": "zz-hub-01", "uic": "9900001"}
    pair[field] = f"99Z{bad}1"
    body = json.dumps({"pairs": [pair]}).replace("\\\\", "\\")

    answer = _raw(client, "POST", f"{BASE}/confirm", body)

    _assert_fixed_422(answer)
    assert "Fields at fault: pairs." in answer.json()["detail"]
    assert module.requests == []
    assert db.commits == 0


@pytest.mark.parametrize("bad", [_SURROGATE, _NUL, ""], ids=["surrogate", "nul", "empty"])
@pytest.mark.parametrize("route", ["resolve", "check"])
def test_a_skip_list_with_an_unwritable_id_is_a_fixed_422(
    client: TestClient, hubs: Hubs, module: Module, route: str, bad: str
) -> None:
    hubs.rows = [_hub(1), _hub(2, uic="9900002", origin="msmm")]
    skip = f'"99Z{bad}1"' if bad else '""'

    answer = _raw(client, "POST", f"{BASE}/{route}", f'{{"skip": [{skip}]}}')

    _assert_fixed_422(answer)
    assert "Fields at fault: skip." in answer.json()["detail"]
    assert hubs.skips == []  # the hubs were never read
    assert module.requests == []


@pytest.mark.parametrize("route", ["resolve", "check", "confirm"])
def test_a_body_of_the_wrong_shape_is_a_fixed_422(
    client: TestClient, hubs: Hubs, module: Module, route: str
) -> None:
    answer = _raw(client, "POST", f"{BASE}/{route}", '["99Z"]')

    _assert_fixed_422(answer)
    assert module.requests == []


def test_a_lookup_row_holding_a_surrogate_stores_nothing_and_answers_utf8(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1), _hub(2)]
    rows = [
        _station("ZZ \ud800 One", "9900001", 45.0, 6.0),
        _station("ZZ Two", "9900002", 45.0, 6.0),
    ]
    module.lookup_response = httpx.Response(
        200,
        content=json.dumps({"stations": rows}).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )

    answer = client.post(f"{BASE}/confirm", json={"pairs": _pairs(3)[1:]})

    assert answer.status_code == 200
    body = json.loads(answer.content.decode("utf-8"))
    assert [r["state"] for r in body["results"]] == ["not_served", "stored"]
    assert [h.uic for h in hubs.rows] == [None, "9900002"]


def test_a_near_row_holding_a_surrogate_is_not_proposed(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    rows = [_near("ZZ \ud800", "9900001", 4)]
    payload = json.dumps({"stations": rows}).encode("ascii")
    module.near = lambda _b: httpx.Response(
        200, content=payload, headers={"Content-Type": "application/json"}
    )

    answer = client.post(f"{BASE}/resolve", json={})

    assert answer.status_code == 200
    assert json.loads(answer.content.decode("utf-8"))["proposals"][0]["candidates"] == []


# ───────────────────── soft-deleted hubs, the module's distance ─────────────────────


def test_confirm_takes_no_code_for_a_soft_deleted_hub(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module
) -> None:
    gone = _hub(1, is_active=False)
    hubs.rows = [gone]

    body = client.post(
        f"{BASE}/confirm", json={"pairs": [{"hub_id": gone.id, "uic": "9900001"}]}
    ).json()

    assert body["results"][0]["state"] == "unknown_hub"
    assert module.requests == []
    assert gone.uic is None


def test_the_proposal_shows_the_modules_distance_and_does_not_filter_again() -> None:
    """The module answers only stations within the radius; VIATOR keeps its
    distance as given, without measuring again (a station at 300 m stays)."""
    proposal = network_coverage._proposal(
        _hub(1, lat=-48.5, lon=-123.25), [_near("ZZ Edge", "9900001", 300)]
    )

    assert proposal.state == "proposed"
    assert proposal.candidates[0].distance_m == 300


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "a minute"),
        (1, "1 second"),
        (17, "17 seconds"),
        (119, "119 seconds"),
        (120, "about 2 minutes"),
        (121, "about 3 minutes"),
        (7199, "about 120 minutes"),
        (7200, "about 2 hours"),
        (50_000, "about 14 hours"),
    ],
)
def test_the_wait_of_a_429_reads_in_seconds_minutes_or_hours(
    seconds: int | None, text: str
) -> None:
    assert network_coverage._wait_text(seconds) == text


def test_a_long_retry_after_is_said_in_hours_and_kept_as_given(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.near = lambda _b: _limited("50000", "user_day")

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["message"] == "The station module's limit is reached; try again in about 14 hours."
    assert body["retry_after"] == 50_000
