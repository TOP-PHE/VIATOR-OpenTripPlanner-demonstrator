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
        ({"type": "dated", "url": "https://x/{date}.zip", "max_days_back": True}, "max_days_back"),
        ({"type": "dated", "url": "https://x/{date}_{foo-bar}.zip"}, "placeholders"),
        ({"type": "permalink", "url": "https://x/{ timetable_year }/p"}, "placeholders"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": True}, "resource_id"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 81653.9}, "resource_id"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": 0}, "resource_id"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": -5}, "resource_id"),
        ({"type": "tdg", "dataset_id": TDG_DATASET, "resource_id": "-5"}, "resource_id"),
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
    assert "80931" in str(exc.value)
    assert "83316" in str(exc.value)


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
        return httpx.Response(410 if calls == 2 else 404)

    today = date(2026, 9, 29)
    async with _client(handler) as c:
        with pytest.raises(ResolveError, match=r"2026-09-26\.\.2026-09-29"):
            await resolve(c, resolver, today=today)
    assert calls == 4


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_dated_stops_on_an_error_that_is_not_a_miss(status: int) -> None:
    """A 401 or 5xx says nothing about whether that day was published —
    walking past it would end in a misleading "no file found"."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    today = date(2026, 9, 29)
    async with _client(handler) as c:
        with pytest.raises(ResolveError, match=f"HTTP {status}") as exc:
            await resolve(c, DE, today=today)
    assert calls == 1
    assert "no file found" not in str(exc.value)


async def test_dated_stops_on_a_network_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    today = date(2026, 9, 29)
    async with _client(handler) as c:
        with pytest.raises(ResolveError, match="failed: boom"):
            await resolve(c, DE, today=today)
    assert calls == 1


async def test_dated_empty_200_is_a_miss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "20260929_" in request.url.path:
            return httpx.Response(200, content=b"")
        return httpx.Response(206, content=b"PK\x03\x04")

    async with _client(handler) as c:
        url = await resolve(c, DE, today=date(2026, 9, 29))
    assert "20260928_" in url


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


# ─────────────────────────── redirect guard ───────────────────────────


async def test_redirect_guard_checks_every_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    def guard(url: str) -> str:
        checked.append(url)
        if "169.254" in url:
            raise ValueError("resolves to non-public address")
        return url

    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", guard)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nap.example":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
        return httpx.Response(200, content=b"metadata")

    async with _client(handler) as c:
        async with feed_resolvers.redirect_guard(c):
            with pytest.raises(httpx.RequestError, match="non-public"):
                await c.get("https://nap.example/permalink")
        assert checked == ["https://nap.example/permalink", "http://169.254.169.254/latest"]
        # Scoped: once the NAP task is done, a plain URL provider is unaffected.
        assert c.event_hooks["request"] == []
        r = await c.get("https://nap.example/permalink")
        assert r.status_code == 200


# ─────────────────────────── json_api ───────────────────────────

ES_API = "https://nap.example.es/api/Fichero/GetList"
JSON_API = {
    "type": "json_api",
    "url": ES_API,
    "items": "conjuntos.ficheros",
    "match": {"_parent.nombre": "(?i)ouigo", "tipo": "GTFS"},
    "sort": "fechaActualizacion",
    "download": "https://nap.example.es/api/Fichero/download/{id}",
}
CATALOGUE = {
    "conjuntos": [
        {
            "nombre": "OUIGO España",
            "ficheros": [
                {"id": 11, "tipo": "GTFS", "fechaActualizacion": "2026-08-01"},
                {"id": 12, "tipo": "GTFS", "fechaActualizacion": "2026-09-15"},
                {"id": 13, "tipo": "NeTEx", "fechaActualizacion": "2026-09-20"},
            ],
        },
        {"nombre": "Iryo", "ficheros": [{"id": 21, "tipo": "GTFS", "fechaActualizacion": "x"}]},
    ]
}


def test_validate_json_api_keeps_a_clean_config() -> None:
    assert validate_resolver({**JSON_API, "type": "JSON_API"}, "r") == JSON_API


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"match": {}}, "match must be"),
        ({"match": {"a b": "x"}}, "dotted field path"),
        ({"match": {"tipo": "("}}, "not a valid regex"),
        ({"match": {"tipo": ""}}, "non-empty regex"),
        ({"match": {f"f{i}": "x" for i in range(6)}}, "at most"),
        ({"items": "a..b"}, "dotted field path"),
        ({"url_field": "url"}, "exactly one"),
        ({"download": None}, "exactly one"),
        ({"download": "https://elsewhere.example/{id}"}, "same host"),
        ({"download": "https://nap.example.es/{bad name}"}, "dotted field path"),
        ({"url": "https://nap.example.es/{year}"}, "unknown placeholders"),
        ({"sort": "-x"}, "dotted field path"),
    ],
)
def test_validate_json_api_rejects(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_resolver({**JSON_API, **patch}, "r")


def test_validate_json_api_with_url_field_and_root_items() -> None:
    raw = {"type": "json_api", "url": ES_API, "match": {"id": "^5$"}, "url_field": "file.href"}
    assert validate_resolver(raw, "r") == {**raw, "items": ""}


def _catalogue(seen: list[httpx.Request], payload: Any = CATALOGUE) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return handler


async def test_json_api_picks_the_newest_match_and_signs_the_lookup() -> None:
    seen: list[httpx.Request] = []
    signed: list[str] = []

    async def auth(url: str) -> tuple[str, dict[str, str]]:
        signed.append(url)
        return url, {"ApiKey": "k"}

    async with _client(_catalogue(seen)) as c:
        url = await resolve(c, JSON_API, auth=auth)
    assert url == "https://nap.example.es/api/Fichero/download/12"
    assert signed == [ES_API]
    assert seen[0].headers["ApiKey"] == "k"


async def test_json_api_without_sort_refuses_to_guess() -> None:
    resolver = {k: v for k, v in JSON_API.items() if k != "sort"}
    async with _client(_catalogue([])) as c:
        with pytest.raises(ResolveError, match="2 entries match") as exc:
            await resolve(c, resolver)
    assert "tipo='GTFS'" in str(exc.value)


async def test_json_api_single_match_without_sort() -> None:
    resolver = {k: v for k, v in JSON_API.items() if k != "sort"}
    resolver["match"] = {"_parent.nombre": "Iryo"}
    async with _client(_catalogue([])) as c:
        assert (await resolve(c, resolver)).endswith("/download/21")


async def test_json_api_no_match_and_no_items_are_errors() -> None:
    async with _client(_catalogue([])) as c:
        with pytest.raises(ResolveError, match="none of 4 entries"):
            await resolve(c, {**JSON_API, "match": {"tipo": "^XML$"}})
        with pytest.raises(ResolveError, match="no objects at 'nothing'"):
            await resolve(c, {**JSON_API, "items": "nothing"})


async def test_json_api_numbers_sort_numerically() -> None:
    payload = [{"id": 9, "v": 9}, {"id": 10, "v": 10}]
    resolver = {**JSON_API, "items": "", "match": {"id": "."}, "sort": "v"}
    async with _client(_catalogue([], payload)) as c:
        assert (await resolve(c, resolver)).endswith("/download/10")


async def test_json_api_download_placeholders_are_quoted_and_checked() -> None:
    payload = [{"id": "a/b", "year": 2027}]
    resolver = {
        **JSON_API,
        "items": "",
        "match": {"id": "."},
        "download": "https://nap.example.es/d/{id}/{timetable_year}",
    }
    async with _client(_catalogue([], payload)) as c:
        url = await resolve(c, resolver, today=date(2026, 12, 20))
        assert url == "https://nap.example.es/d/a%2Fb/2027"
        with pytest.raises(ResolveError, match="no usable 'missing'"):
            await resolve(c, {**resolver, "download": "https://nap.example.es/{missing}"})


async def test_json_api_url_field_must_stay_on_the_catalogue_host() -> None:
    payload = [
        {"id": 1, "href": "https://nap.example.es/f.zip"},
        {"id": 2, "href": "https://evil.example/f.zip"},
    ]
    base = {"type": "json_api", "url": ES_API, "items": "", "url_field": "href"}
    async with _client(_catalogue([], payload)) as c:
        assert await resolve(c, {**base, "match": {"id": "^1$"}}) == "https://nap.example.es/f.zip"
        with pytest.raises(ResolveError, match="not on the catalogue's host"):
            await resolve(c, {**base, "match": {"id": "^2$"}})
        with pytest.raises(ResolveError, match="no 'nope'"):
            await resolve(c, {**base, "match": {"id": "^1$"}, "url_field": "nope"})


async def test_json_api_lookup_error_hides_the_query_string() -> None:
    async with _client(lambda r: httpx.Response(401)) as c:
        with pytest.raises(ResolveError, match="HTTP 401") as exc:
            await resolve(c, {**JSON_API, "url": ES_API + "?apikey=SECRET"})
    assert str(exc.value).count("SECRET") == 1  # the configured URL only, never the signed one


async def test_json_api_lookup_is_ssrf_checked_before_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(url: str) -> str:
        raise ValueError("private")

    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", refuse)
    signed: list[str] = []

    async def auth(url: str) -> tuple[str, dict[str, str]]:
        signed.append(url)
        return url, {}

    async with _client(_catalogue([])) as c:
        with pytest.raises(ResolveError, match="private"):
            await resolve(c, JSON_API, auth=auth)
    assert signed == []
