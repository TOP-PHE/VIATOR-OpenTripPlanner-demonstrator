"""The coverage cell modal names an ÖBB leg's stops by the station module's
station for ÖBB's stop number.

ÖBB HAFAS gives each stop its own number (`extId`: 8100042 for
Bischofshofen), kept on the leg. The page asks POST
/api/admin/network-coverage/oebb-stops/resolve which station of the module
each number is linked to; the route asks the module one `POST
/internal/v1/stops/resolve {"series", "code"}` per number not cached, in the
series `COVERAGE_OEBB_STOP_SERIES`. Pinned here, the module never reached
(`httpx.MockTransport`):

- the client call: the body (never the URL), its own headers, the rows
  (how each matched), the values it refuses before any call, no pause after
  a failure;
- the route: the setting's series, the cache, the first failure stopping the
  rest (`unanswered`), the module off, the body refused;
- the page: the stop's number on its span, the request in the body, the name
  written as text, the legend; the setting on the Config page.

Invented values only (ZZ stations, numbers with 99).
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app import config_service, station_module
from app.api.admin import network_coverage as api
from app.config_schema import CONFIG_SCHEMA
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
SERIES = "ZZ_stop_key"
REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "app" / "templates" / "admin" / "network_coverage.html"
CONFIG_PAGE = REPO / "app" / "templates" / "admin" / "config.html"


def _match(name: str = "Zzdorf Hbf", uic: str | None = "9900001", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "latitude": 47.99,
        "longitude": 13.99,
        "country_iso": "ZZ",
        "uic": uic,
        "matched_by": "code",
        "asserted": None,
        "tier": None,
        **extra,
    }


class Module:
    """The module's stand-in: answers each resolve from `by_code` (no match
    for a code it lacks) unless `handler` is set; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.by_code: dict[str, list[dict[str, Any]]] = {}
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        code = json.loads(request.content)["code"]
        return httpx.Response(200, json={"matches": self.by_code.get(code, [])})

    def codes(self) -> list[str]:
        return [json.loads(r.content)["code"] for r in self.requests]


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
    api._oebb_stop_cache.clear()
    yield stand_in
    station_module.reset()
    api._oebb_stop_cache.clear()


# ───────────────────────────── the client call ─────────────────────────────


async def test_resolve_posts_series_and_code_in_the_body(module: Module) -> None:
    user = uuid.uuid4()
    module.by_code["8199042"] = [_match()]

    outcome = await station_module.resolve_code_outcome(SERIES, "8199042", user)

    assert outcome.reason == "ok"
    assert outcome.rows == [_match()]
    (request,) = module.requests
    assert request.method == "POST"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/stops/resolve"
    assert request.url.query == b""
    assert "8199042" not in str(request.url)
    assert json.loads(request.content) == {"series": SERIES, "code": "8199042"}
    assert request.headers["x-viator-user-id"] == str(user)
    assert request.headers["content-type"] == "application/json"


async def test_resolve_without_a_series_sends_null(module: Module) -> None:
    await station_module.resolve_code_outcome(None, "8199042", uuid.uuid4())
    assert json.loads(module.requests[0].content) == {"series": None, "code": "8199042"}


async def test_resolve_rows_keep_how_they_matched_and_drop_bad_rows(module: Module) -> None:
    link = _match("Zzburg", "9900002", matched_by="link", asserted=True, tier="T0_code")
    no_merits = _match("Zzheim", None)
    bad = [
        _match(name=""),
        _match(latitude=None),
        _match(uic=99),
        "not a row",
    ]
    odd = _match("Zzfeld", "9900003", matched_by="guess", asserted="yes", tier="x" * 41)
    module.handler = lambda _r: httpx.Response(200, json={"matches": [link, *bad, no_merits, odd]})

    outcome = await station_module.resolve_code_outcome(SERIES, "8199042", uuid.uuid4())

    assert outcome.rows is not None
    assert [row["name"] for row in outcome.rows] == ["Zzburg", "Zzheim", "Zzfeld"]
    assert outcome.rows[0] | {} == link
    assert outcome.rows[1]["uic"] is None
    assert (outcome.rows[2]["matched_by"], outcome.rows[2]["asserted"]) == (None, None)
    assert outcome.rows[2]["tier"] is None


@pytest.mark.parametrize(
    ("series", "code"),
    [(SERIES, ""), (SERIES, "8" * 101), (SERIES, "81\x0099"), ("", "8199042"), ("\ud800", "1")],
)
async def test_a_value_the_module_refuses_raises_before_any_call(
    module: Module, series: str, code: str
) -> None:
    with pytest.raises(ValueError, match="refuses"):
        await station_module.resolve_code_outcome(series, code, uuid.uuid4())
    assert module.requests == []


