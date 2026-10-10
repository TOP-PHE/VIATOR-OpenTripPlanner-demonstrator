"""Unit tests for app.station_module — the client of the station module (MSMM).

The module is never reached: every call goes through `httpx.MockTransport`,
the same wiring as tests/unit/test_geocode_api.py. Values are invented: ZZ
names, codes beginning with 99, a `.invalid` address, a token and user ids
drawn at run time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

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
        # The keyword arguments of every httpx client the code built.
        self.clients: list[dict[str, Any]] = []
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
    real_async = httpx.AsyncClient

    def async_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        stand_in.clients.append(kwargs.copy())
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(token))
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
    assert request.headers["accept-encoding"] == "identity"
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


async def test_attribution_is_a_get_with_the_token_and_no_user(module: Module, token: str) -> None:
    module.answer(200, {"statement": "ZZ statement", "sources": []})

    assert await station_module.attribution() == {"statement": "ZZ statement", "sources": []}
    (request,) = module.requests
    assert request.method == "GET"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/attribution"
    assert request.headers["authorization"] == f"Bearer {token}"
    assert "x-viator-user-id" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
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
    assert rows[2]["latitude"] == 45.0
    assert isinstance(rows[2]["latitude"], float)
    assert set(rows[0]) == {"name", "latitude", "longitude", "country_iso", "uic"}


async def test_at_most_ten_rows_are_kept(module: Module) -> None:
    module.answer(200, {"stations": [_row(f"Zz {i}", f"99000{i:02d}") for i in range(15)]})

    rows = await station_module.search("Zzville", uuid.uuid4())

    assert rows is not None
    assert len(rows) == 10
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
        assert await station_module.attribution() is None
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
    monkeypatch.setattr(settings, "station_module_token", SecretStr(token))

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert await station_module.attribution() is None

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
            await station_module.attribution()
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


async def test_attribution_is_kept_sixty_seconds(module: Module, clock: FakeClock) -> None:
    module.answer(200, _attribution())

    first = await station_module.attribution()
    clock.now += 59.9
    second = await station_module.attribution()

    assert first == second == _attribution()
    assert len(module.requests) == 1

    clock.now += 0.2
    await station_module.attribution()
    assert len(module.requests) == 2


async def test_attribution_is_forgotten_at_the_first_failure(
    module: Module, clock: FakeClock
) -> None:
    module.answer(200, _attribution())
    await station_module.attribution()

    clock.now += 61
    module.answer(429, {"detail": "ZZ", "code": "all_minute"})  # a failure that does not pause
    assert await station_module.attribution() is None

    module.answer(200, _attribution())
    assert await station_module.attribution() == _attribution()
    assert len(module.requests) == 3


async def test_a_search_failure_that_pauses_also_forgets_the_attribution(
    module: Module, clock: FakeClock
) -> None:
    module.answer(200, _attribution())
    await station_module.attribution()
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())

    clock.now += PAUSE_PLUS
    module.answer(200, _attribution(licence="ZZ Licence Two"))
    assert await station_module.attribution() == _attribution(licence="ZZ Licence Two")


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://licence.invalid/zz", "//licence.invalid/zz", "zz"]
)
async def test_a_licence_address_that_is_not_a_web_address_is_dropped_and_the_group_kept(
    module: Module, url: str
) -> None:
    module.answer(200, _attribution(licence_url=url))

    answer = await station_module.attribution()

    assert answer == _attribution(licence_url=None)


async def test_a_null_licence_address_stays_null(module: Module) -> None:
    module.answer(200, _attribution(licence_url=None))
    assert await station_module.attribution() == _attribution(licence_url=None)


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
async def test_an_attribution_of_the_wrong_shape_is_refused_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, payload: Any
) -> None:
    module.answer(200, payload)

    with caplog.at_level(logging.INFO):
        assert await station_module.attribution() is None
        assert await station_module.attribution() is None

    assert _reasons(caplog) == ["shape", "paused"]
    assert len(module.requests) == 1


async def test_an_attribution_that_times_out_pauses(module: Module) -> None:
    module.fail(httpx.ReadTimeout)
    assert await station_module.attribution() is None
    assert await station_module.attribution() is None
    assert len(module.requests) == 1


async def test_an_unreachable_attribution_pauses(module: Module) -> None:
    module.fail(httpx.ConnectError)
    assert await station_module.attribution() is None
    assert len(module.requests) == 1


# ───────────────────────── the deadline, the size cap, the rest ─────────────────────────


class _TrickleAsync(httpx.AsyncByteStream):
    """A body that arrives one byte every `gap` seconds: each read is quick,
    the whole answer is not."""

    def __init__(self, gap: float, count: int) -> None:
        self.gap, self.count = gap, count

    async def __aiter__(self):  # type: ignore[override]
        for _ in range(self.count):
            await asyncio.sleep(self.gap)
            yield b" "


async def test_a_trickling_search_answer_is_cut_at_the_deadline_and_pauses(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(station_module, "SEARCH_DEADLINE", 0.2)
    module.handler = lambda _r: httpx.Response(200, stream=_TrickleAsync(0.05, 100))

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert await station_module.search("Zzville", uuid.uuid4()) is None

    assert time.monotonic() - started < 1.0  # 100 bytes x 0.05 s would be 5 s
    assert _reasons(caplog) == ["timeout", "paused"]
    assert len(module.requests) == 1


async def test_a_trickling_attribution_is_cut_at_the_deadline_and_pauses(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(station_module, "ATTRIBUTION_DEADLINE", 0.1)
    module.handler = lambda _r: httpx.Response(200, stream=_TrickleAsync(0.03, 100))

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        assert await station_module.attribution() is None
        assert await station_module.attribution() is None

    assert time.monotonic() - started < 1.0
    assert _reasons(caplog) == ["timeout", "paused"]


def _slow_headers(delay: float) -> Callable[[httpx.Request], Any]:
    """A module that takes `delay` seconds before it sends its headers."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"stations": [], "statement": "ZZ", "sources": []})

    return handler


