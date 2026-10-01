"""Resolve a NAP-published feed to its *current* download URL.

Operator workflow problem we solve:

    26 of the eu19 session's timetables were downloaded by hand from
    national access points and uploaded as files, because their download
    URL is not a fixed address. Each portal names its current file its own
    way: a stable per-resource redirect (FR), a dataset API listing dated
    files (LU), a permalink that redirects to the newest file (CH), a
    filename carrying the publication date (DE).

A provider with `timetable.source = "nap"` stores a *resolver* instead of
a URL. At refresh time `resolve()` turns it into the URL to download; the
download itself then goes through the same hardened fetch as any URL
provider (`app/feed_fetch.py`: conditional GET, magic-byte + format check,
unchanged-content skip).

Resolver types (probed live against each portal 2026-09-29):

    tdg        transport.data.gouv.fr — {dataset_id, resource_id}. The
               `/resources/<id>/download` URL is stable across the
               publisher's file rotations; the dataset API confirms the
               resource still exists and is available.
    udata      udata dataset API (data.public.lu) — {api, dataset_id,
               title_regex}. Newest resource whose title matches.
    permalink  a URL that redirects to the newest file — {url}; may carry
               `{timetable_year}` (CH `timetablenetex_<YYYY>/permalink`).
    dated      a URL carrying the publication date — {url, max_days_back};
               `{date}` becomes YYYYMMDD, walked back day by day until the
               server answers with a zip (DE DELFI, published Mondays).
               Only a 404/410 or a non-zip 200 steps back a day; any other
               error stops the walk and is reported as-is.
    json_api   any JSON catalogue API — {url, items, match, sort,
               download | url_field}. Picks one entry of a JSON listing and
               builds its file URL. The only type whose lookup is sent with
               the provider's credential: it exists for portals that need an
               account (AT mobilitaetsverbuende login, ES NAP API key).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .master.nap_importer import _validate_safe_http_url

RESOLVER_TYPES: frozenset[str] = frozenset({"tdg", "udata", "permalink", "dated", "json_api"})

# Signs a request for an authenticated portal: URL in, (URL, extra headers)
# out. Raises ResolveError when the credential is unusable.
Authorizer = Callable[[str], Awaitable[tuple[str, dict[str, str]]]]

TDG_API = "https://transport.data.gouv.fr/api/datasets"
TDG_DOWNLOAD = "https://transport.data.gouv.fr/resources/{resource_id}/download"

_HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
_DATED_DEFAULT_DAYS_BACK = 21
_DATED_MAX_DAYS_BACK = 60
_ZIP_MAGIC = b"PK\x03\x04"


class ResolveError(Exception):
    """The resolver could not name a current file. The refresh task is
    skipped with this message; the provider's previous file stays in place."""


# ──────────────────────────── validation ────────────────────────────


def _require_https(value: object, field: str) -> str:
    s = str(value or "").strip()
    parsed = urlparse(s)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field}={value!r} must be an https URL")
    return s


_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def _check_placeholders(url: str, allowed: set[str], field: str) -> None:
    # `[^{}]*`, not `\w*`: `{foo-bar}` or `{ date }` must be reported, not
    # passed through to the server as literal braces.
    unknown = set(_PLACEHOLDER_RE.findall(url)) - allowed
    if unknown:
        raise ValueError(f"{field} has unknown placeholders {sorted(unknown)}")


def _validate_tdg(raw: dict[str, Any], where: str) -> dict[str, Any]:
    value = raw.get("resource_id")
    # bool is an int subclass and float("81653.9") would truncate — accept
    # only a real int or a string of digits, and only a positive one.
    if isinstance(value, str) and value.strip().isdigit():
        resource_id: int | None = int(value.strip())
    elif isinstance(value, int) and not isinstance(value, bool):
        resource_id = value
    else:
        resource_id = None
    if resource_id is None or resource_id <= 0:
        raise ValueError(
            f"{where}.resource_id={value!r} must be the integer "
            "transport.data.gouv.fr resource id (e.g. 81653)"
        )
    dataset_id = str(raw.get("dataset_id") or "").strip().lower()
    if not _HEX24_RE.match(dataset_id):
        raise ValueError(
            f"{where}.dataset_id={raw.get('dataset_id')!r} must be the 24-hex "
            "transport.data.gouv.fr dataset id"
        )
    return {"type": "tdg", "dataset_id": dataset_id, "resource_id": resource_id}