async def test_a_failed_resolve_never_starts_the_pause(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        429, json={"detail": "ZZ", "code": "user_minute"}, headers={"Retry-After": "17"}
    )
    outcome = await station_module.resolve_code_outcome(SERIES, "8199042", uuid.uuid4())
    assert (outcome.rows, outcome.reason, outcome.retry_after) == (None, "status_429", 17)

    module.handler = lambda _r: httpx.Response(200, json={"stations": []})  # no `matches`
    outcome = await station_module.resolve_code_outcome(SERIES, "8199042", uuid.uuid4())
    assert (outcome.rows, outcome.reason) == (None, "shape")
    assert station_module.paused() is False


async def test_resolve_makes_no_call_without_the_settings(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    outcome = await station_module.resolve_code_outcome(SERIES, "8199042", uuid.uuid4())
    assert (outcome.rows, outcome.reason) == (None, "off")
    assert module.requests == []


# ───────────────────────────── the route ─────────────────────────────


class _Admin:
    id = uuid.UUID("99999999-9999-4999-8999-999999999999")


@pytest.fixture
def series(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    def set_series(value: Any) -> None:
        monkeypatch.setattr(
            config_service, "get_all", lambda _db, **_kw: {"COVERAGE_OEBB_STOP_SERIES": value}
        )

    set_series(SERIES)
    return set_series


async def _resolve(codes: Any) -> api.OebbStopsResponse:
    return await api.resolve_oebb_stops(body={"codes": codes}, db=object(), admin=_Admin())  # type: ignore[arg-type]


async def test_the_route_answers_each_number_with_its_station_or_none(
    module: Module, series: Callable[[Any], None]
) -> None:
    module.by_code["8199042"] = [_match("Zzdorf Hbf", "9900001"), _match("Zzdorf Süd", "9900002")]

    answer = await _resolve(["8199042", "999099"])

    assert answer.status == "ok"
    assert answer.series == SERIES
    station = answer.stations["8199042"]
    assert station is not None
    assert (station.name, station.uic, station.others) == ("Zzdorf Hbf", "9900001", 1)
    assert answer.stations["999099"] is None
    assert answer.unanswered == []
    assert module.codes() == ["8199042", "999099"]
    assert {json.loads(r.content)["series"] for r in module.requests} == {SERIES}
    assert {r.headers["x-viator-user-id"] for r in module.requests} == {str(_Admin.id)}


async def test_a_number_answered_once_is_not_asked_again(
    module: Module, series: Callable[[Any], None]
) -> None:
    module.by_code["8199042"] = [_match()]
    await _resolve(["8199042", "999099"])
    module.requests.clear()

    answer = await _resolve(["999099", "8199042", "8199043"])

    assert module.codes() == ["8199043"]
    assert answer.stations["8199042"] is not None
    assert answer.stations["999099"] is None


async def test_another_series_is_asked_again(module: Module, series: Callable[[Any], None]) -> None:
    await _resolve(["8199042"])
    series("ZZ_other_key")
    module.requests.clear()
    await _resolve(["8199042"])
    assert module.codes() == ["8199042"]


async def test_the_cache_keeps_at_most_its_cap(
    module: Module, series: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "OEBB_STOP_CACHE_MAX", 2)
    await _resolve(["8199041", "8199042", "8199043"])
    assert [key[1] for key in api._oebb_stop_cache] == ["8199042", "8199043"]


async def test_an_expired_answer_is_asked_again(
    module: Module, series: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "OEBB_STOP_CACHE_SECONDS", -1.0)
    await _resolve(["8199042"])
    await _resolve(["8199042"])
    assert module.codes() == ["8199042", "8199042"]


async def test_the_first_failure_stops_the_rest(
    module: Module, series: Callable[[Any], None]
) -> None:
    calls = {"n": 0}

    def second_is_limited(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(
                429, json={"detail": "ZZ", "code": "user_minute"}, headers={"Retry-After": "17"}
            )
        return httpx.Response(200, json={"matches": [_match()]})

    module.handler = second_is_limited

    answer = await _resolve(["8199041", "8199042", "8199043"])

    assert answer.status == "limited"
    assert answer.retry_after == 17
    assert answer.message is not None
    assert "17 seconds" in answer.message
    assert list(answer.stations) == ["8199041"]
    assert answer.unanswered == ["8199042", "8199043"]
    assert len(module.requests) == 2  # never retried, the rest never asked


async def test_with_the_module_off_nothing_is_asked(
    module: Module, series: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")
    answer = await _resolve(["8199042"])
    assert answer.status == "unavailable"
    assert answer.unanswered == ["8199042"]
    assert module.requests == []


@pytest.mark.parametrize(
    ("value", "expected"), [("", None), ("  ", None), (None, None), (" ZZ_key ", "ZZ_key")]
)
async def test_an_empty_setting_means_no_series(
    module: Module, series: Callable[[Any], None], value: Any, expected: str | None
) -> None:
    series(value)
    answer = await _resolve(["8199042"])
    assert answer.series == expected
    assert json.loads(module.requests[0].content)["series"] == expected


async def test_a_series_the_module_refuses_is_said_and_nothing_asked(
    module: Module, series: Callable[[Any], None]
) -> None:
    series("x" * 101)
    answer = await _resolve(["8199042"])
    assert answer.status == "unavailable"
    assert answer.message is not None
    assert "COVERAGE_OEBB_STOP_SERIES" in answer.message
    assert module.requests == []


@pytest.mark.parametrize(
    "codes",
    [
        [],
        ["8199042", "8199042"],
        ["81 99"],
        ["<b>"],
        ["9" * 41],
        [f"8199{i:03d}" for i in range(41)],
    ],
)
async def test_a_body_out_of_shape_is_a_422_naming_no_value(
    module: Module, series: Callable[[Any], None], codes: list[str]
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _resolve(codes)
    assert refused.value.status_code == 422
    assert "<b>" not in str(refused.value.detail)
    assert module.requests == []


def test_the_route_is_for_platform_admins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fcntl")  # app.main needs it: Linux CI runs this one
    from fastapi.testclient import TestClient

    from app.auth import tokens
    from app.main import app

    path = "/api/admin/network-coverage/oebb-stops/resolve"
    client = TestClient(app)
    assert client.post(path, json={"codes": ["8199042"]}).status_code in (401, 403)
    user = uuid.uuid4()
    cookie = tokens.issue_jwt(user, f"zz-{user.hex[:8]}@example.invalid", "content_manager")
    client.cookies.set(settings.jwt_cookie_name, cookie)
    assert client.post(path, json={"codes": ["8199042"]}).status_code == 403


# ───────────────────────────── the setting and the page ─────────────────────────────


def test_the_setting_defaults_to_the_series_of_the_delfi_stop_keys() -> None:
    assert CONFIG_SCHEMA["COVERAGE_OEBB_STOP_SERIES"] == {
        "type": "str",
        "default": "DELFI_stop_key",
    }
    assert "'COVERAGE_OEBB_STOP_SERIES'" in CONFIG_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    match = re.search(rf"^(?:async )?function {name}\(.*?^}}$", text, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def test_each_oebb_stop_carries_its_number_escaped(page: str) -> None:
    leg = _function(page, "renderModalOebbLeg")
    assert "${oebbStopHtml(leg, 'from')} → ${oebbStopHtml(leg, 'to')}" in leg
    stop = _function(page, "oebbStopHtml")
    assert "' data-oebb-code=\"' + escHTML(number) + '\"'" in stop
    assert "escHTML(oebbName)" in stop
    assert "escHTML(label)" in stop
    number = _function(page, "oebbStopNumber")
    assert "leg[end + '_id_raw']" in number
    assert "token.replace(/^(UIC|OEBB):/, '')" in number


def test_the_page_asks_in_the_body_and_names_stops_as_text(page: str) -> None:
    resolve = _function(page, "resolveModalOebbStops")
    assert "fetch('/api/admin/network-coverage/oebb-stops/resolve', {" in resolve
    assert "method: 'POST'" in resolve
    assert "body: JSON.stringify({ codes: numbers })" in resolve
    assert ".slice(0, OEBB_STOPS_PER_CALL)" in resolve
    assert "!OEBB_STOP_STATIONS.has(number)" in resolve
    apply = _function(page, "applyOebbStopStation")
    assert "span.textContent = station.name;" in apply
    assert "innerHTML" not in apply  # a station name is a third party's text


def test_the_modal_names_its_stops_once_rendered_and_after_a_live_verify(page: str) -> None:
    assert (
        "modalBody.innerHTML = tripsHtml + rerunLink;\n  resolveModalOebbStops(modalBody);" in page
    )
    refresh = _function(page, "refreshModalOebbColumn")
    assert "resolveModalOebbStops(oebbBody);" in refresh
    assert "${OEBB_STOPS_LEGEND}" in _function(page, "renderModalControls")
    assert "ÖBB stops: the station module" in page
