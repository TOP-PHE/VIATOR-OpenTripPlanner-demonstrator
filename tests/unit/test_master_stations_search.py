"""Unit tests for `POST /api/master/stations/search` (app/api/master/stations.py).

The search route of the "Stations" admin page (MSMM step 3, design 21.3,
decision 54): the journey typeahead's search under the content-manager gate,
the origin said once for the whole answer.

Driven through the real application, slowapi middleware included. No
database: `get_db` is overridden and VIATOR's fallback query is replaced by a
stand-in that records its calls (the query itself is proved on PostgreSQL in
tests/integration/test_station_suggest_fallback.py). The module is reached
only through `httpx.MockTransport`. Invented values only.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import station_module
from app.api import station_suggest
from app.api.master import stations
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.settings import settings

ROUTE = "/api/master/stations/search"
SUGGEST = "/api/stations/suggest"
MODULE_URL = "http://msmm.invalid:8000"

MODULE_ROW = {
    "name": "Zzville Central",
    "latitude": 45.5,
    "longitude": 6.25,
    "country_iso": "ZZ",
    "uic": "9900001",
}
# As `fallback_rows` gives a row: the typeahead's `source` tag included.
VIATOR_ROW = {
    "name": "Zzton",
    "latitude": 46.0,
    "longitude": 7.0,
    "country_iso": "ZZ",
    "uic": "9900002",
    "source": "viator",
}
# The same row on the page: the five fields.
SHOWN_VIATOR_ROW = {key: value for key, value in VIATOR_ROW.items() if key != "source"}

LENGTH_REFUSED = "q must be 3 to 100 characters long"
CONTROL_REFUSED = "q must not contain a control character or a lone surrogate"
BODY_REFUSED = 'The body must be a JSON object {"q": <text>} and nothing else.'


class Fallback:
    """Stands in for `fallback_rows`: records the text it was asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows = [dict(VIATOR_ROW)]

    def __call__(self, db: Any, q: str) -> list[dict[str, Any]]:
        self.calls.append(q)
        return [dict(row) for row in self.rows]


class Module:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json={"stations": [MODULE_ROW]})
        self.error: type[httpx.HTTPError] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error("stand-in failure", request=request)  # type: ignore[call-arg]
        return self.response


@pytest.fixture
def fallback(monkeypatch: pytest.MonkeyPatch) -> Fallback:
    stand_in = Fallback()
    monkeypatch.setattr(station_suggest, "fallback_rows", stand_in)
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
    station_module.reset()
    yield stand_in
    station_module.reset()


@pytest.fixture
def module_on(monkeypatch: pytest.MonkeyPatch, module: Module) -> str:
    token = secrets.token_hex(32)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(token))
    return token


@pytest.fixture
def module_off(monkeypatch: pytest.MonkeyPatch, module: Module) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))


@pytest.fixture
def client(fallback: Fallback) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _login(role: str = "content_manager") -> tuple[uuid.UUID, dict[str, str]]:
    """A fresh user id and the cookie of its session."""
    user_id = uuid.uuid4()
    jwt = tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)
    return user_id, {settings.jwt_cookie_name: jwt}


def _post(
    client: TestClient,
    body: Any,
    cookies: dict[str, str] | None = None,
    route: str = ROUTE,
    **kw: Any,
) -> httpx.Response:
    client.cookies.clear()
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value)
    return client.post(route, json=body, **kw)


def _post_raw(client: TestClient, raw: bytes, cookies: dict[str, str]) -> httpx.Response:
    client.cookies.clear()
    for name, value in cookies.items():
        client.cookies.set(name, value)
    return client.post(ROUTE, content=raw, headers={"Content-Type": "application/json"})


# ───────────────────────────── who is served ─────────────────────────────


@pytest.mark.parametrize("role", ["content_manager", "platform_admin"])
def test_both_roles_of_the_page_are_served(
    client: TestClient, module_on: str, module: Module, role: str
) -> None:
    _, cookies = _login(role)

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.status_code == 200
    assert answer.json() == {"origin": "msmm", "stations": [MODULE_ROW]}


