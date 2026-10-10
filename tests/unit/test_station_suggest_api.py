"""Unit tests for `POST /api/stations/suggest` (app/api/station_suggest.py).

Driven through the real application (`app.main.app`), slowapi middleware
included, so the "no VIATOR-side limit" test proves what production does.
No database: `get_db` is overridden and the fallback query is replaced by a
stand-in that records its calls (the query itself is proved on PostgreSQL in
tests/integration/test_station_suggest_fallback.py). The module is never
reached: its calls go through `httpx.MockTransport`. Invented values only.
"""

from __future__ import annotations

import json
import logging
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
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.settings import settings

ROUTE = "/api/stations/suggest"
MODULE_URL = "http://msmm.invalid:8000"

MODULE_ROW = {
    "name": "Zzville Central",
    "latitude": 45.5,
    "longitude": 6.25,
    "country_iso": "ZZ",
    "uic": "9900001",
}
VIATOR_ROW = {
    "name": "Zzton",
    "latitude": 46.0,
    "longitude": 7.0,
    "country_iso": "ZZ",
    "uic": "9900002",
    "source": "viator",
}


class Fallback:
    """Stands in for `fallback_rows`: records the text it was asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, db: Any, q: str) -> list[dict[str, Any]]:
        self.calls.append(q)
        return [dict(VIATOR_ROW)]


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


def _login(role: str = "end_user") -> tuple[uuid.UUID, dict[str, str]]:
    """A fresh user id and the cookie of its session."""
    user_id = uuid.uuid4()
    jwt = tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)
    return user_id, {settings.jwt_cookie_name: jwt}


def _post(client: TestClient, body: Any, cookies: dict[str, str] | None = None, **kw: Any):
    client.cookies.clear()
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value)
    return client.post(ROUTE, json=body, **kw)


# ───────────────────────────── who is served ─────────────────────────────


@pytest.mark.parametrize("role", ["end_user", "content_manager", "platform_admin"])
def test_every_logged_in_role_is_served(
    client: TestClient, module_off: None, fallback: Fallback, role: str
) -> None:
    _, cookies = _login(role)

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.status_code == 200
    assert answer.json() == [VIATOR_ROW]


def test_an_anonymous_request_is_refused(client: TestClient, module_on: str, module: Module):
    answer = _post(client, {"q": "Zzville"})

    assert answer.status_code == 401
    assert module.requests == []


def test_the_route_is_post_only(client: TestClient, module_off: None) -> None:
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))
    assert client.get(ROUTE, params={"q": "Zzville"}).status_code == 405


# ───────────────────────────── the text ─────────────────────────────

# The 422 sentences of the route, written out: a text rule names itself; a
# body that is not `{"q": <text>}` gets one fixed sentence.
LENGTH_REFUSED = "q must be 3 to 100 characters long"
CONTROL_REFUSED = "q must not contain a control character or a lone surrogate"
BODY_REFUSED = 'The body must be a JSON object {"q": <text>} and nothing else.'


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param({"q": ""}, LENGTH_REFUSED, id="empty"),
        pytest.param({"q": "zz"}, LENGTH_REFUSED, id="two-characters"),
        pytest.param({"q": "  zz  "}, LENGTH_REFUSED, id="two-once-trimmed"),
        pytest.param({"q": "z" * 101}, LENGTH_REFUSED, id="a-hundred-and-one"),
        pytest.param({"q": "zz\x00zz"}, CONTROL_REFUSED, id="a-null"),
        pytest.param({"q": "zz\tzz"}, CONTROL_REFUSED, id="a-tab"),
        pytest.param({"q": "zz\nzz"}, CONTROL_REFUSED, id="a-line-end"),
        pytest.param({"q": "zzzz\r"}, CONTROL_REFUSED, id="a-carriage-return-at-the-end"),
        pytest.param({"q": "zz\x1bzz"}, CONTROL_REFUSED, id="an-escape"),
        pytest.param({"q": "zz\x7fzz"}, CONTROL_REFUSED, id="delete"),
        pytest.param({"q": "zz\x85zz"}, CONTROL_REFUSED, id="a-c1-control"),
        pytest.param({"q": "u\u0308u\u0308"}, LENGTH_REFUSED, id="two-letters-once-composed"),
        pytest.param({"q": 999}, BODY_REFUSED, id="q-a-number"),
        pytest.param({"q": ["Zzville"]}, BODY_REFUSED, id="q-a-list"),
        pytest.param({"q": "Zzville", "size": 50}, BODY_REFUSED, id="an-unknown-field"),
        pytest.param({}, BODY_REFUSED, id="nothing"),
    ],
)
def test_a_text_the_module_would_refuse_is_422_and_nothing_is_searched(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    body: dict[str, Any],
    detail: str,
) -> None:
    """A text refused by the text rules says which rule; a body of the wrong
    shape gets the one fixed sentence. Neither stands in for the other."""
    _, cookies = _login()

    answer = _post(client, body, cookies)

    assert answer.status_code == 422
    assert answer.json() == {"detail": detail}
    assert module.requests == []
    assert fallback.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"q": "zz\\ud800z"}', id="a-lone-high-surrogate"),
        pytest.param(b'{"q": "\\udfffzzz"}', id="a-lone-low-surrogate-first"),
    ],
)
def test_a_lone_surrogate_is_422_and_nothing_is_searched(
    client: TestClient, module_on: str, module: Module, fallback: Fallback, raw: bytes
) -> None:
    """JSON can carry a lone surrogate, which no UTF-8 text holds; sent on,
    it makes the module fail with a 500, which pauses it for every user."""
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))

    answer = client.post(ROUTE, content=raw, headers={"Content-Type": "application/json"})

    assert answer.status_code == 422
    assert answer.json() == {"detail": CONTROL_REFUSED}
    assert answer.json()["detail"] != station_suggest._BODY_REFUSED
    assert module.requests == []
    assert fallback.calls == []


class UntouchedDb:
    """Stands in for the session of `get_db`: any use of it is recorded."""

    def __init__(self) -> None:
        self.used: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.used.append(name)
        raise AssertionError(f"the database was used: {name}")


# Bodies the framework's own 422 could not answer: its answer copies the
# input, and a lone surrogate (in a value, in a field name, in a list) or
# bytes that are not UTF-8 cannot be written as UTF-8 JSON; nor can NaN and
# infinities, which Python's JSON reader accepts; nor could a deeply nested
# body be. Each was a 500.
UNANSWERABLE_BODIES = [
    pytest.param(
        b'{"q": "zzz", "x": "\\ud800"}', "application/json", id="surrogate-in-other-field"
    ),
    pytest.param(b'{"q": "zzz", "\\ud800": 1}', "application/json", id="surrogate-as-field-name"),
    pytest.param(b'{"q": ["\\ud800"]}', "application/json", id="q-a-list-with-a-surrogate"),
    pytest.param(
        b'{"q": {"zz": "\\udfff"}}', "application/json", id="q-an-object-with-a-surrogate"
    ),
    pytest.param(b'["\\ud800zz"]', "application/json", id="a-list-not-an-object"),
    pytest.param(b'"\\ud800zz"', "application/json", id="a-text-not-an-object"),
    pytest.param(b'{"q": "zz\xff\xfezz"}', "text/plain", id="not-utf8-not-json-type"),
    pytest.param(b'{"q": NaN}', "application/json", id="q-nan"),
    pytest.param(b'{"q": "zzz", "n": Infinity}', "application/json", id="infinity-in-other-field"),
    pytest.param(b'{"q": 1e999}', "application/json", id="q-a-number-too-large"),
    pytest.param(
        b'{"q": "zzz", "x": ' + b"[" * 1000 + b"]" * 1000 + b"}",
        "application/json",
        id="nested-a-thousand-deep",
    ),
    pytest.param(
        b'{"q": "zzz", "\xed\xa0\x80": 1}', "application/json", id="raw-surrogate-in-name"
    ),
]


@pytest.mark.parametrize(("raw", "content_type"), UNANSWERABLE_BODIES)
def test_a_body_the_framework_could_not_echo_is_the_routes_own_422(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    raw: bytes,
    content_type: str,
) -> None:
    db = UntouchedDb()
    app.dependency_overrides[get_db] = lambda: db
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))

    answer = client.post(ROUTE, content=raw, headers={"Content-Type": content_type})

    assert answer.status_code == 422
    assert answer.headers["content-type"] == "application/json"
    text = answer.content.decode("utf-8")
    assert json.loads(text) == {"detail": BODY_REFUSED}
    for piece in ("ud800", "udfff", "\\x", "zzz", 'zz"', "Input should", "NaN", "Infinity", "[["):
        assert piece not in text
    assert module.requests == []
    assert fallback.calls == []
    assert db.used == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"q": 999}', id="q-a-number"),
        pytest.param(b'{"q": "Zzville", "size": 50}', id="an-unknown-field"),
        pytest.param(b"{}", id="nothing"),
    ],
)
def test_every_refusal_of_the_body_is_the_same_fixed_sentence(
    client: TestClient, module_on: str, module: Module, fallback: Fallback, body: bytes
) -> None:
    _, cookies = _login()
    client.cookies.clear()
    client.cookies.set(*next(iter(cookies.items())))

    answer = client.post(ROUTE, content=body, headers={"Content-Type": "application/json"})

    assert answer.status_code == 422
    assert answer.json() == {"detail": BODY_REFUSED}
    assert module.requests == []
    assert fallback.calls == []


@pytest.mark.parametrize(("raw", "content_type"), UNANSWERABLE_BODIES)
def test_an_anonymous_request_is_refused_before_its_body_is_read(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    raw: bytes,
    content_type: str,
) -> None:
    """As before the route checked its body itself: the login check first."""
    client.cookies.clear()

    answer = client.post(ROUTE, content=raw, headers={"Content-Type": content_type})

    assert answer.status_code == 401
    assert module.requests == []
    assert fallback.calls == []


@pytest.mark.parametrize("logged_in", [False, True], ids=["anonymous", "logged-in"])
def test_a_body_that_is_not_json_is_still_the_frameworks_422_without_the_input(
    client: TestClient, module_on: str, module: Module, logged_in: bool
) -> None:
    """Unchanged: the framework refuses unparsable JSON before the login
    check; its answer names the position, never the text."""
    client.cookies.clear()
    if logged_in:
        _, cookies = _login()
        client.cookies.set(*next(iter(cookies.items())))

    answer = client.post(
        ROUTE, content=b'{"q": "zzz\\ud800', headers={"Content-Type": "application/json"}
    )

    assert answer.status_code == 422
    (error,) = answer.json()["detail"]
    assert error["type"] == "json_invalid"
    assert error["input"] == {}
    assert "zzz" not in answer.text
    assert module.requests == []


def test_the_published_request_body_is_still_the_models_schema() -> None:
    published = app.openapi()
    operation = published["paths"][ROUTE]["post"]
    # Inline, not a named component: the route no longer lets the framework
    # read the body as the model (see the route's docstring).
    schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert operation["requestBody"]["required"] is True
    assert schema["properties"] == {"q": {"type": "string", "title": "Q"}}
    assert schema["required"] == ["q"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("given", "searched"),
    [
        pytest.param("Zzville", "Zzville", id="as-it-is"),
        pytest.param("  Zzville  ", "Zzville", id="spaces-at-both-ends"),
        pytest.param("Zz   upon    Zz", "Zz upon Zz", id="runs-of-spaces"),
        pytest.param("Zz\u00a0\u2003Halt", "Zz Halt", id="other-white-space"),
        pytest.param("Zzu\u0308rich", "Zz\u00fcrich", id="nfc"),
        pytest.param("Zz%_\\", "Zz%_\\", id="wildcards-kept-as-written"),
        pytest.param("zzz", "zzz", id="three-characters"),
        pytest.param("z z", "z z", id="three-with-a-space"),
        pytest.param("z" * 100, "z" * 100, id="a-hundred-characters"),
        pytest.param(" " + "z" * 100 + " ", "z" * 100, id="a-hundred-once-trimmed"),
        pytest.param("\u00fc" * 3, "\u00fc" * 3, id="three-letters-not-ascii"),
    ],
)
def test_the_text_is_normalised_as_the_module_does_and_sent_so(
    client: TestClient, module_on: str, module: Module, given: str, searched: str
) -> None:
    _, cookies = _login()

    answer = _post(client, {"q": given}, cookies)

    assert answer.status_code == 200
    assert json.loads(module.requests[0].content) == {"q": searched}
    assert station_suggest.normalise_query(given) == searched


# ───────────────────────────── the module ─────────────────────────────


def test_the_module_rows_pass_through_tagged_msmm(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    _, cookies = _login()

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.json() == [{**MODULE_ROW, "source": "msmm"}]
    assert fallback.calls == []


@pytest.mark.parametrize(
    "field", ["name", "uic", "country_iso"], ids=["in-the-name", "in-the-code", "in-the-country"]
)
def test_a_module_row_holding_a_lone_surrogate_is_dropped_never_a_500(
    client: TestClient, module_on: str, module: Module, fallback: Fallback, field: str
) -> None:
    """A module may write `\\ud800` in its JSON; such a row cannot be written
    in VIATOR's UTF-8 answer and is dropped, the others served."""
    broken = {**MODULE_ROW, "uic": "9900003", field: "Zz\ud800"}
    body = json.dumps({"stations": [broken, MODULE_ROW]}).encode("ascii")
    module.response = httpx.Response(
        200, content=body, headers={"Content-Type": "application/json"}
    )
    _, cookies = _login()

    answer = _post(client, {"q": "Zzville"}, cookies)

    assert answer.status_code == 200
    assert json.loads(answer.content.decode("utf-8")) == [{**MODULE_ROW, "source": "msmm"}]
    assert fallback.calls == []