async def test_a_search_whose_headers_trickle_is_cut_at_the_deadline_and_pauses(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deadline covers the whole call, the wait for the headers included."""
    monkeypatch.setattr(station_module, "SEARCH_DEADLINE", 0.2)
    module.handler = _slow_headers(3.0)

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None

    assert time.monotonic() - started < 1.0
    assert _reasons(caplog) == ["timeout"]
    assert station_module._paused()


async def test_an_attribution_whose_headers_trickle_is_cut_at_the_deadline_and_pauses(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(station_module, "ATTRIBUTION_DEADLINE", 0.2)
    module.handler = _slow_headers(3.0)

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        assert await station_module.attribution() is None

    assert time.monotonic() - started < 1.0
    assert _reasons(caplog) == ["timeout"]
    assert station_module._paused()


async def test_a_cancelled_search_is_cancelled_not_a_fallback_and_sets_no_pause(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    """Cancellation (the client went away) must propagate: the catch-all is
    `except Exception`, which CancelledError is not."""
    module.handler = _slow_headers(3.0)

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(station_module.search("Zzville", uuid.uuid4()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _reasons(caplog) == []
    assert not station_module._paused()


async def test_a_cancelled_attribution_is_cancelled_not_a_fallback_and_sets_no_pause(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    module.handler = _slow_headers(3.0)

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(station_module.attribution())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _reasons(caplog) == []
    assert not station_module._paused()


@pytest.mark.parametrize("encoding", ["gzip", "deflate", " GZIP ", "zz"])
async def test_a_compressed_answer_is_refused_unread_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, encoding: str
) -> None:
    """A small compressed body could inflate far past the cap in one chunk:
    the client asks for none and refuses any, even one that would inflate to
    a valid, small answer. An encoding httpx does not know ("zz") is sent
    uncompressed, so only the encoding check refuses it."""
    import gzip
    import zlib

    plain = json.dumps({"stations": [_row()], "statement": "ZZ", "sources": []}).encode()
    if encoding == "zz":
        packed = plain
    elif encoding == "deflate":
        packed = zlib.compress(plain)
    else:
        packed = gzip.compress(plain)
    module.handler = lambda _r: httpx.Response(
        200, content=packed, headers={"Content-Encoding": encoding}
    )

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        station_module.reset()
        assert await station_module.attribution() is None

    assert _reasons(caplog) == ["shape", "shape"]


async def test_an_identity_encoding_is_accepted(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        200, json={"stations": [_row()]}, headers={"Content-Encoding": "identity"}
    )
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]


@pytest.mark.parametrize("announced", [True, False], ids=["content-length", "chunked"])
async def test_an_oversized_search_answer_is_refused_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, announced: bool
) -> None:
    big = b'{"stations": [' + b" " * (station_module.SEARCH_MAX_BYTES + 1) + b"]}"
    if announced:
        module.handler = lambda _r: httpx.Response(200, content=big)
    else:
        chunks = [big[i : i + 4096] for i in range(0, len(big), 4096)]
        module.handler = lambda _r: httpx.Response(200, stream=_Chunks(chunks))

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert await station_module.search("Zzville", uuid.uuid4()) is None

    assert _reasons(caplog) == ["shape", "paused"]


class _Chunks(httpx.AsyncByteStream, httpx.SyncByteStream):
    """A body sent in chunks, without a Content-Length."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self.chunks:
            yield chunk

    def __iter__(self):  # type: ignore[override]
        yield from self.chunks


@pytest.mark.parametrize("announced", [True, False], ids=["content-length", "chunked"])
async def test_an_oversized_attribution_is_refused_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, announced: bool
) -> None:
    big = b" " * (station_module.ATTRIBUTION_MAX_BYTES + 1)
    if announced:
        module.handler = lambda _r: httpx.Response(200, content=big)
    else:
        module.handler = lambda _r: httpx.Response(200, stream=_Chunks([big[:4096], big[4096:]]))

    with caplog.at_level(logging.INFO):
        assert await station_module.attribution() is None

    assert _reasons(caplog) == ["shape"]


async def test_an_address_httpx_refuses_falls_back_as_network_and_pauses(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "http://[::1:8000")

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        assert await station_module.attribution() is None

    assert _reasons(caplog) == ["network", "paused"]
    assert module.requests == []


async def test_a_token_httpx_cannot_encode_falls_back_and_never_reaches_the_log(
    module: Module, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    token = "zzé€" + secrets.token_hex(8)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(token))

    with caplog.at_level(logging.DEBUG):
        assert await station_module.search("Zzville", uuid.uuid4()) is None
        station_module.reset()
        assert await station_module.attribution() is None

    assert _reasons(caplog) == ["network", "network"]
    assert module.requests == []
    logged = caplog.text + "".join(str(r.args) + str(r.exc_info) for r in caplog.records)
    for character in ("é", "€", token[4:]):
        assert character not in logged


