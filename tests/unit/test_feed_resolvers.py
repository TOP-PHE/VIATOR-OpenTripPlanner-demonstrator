"""NAP feed resolvers (app/feed_resolvers.py) — validation + resolution.

Resolution runs against httpx.MockTransport shaped like the real portal
responses probed 2026-09-29 (transport.data.gouv.fr, data.public.lu,
opentransportdata.swiss permalink, opendata-oepnv.de dated files).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app import feed_resolvers
from app.feed_resolvers import ResolveError, resolve, timetable_year, validate_resolver

TDG_DATASET = "64635525318cc75a9a8a771f"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard resolves hostnames; unit tests must not touch DNS."""
    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", lambda url: url)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# ─────────────────────────── validate_resolver ───────────────────────────


def test_validate_tdg_normalises_ids() -> None:
    out = validate_resolver(
        {"type": "TDG", "dataset_id": TDG_DATASET.upper(), "resource_id": "81653"}, "r"
    )
    assert out == {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653}


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (None, "must be an object"),
        ({"type": "ftp"}, "must be one of"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": "x"}, "resource_id"),
        ({"type": "tdg", "dataset_id": "not-hex", "resource_id": 1}, "dataset_id"),
        (
            {"type": "udata", "api": "http://x.lu/api/1", "dataset_id": "d", "title_regex": "."},
            "https",
        ),
        (
            {"type": "udata", "api": "https://x.lu/api/1", "dataset_id": "d", "title_regex": "("},
            "regex",
        ),
        (
            {"type": "udata", "api": "https://x.lu/api/1", "dataset_id": "d", "title_regex": ""},
            "required",
        ),
        (
            {"type": "udata", "api": "https://x.lu/api/1", "dataset_id": "a/b", "title_regex": "."},
            "dataset id",
        ),
        ({"type": "permalink", "url": "https://x/{year}/permalink"}, "placeholders"),
        ({"type": "dated", "url": "https://x/feed.zip"}, "{date}"),
        ({"type": "dated", "url": "https://x/{date}_{foo}.zip"}, "placeholders"),
        ({"type": "dated", "url": "https://x/{date}.zip", "max_days_back": 0}, "max_days_back"),
        ({"type": "dated", "url": "https://x/{date}.zip", "max_days_back": 999}, "max_days_back"),
    ],
)
def test_validate_rejects_bad_resolvers(raw: object, fragment: str) -> None:
    with pytest.raises(ValueError, match=None) as exc:
        validate_resolver(raw, "providers[0].timetable.resolver")
    assert fragment in str(exc.value)
    assert "providers[0].timetable.resolver" in str(exc.value)


def test_validate_dated_defaults_days_back() -> None:
    out = validate_resolver({"type": "dated", "url": "https://x/{date}.zip"}, "r")
    assert out["max_days_back"] == 21


def test_describe_gives_a_human_url() -> None:
    assert feed_resolvers.describe({"type": "tdg", "resource_id": 81653}) == (
        "https://transport.data.gouv.fr/resources/81653/download"
    )
    assert feed_resolvers.describe(
        {"type": "udata", "api": "https://data.public.lu/api/1", "dataset_id": "abc"}
    ) == ("https://data.public.lu/api/1/datasets/abc/")
    assert feed_resolvers.describe({"type": "permalink", "url": "https://p"}) == "https://p"


# ─────────────────────────── timetable_year ───────────────────────────


@pytest.mark.parametrize(
    ("day", "year"),
    [
        (date(2026, 9, 29), 2026),
        (date(2026, 12, 12), 2026),  # last day of timetable 2026 (second Saturday)
        (date(2026, 12, 13), 2027),  # timetable change Sunday
        (date(2027, 12, 11), 2027),  # Dec 1 2027 is a Wednesday → change on Sunday 12th
        (date(2027, 12, 12), 2028),
    ],
)
def test_timetable_year_switches_on_the_december_change(day: date, year: int) -> None:
    assert timetable_year(day) == year


# ─────────────────────────── tdg ───────────────────────────