def test_an_end_user_is_refused_and_nothing_is_searched(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    _, cookies = _login("end_user")

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.status_code == 403
    assert module.requests == []
    assert fallback.calls == []


def test_an_anonymous_request_is_refused(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    answer = _post(client, {"q": "Zzville"})

    assert answer.status_code == 401
    assert module.requests == []
    assert fallback.calls == []


def test_an_end_user_with_a_bad_body_is_refused_before_the_body_is_read(
    client: TestClient, module_on: str, module: Module
) -> None:
    _, cookies = _login("end_user")

    answer = _post_raw(client, b'{"q": "zz\\ud800z"}', cookies)

    assert answer.status_code == 403


def test_the_route_is_post_only(client: TestClient, module_off: None) -> None:
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))

    assert client.get(ROUTE, params={"q": "Zzville"}).status_code == 405


# ───────────────────────────── the module ─────────────────────────────


def test_the_module_rows_come_back_with_origin_msmm(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    _, cookies = _login()

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.json() == {"origin": "msmm", "stations": [MODULE_ROW]}
    assert fallback.calls == []
    assert json.loads(module.requests[0].content) == {"q": "Zzville"}


def test_an_empty_answer_of_the_module_stays_msmm_without_a_trainline_row(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    module.response = httpx.Response(200, json={"stations": []})
    _, cookies = _login()

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.json() == {"origin": "msmm", "stations": []}
    assert fallback.calls == []


def test_the_module_gets_the_jwt_user_the_same_counters_as_the_typeahead(
    client: TestClient, module_on: str, module: Module
) -> None:
    """The page and the typeahead send the same person's id: the module
    counts both searches on the same per-person limits."""
    user_id, cookies = _login()
    forged = str(uuid.uuid4())

    _post(client, {"q": "Zzville"}, cookies, headers={"X-Viator-User-Id": forged})
    _post(client, {"q": "Zzville"}, cookies, route=SUGGEST)

    page, typeahead = module.requests
    assert page.url == typeahead.url
    assert page.headers["x-viator-user-id"] == str(user_id)
    assert typeahead.headers["x-viator-user-id"] == str(user_id)
    assert page.headers["authorization"] == f"Bearer {module_on}"
    assert forged not in " ".join(page.headers.values())


def test_never_more_than_ten_stations(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    many = [{**MODULE_ROW, "uic": f"99000{n:02d}", "name": f"Zzville {n}"} for n in range(15)]
    module.response = httpx.Response(200, json={"stations": many})
    _, cookies = _login()

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.json()["origin"] == "msmm"
    assert [row["uic"] for row in answer.json()["stations"]] == [r["uic"] for r in many[:10]]


def test_a_station_has_the_five_fields_and_nothing_else(
    client: TestClient, module_on: str, module: Module
) -> None:
    extra = {**MODULE_ROW, "parent_uic": "9900009", "zz_secret": "ZZ"}
    module.response = httpx.Response(200, json={"stations": [extra]})
    _, cookies = _login()

    (row,) = _post(client, {"q": "Zzville"}, cookies).json()["stations"]

    assert set(row) == set(stations.STATION_FIELDS)


# ───────────────────────────── the fallback ─────────────────────────────

FAILURES = [
    pytest.param(httpx.Response(401, json={"code": "token"}), True, id="401"),
    pytest.param(httpx.Response(403, json={"code": "token"}), True, id="403"),
    pytest.param(httpx.Response(404, json={"detail": "Not Found"}), True, id="404"),
    pytest.param(httpx.Response(500, json={"error_id": "zz"}), True, id="500"),
    pytest.param(httpx.Response(503, json={"code": "no_build"}), True, id="503-no-build"),
    pytest.param(httpx.Response(200, text="ZZ not json"), True, id="not-json"),
    pytest.param(httpx.Response(200, json={"rows": []}), True, id="wrong-shape"),
    pytest.param(httpx.Response(429, json={"code": "user_minute"}), False, id="429"),
    pytest.param(httpx.Response(503, json={"code": "busy"}), False, id="503-busy"),
]


@pytest.mark.parametrize(("response", "pauses"), FAILURES)
def test_each_failure_gives_origin_trainline_and_the_fallback_rows(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    response: httpx.Response,
    pauses: bool,
) -> None:
    """Every failure gives VIATOR's Trainline list, labelled. A 429 or a
    503 `busy` concerns that search only: the next search asks the module
    again. The others start the client's pause: the next one does not."""
    module.response = response
    _, cookies = _login()

    first = _post(client, {"q": "  Zzt  "}, cookies)
    second = _post(client, {"q": "Zzt"}, cookies)

    for answer in (first, second):
        assert answer.status_code == 200
        assert answer.json() == {"origin": "trainline", "stations": [SHOWN_VIATOR_ROW]}
    assert fallback.calls == ["Zzt", "Zzt"]
    assert len(module.requests) == (1 if pauses else 2)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_an_unreachable_module_gives_origin_trainline(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    error: type[httpx.HTTPError],
) -> None:
    module.error = error
    _, cookies = _login()

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.json() == {"origin": "trainline", "stations": [SHOWN_VIATOR_ROW]}


def test_without_the_setting_trainline_is_used_without_any_call(
    client: TestClient, module_off: None, module: Module, fallback: Fallback
) -> None:
    _, cookies = _login()

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.json() == {"origin": "trainline", "stations": [SHOWN_VIATOR_ROW]}
    assert module.requests == []
    assert fallback.calls == ["Zzt"]


def test_an_empty_trainline_answer_is_an_empty_list(
    client: TestClient, module_off: None, fallback: Fallback
) -> None:
    fallback.rows = []
    _, cookies = _login()

    assert _post(client, {"q": "Zzt"}, cookies).json() == {"origin": "trainline", "stations": []}


def test_a_user_without_a_viator_id_gets_trainline_without_any_call(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    """The basic-auth shadow user has no id: no identity to give the module."""
    from app.security import CurrentUser, require_content_manager

    app.dependency_overrides[require_content_manager] = lambda: CurrentUser(
        id=None, username="zz-basic", role="platform_admin"
    )
    try:
        answer = _post(client, {"q": "Zzt"})
    finally:
        app.dependency_overrides.pop(require_content_manager, None)

    assert answer.json() == {"origin": "trainline", "stations": [SHOWN_VIATOR_ROW]}
    assert module.requests == []


# ───────────────────────────── the text ─────────────────────────────


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param({"q": ""}, LENGTH_REFUSED, id="empty"),
        pytest.param({"q": "zz"}, LENGTH_REFUSED, id="two-characters"),
        pytest.param({"q": "  zz  "}, LENGTH_REFUSED, id="two-once-trimmed"),
        pytest.param({"q": "z" * 101}, LENGTH_REFUSED, id="a-hundred-and-one"),
        pytest.param({"q": "zz\x00zz"}, CONTROL_REFUSED, id="a-null"),
        pytest.param({"q": "zz\tzz"}, CONTROL_REFUSED, id="a-tab"),
        pytest.param({"q": "zz\x85zz"}, CONTROL_REFUSED, id="a-c1-control"),
        pytest.param({"q": 999}, BODY_REFUSED, id="q-a-number"),
        pytest.param({"q": "Zzville", "page": 1}, BODY_REFUSED, id="a-page"),
        pytest.param({"q": "Zzville", "size": 500}, BODY_REFUSED, id="a-size"),
        pytest.param({"q": "Zzville", "country": "ZZ"}, BODY_REFUSED, id="a-country"),
        pytest.param({"q": "Zzville", "mode": "context"}, BODY_REFUSED, id="a-mode"),
        pytest.param({}, BODY_REFUSED, id="nothing"),
    ],
)
def test_a_text_or_body_the_search_refuses_is_422_and_nothing_is_searched(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    body: dict[str, Any],
    detail: str,
) -> None:
    _, cookies = _login()

    answer = _post(client, body, cookies)

    assert answer.status_code == 422
    assert answer.json() == {"detail": detail}
    assert module.requests == []
    assert fallback.calls == []


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        pytest.param(b'{"q": "zz\\ud800z"}', CONTROL_REFUSED, id="a-lone-surrogate-in-q"),
        pytest.param(b'{"q": "zzz", "x": "\\ud800"}', BODY_REFUSED, id="a-surrogate-elsewhere"),
        pytest.param(b'{"q": "zzz", "\\udfff": 1}', BODY_REFUSED, id="a-surrogate-field-name"),
        pytest.param(b'{"q": "zz\\u0000zz"}', CONTROL_REFUSED, id="an-escaped-null"),
        pytest.param(b'{"q": NaN}', BODY_REFUSED, id="q-nan"),
    ],
)
def test_a_body_no_answer_could_echo_is_a_fixed_422_never_a_500(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    raw: bytes,
    detail: str,
) -> None:
    _, cookies = _login()

    answer = _post_raw(client, raw, cookies)

    assert answer.status_code == 422
    text = answer.content.decode("utf-8")
    assert json.loads(text) == {"detail": detail}
    for piece in ("ud800", "udfff", "zzz", "NaN"):
        assert piece not in text
    assert module.requests == []
    assert fallback.calls == []