# The stack of the thread `_on_a_fixed_stack` runs in. Since Python 3.14 the
# json parser stops at a depth set by the C stack it has, no longer at a fixed
# count: 20,000 levels raise RecursionError on 3.12 but parse on 3.14 with the
# usual 8 MiB stack, and the limit grows with `ulimit -s`. A thread's stack is
# set here, whatever the machine, so the depth below fails on every Python.
_PARSER_STACK = 16 * 1024 * 1024
_TOO_DEEP = 1_000_000  # about 130,000 levels fit in 16 MiB on 3.14; 3.12 stops far sooner


def _on_a_fixed_stack[T](work: Callable[[], T]) -> T:
    """The result of `work()`, run in a new thread with a `_PARSER_STACK` stack."""
    results: list[T] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(work())
        except Exception as error:
            errors.append(error)

    previous = threading.stack_size(_PARSER_STACK)
    try:
        thread = threading.Thread(target=run)
        thread.start()
    finally:
        threading.stack_size(previous)
    thread.join()
    if errors:
        raise errors[0]
    return results[0]


def _raises_recursion_error(body: bytes) -> bool:
    try:
        json.loads(body)
    except RecursionError:
        return True
    return False


def test_a_json_the_parser_cannot_handle_falls_back(
    module: Module, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = b"[" * _TOO_DEEP + b"]" * _TOO_DEEP  # RecursionError in json, not a ValueError
    # 2 MB: above the cap, which would refuse it as `shape` before the parser.
    monkeypatch.setattr(station_module, "SEARCH_MAX_BYTES", len(deep))
    module.handler = lambda _r: httpx.Response(200, stream=_Chunks([deep]))

    # The input really is one the parser cannot handle, on the same stack.
    assert _on_a_fixed_stack(lambda: _raises_recursion_error(deep))
    with caplog.at_level(logging.INFO):
        rows = _on_a_fixed_stack(
            lambda: asyncio.run(station_module.search("Zzville", uuid.uuid4()))
        )

    assert rows is None
    assert _reasons(caplog) == ["network"]
    assert len(module.requests) == 1


async def test_the_clients_are_built_with_their_timeouts_and_without_the_environment(
    module: Module,
) -> None:
    module.answer(200, {"stations": []})
    await station_module.search("Zzville", uuid.uuid4())
    module.answer(200, {"statement": "ZZ", "sources": []})
    await station_module.attribution()

    search_client, attribution_client = module.clients
    assert search_client["timeout"] == httpx.Timeout(1.0, connect=0.3)
    assert search_client["trust_env"] is False
    assert attribution_client["timeout"] == httpx.Timeout(0.5)
    assert attribution_client["trust_env"] is False


@pytest.mark.parametrize(
    ("kind", "level"),
    [
        ("429", logging.INFO),
        ("503-busy", logging.INFO),
        ("403", logging.WARNING),
        ("network", logging.WARNING),
        ("timeout", logging.WARNING),
    ],
)
async def test_one_request_refusals_log_at_info_and_faults_at_warning(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, level: int
) -> None:
    _setup_failure(module, kind)

    with caplog.at_level(logging.DEBUG):
        await station_module.search("Zzville", uuid.uuid4())

    (record,) = [r for r in caplog.records if r.name == station_module.log.name]
    assert record.levelno == level


# ───────────────────── the detailed form: search_outcome() ─────────────────────


async def test_search_outcome_gives_the_rows_and_ok(module: Module) -> None:
    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome([_row()], "ok", None)
    assert len(module.requests) == 1


async def test_a_429_with_retry_after_gives_its_seconds(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        429, json={"detail": "ZZ", "code": "user_minute"}, headers={"Retry-After": "17"}
    )

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "status_429", 17, "user_minute")
    assert not station_module.paused()


@pytest.mark.parametrize(
    "code",
    [None, 7, "zz_window", "USER_MINUTE", "user_minute ", "near_user_day\u0000"],
    ids=["none", "a-number", "unknown", "capitals", "a-space", "a-nul"],
)
async def test_a_429_window_word_outside_the_known_ones_gives_none(
    module: Module, code: Any
) -> None:
    module.handler = lambda _r: httpx.Response(429, json={"detail": "ZZ", "code": code})

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "status_429", None, None)


@pytest.mark.parametrize("code", sorted(station_module.LIMIT_CODES))
async def test_each_known_429_window_word_is_kept(module: Module, code: str) -> None:
    module.handler = lambda _r: httpx.Response(429, json={"detail": "ZZ", "code": code})

    assert (await station_module.search_outcome("Zzville", uuid.uuid4())).code == code


@pytest.mark.parametrize(
    "header",
    [None, "Wed, 21 Oct 2026 07:28:00 GMT", "0", "86401", "1.5", "-3", "", "17 seconds"],
    ids=["none", "a-date", "zero", "over-a-day", "a-fraction", "negative", "empty", "words"],
)
async def test_a_429_without_a_plain_number_of_seconds_gives_no_retry_after(
    module: Module, header: str | None
) -> None:
    headers = {} if header is None else {"Retry-After": header}
    module.handler = lambda _r: httpx.Response(429, json={"code": "user_minute"}, headers=headers)

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome.rows is None
    assert outcome.reason == "status_429"
    assert outcome.retry_after is None


@pytest.mark.parametrize("seconds", ["1", "86400", " 60 "])
async def test_the_bounds_of_retry_after_are_kept(module: Module, seconds: str) -> None:
    module.handler = lambda _r: httpx.Response(429, json={}, headers={"Retry-After": seconds})

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome.retry_after == int(seconds)


async def test_a_retry_after_on_another_status_is_ignored(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        503, json={"code": "busy"}, headers={"Retry-After": "5"}
    )

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "busy", None)


