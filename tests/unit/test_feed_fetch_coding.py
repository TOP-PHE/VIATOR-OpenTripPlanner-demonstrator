"""Servers that refuse `Accept-Encoding: identity` (app/feed_fetch.py).

Feed downloads ask for the file's own bytes so that a cut can be resumed
(#360). Some servers refuse that with HTTP 406 Not Acceptable (Fintraffic's
Digitraffic does for its rail GTFS). These tests pin the answer: one more
request in gzip from byte 0, never resumed, its decoded size capped, the
coding remembered in the fetch state for the same URL, and a clear reason
when both codings are refused.

Every server here is an `httpx.MockTransport` stand-in with invented bodies
(seeded pseudo-random bytes in an invented GTFS zip) and an invented
`.example` URL whose query holds a fake key that must never reach a log.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import random
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_fetch
from app.feed_fetch import FetchError, fetch_validated

MIB = 1024 * 1024
FETCH_URL = "https://zz-rail.example/gtfs.zip?key=zz-fake-key"
STATE_URL = "https://zz-rail.example/gtfs.zip"
REFUSED_BOTH = (
    "download failed: HTTP 406 Not Acceptable (refused with Accept-Encoding identity and gzip)"
)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_fetch, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(feed_fetch, "RESUME_DELAYS", (0.0,) * 8)
    monkeypatch.setattr(feed_fetch, "RESUME_BUSY_DELAYS", (0.0,) * 5)


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _gtfs(seed: int = 99, padding: int = 3 * MIB) -> bytes:
    """An invented GTFS zip: the five files plus one stored member of seeded
    pseudo-random bytes, so a cut lands mid-body."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\nZZ1\n")
        z.writestr("zz_padding.bin", random.Random(seed).randbytes(padding), zipfile.ZIP_STORED)
    return buf.getvalue()


BODY = _gtfs()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _chunks(data: bytes, cut: int | None) -> AsyncIterator[bytes]:
    """`data` in 256 KiB pieces, ended at byte `cut` by a cut connection."""
    end = len(data) if cut is None else cut
    for pos in range(0, end, 256 * 1024):
        yield data[pos : min(pos + 256 * 1024, end)]
    if cut is not None:
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


class CodingServer:
    """A file server stand-in. Answers 406 to a request whose Accept-Encoding
    is in `refuse`. A request in gzip gets a gzip Content-Encoding unless
    `plain_for_gzip` (a server that ignores the coding asked). It honours
    Range under If-Range, answers 304 to a matching If-None-Match, and ends
    the n-th body at byte `cuts[n]` of what it sends (None: no cut)."""

    def __init__(
        self,
        body: bytes = BODY,
        *,
        refuse: frozenset[str] = frozenset({"identity"}),
        plain_for_gzip: bool = False,
        cuts: list[int | None] | None = None,
        statuses: list[int] | None = None,
        etag: str = '"zz-v1"',
    ) -> None:
        self.body = body
        self.refuse = refuse
        self.plain_for_gzip = plain_for_gzip
        self.cuts = list(cuts or [])
        self.statuses = list(statuses or [])  # answers before any body, popped in order
        self.etag = etag
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) > 8:  # a download that never gives up
            raise AssertionError("more requests than any download may make")
        coding = request.headers.get("accept-encoding", "")
        if coding in self.refuse:
            return httpx.Response(406)
        if self.statuses:
            return httpx.Response(self.statuses.pop(0))
        headers = {"ETag": self.etag, "Accept-Ranges": "bytes"}
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers=headers)
        cut = self.cuts.pop(0) if self.cuts else None
        if coding == "gzip" and not self.plain_for_gzip:
            sent = gzip.compress(self.body, compresslevel=1)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(sent))
            return httpx.Response(200, headers=headers, content=_chunks(sent, cut))
        wanted = request.headers.get("range")
        if wanted and request.headers.get("if-range") == self.etag:
            start = int(wanted.removeprefix("bytes=").removesuffix("-"))
            size = len(self.body)
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
            headers["Content-Length"] = str(size - start)
            return httpx.Response(206, headers=headers, content=_chunks(self.body[start:], cut))
        headers["Content-Length"] = str(len(self.body))
        return httpx.Response(200, headers=headers, content=_chunks(self.body, cut))

    def codings(self) -> list[str | None]:
        return [r.headers.get("accept-encoding") for r in self.requests]

    def ranges(self) -> list[str | None]:
        return [r.headers.get("range") for r in self.requests]