def test_a_query_string_is_ignored(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    """No paging and no filter: `page`, `size` and `country` in the address
    change nothing."""
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))

    answer = client.post(
        ROUTE, params={"page": "3", "size": "500", "country": "ZZ"}, json={"q": "Zzville"}
    )

    assert answer.json() == {"origin": "msmm", "stations": [MODULE_ROW]}
    assert json.loads(module.requests[0].content) == {"q": "Zzville"}
    assert "page" not in str(module.requests[0].url)


@pytest.mark.parametrize(
    ("given", "searched"),
    [
        pytest.param("  Zzville  ", "Zzville", id="spaces-at-both-ends"),
        pytest.param("Zz   upon    Zz", "Zz upon Zz", id="runs-of-spaces"),
        pytest.param("Zzürich", "Zzürich", id="nfc"),
        pytest.param("Zz%_\\", "Zz%_\\", id="wildcards-kept-as-written"),
        pytest.param("9900001", "9900001", id="a-code"),
    ],
)
def test_the_text_is_the_typeaheads_normalised_text(
    client: TestClient, module_off: None, fallback: Fallback, given: str, searched: str
) -> None:
    """The same text reaches the search from the page as from the typeahead."""
    _, cookies = _login()

    _post(client, {"q": given}, cookies)
    _post(client, {"q": given}, cookies, route=SUGGEST)

    assert fallback.calls == [searched, searched]


# ───────────────────────────── no VIATOR-side limit ─────────────────────────────


def test_no_slowapi_limit_on_the_route(client: TestClient, module_on: str, module: Module) -> None:
    """As on the typeahead: the limits are the module's, per person."""
    module.response = httpx.Response(200, json={"stations": []})
    _, cookies = _login()

    statuses = [_post(client, {"q": "Zzville"}, cookies).status_code for _ in range(121)]

    assert statuses == [200] * 121
    assert len(module.requests) == 121


def test_the_route_reuses_the_typeaheads_functions() -> None:
    """Shared, not copied (design 21.7)."""
    import inspect

    source = inspect.getsource(stations.search_stations)
    assert "station_suggest.query_or_422(given)" in source
    assert "station_suggest.find_stations(db, q, user)" in source
    assert "limiter" not in inspect.getsource(stations)


# ───────────────────────────── the published contract ─────────────────────────────


def test_the_published_contract() -> None:
    operation = app.openapi()["paths"][ROUTE]["post"]

    assert {"403", "422"} <= set(operation["responses"])
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"] == {"q": {"type": "string", "title": "Q"}}
    assert schema["additionalProperties"] is False
    assert "parameters" not in operation