@pytest.mark.parametrize(("kind", "reason"), PAUSING + NOT_PAUSING)
async def test_each_search_failure_gives_its_reason_word_and_no_rows(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)

    with caplog.at_level(logging.INFO):
        outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome(None, reason, None)
    assert _reasons(caplog) == [reason]


@pytest.mark.parametrize(("kind", "reason"), PAUSING)
async def test_search_outcome_keeps_the_pause_rules(
    module: Module, clock: FakeClock, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)
    await station_module.search_outcome("Zzville", uuid.uuid4())
    module.answer(200, {"stations": [_row()]})

    clock.now += 29.9
    assert await station_module.search_outcome("Zzville", uuid.uuid4()) == station_module.Outcome(
        None, "paused", None
    )
    assert len(module.requests) == 1
    clock.now += 0.2
    assert (await station_module.search_outcome("Zzville", uuid.uuid4())).reason == "ok"


@pytest.mark.parametrize(("kind", "reason"), NOT_PAUSING)
async def test_search_outcome_does_not_pause_on_one_request_refusals(
    module: Module, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)
    await station_module.search_outcome("Zzville", uuid.uuid4())

    assert not station_module.paused()
    module.answer(200, {"stations": []})
    assert await station_module.search("Zzville", uuid.uuid4()) == []


async def test_search_outcome_off_without_the_settings(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")

    assert await station_module.search_outcome("Zzville", uuid.uuid4()) == station_module.Outcome(
        None, "off", None
    )
    assert module.requests == []


async def test_search_outcome_falls_back_as_network_on_an_unforeseen_error(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "http://[::1:8000")

    outcome = await station_module.search_outcome("Zzville", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "network", None)
    assert station_module.paused()


async def test_search_returns_the_rows_of_search_outcome(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(429, json={}, headers={"Retry-After": "17"})
    assert await station_module.search("Zzville", uuid.uuid4()) is None
    module.answer(200, {"stations": [_row()]})
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]


# ───────────────────────────── paused() ─────────────────────────────


async def test_paused_is_true_during_the_pause_and_false_after(
    module: Module, clock: FakeClock
) -> None:
    assert not station_module.paused()
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())

    assert station_module.paused()
    clock.now += station_module.PAUSE_SECONDS - 0.1
    assert station_module.paused()
    clock.now += 0.2
    assert not station_module.paused()


# ───────────────────────────── lookup() ─────────────────────────────


def _lookup_row(uic: str, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    return {**_row(f"Zz Station {uic}", uic), "parent_uic": parent, **extra}


def _lookup_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "station_module.lookup_failed reason="
    return [
        r.getMessage().removeprefix(prefix)
        for r in caplog.records
        if r.name == station_module.log.name and r.getMessage().startswith(prefix)
    ]


async def test_lookup_posts_the_codes_in_the_body_with_only_its_own_headers(
    module: Module, token: str
) -> None:
    user = uuid.uuid4()
    module.answer(200, {"stations": [_lookup_row("9900001")]})

    outcome = await station_module.lookup(["9900001", "9900002"], user)

    assert outcome.reason == "ok"
    (request,) = module.requests
    assert request.method == "POST"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/stations/lookup"
    assert request.url.query == b""
    assert json.loads(request.content) == {"uics": ["9900001", "9900002"]}
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["x-viator-user-id"] == str(user)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    ours = {"authorization", "x-viator-user-id", "content-type", "content-length"}
    assert set(request.headers.keys()) <= ours | _HTTPX_DEFAULTS
    assert "9900001" not in str(request.url)


async def test_lookup_gives_the_rows_with_their_parent_in_the_order_of_the_answer(
    module: Module,
) -> None:
    module.answer(
        200,
        {"stations": [_lookup_row("9900003", "9900009"), _lookup_row("9900001")]},
    )

    outcome = await station_module.lookup(["9900001", "9900002", "9900003"], uuid.uuid4())

    assert outcome.reason == "ok"
    assert outcome.retry_after is None
    assert outcome.rows == [_lookup_row("9900003", "9900009"), _lookup_row("9900001")]


async def test_a_bad_lookup_row_is_dropped_and_the_others_kept(module: Module) -> None:
    good = [_lookup_row("9900001"), _lookup_row("9900004", "9900001")]
    bad = [
        _lookup_row("9900002", parent=99),  # a parent that is not text
        _lookup_row("9900003", name=""),  # no name
        _lookup_row("9900005", latitude=None),  # no position
        _lookup_row("9900099"),  # a code that was not asked for
        _lookup_row("9900001"),  # a second row for a code
        "not a row",
    ]
    module.answer(200, {"stations": [good[0], *bad, good[1]]})

    outcome = await station_module.lookup(
        ["9900001", "9900002", "9900003", "9900004", "9900005"], uuid.uuid4()
    )

    assert outcome.rows == good


async def test_an_empty_lookup_answer_is_an_answer(module: Module) -> None:
    module.answer(200, {"stations": []})
    assert await station_module.lookup(["9900001"], uuid.uuid4()) == station_module.Outcome(
        [], "ok", None
    )


@pytest.mark.parametrize(("kind", "reason"), PAUSING + NOT_PAUSING)
async def test_each_lookup_failure_gives_its_reason_and_never_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)
    if kind == "no-stations-list":
        module.answer(200, {"stations": "ZZ"})

    with caplog.at_level(logging.INFO):
        outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome == station_module.Outcome(None, reason, None)
    assert _lookup_reasons(caplog) == [reason]
    assert _reasons(caplog) == [f"station_module.lookup_failed reason={reason}"]
    # No pause: the typeahead goes on calling the module.
    assert not station_module.paused()
    module.answer(200, {"stations": [_row()]})
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]