def test_an_empty_answer_of_the_module_is_returned_as_it_is(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    module.response = httpx.Response(200, json={"stations": []})
    _, cookies = _login()

    assert _post(client, {"q": "Zzville"}, cookies).json() == []
    assert fallback.calls == []


def test_the_module_gets_the_jwt_user_and_no_header_of_the_request(
    client: TestClient, module_on: str, module: Module
) -> None:
    user_id, cookies = _login()
    forged = str(uuid.uuid4())
    jwt = cookies[settings.jwt_cookie_name]

    _post(
        client,
        {"q": "Zzville"},
        cookies,
        headers={
            "X-Viator-User-Id": forged,
            "Origin": "http://elsewhere.invalid",
            "X-Forwarded-For": "192.0.2.99",
            "X-Zz-Probe": "zz",
        },
    )

    (request,) = module.requests
    assert request.headers["x-viator-user-id"] == str(user_id)
    assert request.headers["authorization"] == f"Bearer {module_on}"
    sent = " ".join(f"{k}: {v}" for k, v in request.headers.items())
    for value in (forged, jwt, "elsewhere.invalid", "192.0.2.99", "x-zz-probe"):
        assert value not in sent
    assert "cookie" not in request.headers


def test_a_bearer_session_is_not_forwarded_either(
    client: TestClient, module_on: str, module: Module
) -> None:
    user_id, cookies = _login()
    jwt = cookies[settings.jwt_cookie_name]
    client.cookies.clear()

    answer = client.post(ROUTE, json={"q": "Zzville"}, headers={"Authorization": f"Bearer {jwt}"})

    assert answer.status_code == 200
    (request,) = module.requests
    assert request.headers["authorization"] == f"Bearer {module_on}"
    assert request.headers["x-viator-user-id"] == str(user_id)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(401, json={"code": "token"}), id="401"),
        pytest.param(httpx.Response(403, json={"code": "token"}), id="403"),
        pytest.param(httpx.Response(404, json={"detail": "Not Found"}), id="404"),
        pytest.param(httpx.Response(405, json={"detail": "zz"}), id="405"),
        pytest.param(httpx.Response(413, json={"code": "size"}), id="413"),
        pytest.param(httpx.Response(415, json={"code": "size"}), id="415"),
        pytest.param(httpx.Response(422, json={"code": "user"}), id="422"),
        pytest.param(httpx.Response(429, json={"code": "user_minute"}), id="429"),
        pytest.param(httpx.Response(500, json={"error_id": "zz"}), id="500"),
        pytest.param(httpx.Response(503, json={"code": "no_build"}), id="503-no-build"),
        pytest.param(httpx.Response(503, json={"code": "database"}), id="503-database"),
        pytest.param(httpx.Response(503, json={"code": "busy"}), id="503-busy"),
        pytest.param(httpx.Response(200, text="ZZ not json"), id="not-json"),
        pytest.param(httpx.Response(200, json={"rows": []}), id="wrong-shape"),
    ],
)
def test_each_failure_of_the_module_gives_viators_own_list(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    response: httpx.Response,
) -> None:
    module.response = response
    _, cookies = _login()

    answer = _post(client, {"q": "  Zzt  "}, cookies)

    assert answer.status_code == 200
    assert answer.json() == [VIATOR_ROW]
    assert fallback.calls == ["Zzt"]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_an_unreachable_module_gives_viators_own_list(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    error: type[httpx.HTTPError],
) -> None:
    module.error = error
    _, cookies = _login()

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.json() == [VIATOR_ROW]
    assert fallback.calls == ["Zzt"]