def _validate_udata(raw: dict[str, Any], where: str) -> dict[str, Any]:
    api = _require_https(raw.get("api"), f"{where}.api").rstrip("/")
    dataset_id = str(raw.get("dataset_id") or "").strip()
    if not dataset_id or "/" in dataset_id:
        raise ValueError(f"{where}.dataset_id={raw.get('dataset_id')!r} must be a dataset id")
    title_regex = str(raw.get("title_regex") or "").strip()
    try:
        re.compile(title_regex)
    except re.error as exc:
        raise ValueError(f"{where}.title_regex is not a valid regex: {exc}") from exc
    if not title_regex:
        raise ValueError(f"{where}.title_regex is required (selects the resource family)")
    return {"type": "udata", "api": api, "dataset_id": dataset_id, "title_regex": title_regex}


def _validate_permalink(raw: dict[str, Any], where: str) -> dict[str, Any]:
    url = _require_https(raw.get("url"), f"{where}.url")
    _check_placeholders(url, {"timetable_year"}, f"{where}.url")
    return {"type": "permalink", "url": url}


def _validate_dated(raw: dict[str, Any], where: str) -> dict[str, Any]:
    url = _require_https(raw.get("url"), f"{where}.url")
    if "{date}" not in url:
        raise ValueError(f"{where}.url must contain a {{date}} placeholder (YYYYMMDD)")
    _check_placeholders(url, {"date"}, f"{where}.url")
    days_raw = raw.get("max_days_back", _DATED_DEFAULT_DAYS_BACK)
    if (
        not isinstance(days_raw, int)
        or isinstance(days_raw, bool)
        or not 1 <= days_raw <= _DATED_MAX_DAYS_BACK
    ):
        raise ValueError(f"{where}.max_days_back must be an integer 1..{_DATED_MAX_DAYS_BACK}")
    return {"type": "dated", "url": url, "max_days_back": days_raw}


_FIELD_PATH_RE = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)*$")
_JSON_API_MAX_MATCH = 5


def _field_path(value: object, field: str) -> str:
    s = str(value or "").strip()
    if not _FIELD_PATH_RE.match(s):
        raise ValueError(f"{field}={value!r} must be a dotted field path like 'data.files'")
    return s


def _validate_json_api_match(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where}.match must be an object of field path -> regex")
    if len(raw) > _JSON_API_MAX_MATCH:
        raise ValueError(f"{where}.match takes at most {_JSON_API_MAX_MATCH} fields")
    match: dict[str, str] = {}
    for key, pattern in raw.items():
        path = _field_path(key, f"{where}.match key")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{where}.match[{path!r}] must be a non-empty regex")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{where}.match[{path!r}] is not a valid regex: {exc}") from exc
        match[path] = pattern
    return match


def _validate_json_api(raw: dict[str, Any], where: str) -> dict[str, Any]:
    url = _require_https(raw.get("url"), f"{where}.url")
    _check_placeholders(url, {"timetable_year"}, f"{where}.url")
    items = str(raw.get("items") or "").strip()
    out: dict[str, Any] = {
        "type": "json_api",
        "url": url,
        "items": _field_path(items, f"{where}.items") if items else "",
        "match": _validate_json_api_match(raw.get("match"), where),
    }
    if raw.get("sort"):
        out["sort"] = _field_path(raw["sort"], f"{where}.sort")
    download, url_field = raw.get("download"), raw.get("url_field")
    if bool(download) == bool(url_field):
        raise ValueError(f"{where} needs exactly one of 'download' (URL template) or 'url_field'")
    if url_field:
        out["url_field"] = _field_path(url_field, f"{where}.url_field")
        return out
    template = _require_https(download, f"{where}.download")
    for name in _PLACEHOLDER_RE.findall(template):
        if name != "timetable_year":
            _field_path(name, f"{where}.download placeholder")
    # The lookup carries the provider's credential and so will the download:
    # keep both on the catalogue's own host.
    if urlparse(template).hostname != urlparse(url).hostname:
        raise ValueError(f"{where}.download must be on the same host as {where}.url")
    out["download"] = template
    return out