async def _fetch(
    server: Any,
    staging: Path,
    *,
    previous: dict[str, Any] | None = None,
    have_current: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> feed_fetch.FetchResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        return await fetch_validated(
            client,
            fetch_url=FETCH_URL,
            state_url=STATE_URL,
            extra_headers=extra_headers or {},
            kind="GTFS",
            staging=staging,
            base_name="20261018-zz-provider",
            suffix=".zip",
            previous=previous or {},
            have_current=have_current,
        )


# ─────────────────────────── the 406 answer ───────────────────────────


async def test_identity_refused_is_asked_again_in_gzip(staging: Path) -> None:
    server = CodingServer()
    result = await _fetch(server, staging)
    assert result.status == "fetched"
    assert result.path is not None
    assert result.path.read_bytes() == BODY
    assert result.sha256 == _sha(BODY)
    assert result.size_bytes == len(BODY)
    assert result.resumes == 0
    assert server.codings() == ["identity", "gzip"]
    assert server.ranges() == [None, None]
    assert result.state["content_coding"] == "gzip"
    assert result.state["url"] == STATE_URL


async def test_remembered_coding_skips_the_406(staging: Path) -> None:
    server = CodingServer()
    previous = {"url": STATE_URL, "content_coding": "gzip"}
    result = await _fetch(server, staging, previous=previous)
    assert result.sha256 == _sha(BODY)
    assert server.codings() == ["gzip"]
    assert result.state["content_coding"] == "gzip"


async def test_coding_of_another_url_is_not_used(staging: Path) -> None:
    server = CodingServer(refuse=frozenset())
    previous = {"url": "https://zz-other.example/gtfs.zip", "content_coding": "gzip"}
    result = await _fetch(server, staging, previous=previous)
    assert server.codings() == ["identity"]
    assert "content_coding" not in result.state


async def test_identity_download_keeps_no_coding(staging: Path) -> None:
    server = CodingServer(refuse=frozenset())
    result = await _fetch(server, staging)
    assert server.codings() == ["identity"]
    assert "content_coding" not in result.state


async def test_both_codings_refused_fails_with_a_clear_reason(staging: Path) -> None:
    server = CodingServer(refuse=frozenset({"identity", "gzip"}))
    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    assert str(exc.value) == REFUSED_BOTH
    assert server.codings() == ["identity", "gzip"]
    assert list(staging.iterdir()) == []


async def test_remembered_gzip_refused_goes_back_to_identity(staging: Path) -> None:
    """The server changed its mind: gzip refused, identity served. The
    download completes and the coding is forgotten."""
    server = CodingServer(refuse=frozenset({"gzip"}))
    previous = {"url": STATE_URL, "content_coding": "gzip"}
    result = await _fetch(server, staging, previous=previous)
    assert result.sha256 == _sha(BODY)
    assert server.codings() == ["gzip", "identity"]
    assert "content_coding" not in result.state


async def test_the_switch_uses_no_retry(staging: Path) -> None:
    """The 406 is a negotiation, not a failure: both retries stay for the
    transient errors that follow it."""
    server = CodingServer(statuses=[503, 503])
    result = await _fetch(server, staging)
    assert result.sha256 == _sha(BODY)
    assert server.codings() == ["identity", "gzip", "gzip", "gzip"]


@pytest.mark.parametrize("status", [400, 403, 415])
async def test_other_refusals_change_nothing(staging: Path, status: int) -> None:
    server = CodingServer(refuse=frozenset(), statuses=[status])
    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    assert str(exc.value).startswith(f"download failed: HTTP {status} ")
    assert "refused with" not in str(exc.value)
    assert server.codings() == ["identity"]


async def test_one_accept_encoding_whatever_the_extra_headers(staging: Path) -> None:
    server = CodingServer()
    await _fetch(server, staging, extra_headers={"accept-encoding": "br"})
    assert [r.headers.get_list("accept-encoding") for r in server.requests] == [
        ["identity"],
        ["gzip"],
    ]


async def test_switch_logs_one_line_without_the_url(
    staging: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.feed_fetch")
    await _fetch(CodingServer(), staging)
    lines = [r.getMessage() for r in caplog.records if "406" in r.getMessage()]
    assert lines == [
        "fetch: server refused Accept-Encoding identity (HTTP 406), asking for gzip from byte 0"
    ]
    for record in caplog.records:
        assert "zz-rail.example" not in record.getMessage()
        assert "zz-fake-key" not in record.getMessage()


# ─────────────────────────── never resumed ───────────────────────────


@pytest.mark.parametrize("plain_for_gzip", [False, True], ids=["gzip-answer", "plain-answer"])
async def test_cut_in_gzip_restarts_from_byte_zero(staging: Path, plain_for_gzip: bool) -> None:
    """A transfer asked in gzip is never resumed, even when the answer is a
    plain body with a size, ranges and a strong ETag: a cut starts again
    from byte 0, still in gzip."""
    server = CodingServer(plain_for_gzip=plain_for_gzip, cuts=[MIB + MIB // 2])
    result = await _fetch(server, staging, previous={"url": STATE_URL, "content_coding": "gzip"})
    assert result.sha256 == _sha(BODY)
    assert result.resumes == 0
    assert server.codings() == ["gzip", "gzip"]
    assert server.ranges() == [None, None]


async def test_cut_after_the_switch_restarts_in_gzip(staging: Path) -> None:
    server = CodingServer(cuts=[MIB + MIB // 2])
    result = await _fetch(server, staging)
    assert result.sha256 == _sha(BODY)
    assert server.codings() == ["identity", "gzip", "gzip"]
    assert server.ranges() == [None, None, None]


# ─────────────────────────── decoded size cap ───────────────────────────


@pytest.mark.parametrize(
    "refuse", [frozenset({"identity"}), frozenset()], ids=["after-406", "coded-unasked"]
)
async def test_gzip_bomb_is_stopped_at_the_cap(
    staging: Path, monkeypatch: pytest.MonkeyPatch, refuse: frozenset[str]
) -> None:
    """A small compressed body that decodes to far more than the cap stops
    there, whether gzip was asked for or the server coded it unasked."""
    monkeypatch.setattr(feed_fetch, "CODED_MAX_BYTES", 2 * MIB)
    bomb = bytes(64 * MIB)
    encoded = gzip.compress(bomb, compresslevel=9)
    assert len(encoded) < MIB // 4

    def server(request: httpx.Request) -> httpx.Response:
        if request.headers.get("accept-encoding") in refuse:
            return httpx.Response(406)
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=_chunks(encoded, None)
        )

    with pytest.raises(FetchError) as exc:
        await _fetch(server, staging)
    assert str(exc.value) == (
        f"download failed: the compressed body decodes to more than {2 * MIB} bytes, "
        "the limit for a content-coded download"
    )
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(("cap", "fits"), [(len(BODY), True), (len(BODY) - 1, False)])
async def test_cap_counts_decoded_bytes(
    staging: Path, monkeypatch: pytest.MonkeyPatch, cap: int, fits: bool
) -> None:
    monkeypatch.setattr(feed_fetch, "CODED_MAX_BYTES", cap)
    server = CodingServer()
    if fits:
        result = await _fetch(server, staging)
        assert result.size_bytes == len(BODY)
        return
    with pytest.raises(FetchError, match="decodes to more than"):
        await _fetch(server, staging)


async def test_cap_never_applies_to_a_plain_body(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain body is bounded by its announced size, not by the cap."""
    monkeypatch.setattr(feed_fetch, "CODED_MAX_BYTES", MIB)
    result = await _fetch(CodingServer(refuse=frozenset()), staging)
    assert result.size_bytes == len(BODY)


# ─────────────────────────── conditional GET ───────────────────────────


async def test_conditional_get_in_gzip_answers_unchanged(staging: Path) -> None:
    server = CodingServer()
    previous = {
        "url": STATE_URL,
        "etag": '"zz-v1"',
        "sha256": _sha(BODY),
        "content_coding": "gzip",
    }
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "unchanged"
    assert result.reason == "not modified (HTTP 304)"
    assert server.codings() == ["gzip"]
    assert server.requests[0].headers["if-none-match"] == '"zz-v1"'
    assert result.state["content_coding"] == "gzip"


async def test_304_after_the_switch_remembers_gzip(staging: Path) -> None:
    server = CodingServer()
    previous = {"url": STATE_URL, "etag": '"zz-v1"', "sha256": _sha(BODY)}
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "unchanged"
    assert server.codings() == ["identity", "gzip"]
    assert server.requests[1].headers["if-none-match"] == '"zz-v1"'
    assert result.state["content_coding"] == "gzip"


async def test_same_content_in_gzip_is_unchanged(staging: Path) -> None:
    """The hash is that of the decoded file, so it matches the one an
    identity download stored: "identical content", gzip remembered."""
    server = CodingServer(etag='"zz-v2"')
    previous = {"url": STATE_URL, "etag": '"zz-v1"', "sha256": _sha(BODY), "fetched_at": "zz"}
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "unchanged"
    assert result.reason == "identical content (sha256 match)"
    assert result.state["content_coding"] == "gzip"
    assert result.state["fetched_at"] == "zz"
    assert list(staging.iterdir()) == []


async def test_same_content_back_in_identity_forgets_gzip(staging: Path) -> None:
    """A remembered gzip server that now refuses gzip and serves the same
    file in identity: "identical content", and the coding is forgotten."""
    server = CodingServer(refuse=frozenset({"gzip"}), etag='"zz-v2"')
    previous = {
        "url": STATE_URL,
        "etag": '"zz-v1"',
        "sha256": _sha(BODY),
        "content_coding": "gzip",
    }
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "unchanged"
    assert result.reason == "identical content (sha256 match)"
    assert server.codings() == ["gzip", "identity"]
    assert "content_coding" not in result.state


async def test_304_back_in_identity_forgets_gzip(staging: Path) -> None:
    server = CodingServer(refuse=frozenset({"gzip"}))
    previous = {"url": STATE_URL, "etag": '"zz-v1"', "content_coding": "gzip"}
    result = await _fetch(server, staging, previous=previous, have_current=True)
    assert result.status == "unchanged"
    assert result.reason == "not modified (HTTP 304)"
    assert "content_coding" not in result.state


async def test_identity_content_encoding_is_a_plain_body(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Content-Encoding: identity` names no coding: no cap, and a cut is
    resumed as for any plain body."""
    monkeypatch.setattr(feed_fetch, "CODED_MAX_BYTES", MIB)
    plain = CodingServer(refuse=frozenset(), cuts=[MIB + MIB // 2])

    def server(request: httpx.Request) -> httpx.Response:
        response = plain(request)
        response.headers["Content-Encoding"] = "identity"
        return response

    result = await _fetch(server, staging)
    assert result.sha256 == _sha(BODY)
    assert result.resumes == 1
    assert plain.ranges() == [None, f"bytes={MIB}-"]
