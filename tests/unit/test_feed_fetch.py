"""Hardened feed download (app/feed_fetch.py).

Pins the three behaviours an unattended download needs: never let a non-feed
reach the slot, never rebuild for an unchanged file, ride out transient errors.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import feed_fetch
from app.feed_fetch import FetchError, fetch_validated

NETEX_XML = (
    b'<?xml version="1.0"?><PublicationDelivery xmlns="http://www.netex.org.uk/netex" '
    b'version="1.0"><ParticipantRef>IT-RAP</ParticipantRef></PublicationDelivery>'
)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_fetch, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    """Own dir — conftest drops an `inbox/` into tmp_path."""
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _gtfs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            z.writestr(name, "id\n1\n")
    return buf.getvalue()


def _netex_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("line.xml", NETEX_XML)
    return buf.getvalue()


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch(
    handler: Any,
    staging: Path,
    *,
    kind: str = "GTFS",
    suffix: str = ".zip",
    previous: dict[str, Any] | None = None,
    have_current: bool = False,
) -> feed_fetch.FetchResult:
    async with _client(handler) as c:
        return await fetch_validated(
            c,
            fetch_url="https://nap.example/feed?key=secret",
            state_url="https://nap.example/feed",
            extra_headers={},
            kind=kind,
            staging=staging,
            base_name="20260929-provider",
            suffix=suffix,
            previous=previous or {},
            have_current=have_current,
        )


# ─────────────────────────── fetched ───────────────────────────


async def test_valid_gtfs_is_fetched_with_validators_recorded(staging: Path) -> None:
    body = _gtfs_zip()
    headers = {"ETag": "abc", "Last-Modified": "Mon, 28 Sep 2026 08:35:00 GMT"}
    result = await _fetch(lambda r: httpx.Response(200, content=body, headers=headers), staging)
    assert result.status == "fetched"
    assert result.path == staging / "20260929-provider.zip"
    assert result.path.read_bytes() == body
    assert result.size_bytes == len(body)
    # state stores the credential-free URL, never the fetch URL
    assert result.state["url"] == "https://nap.example/feed"
    assert result.state["etag"] == "abc"
    assert result.state["last_modified"].startswith("Mon, 28 Sep")
    assert result.state["sha256"] == result.sha256
    assert not (staging / "20260929-provider.download").exists()


async def test_gzip_netex_is_rewrapped_as_a_zip(staging: Path) -> None:
    """The Italian NAP serves `IT-IT-TRENITALIA_L1.xml.gz`, not a zip."""
    headers = {"Content-Disposition": 'attachment; filename="IT-IT-TRENITALIA_L1.xml.gz"'}
    body = gzip.compress(NETEX_XML)
    result = await _fetch(
        lambda r: httpx.Response(200, content=body, headers=headers), staging, kind="NeTEx-EPIP"
    )
    assert result.status == "fetched"
    assert result.path is not None
    with zipfile.ZipFile(result.path) as z:
        assert z.namelist() == ["IT-IT-TRENITALIA_L1.xml"]
        assert z.read("IT-IT-TRENITALIA_L1.xml") == NETEX_XML


async def test_gzip_without_disposition_gets_a_fallback_member_name(staging: Path) -> None:
    body = gzip.compress(NETEX_XML)
    result = await _fetch(lambda r: httpx.Response(200, content=body), staging, kind="NeTEx-EPIP")
    assert result.path is not None
    with zipfile.ZipFile(result.path) as z:
        assert z.namelist() == ["20260929-provider.xml"]


async def test_osm_pbf_keeps_pbf_suffix(staging: Path) -> None:
    body = b"\x00\x00\x00\x0d" + b"\x0a\x09OSMHeader" + b"\x00" * 100
    result = await _fetch(lambda r: httpx.Response(200, content=body), staging, kind="OSM-PBF")
    assert result.path == staging / "20260929-provider.pbf"


async def test_csv_kind_keeps_the_url_suffix(staging: Path) -> None:
    body = b"uic;gare\n8700011;Paris Nord\n"
    result = await _fetch(
        lambda r: httpx.Response(200, content=body), staging, kind="SNCF-Stations", suffix=".csv"
    )
    assert result.path == staging / "20260929-provider.csv"


# ─────────────────────────── rejected ───────────────────────────


@pytest.mark.parametrize(
    ("kind", "body", "fragment"),
    [
        ("GTFS", b"<!DOCTYPE html><html><body>Login</body></html>", "HTML"),
        ("GTFS", b'{"error": "rate limited"}', "JSON"),
        ("GTFS", b"", "empty"),
        ("GTFS", b"PK\x05\x06" + b"\x00" * 18, "empty zip"),
        ("GTFS", b"\x89PNG\r\n\x1a\n", "unrecognised"),
        ("NeTEx-EPIP", None, "declared NeTEx-EPIP but the file is GTFS"),
        ("GTFS", b"PK\x03\x04truncated-garbage", "not a usable GTFS archive"),
        ("GTFS", b"\x1f\x8b\x08\x00corrupt", "gzip body could not be unpacked"),
        ("OSM-PBF", b"<html>Geofabrik 410</html>", "expected an OSM PBF"),
        ("SNCF-MCT", b"  <html>nope</html>", "expected a SNCF-MCT file"),
    ],
)
async def test_non_feeds_never_reach_the_slot(
    staging: Path, kind: str, body: bytes | None, fragment: str
) -> None:
    content = _gtfs_zip() if body is None else body
    with pytest.raises(FetchError) as exc:
        await _fetch(lambda r: httpx.Response(200, content=content), staging, kind=kind)
    assert fragment in str(exc.value)
    assert list(staging.iterdir()) == [], "rejected downloads must leave nothing behind"


async def test_netex_zip_declared_gtfs_is_rejected(staging: Path) -> None:
    with pytest.raises(FetchError, match="declared GTFS but the file is NeTEx-EPIP"):
        await _fetch(lambda r: httpx.Response(200, content=_netex_zip()), staging)


# ─────────────────────────── unchanged ───────────────────────────

PREVIOUS = {
    "url": "https://nap.example/feed",
    "etag": "abc",  # unquoted, as LIO's server sends it — must be replayed verbatim
    "last_modified": "Mon, 28 Sep 2026 08:35:00 GMT",
    "sha256": "0" * 64,
    "fetched_at": "2026-09-28T08:40:00+00:00",
}


async def test_304_on_a_conditional_request_is_unchanged(staging: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == "abc"
        assert request.headers["if-modified-since"] == PREVIOUS["last_modified"]
        return httpx.Response(304)

    result = await _fetch(handler, staging, previous=PREVIOUS, have_current=True)
    assert result.status == "unchanged"
    assert "304" in (result.reason or "")
    assert result.state["fetched_at"] == PREVIOUS["fetched_at"]
    assert result.state["checked_at"]


async def test_no_conditional_headers_when_the_slot_is_empty(staging: Path) -> None:
    body = _gtfs_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "if-none-match" not in request.headers
        return httpx.Response(200, content=body)

    result = await _fetch(handler, staging, previous=PREVIOUS, have_current=False)
    assert result.status == "fetched"


async def test_no_conditional_headers_when_the_url_changed(staging: Path) -> None:
    """LU publishes each week's file at a new URL — old validators don't apply."""
    body = _gtfs_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "if-none-match" not in request.headers
        return httpx.Response(200, content=body)

    previous = {**PREVIOUS, "url": "https://nap.example/last-week.zip"}
    result = await _fetch(handler, staging, previous=previous, have_current=True)
    assert result.status == "fetched"


async def test_unsolicited_304_is_an_error(staging: Path) -> None:
    with pytest.raises(FetchError, match="unconditional"):
        await _fetch(lambda r: httpx.Response(304), staging)


async def test_identical_content_is_unchanged(staging: Path) -> None:
    """OURA sends no validators; RENFE's ETag flaps between backends."""
    body = _gtfs_zip()
    first = await _fetch(lambda r: httpx.Response(200, content=body), staging)
    assert first.path is not None
    first.path.unlink()
    again = await _fetch(
        lambda r: httpx.Response(200, content=body, headers={"ETag": "flapped"}),
        staging,
        previous=first.state,
        have_current=True,
    )
    assert again.status == "unchanged"
    assert "sha256" in (again.reason or "")
    assert again.state["etag"] == "flapped"
    assert list(staging.iterdir()) == []


