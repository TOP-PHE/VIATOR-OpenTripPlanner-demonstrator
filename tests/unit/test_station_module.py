"""Unit tests for app.station_module — the client of the station module (MSMM).

The module is never reached: every call goes through `httpx.MockTransport`,
the same wiring as tests/unit/test_geocode_api.py. Values are invented: ZZ
names, codes beginning with 99, a `.invalid` address, a token and user ids
drawn at run time.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app import station_module
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
PAUSE_PLUS = station_module.PAUSE_SECONDS + 1

# What httpx adds to every request by itself; anything else must be ours.
_HTTPX_DEFAULTS = {"host", "accept", "accept-encoding", "connection", "user-agent"}


def _row(name: str = "Zzville Central", uic: str = "9900001", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "latitude": 45.5,
        "longitude": 6.25,
        "country_iso": "ZZ",
        "uic": uic,
        **extra,
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Module:
    """A stand-in for the module: answers with `handler`, records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(
            200, json={"stations": [_row()]}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def answer(self, status: int, body: Any = None, *, text: str | None = None) -> None:
        if text is not None:
            self.handler = lambda _r: httpx.Response(status, text=text)
        else:
            self.handler = lambda _r: httpx.Response(status, json=body)

    def fail(self, error: type[httpx.HTTPError]) -> None:
        def raise_it(request: httpx.Request) -> httpx.Response:
            raise error("stand-in failure", request=request)  # type: ignore[call-arg]

        self.handler = raise_it


@pytest.fixture
def token() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(station_module, "clock", fake)
    return fake


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch, token: str, clock: FakeClock) -> Iterator[Module]:
    stand_in = Module()
    transport = httpx.MockTransport(stand_in)
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def async_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    def sync_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_sync(*args, **kwargs)

    # alembic's fileConfig (run by the integration tests) disables every logger
    # that exists at that moment; this one must be live for caplog.
    monkeypatch.setattr(station_module.log, "disabled", False)
    monkeypatch.setattr(station_module.httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(station_module.httpx, "Client", sync_factory)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", token)
    station_module.reset()
    yield stand_in
    station_module.reset()


def _reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage().removeprefix("station_module.fallback reason=")
        for r in caplog.records
        if r.name == station_module.log.name
    ]


# ───────────────────────────── the request ─────────────────────────────


