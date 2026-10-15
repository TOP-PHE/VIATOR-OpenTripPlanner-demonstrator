"""Resumed downloads of large feeds (app/feed_fetch.py `_download`).

A national archive of ~2 GB that a server cuts at about the same point on
every attempt can never arrive by downloading again from byte 0. These tests
pin the resume: a range request from the first missing byte under If-Range,
checked before a byte is appended, and the cases where the download must
start again from byte 0 or give up with a clear reason.

Every server here is an `httpx.MockTransport` stand-in with invented bodies
(seeded pseudo-random bytes, an invented GTFS zip) and an invented `.example`
URL whose query holds a fake key that must never reach a log or a reason.
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import tracemalloc
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_fetch
from app.feed_fetch import FetchError, fetch_validated

MIB = 1024 * 1024
FETCH_URL = "https://zz-nap.example/feed?key=zz-fake-key"
STATE_URL = "https://zz-nap.example/feed"


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_fetch, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(feed_fetch, "RESUME_DELAYS", (0.0,) * 8)


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _big_gtfs(seed: int = 99, padding: int = 5 * MIB) -> bytes:
    """An invented GTFS zip of a few MiB: the five files plus one stored
    member of seeded pseudo-random bytes, so a cut lands mid-body."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\nZZ1\n")
        z.writestr("zz_padding.bin", random.Random(seed).randbytes(padding), zipfile.ZIP_STORED)
    return buf.getvalue()


BODY = _big_gtfs()
OTHER_BODY = _big_gtfs(seed=98)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Server:
    """A file server stand-in. Serves `body` (switching to `next_body` and
    `next_etag` after the first response when given), honours Range under
    If-Range when `honour_range`, and ends the n-th body at the absolute byte
    offset `cuts[n]` with `RemoteProtocolError` (None: no cut).

    `bad_range` replaces the Content-Range of every 206 (a misbehaving
    server); `on_range` answers range requests itself when given."""

    def __init__(
        self,
        body: bytes = BODY,
        *,
        cuts: list[int | None] | None = None,
        etag: str | None = '"zz-v1"',
        last_modified: str | None = None,
        accept_ranges: str | None = "bytes",
        honour_range: bool = True,
        next_body: bytes | None = None,
        next_etag: str | None = None,
        bad_range: Callable[[int, int], str] | None = None,
        on_range: Callable[[httpx.Request], httpx.Response] | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.cuts = list(cuts or [])
        self.etag = etag
        self.last_modified = last_modified
        self.accept_ranges = accept_ranges
        self.honour_range = honour_range
        self.next_body = next_body
        self.next_etag = next_etag
        self.bad_range = bad_range
        self.on_range = on_range
        self.extra = extra or {}
        self.requests: list[httpx.Request] = []

    def _headers(self) -> dict[str, str]:
        headers = dict(self.extra)
        if self.etag:
            headers["ETag"] = self.etag
        if self.last_modified:
            headers["Last-Modified"] = self.last_modified
        if self.accept_ranges:
            headers["Accept-Ranges"] = self.accept_ranges
        return headers

    def _validator(self) -> str | None:
        """What a matching If-Range names: a strong ETag, else Last-Modified."""
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 2 and self.next_body is not None:
            self.body, self.etag = self.next_body, self.next_etag
        wanted = request.headers.get("range")
        if wanted and self.on_range is not None:
            return self.on_range(request)
        cut = self.cuts.pop(0) if self.cuts else None
        headers = self._headers()
        size = len(self.body)
        if wanted and self.honour_range and request.headers.get("if-range") == self._validator():
            start = int(wanted.removeprefix("bytes=").removesuffix("-"))
            headers["Content-Range"] = (
                self.bad_range(start, size)
                if self.bad_range
                else f"bytes {start}-{size - 1}/{size}"
            )
            headers["Content-Length"] = str(size - start)
            return httpx.Response(206, headers=headers, content=self._stream(start, cut))
        headers["Content-Length"] = str(size)
        return httpx.Response(200, headers=headers, content=self._stream(0, cut))

    async def _stream(self, start: int, cut: int | None) -> AsyncIterator[bytes]:
        body = self.body
        end = len(body) if cut is None else max(cut, start)
        pos = start
        while pos < end:
            step = min(256 * 1024, end - pos)
            yield body[pos : pos + step]
            pos += step
        if cut is not None:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )


async def _fetch(
    server: Any,
    staging: Path,
    *,
    previous: dict[str, Any] | None = None,
    have_current: bool = False,
) -> feed_fetch.FetchResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        return await fetch_validated(
            client,
            fetch_url=FETCH_URL,
            state_url=STATE_URL,
            extra_headers={},
            kind="GTFS",
            staging=staging,
            base_name="20261015-zz-provider",
            suffix=".zip",
            previous=previous or {},
            have_current=have_current,
        )


def _ranges(server: Server) -> list[str | None]:
    return [r.headers.get("range") for r in server.requests]


# ─────────────────────────── resumed ───────────────────────────


async def test_cut_body_is_resumed_and_the_hash_is_the_full_body(staging: Path) -> None:
    server = Server(cuts=[MIB + MIB // 2])
    result = await _fetch(server, staging)
    assert result.status == "fetched"
    assert result.resumes == 1
    assert result.path is not None
    assert result.path.read_bytes() == BODY
    assert result.sha256 == _sha(BODY)
    assert result.size_bytes == len(BODY)
    # httpx hands the body over in whole MiB: the bytes after the last whole
    # MiB of a cut never reach the file, and the resume asks for them again.
    assert _ranges(server) == [None, f"bytes={MIB}-"]
    resume = server.requests[1]
    assert resume.headers["if-range"] == '"zz-v1"'
    assert resume.headers["accept-encoding"] == "identity"


async def test_resume_never_replays_the_conditional_get(staging: Path) -> None:
    """The slot's ETag goes on the first request only: on a resume an
    If-None-Match could turn the answer into a 304."""
    server = Server(cuts=[2 * MIB + 7])
    previous = {"url": STATE_URL, "etag": '"zz-v0"', "last_modified": "Mon, 05 Oct 2026"}
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "fetched"
    first, resume = server.requests
    assert first.headers["if-none-match"] == '"zz-v0"'
    assert "if-none-match" not in resume.headers
    assert "if-modified-since" not in resume.headers
    assert resume.headers["range"] == f"bytes={2 * MIB}-"


async def test_resume_across_several_cuts(staging: Path) -> None:
    server = Server(cuts=[MIB + 1, 2 * MIB + 5, 3 * MIB + MIB // 2, 4 * MIB + 9])
    result = await _fetch(server, staging)
    assert result.resumes == 4
    assert result.sha256 == _sha(BODY)
    assert result.path is not None
    assert result.path.read_bytes() == BODY
    assert _ranges(server) == [
        None,
        f"bytes={MIB}-",
        f"bytes={2 * MIB}-",
        f"bytes={3 * MIB}-",
        f"bytes={4 * MIB}-",
    ]


async def test_body_that_ends_early_without_an_error_is_resumed(staging: Path) -> None:
    """A body shorter than its Content-Length that ends cleanly is a cut too."""
    calls = 0

    def short_then_server(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            head = BODY[: 2 * MIB]
            return httpx.Response(
                200,
                headers={"ETag": '"zz-v1"', "Content-Length": str(len(BODY))},
                content=_once(head),
            )
        return Server()(request)

    result = await _fetch(short_then_server, staging)
    assert result.resumes == 1
    assert result.sha256 == _sha(BODY)


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_without_accept_ranges_a_range_is_still_tried(staging: Path) -> None:
    """No Accept-Ranges at all: the 206 decides."""
    server = Server(cuts=[MIB + 3], accept_ranges=None)
    result = await _fetch(server, staging)
    assert result.resumes == 1
    assert result.sha256 == _sha(BODY)


async def test_last_modified_serves_when_the_etag_is_weak(staging: Path) -> None:
    lm = "Thu, 15 Oct 2026 03:00:00 GMT"
    server = Server(cuts=[MIB + 3], etag='W/"zz-weak"', last_modified=lm)
    result = await _fetch(server, staging)
    assert result.resumes == 1
    assert server.requests[1].headers["if-range"] == lm


# ─────────────────────────── restarted ───────────────────────────


@pytest.mark.parametrize(
    "server_kw",
    [
        pytest.param({"accept_ranges": "none"}, id="accept-ranges-none"),
        pytest.param({"etag": None}, id="no-validator"),
        pytest.param({"etag": 'W/"zz-weak"'}, id="weak-etag-only"),
    ],
)
async def test_no_resume_without_a_safe_range(staging: Path, server_kw: dict[str, Any]) -> None:
    """Without ranges or a validator for If-Range, a cut body is downloaded
    again from byte 0, never resumed."""
    server = Server(cuts=[MIB + 3], **server_kw)
    result = await _fetch(server, staging)
    assert _ranges(server) == [None, None]
    assert result.resumes == 0
    assert result.sha256 == _sha(BODY)


async def test_content_encoded_body_is_never_resumed(staging: Path) -> None:
    """httpx decodes a gzip Content-Encoding: the decoded byte count is not
    an offset into what the server holds, so a cut restarts from byte 0."""
    import gzip

    encoded = gzip.compress(BODY, compresslevel=1)
    server = Server(encoded, cuts=[len(encoded) - 10], extra={"Content-Encoding": "gzip"})
    result = await _fetch(server, staging)
    assert _ranges(server) == [None, None]
    assert result.resumes == 0
    assert result.sha256 == _sha(BODY)


async def test_cut_before_any_byte_was_kept_is_a_plain_retry(staging: Path) -> None:
    """Nothing reached the file (httpx hands bytes over in whole MiB): there
    is nothing to resume, the next request is a plain GET."""
    server = Server(cuts=[MIB - 1])
    result = await _fetch(server, staging)
    assert _ranges(server) == [None, None]
    assert result.resumes == 0
    assert result.sha256 == _sha(BODY)


async def test_server_that_ignores_range_restarts_from_zero(staging: Path) -> None:
    server = Server(cuts=[MIB + 3], honour_range=False)
    result = await _fetch(server, staging)
    assert result.resumes == 1  # one resume was tried, then refused
    assert result.sha256 == _sha(BODY)
    assert _ranges(server) == [None, f"bytes={MIB}-", None]


async def test_changed_file_restarts_and_is_never_mixed(staging: Path) -> None:
    """The file changes between the cut and the resume: If-Range no longer
    matches, the server answers 200, and the whole new version is fetched."""
    server = Server(cuts=[2 * MIB + 3], next_body=OTHER_BODY, next_etag='"zz-v2"')
    result = await _fetch(server, staging)
    assert result.path is not None
    assert result.path.read_bytes() == OTHER_BODY
    assert result.sha256 == _sha(OTHER_BODY)
    assert result.state["etag"] == '"zz-v2"'
    assert result.resumes == 1
    assert _ranges(server) == [None, f"bytes={2 * MIB}-", None]


@pytest.mark.parametrize(
    "bad_range",
    [
        pytest.param(lambda start, size: f"bytes {start - 10}-{size - 1}/{size}", id="start-early"),
        pytest.param(lambda start, size: f"bytes {start + 10}-{size - 1}/{size}", id="start-late"),
        pytest.param(lambda start, size: f"bytes {start}-{size}/{size + 1}", id="other-total"),
        pytest.param(lambda start, size: f"bytes {start}-{size - 1}/*", id="unknown-total"),
        pytest.param(
            lambda start, size: f"bytes {start}-{start - 1}/{size}", id="end-before-start"
        ),
        pytest.param(lambda start, size: f"bytes {start}-{size}/{size}", id="end-past-total"),
        pytest.param(lambda start, size: "", id="missing"),
    ],
)
async def test_206_that_does_not_continue_the_file_restarts(
    staging: Path, bad_range: Callable[[int, int], str]
) -> None:
    server = Server(cuts=[MIB + 3], bad_range=bad_range)
    result = await _fetch(server, staging)
    # The bad 206 is refused before any byte is appended; the next request
    # starts again from byte 0 and ends cleanly.
    assert result.sha256 == _sha(BODY)
    assert result.resumes == 1
    assert _ranges(server) == [None, f"bytes={MIB}-", None]


async def test_416_to_a_resume_restarts(staging: Path) -> None:
    server = Server(cuts=[MIB + 3], on_range=lambda r: httpx.Response(416))
    result = await _fetch(server, staging)
    assert result.sha256 == _sha(BODY)
    assert _ranges(server) == [None, f"bytes={MIB}-", None]


async def test_partial_file_changed_on_disk_restarts(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partial file must hold exactly the bytes counted before any
    resume appends to it."""
    real_sleep = feed_fetch.asyncio.sleep

    async def tamper(delay: float) -> None:
        for leftover in staging.glob("*.download"):
            with leftover.open("ab") as fh:
                fh.write(b"ZZ")
        await real_sleep(0)

    monkeypatch.setattr(feed_fetch.asyncio, "sleep", tamper)
    server = Server(cuts=[MIB + 3])
    result = await _fetch(server, staging)
    assert result.sha256 == _sha(BODY)
    assert _ranges(server) == [None, None]


# ─────────────────────────── given up ───────────────────────────


async def test_two_resumes_in_a_row_without_progress_fail(staging: Path) -> None:
    size = len(BODY)
    server = Server(cuts=[2 * MIB + 3, 2 * MIB, 2 * MIB, None])
    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    reason = str(exc.value)
    assert reason.startswith("download failed: peer closed connection")
    assert "no progress in 2 resume attempts in a row" in reason
    assert f"stopped at byte {2 * MIB} of {size}" in reason
    assert "(resumed 2 times)" in reason
    assert len(server.requests) == 3
    assert list(staging.iterdir()) == []


async def test_one_resume_without_progress_is_forgiven(staging: Path) -> None:
    server = Server(cuts=[2 * MIB + 3, 2 * MIB, 3 * MIB + 1])
    result = await _fetch(server, staging)
    assert result.resumes == 3
    assert result.sha256 == _sha(BODY)


async def test_server_errors_on_resume_count_as_no_progress(staging: Path) -> None:
    server = Server(cuts=[MIB + 3], on_range=lambda r: httpx.Response(503))
    with pytest.raises(FetchError, match=r"HTTP 503.*no progress in 2 resume attempts"):
        await _fetch(server, staging)
    assert list(staging.iterdir()) == []


async def test_refusal_on_resume_fails_at_once(staging: Path) -> None:
    """A 403 (an expired signed link, say) would not come right by retrying."""
    server = Server(cuts=[MIB + 3], on_range=lambda r: httpx.Response(403))
    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    assert str(exc.value) == "download failed: HTTP 403 Forbidden (resumed 1 time)"
    assert len(server.requests) == 2
    assert list(staging.iterdir()) == []


async def test_resumes_are_bounded(staging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_fetch, "RESUME_DELAYS", (0.0, 0.0))
    server = Server(cuts=[MIB + 1, 2 * MIB + 1, 3 * MIB + 1, 4 * MIB + 1])
    with pytest.raises(FetchError, match=rf"gave up after 2 resumes, stopped at byte {3 * MIB} "):
        await _fetch(server, staging)
    assert len(server.requests) == 3
    assert list(staging.iterdir()) == []


async def test_restarts_stay_bounded_when_every_resume_is_refused(staging: Path) -> None:
    server = Server(cuts=[MIB + 3] * 6, honour_range=False)
    with pytest.raises(FetchError, match="server answered 200 to a range request"):
        await _fetch(server, staging)
    # full, range (200: restart 1), full, range (200: restart 2), full, range: give up
    assert _ranges(server) == [None, f"bytes={MIB}-"] * 3
    assert list(staging.iterdir()) == []


async def test_body_longer_than_announced_fails_with_size_mismatch(staging: Path) -> None:
    def too_long(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": str(MIB)}, content=_once(BODY[: 2 * MIB])
        )

    calls = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return too_long(request)

    with pytest.raises(
        FetchError, match=rf"size mismatch, the server sent more than the {MIB} bytes"
    ):
        await _fetch(counting, staging)
    assert len(calls) == 1
    assert list(staging.iterdir()) == []


async def test_206_longer_than_its_range_fails_with_size_mismatch(staging: Path) -> None:
    size = len(BODY)

    def overlong(request: httpx.Request) -> httpx.Response:
        start = int(request.headers["range"].removeprefix("bytes=").removesuffix("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{start + 9}/{size}"},
            content=_once(BODY[start:]),
        )

    server = Server(cuts=[MIB + 3], on_range=overlong)
    with pytest.raises(
        FetchError, match=rf"size mismatch, the server sent more than the {MIB + 10} bytes"
    ):
        await _fetch(server, staging)
    assert list(staging.iterdir()) == []


# ─────────────────────────── reasons, logs, memory ───────────────────────────


async def test_each_resume_logs_one_line_without_the_url(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.feed_fetch")
    server = Server(cuts=[MIB + 1, 3 * MIB + 1])
    await _fetch(server, staging)
    lines = [r.getMessage() for r in caplog.records if "resume" in r.getMessage()]
    size = len(BODY)
    assert lines == [
        f"fetch resume 1 from byte {MIB} of {size} after RemoteProtocolError",
        f"fetch resume 2 from byte {3 * MIB} of {size} after RemoteProtocolError",
    ]
    for record in caplog.records:
        assert "zz-nap.example" not in record.getMessage()
        assert "zz-fake-key" not in record.getMessage()


async def test_failure_reason_never_carries_the_fetch_url(staging: Path) -> None:
    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"connection to {request.url} reset")

    server = Server(cuts=[MIB + 3], on_range=leaky)
    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    assert "zz-fake-key" not in str(exc.value)
    assert STATE_URL in str(exc.value)


async def test_memory_stays_bounded_for_a_large_body(tmp_path: Path) -> None:
    """The body streams to disk in chunks: a 48 MiB download cut twice never
    holds more than a few MiB in memory."""
    block = random.Random(97).randbytes(MIB)
    total = 48 * MIB
    cuts = [16 * MIB + 5, 32 * MIB + 5]
    seen: list[httpx.Request] = []

    async def stream(start: int, cut: int | None) -> AsyncIterator[bytes]:
        pos = start
        end = cut if cut is not None else total
        while pos < end:
            offset = pos % MIB
            step = min(MIB - offset, end - pos)
            yield block[offset : offset + step]
            pos += step
        if cut is not None:
            raise httpx.ReadError("zz cut")

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cut = cuts.pop(0) if cuts else None
        headers = {"ETag": '"zz-big"', "Accept-Ranges": "bytes"}
        wanted = request.headers.get("range")
        start = int(wanted.removeprefix("bytes=").removesuffix("-")) if wanted else 0
        headers["Content-Length"] = str(total - start)
        if wanted:
            headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
            return httpx.Response(206, headers=headers, content=stream(start, cut))
        return httpx.Response(200, headers=headers, content=stream(0, cut))

    dest = tmp_path / "zz.download"
    tracemalloc.start()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            download = await feed_fetch._download(client, FETCH_URL, {}, dest, state_url=STATE_URL)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert download.size == total
    assert download.resumes == 2
    assert dest.stat().st_size == total
    expected = hashlib.sha256()
    for _ in range(48):
        expected.update(block)
    assert download.sha256 == expected.hexdigest()
    assert peak < 8 * MIB
    assert len(seen) == 3