def test_without_the_setting_the_fallback_is_used_without_any_call(
    client: TestClient, module_off: None, module: Module, fallback: Fallback
) -> None:
    _, cookies = _login()

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.json() == [VIATOR_ROW]
    assert module.requests == []
    assert fallback.calls == ["Zzt"]


# ───────────────────────────── no VIATOR-side limit ─────────────────────────────


def test_two_users_can_each_make_more_than_120_calls_a_minute(
    client: TestClient, module_on: str, module: Module
) -> None:
    """No slowapi limit on this route: behind nginx, slowapi would key every
    user on nginx's address, one counter for the whole site. The limits are
    the module's, per person."""
    module.response = httpx.Response(200, json={"stations": []})
    first, second = _login(), _login()
    statuses: list[int] = []

    for _ in range(121):
        for _user, cookies in (first, second):
            statuses.append(_post(client, {"q": "Zzville"}, cookies).status_code)

    assert statuses == [200] * 242
    assert len(module.requests) == 242
    users = {r.headers["x-viator-user-id"] for r in module.requests}
    assert users == {str(first[0]), str(second[0])}


# ───────────────────────────── the fallback query ─────────────────────────────


def test_the_fallback_query_binds_the_text_and_never_writes_it_into_the_statement() -> None:
    """VIATOR's SQLAlchemy tracing records the statement's text: the typed text
    must only ever be a bound parameter."""
    from sqlalchemy.dialects import postgresql

    marker = f"Zz{secrets.token_hex(6)}"
    compiled = station_suggest.fallback_statement(marker).compile(dialect=postgresql.dialect())

    assert marker not in str(compiled)
    assert set(compiled.params.values()) >= {f"%{marker}%", marker}
    assert "LIMIT" in str(compiled)
    # The LIKE names its escape character: PostgreSQL's default happens to be
    # the same backslash, but the statement must not rely on it.
    assert "ESCAPE '" + chr(92) in str(compiled)