async def test_search_posts_the_text_in_the_body_with_only_its_own_headers(
    module: Module, token: str
) -> None:
    user = uuid.uuid4()

    rows = await station_module.search("Zzville", user)

    assert rows == [_row()]
    (request,) = module.requests
    assert request.method == "POST"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/stations/search"
    assert request.url.query == b""
    assert json.loads(request.content) == {"q": "Zzville"}
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["x-viator-user-id"] == str(user)
    assert request.headers["content-type"] == "application/json"
    ours = {"authorization", "x-viator-user-id", "content-type", "content-length"}
    assert set(request.headers.keys()) <= ours | _HTTPX_DEFAULTS
    for browser_or_proxy in ("cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "referer"):
        assert browser_or_proxy not in request.headers


async def test_a_trailing_slash_on_the_address_is_not_doubled(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL + "/")

    await station_module.search("Zzville", uuid.uuid4())

    assert module.requests[0].url.path == "/internal/v1/stations/search"


def test_attribution_is_a_get_with_the_token_and_no_user(module: Module, token: str) -> None:
    module.answer(200, {"statement": "ZZ statement", "sources": []})

    assert station_module.attribution() == {"statement": "ZZ statement", "sources": []}
    (request,) = module.requests
    assert request.method == "GET"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/attribution"
    assert request.headers["authorization"] == f"Bearer {token}"
    assert "x-viator-user-id" not in request.headers
    assert set(request.headers.keys()) <= {"authorization"} | _HTTPX_DEFAULTS


def test_the_token_is_not_in_the_repr_of_the_settings(token: str, module: Module) -> None:
    assert token not in repr(settings)
    assert "station_module_token" not in repr(settings)


# ───────────────────────────── the rows ─────────────────────────────


async def test_bad_rows_are_dropped_and_the_others_kept(module: Module) -> None:
    good = [
        _row("Zz <b>Markup</b> Halt", "9900002"),
        _row("Zzton", "9900003", country_iso=None),
        _row("Zz Whole Degrees", "9900004", latitude=45, longitude=6),
    ]
    bad = [
        _row(None),
        _row(""),
        _row("Zz No Latitude", latitude=None),
        _row("Zz Text Position", longitude="6.25"),
        _row("Zz Boolean Position", latitude=True),
        _row("Zz Number Country", country_iso=99),
        _row("Zz No Code", uic=None),
        {k: v for k, v in _row("Zz Missing Longitude").items() if k != "longitude"},
        "not a row",
        None,
    ]
    module.answer(200, {"stations": [bad[0], good[0], *bad[1:], good[1], good[2]]})

    rows = await station_module.search("Zzville", uuid.uuid4())

    assert rows is not None
    assert [r["name"] for r in rows] == [g["name"] for g in good]
    assert rows[0]["name"] == "Zz <b>Markup</b> Halt"  # kept as sent; the page escapes it
    assert rows[2]["latitude"] == 45.0 and isinstance(rows[2]["latitude"], float)
    assert set(rows[0]) == {"name", "latitude", "longitude", "country_iso", "uic"}


async def test_at_most_ten_rows_are_kept(module: Module) -> None:
    module.answer(200, {"stations": [_row(f"Zz {i}", f"99000{i:02d}") for i in range(15)]})

    rows = await station_module.search("Zzville", uuid.uuid4())

    assert rows is not None and len(rows) == 10
    assert rows[-1]["name"] == "Zz 9"


async def test_an_empty_list_is_an_answer_not_a_failure(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    module.answer(200, {"stations": []})

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) == []
    assert _reasons(caplog) == []


# ───────────────────────────── the failures ─────────────────────────────


def _setup_failure(module: Module, kind: str) -> None:
    """Make the stand-in fail in the way named by `kind`."""
    if kind == "timeout":
        module.fail(httpx.ReadTimeout)
    elif kind == "connect-timeout":
        module.fail(httpx.ConnectTimeout)
    elif kind == "network":
        module.fail(httpx.ConnectError)
    elif kind == "not-json":
        module.answer(200, text="<html>ZZ</html>")
    elif kind == "not-an-object":
        module.answer(200, [_row()])
    elif kind == "no-stations-list":
        module.answer(200, {"stations": {"name": "Zz"}})
    elif kind.startswith("503-"):
        module.answer(503, {"detail": "ZZ", "code": kind.removeprefix("503-")})
    else:
        module.answer(int(kind), {"detail": "ZZ", "code": "zz"})


# (what fails, the reason word logged)
PAUSING = [
    ("timeout", "timeout"),
    ("connect-timeout", "timeout"),
    ("network", "network"),
    ("401", "status_401"),
    ("403", "status_403"),
    ("404", "status_404"),
    ("405", "status_405"),
    ("413", "status_413"),
    ("415", "status_415"),
    ("500", "status_500"),
    ("503-no_build", "status_503"),
    ("503-database", "status_503"),
    ("not-json", "shape"),
    ("not-an-object", "shape"),
    ("no-stations-list", "shape"),
]
NOT_PAUSING = [
    ("429", "status_429"),
    ("422", "status_422"),
    ("503-busy", "busy"),
]


@pytest.mark.parametrize(("kind", "reason"), PAUSING + NOT_PAUSING)
async def test_each_failure_gives_none_and_its_reason_word(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None

    assert _reasons(caplog) == [reason]


@pytest.mark.parametrize(("kind", "reason"), PAUSING)
async def test_a_failure_that_says_unreachable_pauses_every_call_for_thirty_seconds(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)
    await station_module.search("Zzville", uuid.uuid4())
    module.answer(200, {"stations": [_row()]})
    calls = len(module.requests)

    clock.now += 29.9
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert station_module.attribution() is None
    assert len(module.requests) == calls  # no call to the module during the pause
    assert _reasons(caplog) == ["paused", "paused"]

    clock.now += 0.2
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]
    assert len(module.requests) == calls + 1


@pytest.mark.parametrize(("kind", "reason"), NOT_PAUSING)
async def test_a_failure_of_one_request_does_not_pause(
    module: Module, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)
    await station_module.search("Zzville", uuid.uuid4())
    module.answer(200, {"stations": [_row()]})

    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]
    assert len(module.requests) == 2


async def test_a_503_with_an_unknown_code_does_not_pause(module: Module) -> None:
    module.answer(503, text="ZZ maintenance")
    assert await station_module.search("Zzville", uuid.uuid4()) is None
    module.answer(200, {"stations": []})
    assert await station_module.search("Zzville", uuid.uuid4()) == []


@pytest.mark.parametrize(
    ("url", "token"),
    [("", ""), (MODULE_URL, ""), ("", "zz-token")],
    ids=["nothing-set", "no-token", "no-address"],
)
async def test_without_both_settings_nothing_is_called(
    module: Module,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    url: str,
    token: str,
) -> None:
    monkeypatch.setattr(settings, "station_module_url", url)
    monkeypatch.setattr(settings, "station_module_token", token)

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert station_module.attribution() is None

    assert module.requests == []
    assert _reasons(caplog) == ["off", "off"]
    assert not station_module.enabled()


