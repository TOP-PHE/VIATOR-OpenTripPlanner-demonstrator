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

import json
import math
import re
import secrets
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

# One degree of latitude is about 111,195 m on the haversine's sphere.
_METRES_PER_DEGREE = 2 * math.pi * 6_371_000.0 / 360


def _north(lat: float, metres: float) -> float:
    return lat + metres / _METRES_PER_DEGREE


def _hub(index: int, *, uic: str | None = None, origin: str | None = None, **extra: Any):
    values: dict[str, Any] = {
        "id": f"zz-hub-{index:02d}",
        "name": f"ZZ Hub {index:02d}",
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


class Module:
    """The module: `search` answers by the text, `lookup` from `served`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.search: Callable[[str], httpx.Response] = lambda _q: httpx.Response(
            200, json={"stations": []}
        )
        self.served: dict[str, dict[str, Any]] = {}
        self.lookup_response: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
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


def _searched_names(module: Module) -> list[str]:
    return [json.loads(r.content)["q"] for r in module.of("search")]


def _looked_up(module: Module) -> list[list[str]]:
    return [json.loads(r.content)["uics"] for r in module.of("lookup")]


def _limited(retry_after: str | None = "17") -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(429, json={"code": "user_minute"}, headers=headers)


# ───────────────────────────── resolve ─────────────────────────────


def test_one_candidate_within_300_m_is_proposed_and_nothing_is_written(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, admin: uuid.UUID
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    near = _station("ZZ Hub 01 Central", "9900001", _north(hub.lat, 120), hub.lon)
    far = _station("ZZ Hub 01 Outer", "9900002", _north(hub.lat, 900), hub.lon)
    module.search = lambda _q: httpx.Response(200, json={"stations": [far, near]})

    answer = client.post(f"{BASE}/resolve", json={})

    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "ok"
    assert body["left"] == 0
    (proposal,) = body["proposals"]
    assert proposal["hub_id"] == hub.id
    assert proposal["state"] == "proposed"
    assert proposal["candidates"] == [
        {"name": "ZZ Hub 01 Central", "uic": "9900001", "country_iso": "ZZ", "distance_m": 120}
    ]
    # The search: the hub's name, on behalf of the administrator.
    (request,) = module.of("search")
    assert json.loads(request.content) == {"q": "ZZ Hub 01"}
    assert request.headers["x-viator-user-id"] == str(admin)
    # Nothing written: no commit, the hub still unresolved, no lookup made.
    assert db.commits == 0
    assert hub.uic is None
    assert hub.uic_origin is None
    assert module.of("lookup") == []


def test_two_candidates_within_300_m_are_to_pick_nearest_first(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    a = _station("ZZ North", "9900011", _north(hub.lat, 290), hub.lon)
    b = _station("ZZ South", "9900012", _north(hub.lat, -40), hub.lon)
    module.search = lambda _q: httpx.Response(200, json={"stations": [a, b]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["state"] == "to_pick"
    assert [(c["uic"], c["distance_m"]) for c in proposal["candidates"]] == [
        ("9900012", 40),
        ("9900011", 290),
    ]


def test_no_candidate_within_300_m_is_to_pick_with_none(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    just_out = _station("ZZ Outer", "9900021", _north(hub.lat, 301), hub.lon)
    module.search = lambda _q: httpx.Response(200, json={"stations": [just_out]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal == {
        "hub_id": hub.id,
        "hub_name": hub.name,
        "state": "to_pick",
        "candidates": [],
    }


def test_a_candidate_whose_code_the_lookup_would_refuse_is_not_offered(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    short_code = _station("ZZ Short Code", "ZZ", hub.lat, hub.lon)
    module.search = lambda _q: httpx.Response(200, json={"stations": [short_code]})

    (proposal,) = client.post(f"{BASE}/resolve", json={}).json()["proposals"]

    assert proposal["candidates"] == []


def test_one_click_makes_exactly_ten_searches_one_after_the_other(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert network_coverage.RESOLVE_BATCH == 10
    assert len(module.of("search")) == 10
    assert _searched_names(module) == [f"ZZ Hub {i:02d}" for i in range(10)]
    assert [p["hub_id"] for p in body["proposals"]] == [f"zz-hub-{i:02d}" for i in range(10)]
    assert body["left"] == 15
    assert body["status"] == "ok"


def test_the_next_click_skips_the_hubs_already_shown(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]
    shown = [f"zz-hub-{i:02d}" for i in range(10)]

    body = client.post(f"{BASE}/resolve", json={"skip": shown}).json()

    assert hubs.skips == [shown]
    assert _searched_names(module) == [f"ZZ Hub {i:02d}" for i in range(10, 20)]
    assert body["left"] == 5


def test_a_429_on_the_fourth_search_stops_the_click_with_three_results(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(i) for i in range(25)]
    answers = iter(
        [httpx.Response(200, json={"stations": []})] * 3
        + [_limited("17")]
        + [httpx.Response(200, json={"stations": []})] * 30
    )
    module.search = lambda _q: next(answers)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("search")) == 4  # no further call, no retry
    assert len(body["proposals"]) == 3
    assert body["status"] == "limited"
    assert body["retry_after"] == 17
    assert "17 seconds" in body["message"]
    assert body["left"] == 22  # the fourth hub was not looked at
    assert not station_module.paused()  # a 429 starts no pause


def test_a_429_without_retry_after_says_a_minute(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1)]
    module.search = lambda _q: _limited(None)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert body["status"] == "limited"
    assert body["retry_after"] is None
    assert "a minute" in body["message"]


@pytest.mark.parametrize("status", [500, 503, 404])
def test_another_search_failure_stops_the_click_and_says_so(
    client: TestClient, db: FakeDb, hubs: Hubs, module: Module, status: int
) -> None:
    hubs.rows = [_hub(i) for i in range(5)]
    answers = iter([httpx.Response(200, json={"stations": []}), httpx.Response(status, json={})])
    module.search = lambda _q: next(answers)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.of("search")) == 2
    assert body["status"] == "unavailable"
    assert body["message"] == "The station module did not answer; nothing was changed."
    assert body["retry_after"] is None
    assert len(body["proposals"]) == 1
    assert body["left"] == 4
    assert db.commits == 0


def test_a_name_the_search_cannot_take_is_to_pick_without_a_call(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hubs.rows = [_hub(1, name="ZZ"), _hub(2)]

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert [p["state"] for p in body["proposals"]] == ["to_pick", "to_pick"]
    assert _searched_names(module) == ["ZZ Hub 02"]


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
        _hub(1, uic="9900001", origin="msmm"),
        _hub(2, uic="9900001", origin="manual"),
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
    same = _hub(1, uic="9900001", origin="msmm")
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
    client.post(f"{BASE}/resolve", json={})  # a 500 pauses the client
    calls = len(module.requests)

    body = client.post(f"{BASE}/resolve", json={}).json()

    assert len(module.requests) == calls
    assert body["status"] == "unavailable"


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


def test_the_distance_is_the_haversine_in_metres() -> None:
    assert network_coverage._distance_m(45.0, 6.0, 45.0, 6.0) == 0
    assert network_coverage._distance_m(45.0, 6.0, _north(45.0, 300), 6.0) == pytest.approx(300)
    assert network_coverage.PROPOSAL_RADIUS_M == 300


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
    assert "setHubCodeButtons(true);" in script  # also while a request runs


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


def test_a_search_row_holding_a_surrogate_is_not_proposed(
    client: TestClient, hubs: Hubs, module: Module
) -> None:
    hub = _hub(1)
    hubs.rows = [hub]
    rows = [_station("ZZ \ud800", "9900001", hub.lat, hub.lon)]
    payload = json.dumps({"stations": rows}).encode("ascii")
    module.search = lambda _q: httpx.Response(
        200, content=payload, headers={"Content-Type": "application/json"}
    )

    answer = client.post(f"{BASE}/resolve", json={})

    assert answer.status_code == 200
    assert json.loads(answer.content.decode("utf-8"))["proposals"][0]["candidates"] == []


# ───────────────────── soft-deleted hubs, the 300 m edge ─────────────────────


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


@pytest.mark.parametrize(("distance", "kept"), [(300.0, True), (300.000001, False)])
def test_a_station_exactly_300_m_away_is_a_candidate(
    monkeypatch: pytest.MonkeyPatch, distance: float, kept: bool
) -> None:
    monkeypatch.setattr(network_coverage, "_distance_m", lambda *_a: distance)

    proposal = network_coverage._proposal(_hub(1), [_station("ZZ Edge", "9900001", 45.0, 6.0)])

    assert (proposal.state == "proposed") is kept
    assert len(proposal.candidates) == int(kept)