async def test_a_lookup_failure_keeps_the_cached_attribution(module: Module) -> None:
    module.answer(200, _attribution())
    await station_module.attribution()
    module.fail(httpx.ConnectError)
    await station_module.lookup(["9900001"], uuid.uuid4())

    assert await station_module.attribution() == _attribution()
    assert len(module.requests) == 2


async def test_a_lookup_429_gives_its_retry_after(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        429, json={"code": "user_minute"}, headers={"Retry-After": "42"}
    )

    outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome == station_module.Outcome(None, "status_429", 42, "user_minute")


async def test_a_running_pause_is_honoured_by_lookup(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())
    calls = len(module.requests)

    with caplog.at_level(logging.INFO):
        outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome == station_module.Outcome(None, "paused", None)
    assert len(module.requests) == calls
    assert _lookup_reasons(caplog) == ["paused"]


async def test_lookup_makes_no_call_without_the_settings(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))

    outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome == station_module.Outcome(None, "off", None)
    assert module.requests == []


async def test_a_trickling_lookup_is_cut_at_the_deadline_without_a_pause(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(station_module, "SEARCH_DEADLINE", 0.2)
    module.handler = lambda _r: httpx.Response(200, stream=_TrickleAsync(0.05, 100))

    started = time.monotonic()
    outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert time.monotonic() - started < 1.0
    assert outcome.reason == "timeout"
    assert not station_module.paused()


async def test_an_oversized_lookup_answer_is_refused_without_a_pause(module: Module) -> None:
    big = b'{"stations": [' + b" " * (station_module.SEARCH_MAX_BYTES + 1) + b"]}"
    module.handler = lambda _r: httpx.Response(200, content=big)

    outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome.reason == "shape"
    assert not station_module.paused()


async def test_a_compressed_lookup_answer_is_refused_without_a_pause(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        200, content=b"{}", headers={"Content-Encoding": "zz"}
    )

    assert (await station_module.lookup(["9900001"], uuid.uuid4())).reason == "shape"
    assert not station_module.paused()


async def test_an_unforeseen_lookup_error_is_network_without_a_pause(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "http://[::1:8000")

    outcome = await station_module.lookup(["9900001"], uuid.uuid4())

    assert outcome == station_module.Outcome(None, "network", None)
    assert not station_module.paused()
    assert module.requests == []


def _codes(count: int) -> list[str]:
    return [f"99{i:05d}" for i in range(count)]


@pytest.mark.parametrize(
    "codes",
    [
        pytest.param([], id="none"),
        pytest.param(_codes(21), id="twenty-one"),
        pytest.param(["9900001", "9900002", "9900001"], id="repeated"),
        pytest.param(["ZZ"], id="two-characters"),
        pytest.param(["9" * 21], id="twenty-one-characters"),
        pytest.param(["99 001"], id="a-space"),
        pytest.param(["99\u00a0001"], id="a-no-break-space"),
        pytest.param(["99\t001"], id="a-tab"),
        pytest.param(["9900001\n"], id="a-line-end"),
        pytest.param(["99\x00001"], id="a-nul"),
        pytest.param(["99\x7f001"], id="a-delete"),
        pytest.param(["99\ud800001"], id="a-surrogate"),
        pytest.param([""], id="empty"),
    ],
)
async def test_a_lookup_the_module_would_refuse_is_refused_locally(
    module: Module, codes: list[str]
) -> None:
    with pytest.raises(ValueError, match=r"lookup|code"):
        await station_module.lookup(codes, uuid.uuid4())
    assert module.requests == []


async def test_codes_that_differ_only_in_capitals_are_two_codes(module: Module) -> None:
    module.answer(200, {"stations": []})

    await station_module.lookup(["zz999", "ZZ999"], uuid.uuid4())

    assert json.loads(module.requests[0].content) == {"uics": ["zz999", "ZZ999"]}


async def test_twenty_codes_of_three_to_twenty_characters_are_sent(module: Module) -> None:
    module.answer(200, {"stations": []})
    codes = ["ZZ9", "Z" * 20, *_codes(18)]

    assert (await station_module.lookup(codes, uuid.uuid4())).reason == "ok"
    assert json.loads(module.requests[0].content) == {"uics": codes}


async def test_lookup_never_logs_a_code_or_the_token(
    module: Module, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = f"ZZ{secrets.token_hex(4)}"
    with caplog.at_level(logging.DEBUG):
        for kind, _reason in PAUSING + NOT_PAUSING:
            _setup_failure(module, kind)
            await station_module.lookup([marker], uuid.uuid4())
        module.answer(200, {"stations": [_lookup_row(marker)]})
        await station_module.lookup([marker], uuid.uuid4())

    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert caplog.records
    assert marker not in logged
    assert token not in logged
    assert "stand-in failure" not in logged


@pytest.mark.parametrize(
    ("code", "word"),
    [
        ("9900001", None),
        ("ZZ9", None),
        ("Z" * 20, None),
        ("zz-99/1", None),
        ("ZZ", "length"),
        ("Z" * 21, "length"),
        ("99 01", "white space"),
        ("99\u200a01", "white space"),
        ("99\t01", "control character"),
        ("99\x0001", "control character"),
        ("99\x8501", "control character"),
        ("99\udfff01", "surrogate"),
        ("\ud800\t", "surrogate"),
    ],
)
def test_code_refused_mirrors_the_module_rule(code: str, word: str | None) -> None:
    assert station_module.code_refused(code) == word


# ─────────────── surrogates and control characters from the module ───────────────

_UNWRITABLE = [
    pytest.param("\ud800", id="a-surrogate"),
    pytest.param("\x00", id="a-nul"),
    pytest.param("\t", id="a-tab"),
]


def _answer_escaped(module: Module, payload: Any) -> None:
    """A 200 whose JSON writes a lone surrogate as `\\ud800`, as a module may."""
    body = json.dumps(payload).encode("ascii")
    module.handler = lambda _r: httpx.Response(
        200, content=body, headers={"Content-Type": "application/json"}
    )


@pytest.mark.parametrize("bad", _UNWRITABLE)
@pytest.mark.parametrize("field", ["name", "uic", "country_iso"])
async def test_a_search_row_with_an_unwritable_character_is_dropped(
    module: Module, field: str, bad: str
) -> None:
    broken = _row("Zz Broken", "9900002")
    broken[field] = f"Z{bad}Z"
    _answer_escaped(module, {"stations": [broken, _row()]})

    rows = await station_module.search("Zzville", uuid.uuid4())

    assert rows == [_row()]
    json.dumps(rows).encode("utf-8")  # VIATOR's own answer can be written


@pytest.mark.parametrize("bad", _UNWRITABLE)
@pytest.mark.parametrize("field", ["name", "uic", "country_iso", "parent_uic"])
async def test_a_lookup_row_with_an_unwritable_character_is_dropped(
    module: Module, field: str, bad: str
) -> None:
    broken = _lookup_row("9900002", "9900009")
    broken[field] = f"9900002{bad}" if field == "uic" else f"Z{bad}Z"
    _answer_escaped(module, {"stations": [broken, _lookup_row("9900001")]})

    outcome = await station_module.lookup(["9900001", "9900002"], uuid.uuid4())

    assert outcome.rows == [_lookup_row("9900001")]


class _Headers:
    def __init__(self, value: str) -> None:
        self.headers = {"retry-after": value}


@pytest.mark.parametrize("value", ["\u0663", "1\u0667", "\uff11\uff17"])
def test_a_retry_after_in_digits_of_another_script_gives_none(value: str) -> None:
    """httpx reads headers as Latin-1, so such digits cannot arrive through
    it today; the rule is pinned on the parser itself (ASCII digits only)."""
    assert station_module._retry_after(_Headers(value)) is None  # type: ignore[arg-type]
    assert station_module._retry_after(_Headers("17")) == 17  # type: ignore[arg-type]


# ───────────────────────────── near_outcome() ─────────────────────────────

# An invented position in the open sea, far from any station.
_SEA_LAT, _SEA_LON = -48.5, -123.25


def _near_row(uic: str, distance: int, parent: str | None = None, **extra: Any) -> dict[str, Any]:
    return {**_lookup_row(uic, parent), "distance_m": distance, **extra}


def _near_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "station_module.near_failed reason="
    return [
        r.getMessage().removeprefix(prefix)
        for r in caplog.records
        if r.name == station_module.log.name and r.getMessage().startswith(prefix)
    ]


async def test_near_posts_the_position_in_the_body_with_only_its_own_headers(
    module: Module, token: str
) -> None:
    user = uuid.uuid4()
    module.answer(200, {"stations": [_near_row("9900001", 12)]})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, user)

    assert outcome.reason == "ok"
    (request,) = module.requests
    assert request.method == "POST"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/stations/near"
    assert request.url.query == b""
    # The radius defaults to the module's largest, 300 m, sent as an integer.
    body = json.loads(request.content)
    assert body == {"lat": _SEA_LAT, "lon": _SEA_LON, "radius_m": 300}
    assert type(body["radius_m"]) is int
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["x-viator-user-id"] == str(user)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    ours = {"authorization", "x-viator-user-id", "content-type", "content-length"}
    assert set(request.headers.keys()) <= ours | _HTTPX_DEFAULTS
    assert "48.5" not in str(request.url)
    (built,) = module.clients
    assert built["trust_env"] is False


async def test_near_sends_the_radius_asked_and_an_integer_position_as_a_number(
    module: Module,
) -> None:
    module.answer(200, {"stations": []})

    await station_module.near_outcome(-48, -123, uuid.uuid4(), radius_m=150)

    body = json.loads(module.requests[0].content)
    assert body == {"lat": -48.0, "lon": -123.0, "radius_m": 150}
    assert isinstance(body["lat"], float)
    assert type(body["radius_m"]) is int


async def test_near_gives_the_rows_with_parent_and_distance_in_the_order_of_the_answer(
    module: Module,
) -> None:
    rows = [_near_row("9900003", 0, "9900009"), _near_row("9900001", 300)]
    module.answer(200, {"stations": rows})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(rows, "ok", None)


async def test_an_empty_near_answer_is_an_answer(module: Module) -> None:
    module.answer(200, {"stations": []})
    assert await station_module.near_outcome(
        _SEA_LAT, _SEA_LON, uuid.uuid4()
    ) == station_module.Outcome([], "ok", None)


async def test_a_bad_near_row_is_dropped_and_the_others_kept(module: Module) -> None:
    good = [_near_row("9900001", 10), _near_row("9900004", 40, "9900001")]
    bad = [
        _near_row("9900002", 20, parent=99),  # a parent that is not text
        _near_row("9900003", 20, name=""),  # no name
        _near_row("9900005", 20, latitude=None),  # no position
        {k: v for k, v in _near_row("9900006", 20).items() if k != "distance_m"},
        _near_row("9900007", 20.5),  # a distance that is not whole
        _near_row("9900008", True),  # a truth value
        _near_row("9900010", -1),  # a negative distance
        _near_row("9900011", 301),  # beyond the radius asked
        _near_row("9900012", "20"),  # a distance as text
        _near_row("9900001", 30),  # a second row for a code
        "not a row",
    ]
    module.answer(200, {"stations": [good[0], *bad, good[1]]})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome.rows == good


async def test_a_distance_beyond_a_smaller_radius_is_dropped(module: Module) -> None:
    module.answer(200, {"stations": [_near_row("9900001", 50), _near_row("9900002", 51)]})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4(), radius_m=50)

    assert outcome.rows == [_near_row("9900001", 50)]


