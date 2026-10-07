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
import secrets
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

    # alembic's fileConfig (run by the integration tests) disables every logger
    # that exists at that moment; this one must be live for caplog.
    monkeypatch.setattr(station_module.log, "disabled", False)
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


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", " GZIP "])
async def test_a_compressed_answer_is_refused_unread_and_pauses(
    module: Module, caplog: pytest.LogCaptureFixture, encoding: str
) -> None:
    """A small compressed body could inflate far past the cap in one chunk:
    the client asks for none and refuses any, even one that would inflate to
    a valid, small answer."""
    import gzip
    import zlib

    plain = json.dumps({"stations": [_row()], "statement": "ZZ", "sources": []}).encode()
    packed = zlib.compress(plain) if encoding == "deflate" else gzip.compress(plain)
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


async def test_a_json_the_parser_cannot_handle_falls_back(
    module: Module, caplog: pytest.LogCaptureFixture
) -> None:
    deep = b"[" * 20_000 + b"]" * 20_000  # RecursionError in json, not a ValueError
    module.handler = lambda _r: httpx.Response(200, stream=_Chunks([deep]))

    with caplog.at_level(logging.INFO):
        assert await station_module.search("Zzville", uuid.uuid4()) is None

    assert _reasons(caplog) == ["network"]


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