@pytest.mark.parametrize(
    ("given", "escaped"),
    [
        pytest.param("Zz", "Zz", id="plain"),
        pytest.param("100%", "100\\%", id="percent"),
        pytest.param("zz_zz", "zz\\_zz", id="underscore"),
        pytest.param("zz\\zz", "zz\\\\zz", id="backslash"),
        pytest.param("\\%", "\\\\\\%", id="backslash-then-percent"),
    ],
)
def test_the_like_pattern_escapes_its_three_special_characters(given: str, escaped: str) -> None:
    assert station_suggest.escape_like(given) == escaped


def test_a_user_without_a_viator_id_gets_the_fallback_without_any_call(
    client: TestClient, module_on: str, module: Module, fallback: Fallback
) -> None:
    """The basic-auth shadow user has no id: no identity to give the module.
    Unreachable through `require_logged_in` (JWT users only), kept as a guard."""
    from app.security import CurrentUser, require_logged_in

    app.dependency_overrides[require_logged_in] = lambda: CurrentUser(
        id=None, username="zz-basic", role="platform_admin"
    )
    try:
        answer = _post(client, {"q": "Zzt"})
    finally:
        app.dependency_overrides.pop(require_logged_in, None)

    assert answer.json() == [VIATOR_ROW]
    assert module.requests == []
    assert fallback.calls == ["Zzt"]


# ───────────────────────── errors that are not answers ─────────────────────────


def test_an_address_httpx_refuses_gives_viators_own_list(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "http://[::1:8000")
    _, cookies = _login()

    answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.status_code == 200
    assert answer.json() == [VIATOR_ROW]
    assert module.requests == []


def test_a_token_httpx_cannot_encode_gives_viators_own_list_and_stays_out_of_the_log(
    client: TestClient,
    module_on: str,
    module: Module,
    fallback: Fallback,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "zz\u00e9\u20ac" + secrets.token_hex(8)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(token))
    _, cookies = _login()

    with caplog.at_level(logging.DEBUG):
        answer = _post(client, {"q": "Zzt"}, cookies)

    assert answer.status_code == 200
    assert answer.json() == [VIATOR_ROW]
    assert module.requests == []
    logged = caplog.text + "".join(str(r.args) + str(r.exc_info) for r in caplog.records)
    assert "station_module.fallback reason=network" in logged
    for character in ("\u00e9", "\u20ac", token[4:]):
        assert character not in logged