async def test_near_keeps_at_most_five_rows(module: Module) -> None:
    rows = [_near_row(f"99000{i:02d}", i) for i in range(8)]
    module.answer(200, {"stations": rows})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert station_module.NEAR_MAX_ROWS == 5
    assert outcome.rows == rows[:5]


@pytest.mark.parametrize("bad", _UNWRITABLE)
@pytest.mark.parametrize("field", ["name", "uic", "country_iso", "parent_uic"])
async def test_a_near_row_with_an_unwritable_character_is_dropped(
    module: Module, field: str, bad: str
) -> None:
    broken = _near_row("9900002", 20, "9900009")
    broken[field] = f"9900002{bad}" if field == "uic" else f"Z{bad}Z"
    _answer_escaped(module, {"stations": [broken, _near_row("9900001", 30)]})

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome.rows == [_near_row("9900001", 30)]


@pytest.mark.parametrize(("kind", "reason"), PAUSING + NOT_PAUSING)
async def test_each_near_failure_gives_its_reason_and_never_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)

    with caplog.at_level(logging.INFO):
        outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(None, reason, None)
    assert _near_reasons(caplog) == [reason]
    assert _reasons(caplog) == [f"station_module.near_failed reason={reason}"]
    # No pause: the typeahead goes on calling the module.
    assert not station_module.paused()
    module.answer(200, {"stations": [_row()]})
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]