# ─────────────────────────── retries ───────────────────────────


async def test_transient_503_is_retried(staging: Path) -> None:
    body = _gtfs_zip()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, content=body)

    result = await _fetch(handler, staging)
    assert result.status == "fetched"
    assert calls == 2


async def test_connection_errors_exhaust_retries(staging: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("reset")

    with pytest.raises(FetchError, match="download failed"):
        await _fetch(handler, staging)
    assert calls == 3


async def test_404_is_not_retried(staging: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with pytest.raises(FetchError, match="404"):
        await _fetch(handler, staging)
    assert calls == 1


# ─────────────────────────── state file ───────────────────────────


def test_state_round_trips_under_a_safe_filename(staging: Path) -> None:
    key = "provider[SNCF].timetable(gtfs)"
    feed_fetch.save_state(staging / "_fetch_state", key, {"etag": "x"})
    assert feed_fetch.load_state(staging / "_fetch_state", key) == {"etag": "x"}
    (path,) = (staging / "_fetch_state").iterdir()
    assert path.name == "provider_SNCF_.timetable_gtfs.json"


def test_missing_or_corrupt_state_is_empty(staging: Path) -> None:
    assert feed_fetch.load_state(staging, "nope") == {}
    feed_fetch.state_path(staging, "bad").write_text("{not json", encoding="utf-8")
    assert feed_fetch.load_state(staging, "bad") == {}
    feed_fetch.state_path(staging, "list").write_text("[1]", encoding="utf-8")
    assert feed_fetch.load_state(staging, "list") == {}


def test_save_state_failure_is_not_fatal(staging: Path) -> None:
    blocker = staging / "file"
    blocker.write_text("x", encoding="utf-8")
    feed_fetch.save_state(blocker / "sub", "k", {"a": 1})  # mkdir under a file fails


# ─────────────────────────── corrupt archives ───────────────────────────


async def test_corrupt_gzip_is_rejected_and_staging_emptied(staging: Path) -> None:
    body = gzip.compress(NETEX_XML * 50)
    corrupt = body[:20] + bytes(b ^ 0xFF for b in body[20:-8]) + body[-8:]  # zlib.error
    with pytest.raises(FetchError, match="gzip body could not be unpacked"):
        await _fetch(lambda r: httpx.Response(200, content=corrupt), staging, kind="NeTEx-EPIP")
    assert list(staging.iterdir()) == []


async def test_truncated_zip_is_rejected_and_staging_emptied(staging: Path) -> None:
    body = _gtfs_zip()[:40]
    with pytest.raises(FetchError, match="not a usable GTFS archive"):
        await _fetch(lambda r: httpx.Response(200, content=body), staging)
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    "exc", [NotImplementedError("Deflate64"), RuntimeError("encrypted"), EOFError()]
)
async def test_any_archive_error_becomes_a_fetch_error(
    staging: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def explode(path: Path) -> str:
        raise exc

    monkeypatch.setattr(feed_fetch.detect, "detect", explode)
    with pytest.raises(FetchError, match="not a usable GTFS archive"):
        await _fetch(lambda r: httpx.Response(200, content=_gtfs_zip()), staging)
    assert list(staging.iterdir()) == []


async def test_unexpected_error_in_the_check_still_cleans_up(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(raw: Path, kind: str, **kw: Any) -> Path:
        raw.with_name(f"{kw['base_name']}.zip").write_bytes(b"half-written")
        raise MemoryError("boom")

    monkeypatch.setattr(feed_fetch, "_validate", explode)
    with pytest.raises(FetchError, match=r"not a usable GTFS file \(MemoryError"):
        await _fetch(lambda r: httpx.Response(200, content=_gtfs_zip()), staging)
    assert list(staging.iterdir()) == []


# ─────────────────────────── kinds ───────────────────────────


async def test_either_netex_profile_satisfies_the_other(staging: Path) -> None:
    """Both profiles dispatch into netex/; detect() calls this file EPIP."""
    result = await _fetch(
        lambda r: httpx.Response(200, content=_netex_zip()), staging, kind="NeTEx-Nordic"
    )
    assert result.status == "fetched"


async def test_netex_declared_but_gtfs_served_is_still_rejected(staging: Path) -> None:
    with pytest.raises(FetchError, match="declared NeTEx-Nordic but the file is GTFS"):
        await _fetch(
            lambda r: httpx.Response(200, content=_gtfs_zip()), staging, kind="NeTEx-Nordic"
        )


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"", "empty"),
        (b"  \n", "empty"),
        (b'{"error": "quota"}', "JSON"),
        (b"\xef\xbb\xbf<!DOCTYPE html><html>", "HTML"),
    ],
)
async def test_csv_kinds_reject_non_csv_bodies(staging: Path, body: bytes, fragment: str) -> None:
    with pytest.raises(FetchError, match=fragment):
        await _fetch(
            lambda r: httpx.Response(200, content=body), staging, kind="SNCF-MCT", suffix=".csv"
        )
    assert list(staging.iterdir()) == []


async def test_csv_with_a_bom_is_accepted(staging: Path) -> None:
    body = b"\xef\xbb\xbfcode_uic;correspondance\n1;2\n"
    result = await _fetch(
        lambda r: httpx.Response(200, content=body), staging, kind="SNCF-MCT", suffix=".csv"
    )
    assert result.status == "fetched"
    assert result.path == staging / "20260929-provider.csv"


# ─────────────────────────── credential hygiene ───────────────────────────


async def test_failure_reason_never_carries_the_fetch_url(staging: Path) -> None:
    with pytest.raises(FetchError) as exc:
        await _fetch(lambda r: httpx.Response(403), staging)
    assert "403" in str(exc.value)
    assert "secret" not in str(exc.value)


async def test_transport_error_message_is_scrubbed(staging: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(FetchError) as exc:
        await _fetch(handler, staging)
    assert "secret" not in str(exc.value)
    assert "https://nap.example/feed" in str(exc.value)