_VALIDATORS = {
    "tdg": _validate_tdg,
    "udata": _validate_udata,
    "permalink": _validate_permalink,
    "dated": _validate_dated,
    "json_api": _validate_json_api,
}


def validate_resolver(raw: object, where: str) -> dict[str, Any]:
    """Validate a `timetable.resolver` object, returning the cleaned dict.

    `where` prefixes error messages (e.g. `providers[3].timetable.resolver`).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object with a 'type'")
    rtype = str(raw.get("type") or "").strip().lower()
    validator = _VALIDATORS.get(rtype)
    if validator is None:
        raise ValueError(
            f"{where}.type={raw.get('type')!r} must be one of {sorted(RESOLVER_TYPES)}"
        )
    return validator(raw, where)


def describe(resolver: dict[str, Any]) -> str:
    """A human-facing URL for the resolver — what the refresh response and
    audit trail show before (or instead of) the resolved file URL."""
    rtype = resolver.get("type")
    if rtype == "tdg":
        return TDG_DOWNLOAD.format(resource_id=resolver["resource_id"])
    if rtype == "udata":
        return f"{resolver['api']}/datasets/{resolver['dataset_id']}/"
    return str(resolver.get("url") or "")


# ──────────────────────────── helpers ────────────────────────────


def timetable_year(d: date) -> int:
    """The European timetable year in force on `d`.

    The timetable changes in the night from the second Saturday to the
    following Sunday of December; from that Sunday on, the *next* year's
    timetable applies (2026-12-13 is the first day of timetable year 2027).
    """
    dec1 = date(d.year, 12, 1)
    first_saturday = dec1 + timedelta(days=(5 - dec1.weekday()) % 7)
    change_day = first_saturday + timedelta(days=8)
    return d.year + 1 if d >= change_day else d.year


async def _get_json(client: httpx.AsyncClient, url: str, auth: Authorizer | None = None) -> Any:
    # SSRF check before signing: a credential is never minted for, or sent
    # to, a refused address.
    try:
        safe_url = _validate_safe_http_url(url)
    except ValueError as exc:
        raise ResolveError(f"catalogue lookup failed ({url}): {exc}") from exc
    fetch_url, headers = await auth(safe_url) if auth else (safe_url, {})
    try:
        r = await client.get(fetch_url, headers={"Accept": "application/json", **headers})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        # `str(exc)` would print the request URL, which may carry a
        # query-string key.
        raise ResolveError(
            f"catalogue lookup failed ({url}): HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ResolveError(f"catalogue lookup failed ({url}): {type(exc).__name__}") from exc


# ──────────────────────────── resolvers ────────────────────────────


async def _resolve_tdg(client: httpx.AsyncClient, r: dict[str, Any]) -> str:
    data = await _get_json(client, f"{TDG_API}/{r['dataset_id']}")
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        raise ResolveError(f"dataset {r['dataset_id']} returned no resource list")
    match = next((res for res in resources if res.get("id") == r["resource_id"]), None)
    if match is None:
        # Never pick a replacement silently — datasets often hold several
        # GTFS resources (IDFM: the publisher's own + third-party rewrites).
        candidates = ", ".join(
            f"{res.get('id')} {res.get('format')}/{res.get('type')} {res.get('title')!r}"
            for res in resources
            if isinstance(res, dict)
        )
        raise ResolveError(
            f"resource {r['resource_id']} is no longer in dataset {r['dataset_id']}; "
            f"pick one of: {candidates or '(none)'}"
        )
    if match.get("is_available") is False:
        raise ResolveError(f"resource {r['resource_id']} is marked unavailable on the NAP")
    return TDG_DOWNLOAD.format(resource_id=r["resource_id"])


async def _resolve_udata(client: httpx.AsyncClient, r: dict[str, Any]) -> str:
    data = await _get_json(client, f"{r['api']}/datasets/{r['dataset_id']}/")
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        raise ResolveError(f"dataset {r['dataset_id']} returned no resource list")
    pattern = re.compile(r["title_regex"])
    matching = [
        res
        for res in resources
        if isinstance(res, dict) and res.get("url") and pattern.search(str(res.get("title") or ""))
    ]
    if not matching:
        raise ResolveError(
            f"no resource in dataset {r['dataset_id']} has a title matching {r['title_regex']!r}"
        )
    newest = max(matching, key=lambda res: str(res.get("created_at") or res.get("last_modified")))
    return str(newest["url"])


# Answers that mean "nothing published that day" — keep walking back. Any
# other failure (401/403, 5xx, network) says nothing about the date, and
# stepping past it would report a misleading "no file found".
_DATED_MISS_STATUSES = frozenset({404, 410})


async def _probe_dated(client: httpx.AsyncClient, url: str) -> bool:
    """True if `url` serves a zip. False for a miss (404/410, or a 200/206
    that isn't a zip). Raises ResolveError for anything else."""
    try:
        # Ranged GET, not HEAD: several portals answer HEAD wrongly. Some
        # ignore Range and stream the whole file — stop after one chunk.
        async with client.stream("GET", url, headers={"Range": "bytes=0-3"}) as resp:
            if resp.status_code in _DATED_MISS_STATUSES:
                return False
            if resp.status_code not in (200, 206):
                raise ResolveError(f"HTTP {resp.status_code} probing {url}")
            async for chunk in resp.aiter_bytes(4):
                return chunk[:4] == _ZIP_MAGIC
            return False
    except httpx.HTTPError as exc:
        raise ResolveError(f"probing {url} failed: {exc}") from exc


async def _resolve_dated(client: httpx.AsyncClient, r: dict[str, Any], today: date) -> str:
    tried: list[str] = []
    for back in range(r["max_days_back"] + 1):
        day = today - timedelta(days=back)
        url = str(r["url"]).replace("{date}", day.strftime("%Y%m%d"))
        tried.append(day.isoformat())
        if await _probe_dated(client, url):
            return url
    raise ResolveError(f"no file found for any date {tried[-1]}..{tried[0]} at {r['url']}")


def _field(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _walk_items(node: Any, parts: list[str], parent: Any) -> Iterator[dict[str, Any]]:
    """Every object at `parts` below `node`, lists flattened at any level.
    Each comes back with `_parent`: the object holding the list it was in,
    so a match can test a file's dataset (`_parent.name`)."""
    if isinstance(node, list):
        for element in node:
            yield from _walk_items(element, parts, parent)
    elif isinstance(node, dict):
        if not parts:
            yield {**node, "_parent": parent}
        else:
            yield from _walk_items(node.get(parts[0]), parts[1:], {**node, "_parent": parent})


def _sort_key(value: Any) -> tuple[int, float, str]:
    # Numbers order numerically (version 10 after 9), anything else as text
    # (ISO dates order correctly as text).
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (1, float(value), "")
    return (0, 0.0, "" if value is None else str(value))


def _describe_entry(entry: dict[str, Any], r: dict[str, Any]) -> str:
    fields = [*r["match"], r.get("sort")]
    return "{" + ", ".join(f"{f}={_field(entry, f)!r}" for f in fields if f) + "}"


def _pick_json_api_entry(data: Any, r: dict[str, Any]) -> dict[str, Any]:
    parts = r["items"].split(".") if r["items"] else []
    entries = list(_walk_items(data, parts, None))
    if not entries:
        raise ResolveError(f"no objects at {r['items'] or '(root)'!r} in {r['url']}")
    patterns = {path: re.compile(rx) for path, rx in r["match"].items()}
    matching = [
        e
        for e in entries
        if all(
            _field(e, path) is not None and rx.search(str(_field(e, path)))
            for path, rx in patterns.items()
        )
    ]
    if not matching:
        raise ResolveError(f"none of {len(entries)} entries matches {r['match']}")
    if "sort" in r:
        return max(matching, key=lambda e: _sort_key(_field(e, r["sort"])))
    if len(matching) > 1:
        # Never guess between several files — same rule as tdg.
        shown = "; ".join(_describe_entry(e, r) for e in matching[:5])
        raise ResolveError(
            f"{len(matching)} entries match {r['match']} — narrow the match or add 'sort': {shown}"
        )
    return matching[0]


def _json_api_file_url(entry: dict[str, Any], r: dict[str, Any], today: date) -> str:
    if "url_field" in r:
        value = _field(entry, r["url_field"])
        if not isinstance(value, str) or not value:
            raise ResolveError(f"matched entry has no {r['url_field']!r}")
        # The download is sent with the provider's credential — never to a
        # host other than the catalogue the operator configured.
        if urlparse(value).hostname != urlparse(r["url"]).hostname:
            raise ResolveError(f"file URL {value} is not on the catalogue's host")
        return value

    def _fill(m: re.Match[str]) -> str:
        name = m.group(1)
        value = timetable_year(today) if name == "timetable_year" else _field(entry, name)
        if value is None or isinstance(value, dict | list):
            raise ResolveError(f"matched entry has no usable {name!r} for the download URL")
        return quote(str(value), safe="")

    return _PLACEHOLDER_RE.sub(_fill, r["download"])


async def _resolve_json_api(
    client: httpx.AsyncClient, r: dict[str, Any], today: date, auth: Authorizer | None
) -> str:
    url = str(r["url"]).replace("{timetable_year}", str(timetable_year(today)))
    entry = _pick_json_api_entry(await _get_json(client, url, auth), r)
    return _json_api_file_url(entry, r, today)


@asynccontextmanager
async def redirect_guard(client: httpx.AsyncClient) -> AsyncIterator[None]:
    """While active, every request `client` sends — each redirect hop
    included — must pass the SSRF guard. `resolve()` only checks the URL it
    returns; a public URL can still 302 to 169.254.169.254. Scoped to one
    NAP task so plain URL providers keep their behaviour. The refresh loop
    runs tasks sequentially, so the hook never leaks onto another task."""

    async def _check(request: httpx.Request) -> None:
        url = str(request.url)
        try:
            await asyncio.to_thread(_validate_safe_http_url, url)
        except ValueError as exc:
            raise httpx.RequestError(f"blocked request to {url}: {exc}", request=request) from exc

    hooks = client.event_hooks
    hooks["request"] = [*hooks.get("request", []), _check]
    client.event_hooks = hooks
    try:
        yield
    finally:
        hooks = client.event_hooks
        hooks["request"] = [h for h in hooks.get("request", []) if h is not _check]
        client.event_hooks = hooks


async def resolve(
    client: httpx.AsyncClient,
    resolver: dict[str, Any],
    *,
    today: date | None = None,
    auth: Authorizer | None = None,
) -> str:
    """Return the URL of the resolver's current file, or raise ResolveError.

    `auth` signs the catalogue lookup of a `json_api` resolver. The other
    types query public catalogues and never see the credential."""
    today = today or date.today()
    rtype = resolver.get("type")
    if rtype == "tdg":
        url = await _resolve_tdg(client, resolver)
    elif rtype == "udata":
        url = await _resolve_udata(client, resolver)
    elif rtype == "permalink":
        url = str(resolver["url"]).replace("{timetable_year}", str(timetable_year(today)))
    elif rtype == "dated":
        url = await _resolve_dated(client, resolver, today)
    elif rtype == "json_api":
        url = await _resolve_json_api(client, resolver, today, auth)
    else:
        raise ResolveError(f"unknown resolver type {rtype!r}")
    # The URL may come from a third-party catalogue's JSON — same SSRF
    # defence as the NAP catalogue importer before we download from it.
    try:
        return _validate_safe_http_url(url)
    except ValueError as exc:
        raise ResolveError(str(exc)) from exc