@pytest.mark.parametrize("code", ["near_user_minute", "near_user_day", "near_all_day"])
async def test_a_near_429_gives_its_retry_after_and_no_pause(module: Module, code: str) -> None:
    module.handler = lambda _r: httpx.Response(
        429, json={"detail": "ZZ", "code": code}, headers={"Retry-After": "42"}
    )

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(None, "status_429", 42, code)
    assert not station_module.paused()


async def test_a_near_failure_keeps_the_cached_attribution(module: Module) -> None:
    module.answer(200, _attribution())
    await station_module.attribution()
    module.answer(500, {"detail": "ZZ", "code": "zz"})
    await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert await station_module.attribution() == _attribution()
    assert len(module.requests) == 2


async def test_a_running_pause_is_honoured_by_near(
    module: Module, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())
    calls = len(module.requests)

    with caplog.at_level(logging.INFO):
        outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(None, "paused", None)
    assert len(module.requests) == calls
    assert _near_reasons(caplog) == ["paused"]
    clock.now += PAUSE_PLUS
    module.answer(200, {"stations": []})
    assert (await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())).reason == "ok"


async def test_near_makes_no_call_without_the_settings(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(None, "off", None)
    assert module.requests == []


async def test_a_trickling_near_answer_is_cut_at_the_deadline_without_a_pause(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(station_module, "SEARCH_DEADLINE", 0.2)
    module.handler = lambda _r: httpx.Response(200, stream=_TrickleAsync(0.05, 100))

    started = time.monotonic()
    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert time.monotonic() - started < 1.0
    assert outcome.reason == "timeout"
    assert not station_module.paused()


async def test_an_oversized_or_compressed_near_answer_is_refused_without_a_pause(
    module: Module,
) -> None:
    big = b'{"stations": [' + b" " * (station_module.SEARCH_MAX_BYTES + 1) + b"]}"
    module.handler = lambda _r: httpx.Response(200, content=big)
    assert (await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())).reason == "shape"

    module.handler = lambda _r: httpx.Response(
        200, content=b"{}", headers={"Content-Encoding": "zz"}
    )
    assert (await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())).reason == "shape"
    assert not station_module.paused()


async def test_an_unforeseen_near_error_is_network_without_a_pause(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "http://[::1:8000")

    outcome = await station_module.near_outcome(_SEA_LAT, _SEA_LON, uuid.uuid4())

    assert outcome == station_module.Outcome(None, "network", None)
    assert not station_module.paused()


@pytest.mark.parametrize(
    ("lat", "lon", "radius"),
    [
        pytest.param(90.5, 0.0, 300, id="latitude-above-90"),
        pytest.param(-90.5, 0.0, 300, id="latitude-below-minus-90"),
        pytest.param(0.0, 180.5, 300, id="longitude-above-180"),
        pytest.param(0.0, -180.5, 300, id="longitude-below-minus-180"),
        pytest.param(math.nan, 0.0, 300, id="latitude-nan"),
        pytest.param(0.0, math.inf, 300, id="longitude-infinite"),
        pytest.param(True, 0.0, 300, id="latitude-truth-value"),
        pytest.param(None, 0.0, 300, id="latitude-none"),
        pytest.param(0.0, "1", 300, id="longitude-text"),
        pytest.param(0.0, 0.0, 0, id="radius-zero"),
        pytest.param(0.0, 0.0, 301, id="radius-301"),
        pytest.param(0.0, 0.0, 300.0, id="radius-float"),
        pytest.param(0.0, 0.0, True, id="radius-truth-value"),
    ],
)
async def test_a_near_call_the_module_would_refuse_is_refused_locally(
    module: Module, lat: Any, lon: Any, radius: Any
) -> None:
    with pytest.raises(ValueError, match="near call"):
        await station_module.near_outcome(lat, lon, uuid.uuid4(), radius_m=radius)
    assert module.requests == []