def _tdg_handler(resources: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/datasets/{TDG_DATASET}"
        return httpx.Response(200, json={"id": TDG_DATASET, "resources": resources})

    return handler


async def test_tdg_returns_the_stable_download_url() -> None:
    resolver = {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653}
    handler = _tdg_handler([{"id": 81653, "format": "GTFS", "is_available": True}])
    async with _client(handler) as c:
        url = await resolve(c, resolver)
    assert url == "https://transport.data.gouv.fr/resources/81653/download"


async def test_tdg_never_picks_a_replacement_silently() -> None:
    """IDFM holds three GTFS resources — a vanished id must be a human decision."""
    resolver = {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 80921}
    handler = _tdg_handler(
        [
            {"id": 80931, "format": "GTFS", "type": "other", "title": "Google rewrite"},
            {"id": 83316, "format": "GTFS", "type": "other", "title": "ITO rewrite"},
        ]
    )
    async with _client(handler) as c:
        with pytest.raises(ResolveError, match="no longer in dataset") as exc:
            await resolve(c, resolver)
    assert "80931" in str(exc.value) and "83316" in str(exc.value)


async def test_tdg_unavailable_resource_is_an_error() -> None:
    resolver = {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653}
    handler = _tdg_handler([{"id": 81653, "is_available": False}])
    async with _client(handler) as c:
        with pytest.raises(ResolveError, match="unavailable"):
            await resolve(c, resolver)


async def test_tdg_api_failure_is_a_resolve_error() -> None:
    resolver = {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653}
    async with _client(lambda r: httpx.Response(503)) as c:
        with pytest.raises(ResolveError, match="catalogue lookup failed"):
            await resolve(c, resolver)


async def test_tdg_malformed_payload_is_a_resolve_error() -> None:
    resolver = {"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653}
    async with _client(lambda r: httpx.Response(200, json={"oops": 1})) as c:
        with pytest.raises(ResolveError, match="no resource list"):
            await resolve(c, resolver)


# ─────────────────────────── udata ───────────────────────────

LU = {
    "type": "udata",
    "api": "https://data.public.lu/api/1",
    "dataset_id": "56fbd4e5855e9b6a1088f54e",
    "title_regex": r"^netex-\d{8}-\d{8}\.zip$",
}


async def test_udata_picks_newest_matching_title() -> None:
    resources = [
        {
            "title": "netex-20260618-20260823.zip",
            "created_at": "2026-06-19T04:00:00",
            "url": "https://d/old.zip",
        },
        {
            "title": "netex-20260924-20261212.zip",
            "created_at": "2026-09-25T04:47:43",
            "url": "https://d/new.zip",
        },
        {
            "title": "opendata-20260930.zip",
            "created_at": "2026-09-30T00:00:00",
            "url": "https://d/other.zip",
        },
        {
            "title": "netex2024-foo.zip",
            "created_at": "2026-09-30T00:00:00",
            "url": "https://d/legacy.zip",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1/datasets/56fbd4e5855e9b6a1088f54e/"
        return httpx.Response(200, json={"resources": resources})

    async with _client(handler) as c:
        assert await resolve(c, LU) == "https://d/new.zip"


async def test_udata_no_match_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resources": [{"title": "x.zip", "url": "https://d"}]})

    async with _client(handler) as c:
        with pytest.raises(ResolveError, match="title matching"):
            await resolve(c, LU)


async def test_udata_malformed_payload_is_an_error() -> None:
    async with _client(lambda r: httpx.Response(200, json=[1, 2])) as c:
        with pytest.raises(ResolveError, match="no resource list"):
            await resolve(c, LU)


# ─────────────────────────── permalink ───────────────────────────


async def test_permalink_substitutes_timetable_year() -> None:
    resolver = {
        "type": "permalink",
        "url": "https://data.opentransportdata.swiss/dataset/timetablenetex_{timetable_year}/permalink",
    }
    async with _client(lambda r: httpx.Response(500)) as c:  # no request needed
        url = await resolve(c, resolver, today=date(2026, 12, 20))
    assert url.endswith("/timetablenetex_2027/permalink")


# ─────────────────────────── dated ───────────────────────────

DE = {
    "type": "dated",
    "url": "https://www.opendata-oepnv.de/fileadmin/datasets/delfi/{date}_fahrplaene_gesamtdeutschland.zip",
    "max_days_back": 21,
}


async def test_dated_walks_back_to_the_latest_published_day() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["range"] == "bytes=0-3"
        if "20260928_" in request.url.path:  # Monday
            return httpx.Response(206, content=b"PK\x03\x04")
        return httpx.Response(404)

    async with _client(handler) as c:
        url = await resolve(c, DE, today=date(2026, 9, 29))
    assert "20260928_fahrplaene" in url
    assert len(seen) == 2  # today (404), yesterday (hit)


async def test_dated_skips_an_html_page_served_with_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "20260929_" in request.url.path:
            return httpx.Response(200, content=b"<!DOCTYPE html><html>")
        if "20260928_" in request.url.path:
            return httpx.Response(200, content=b"PK\x03\x04rest-of-file-ignored-range")
        return httpx.Response(404)

    async with _client(handler) as c:
        url = await resolve(c, DE, today=date(2026, 9, 29))
    assert "20260928_" in url


async def test_dated_gives_up_after_max_days_back() -> None:
    resolver = {**DE, "max_days_back": 3}
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(404)

    async with _client(handler) as c:
        with pytest.raises(ResolveError, match=r"2026-09-26\.\.2026-09-29"):
            await resolve(c, resolver, today=date(2026, 9, 29))
    assert calls == 4


# ─────────────────────────── SSRF + unknown ───────────────────────────


async def test_resolved_url_goes_through_the_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(url: str) -> str:
        if "169.254" in url:
            raise ValueError("resolves to non-public address")
        return url

    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", guard)
    resolver = {"type": "permalink", "url": "https://169.254.169.254/latest"}
    async with _client(lambda r: httpx.Response(500)) as c:
        with pytest.raises(ResolveError, match="non-public"):
            await resolve(c, resolver)


async def test_unknown_type_is_a_resolve_error() -> None:
    async with _client(lambda r: httpx.Response(500)) as c:
        with pytest.raises(ResolveError, match="unknown resolver type"):
            await resolve(c, {"type": "ftp"})