async def test_the_token_and_the_text_are_never_logged(
    module: Module, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = f"Zz{secrets.token_hex(6)}"
    with caplog.at_level(logging.DEBUG):
        for kind, _reason in PAUSING + NOT_PAUSING:
            station_module.reset()
            _setup_failure(module, kind)
            await station_module.search(marker, uuid.uuid4())
            station_module.attribution()
        station_module.reset()
        module.answer(200, {"stations": [_row(marker)]})
        await station_module.search(marker, uuid.uuid4())

    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert caplog.records  # the failures did log
    assert token not in logged
    assert marker not in logged
    assert "stand-in failure" not in logged  # no exception text either


# ───────────────────────────── the attribution ─────────────────────────────


def _attribution(**group: Any) -> dict[str, Any]:
    return {
        "statement": "ZZ statement of the module.",
        "sources": [
            {
                "licence": "ZZ Licence",
                "licence_url": "https://licence.invalid/zz",
                "labels": ["ZZ Source A", "ZZ Source B"],
                **group,
            }
        ],
    }


def test_attribution_is_kept_sixty_seconds(module: Module, clock: FakeClock) -> None:
    module.answer(200, _attribution())

    first = station_module.attribution()
    clock.now += 59.9
    second = station_module.attribution()

    assert first == second == _attribution()
    assert len(module.requests) == 1

    clock.now += 0.2
    station_module.attribution()
    assert len(module.requests) == 2


def test_attribution_is_forgotten_at_the_first_failure(module: Module, clock: FakeClock) -> None:
    module.answer(200, _attribution())
    station_module.attribution()

    clock.now += 61
    module.answer(429, {"detail": "ZZ", "code": "all_minute"})  # a failure that does not pause
    assert station_module.attribution() is None

    module.answer(200, _attribution())
    assert station_module.attribution() == _attribution()
    assert len(module.requests) == 3


async def test_a_search_failure_that_pauses_also_forgets_the_attribution(
    module: Module, clock: FakeClock
) -> None:
    module.answer(200, _attribution())
    station_module.attribution()
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())

    clock.now += PAUSE_PLUS
    module.answer(200, _attribution(licence="ZZ Licence Two"))
    assert station_module.attribution() == _attribution(licence="ZZ Licence Two")


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://licence.invalid/zz", "//licence.invalid/zz", "zz"]
)
def test_a_licence_address_that_is_not_a_web_address_is_dropped_and_the_group_kept(
    module: Module, url: str
) -> None:
    module.answer(200, _attribution(licence_url=url))

    answer = station_module.attribution()

    assert answer == _attribution(licence_url=None)


def test_a_null_licence_address_stays_null(module: Module) -> None:
    module.answer(200, _attribution(licence_url=None))
    assert station_module.attribution() == _attribution(licence_url=None)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="not-an-object"),
        pytest.param({"sources": []}, id="no-statement"),
        pytest.param({"statement": "ZZ", "sources": {}}, id="sources-not-a-list"),
        pytest.param({"statement": "z" * 501, "sources": []}, id="statement-too-long"),
        pytest.param(
            {"statement": "ZZ", "sources": [_attribution()["sources"][0]] * 21},
            id="too-many-groups",
        ),
        pytest.param({"statement": "ZZ", "sources": ["ZZ"]}, id="group-not-an-object"),
        pytest.param(_attribution(licence=None), id="no-licence"),
        pytest.param(_attribution(labels="ZZ Source"), id="labels-not-a-list"),
        pytest.param(_attribution(labels=[99]), id="label-not-text"),
        pytest.param(_attribution(licence_url=99), id="address-not-text"),
    ],
)
def test_an_attribution_of_the_wrong_shape_is_refused_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, payload: Any
) -> None:
    module.answer(200, payload)

    with caplog.at_level(logging.INFO):
        assert station_module.attribution() is None
        assert station_module.attribution() is None

    assert _reasons(caplog) == ["shape", "paused"]
    assert len(module.requests) == 1


def test_an_attribution_that_times_out_pauses(module: Module) -> None:
    module.fail(httpx.ReadTimeout)
    assert station_module.attribution() is None
    assert station_module.attribution() is None
    assert len(module.requests) == 1


def test_an_unreachable_attribution_pauses(module: Module) -> None:
    module.fail(httpx.ConnectError)
    assert station_module.attribution() is None
    assert len(module.requests) == 1