@pytest.mark.parametrize(
    ("lat", "lon", "radius"),
    [(90.0, 180.0, 1), (-90.0, -180.0, 300), (0, 0, 300)],
    ids=["north-east-corner", "south-west-corner", "zero"],
)
async def test_the_edges_of_a_near_call_are_sent(
    module: Module, lat: float, lon: float, radius: int
) -> None:
    module.answer(200, {"stations": []})

    assert (await station_module.near_outcome(lat, lon, uuid.uuid4(), radius)).reason == "ok"
    assert json.loads(module.requests[0].content)["radius_m"] == radius


async def test_near_never_logs_a_position_a_code_or_the_token(
    module: Module, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = f"ZZ{secrets.token_hex(4)}"
    with caplog.at_level(logging.DEBUG):
        for kind, _reason in PAUSING + NOT_PAUSING:
            _setup_failure(module, kind)
            await station_module.near_outcome(-48.123456, -123.654321, uuid.uuid4())
        module.answer(200, {"stations": [_near_row(marker, 5)]})
        await station_module.near_outcome(-48.123456, -123.654321, uuid.uuid4())

    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert caplog.records
    assert "48.123456" not in logged
    assert "123.654321" not in logged
    assert marker not in logged
    assert token not in logged
    assert "stand-in failure" not in logged


@pytest.mark.parametrize(
    ("lat", "lon", "refused"),
    [
        (45.0, 6.0, False),
        (-90, 180, False),
        (None, 6.0, True),
        (45.0, None, True),
        (math.nan, 6.0, True),
        (45.0, -math.inf, True),
        (False, 6.0, True),
        (90.000001, 6.0, True),
        (45.0, 180.000001, True),
    ],
    ids=["plain", "edges", "no-lat", "no-lon", "nan", "infinite", "truth", "lat-out", "lon-out"],
)
def test_position_refused_is_the_near_calls_rule(lat: Any, lon: Any, refused: bool) -> None:
    assert (station_module.position_refused(lat, lon) is not None) is refused


# ───────────────────────── the admin search ─────────────────────────


def _admin_search_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "station_module.admin_search_failed reason="
    return [
        r.getMessage().removeprefix(prefix)
        for r in caplog.records
        if r.name == station_module.log.name and r.getMessage().startswith(prefix)
    ]


async def test_admin_search_posts_the_text_in_the_body_with_only_its_own_headers(
    module: Module, token: str
) -> None:
    user = uuid.uuid4()

    outcome = await station_module.admin_search_outcome("ZZ Hub Name", user)

    assert outcome == station_module.Outcome([_row()], "ok", None)
    (request,) = module.requests
    assert request.method == "POST"
    assert str(request.url) == f"{MODULE_URL}/internal/v1/stations/search"
    assert request.url.query == b""
    assert json.loads(request.content) == {"q": "ZZ Hub Name"}
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["x-viator-user-id"] == str(user)
    assert request.headers["accept-encoding"] == "identity"
    ours = {"authorization", "x-viator-user-id", "content-type", "content-length"}
    assert set(request.headers.keys()) <= ours | _HTTPX_DEFAULTS
    (built,) = module.clients
    assert built["trust_env"] is False


async def test_admin_search_drops_bad_rows_and_keeps_at_most_ten(module: Module) -> None:
    rows = [_row(uic=f"99000{i:02d}") for i in range(12)]
    module.answer(200, {"stations": [_row(name=""), *rows]})

    outcome = await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())

    assert outcome.rows == rows[:10]


@pytest.mark.parametrize(("kind", "reason"), PAUSING + NOT_PAUSING)
async def test_each_admin_search_failure_gives_its_reason_and_never_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, kind: str, reason: str
) -> None:
    _setup_failure(module, kind)

    with caplog.at_level(logging.INFO):
        outcome = await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())

    assert outcome.rows is None
    assert outcome.reason == reason
    assert _admin_search_reasons(caplog) == [reason]
    # The text is never logged.
    assert not any("ZZ Hub" in r.getMessage() for r in caplog.records)
    # No pause: the typeahead of every user goes on calling the module.
    assert not station_module.paused()
    module.answer(200, {"stations": [_row()]})
    assert await station_module.search("Zzville", uuid.uuid4()) == [_row()]


async def test_an_admin_search_429_gives_its_retry_after_and_window(module: Module) -> None:
    module.handler = lambda _r: httpx.Response(
        429, json={"detail": "ZZ", "code": "user_minute"}, headers={"Retry-After": "9"}
    )

    outcome = await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "status_429", 9, "user_minute")
    assert not station_module.paused()


async def test_a_running_pause_is_honoured_by_admin_search(
    module: Module, clock: FakeClock
) -> None:
    module.fail(httpx.ConnectError)
    await station_module.search("Zzville", uuid.uuid4())
    calls = len(module.requests)

    outcome = await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "paused", None)
    assert len(module.requests) == calls
    clock.now += PAUSE_PLUS
    module.answer(200, {"stations": []})
    assert (await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())).reason == "ok"


async def test_admin_search_makes_no_call_without_the_settings(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))

    outcome = await station_module.admin_search_outcome("ZZ Hub", uuid.uuid4())

    assert outcome == station_module.Outcome(None, "off", None)
    assert module.requests == []
