"""The stop extract of one timetable feed, for the station module (MSMM).

The station module builds its VIATOR station referential from the stops of
the timetables VIATOR already downloads (MSMM `docs/service-design.md`,
section 22). VIATOR never sends an archive: at refresh time it writes, per
feed version, one small file of the rail stations of that feed, and a later
step (MSMM pull request 5 on the VIATOR side) sends it to the module.

This module makes that file. Its public interface is small and stable:

    EXTRACTOR_VERSION
    extractor_version_for(provider_cfg) -> str
    feed_key_for(provider_cfg, *, upload_sha256=None) -> str | None
    ensure_extract(provider_cfg, archive_path, archive_sha256, *,
                   extracted_at, session_id, cache_dir=None,
                   wait_seconds=None, cache_only=False) -> ExtractResult
    ExtractResult(path_gz, feed_key, feed_version, extractor_version, rows,
                  bytes, body_sha256, dropped)
    ExtractError (with a `reason` word; the only exception it raises)
    provider_archive_snapshot(session_id, provider_cfg, *, upload_sha256=None)
    extract_known(provider_cfg, archive_sha256, *, session_id) -> bool
    warm_session_extracts(session_id, config, *, only_provider=None,
                          upload_sha256=None)
    queue_warm(session_id, config, *, only_provider=None,
               upload_sha256=None) -> bool    (the web process: one warm thread)

**Every caller uses `ensure_extract`** (the web process after a refresh, the
worker before a build): it returns a valid cached extract at once, and
otherwise makes it in a child process with a memory, processor-time and
wall-clock limit, one at a time on the machine, so that a hostile or huge
archive can only end that child (`memory_limit`, `cpu_limit`,
`wall_clock_limit`: which limit it passed), never the caller. A
failure that the same archive would meet again (`REMEMBERED_REASONS`) is
remembered beside the cache, so that a national archive that cannot be
extracted is not read again by the next caller (the send pass after the
warm thread, the next refresh): its reason is given at once, until the
archive, the provider's format, the extractor, one of the bounds or the
readers' code changes.
`build_extract` is the extraction itself, in the calling process: the child
runs it, and the tests; nothing else may call it.

The form of the file is the contract of MSMM 22.2, point 6, and MSMM's
`app/master/feed_intake.py::check_extract` is its reference: one bad item
refuses the whole extract there, so everything is cleaned or dropped here,
before it is written, and an extract that would pass a limit is not written
at all (`ExtractError`).
"""

from __future__ import annotations

import contextlib
import csv
import ctypes
import fcntl
import functools
import gzip
import hashlib
import io
import json
import logging
import math
import multiprocessing
import os
import queue
import re
import resource
import signal
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import (
    feed_fetch,
    ingestion,
    osm_geo,
    provider_catalogue,
    station_extract_common,
    station_extract_gtfs,
    station_extract_netex,
    station_extract_skip,
)
from .feed_resolvers import validate_resolver
from .settings import settings
from .station_extract_common import CENSUS_REASONS, SUMMARY, ArchiveError, Census, Station
from .station_extract_gtfs import GtfsCensus, provider_operator, read_gtfs
from .station_extract_netex import NetexCensus, read_netex

log = logging.getLogger(__name__)

# The version of the rules below. MSMM keeps one version of an archive per
# extractor version (MSMM 22.3, point 1): raise it whenever the rows written
# for the same archive would change, so the module receives the new extract.
# 2 (16 October): a NeTEx `TransportMode` that gives no information on the
# mode (`unknown`, `all`, `anyMode`, `urbanRail`) is read as none,
# `intercityRail` as rail (`station_extract_netex._mode`).
# 18 October, not raised for #371 (a NeTEx StopPlaceRef that names a Quay is
# read through the Quay's StopPlace): the rows of every other archive are
# unchanged and a remembered failure is retried on the code signature; only
# an archive mixing StopPlaceRef→Quay with other assignments that already
# had an extract keeps it until its next file.
# 3 (22 October): the country of a stop's position (`iso2_point`) is any
# bundled country, no longer the v1 list only (Serbia, North Macedonia,
# Montenegro and the other European countries, Turkey), and a stop on a coast,
# an estuary or an island the 1:50m boundaries leave out takes the country of
# the nearest boundary within 10 km (`osm_geo.country_of_position`). Such
# stops were in no country, so the station module could neither match nor
# mint them: each was a review item for an administrator.
EXTRACTOR_VERSION = "viator-stop-extract-3"

# Rail by default (a provider's `timetable.default_mode: "rail"`, the one
# value `ingestion` accepts): the readers take a journey (a GTFS route) that
# names no mode, at a stop that names none either, as rail
# (`station_extract_netex`, `station_extract_gtfs`). The same archive then
# gives other rows, so such an extract carries its own extractor version,
# `EXTRACTOR_VERSION` and this suffix: a version of its own in MSMM (whose
# identity is the feed, the archive and the extractor version, 22.3 point
# 1), its own cached file and its own remembered failure, so that a feed
# remembered `no_rail_stop` is read again once the box is ticked.
DEFAULT_MODE_RAIL = "rail"
RAIL_BY_DEFAULT_SUFFIX = "+rail-default"

# The feed keys MSMM accepts (MSMM `feed_intake.FEED_KEY_PATTERN`).
FEED_KEY_PATTERN = (
    r"(?:nap:tdg:[1-9][0-9]{0,17}"
    r"|nap:(?:udata|permalink|dated|json_api):[0-9a-f]{16}"
    r"|url:[0-9a-f]{16}|upload:[0-9a-f]{64})"
)
FEED_KEY = re.compile(FEED_KEY_PATTERN, re.ASCII)

# The fields of each resolver's configuration that say which file it
# fetches (MSMM 22.2, point 6; `feed_intake.RESOLVER_IDENTITY_FIELDS`), as
# `feed_resolvers.validate_resolver` returns them. `max_days_back` of
# `dated` says how far back to look, not which file: left out.
RESOLVER_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "udata": ("api", "dataset_id", "title_regex"),
    "permalink": ("url",),
    "dated": ("url",),
    "json_api": ("url", "items", "match", "sort", "download", "url_field"),
}

_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class ExtractError(Exception):
    """No extract was written. `reason` is a short word for the log and the
    admin status (never a stop, a code or a name): `no_feed_key`,
    `bad_sha256`, `bad_provider`, `bad_time`, `unsupported_format`,
    `bad_archive`, `input_too_large`, `no_stop_place`, `no_rail_stop`,
    `too_large`, `too_many_rows`, `line_too_long`, `cell_too_long`,
    `memory_limit`, `cpu_limit`, `wall_clock_limit` (the child process
    passed its memory, processor-time or wall-clock limit), `stopped` (the
    child was ended from outside by a signal), `crashed` (the child was
    killed by another signal, its own fault), `busy` (another extraction
    held the machine for longer than the wait), `not_cached` (`ensure_extract(cache_only=True)` found no
    valid cached extract), `cache` (the cache folder cannot be written), `error`
    (anything else).

    For `no_rail_stop` and `no_stop_place` (`CENSUS_REASONS`), what the
    reader saw: `summary` in at most 100 characters (the admin status, the
    remembered failure; also when remembered) and `census`, every count
    (the web log; only when the archive was read). Counts and enumerated
    words only (`station_extract_common.Census`)."""

    def __init__(
        self, reason: str, detail: str = "", *, summary: str = "", census: str = ""
    ) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.summary = summary
        self.census = census


@dataclass(frozen=True)
class ExtractResult:
    """One written extract.

    `path_gz` the gzip file; `feed_key` and `feed_version` (the sha256 of the
    downloaded archive) as in its rows; `rows` stops; `bytes` and
    `body_sha256` of the gzip file itself, the body MSMM receives."""

    path_gz: Path
    feed_key: str
    feed_version: str
    extractor_version: str
    rows: int
    bytes: int
    body_sha256: str
    # What was cleaned or dropped while writing, by reason word; empty when
    # the extract came from the cache.
    dropped: Mapping[str, int] = field(default_factory=dict)
    # What rail by default made rail ("rail by default: N calls at M stop
    # places") and the NeTEx StopPlaceRefs read through their Quay ("stop
    # places read through a quay: N"), '' when neither; kept beside the cached
    # file (`_COUNTS_SUFFIX`), so a cached extract says it too.
    summary: str = ""


# ── The feed key (MSMM 22.2, point 6) ──────────────────────────────────────


def default_rail(provider_cfg: Mapping[str, Any]) -> bool:
    """Whether the provider's timetable is rail by default."""
    timetable = provider_cfg.get("timetable")
    return isinstance(timetable, Mapping) and timetable.get("default_mode") == DEFAULT_MODE_RAIL


def extractor_version_for(provider_cfg: Mapping[str, Any], base: str | None = None) -> str:
    """The extractor version of the provider's extracts: `base` (by default
    `EXTRACTOR_VERSION`), with `RAIL_BY_DEFAULT_SUFFIX` when the provider is
    rail by default."""
    version = EXTRACTOR_VERSION if base is None else base
    return version + RAIL_BY_DEFAULT_SUFFIX if default_rail(provider_cfg) else version


def _sixteen_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolver_identity(resolver: Mapping[str, Any]) -> str:
    """The key of a NAP provider from its validated resolver: `nap:tdg:<id>`,
    else `nap:<type>:` and 16 hex of the sha256 of the canonical JSON (keys
    sorted, no space, UTF-8) of the resolver's own identity fields."""
    kind = str(resolver["type"])
    if kind == "tdg":
        return f"nap:tdg:{int(str(resolver['resource_id']))}"
    fields = {name: resolver[name] for name in RESOLVER_IDENTITY_FIELDS[kind] if name in resolver}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"nap:{kind}:{_sixteen_hex(canonical)}"


def url_identity(url: str) -> str:
    """`url:` and 16 hex of the sha256 of the host in small letters, the path
    as given and, when there is one, `?` and the query with its pairs sorted
    and written again. No scheme, no port, no fragment."""
    parts = urlsplit(url)
    pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
    query = f"?{urlencode(pairs)}" if pairs else ""
    return f"url:{_sixteen_hex((parts.hostname or '') + parts.path + query)}"


def feed_key_for(
    provider_cfg: Mapping[str, Any], *, upload_sha256: str | None = None
) -> str | None:
    """The MSMM feed key of a session provider, derived from its configuration
    only, never from what a fetch returned.

    `nap` -> the resolver's identity; `url` -> `url_identity`; `upload` ->
    `upload:<sha256 of the uploaded file>` (None until `upload_sha256` is
    given); `cross_border_filter` -> None (a subset of another session's
    national feed, which reaches the module from that session). None too for
    a provider whose configuration cannot give a key (no URL, a resolver
    that does not validate): such a provider sends no extract.
    """
    timetable = provider_cfg.get("timetable")
    if not isinstance(timetable, Mapping):
        return None
    key = _key_of_source(timetable, upload_sha256)
    if key is None or FEED_KEY.fullmatch(key) is None:
        return None
    return key


def _key_of_source(timetable: Mapping[str, Any], upload_sha256: str | None) -> str | None:
    """The key by the timetable's source, before its form is checked."""
    source = str(timetable.get("source") or ("url" if timetable.get("url") else "upload"))
    if source == "nap":
        try:
            return resolver_identity(validate_resolver(timetable.get("resolver"), "resolver"))
        except ValueError:
            return None
    if source == "url":
        url = str(timetable.get("url") or "").strip()
        return url_identity(url) if url else None
    if source == "upload" and upload_sha256 is not None and _SHA256.fullmatch(upload_sha256):
        return f"upload:{upload_sha256}"
    return None


# ── The form of the file (MSMM 22.2, point 6) ─────────────────────────────

FORMAT_VERSION = "1"
COLUMNS = (
    "format_version",
    "feed_key",
    "feed_version",
    "extracted_at",
    "extractor_version",
    "stop_key",
    "stop_name",
    "lat",
    "lon",
    "iso2_point",
    "modes",
    "n_calls",
    "operators",
    "codes",
    "provider_id",
    "provider_label",
    "provider_country_iso",
    "provider_provenance",
)
# MSMM's limits (`feed_intake.py`): past any of them MSMM refuses the whole
# extract, so none of them is ever passed here. They bound the file written;
# what is read is bounded by the readers (`station_extract_common`) and the
# child process's limits (below).
MEGABYTE = 1024 * 1024
TEXT_MAX = 200
ITEMS_MAX = 500
LINE_MAX = MEGABYTE
FIELD_MAX = 131_072
MAX_ROWS = 200_000
MAX_CALLS = 1_000_000_000
EXTRACT_MAX_BYTES = 32 * MEGABYTE
UNPACKED_MAX_BYTES = 256 * MEGABYTE
PROVENANCES = frozenset({"nap", "official", "community"})

_CODE_FIELD = re.compile(r"[A-Za-z@][A-Za-z0-9_.:@/-]{0,63}", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ISO2 = re.compile(r"[A-Z]{2}", re.ASCII)
_PROVIDER_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,15}", re.ASCII)


def _clean_label(value: str, dropped: Counter[str]) -> str:
    """A name or label as MSMM takes it: control characters as spaces, runs
    of spaces as one, at most `TEXT_MAX` characters (cut, and counted)."""
    text = " ".join(_CONTROL.sub(" ", value).split())
    if len(text) > TEXT_MAX:
        dropped["label_cut"] += 1
        text = text[:TEXT_MAX].rstrip()
    return text


def _clean_item(item: str, dropped: Counter[str], kind: str) -> str | None:
    """An item of a list column, or None (counted) when MSMM would refuse it:
    a `|` (there is no escape), a control character, more than `TEXT_MAX`
    characters. Never cut: a cut code would be another code."""
    if "|" in item:
        dropped[f"{kind}_bar"] += 1
        return None
    if _CONTROL.search(item):
        dropped[f"{kind}_control"] += 1
        return None
    if len(item) > TEXT_MAX:
        dropped[f"{kind}_long"] += 1
        return None
    return item


def _code_item(field: str, value: str, dropped: Counter[str]) -> str | None:
    """`field=value`, or None (counted) when the field is not of MSMM's form
    (a `keyList` key with a space, a `PrivateCode` type beyond ASCII)."""
    if _CODE_FIELD.fullmatch(field) is None:
        dropped["code_field"] += 1
        return None
    return _clean_item(f"{field}={value}", dropped, "code")


def _capped(items: list[str], dropped: Counter[str], kind: str) -> list[str]:
    """At most `ITEMS_MAX` distinct items, in order; the rest counted."""
    unique = list(dict.fromkeys(items))
    if len(unique) < len(items):
        dropped["duplicate_item"] += len(items) - len(unique)
    if len(unique) > ITEMS_MAX:
        dropped[f"{kind}_over_cap"] += len(unique) - ITEMS_MAX
        unique = unique[:ITEMS_MAX]
    return unique


def _decimal(value: float) -> str:
    """MSMM 22.2, point 6: `format(value, ".7f")`, never an exponent."""
    return format(value, ".7f")


@dataclass(frozen=True)
class _Meta:
    """The columns that are the same on every row."""

    feed_key: str
    feed_version: str
    extracted_at: str
    provider_id: str
    provider_label: str
    provider_country_iso: str
    provider_provenance: str
    extractor_version: str
    default_rail: bool


# The reasons that drop a whole row (`_station_row`), for the census.
ROW_DROPS = ("stop_key", "no_name", "no_position", "no_rail_call", "no_code")


def _station_row(station: Station, meta: _Meta, dropped: Counter[str]) -> list[str] | None:
    """The row of one station, or None (counted) when MSMM could not take it."""
    key = station.key
    if not key or len(key) > TEXT_MAX or _CONTROL.search(key):
        dropped["stop_key"] += 1
        return None
    name = _clean_label(station.name, dropped)
    if not name:
        dropped["no_name"] += 1
        return None
    lat, lon = station.lat, station.lon
    if (
        lat is None
        or lon is None
        or not (math.isfinite(lat) and math.isfinite(lon))
        or abs(lat) > 90
        or abs(lon) > 180
    ):
        dropped["no_position"] += 1
        return None
    modes = sorted(mode for mode, count in station.calls.items() if count > 0)
    calls = sum(station.calls.values())
    operators = _capped(
        sorted(
            item
            for raw in station.operators
            if (item := _clean_item(_clean_label(raw, dropped), dropped, "operator"))
        ),
        dropped,
        "operators",
    )
    if not operators:
        # MSMM needs one operator: a feed that names none (no agency, journeys
        # without an operator or authority) is the provider's own, marked so
        # that no agency name can be taken for it.
        dropped["operator_from_provider"] += 1
        operators = [provider_operator(meta.provider_id)]
    if station.codes_over_cap:
        dropped["codes_over_cap"] += station.codes_over_cap
    codes = _capped(
        [
            item
            for field, value in station.code_pairs()
            if (item := _code_item(field, value, dropped))
        ],
        dropped,
        "codes",
    )
    for empty, reason in (
        (not modes or calls < 1, "no_rail_call"),
        (not codes, "no_code"),
    ):
        if empty:
            dropped[reason] += 1
            return None
    iso = osm_geo.country_of_position(lat, lon) or ""
    return [
        FORMAT_VERSION,
        meta.feed_key,
        meta.feed_version,
        meta.extracted_at,
        meta.extractor_version,
        key,
        name,
        _decimal(lat),
        _decimal(lon),
        iso if _ISO2.fullmatch(iso) else "",
        "|".join(modes),
        str(min(calls, MAX_CALLS)),
        "|".join(operators),
        "|".join(codes),
        meta.provider_id,
        meta.provider_label,
        meta.provider_country_iso,
        meta.provider_provenance,
    ]


def _csv_line(record: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(record)
    return buffer.getvalue()


def _write(stations: dict[str, Station], meta: _Meta, target: Path, dropped: Counter[str]) -> int:
    """Write the extract to `target` (gzip) and return its number of rows;
    raise `ExtractError` before passing any of MSMM's limits."""
    rows = 0
    unpacked = 0
    records = (
        row
        for key in sorted(stations)
        if (row := _station_row(stations[key], meta, dropped)) is not None
    )
    # mtime 0 and no file name: the same rows give the same bytes.
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed:
            for record in _with_header(records):
                rows += 1
                unpacked += _write_record(packed, record, rows, unpacked)
        raw.flush()
        os.fsync(raw.fileno())
    if rows <= 1:
        raise ExtractError("no_rail_stop", "no station-level stop with a rail call")
    if target.stat().st_size > EXTRACT_MAX_BYTES:
        raise ExtractError("too_large", "the gzip file is larger than MSMM takes")
    return rows - 1


def _with_header(records: Iterator[list[str]]) -> Iterator[list[str]]:
    yield list(COLUMNS)
    yield from records


def _write_record(packed: gzip.GzipFile, record: list[str], rows: int, unpacked: int) -> int:
    """Write one record (the `rows`-th line) and return its size in bytes;
    refuse before passing one of MSMM's limits."""
    if any(len(cell) > FIELD_MAX for cell in record):
        raise ExtractError("cell_too_long", "a cell is longer than MSMM reads")
    line = _csv_line(record)
    if len(line) > LINE_MAX:
        raise ExtractError("line_too_long", "a row is longer than MSMM reads")
    data = line.encode("utf-8")
    if unpacked + len(data) > UNPACKED_MAX_BYTES:
        raise ExtractError("too_large", "the extract unpacks past MSMM's cap")
    if rows - 1 > MAX_ROWS:
        raise ExtractError("too_many_rows", "more rows than MSMM takes")
    packed.write(data)
    return len(data)


# ── The provider's columns ────────────────────────────────────────────────


def _provenance(provider_cfg: Mapping[str, Any], feed_key: str) -> str:
    """`nap`, `official`, `community`, or '' (MSMM 22.2, point 6). A session
    provider carries none: VIATOR keeps it in its catalogue, so a provider
    that is the catalogue's entry of the same id and the same feed takes the
    catalogue's; a NAP-resolved provider is `nap` by its source."""
    own = str(provider_cfg.get("provenance") or "")
    if own in PROVENANCES:
        return own
    entry = provider_catalogue.load().get(str(provider_cfg.get("id") or ""))
    if entry is not None and feed_key_for(entry) == feed_key:
        listed = str(entry.get("provenance") or "")
        if listed in PROVENANCES:
            return listed
    timetable = provider_cfg.get("timetable")
    if isinstance(timetable, Mapping) and timetable.get("source") == "nap":
        return "nap"
    return ""


def _meta(
    provider_cfg: Mapping[str, Any], feed_key: str, archive_sha256: str, extracted_at: datetime
) -> _Meta:
    provider_id = str(provider_cfg.get("id") or "")
    if _PROVIDER_ID.fullmatch(provider_id) is None:
        raise ExtractError("bad_provider", "the provider id is not a VIATOR provider id")
    if extracted_at.tzinfo is None:
        raise ExtractError("bad_time", "extracted_at has no time zone")
    country = str(provider_cfg.get("country_iso") or "")
    return _Meta(
        feed_key=feed_key,
        feed_version=archive_sha256,
        extracted_at=extracted_at.astimezone(UTC).isoformat(timespec="seconds"),
        provider_id=provider_id,
        provider_label=_clean_label(str(provider_cfg.get("label") or ""), Counter()),
        provider_country_iso=country if _ISO2.fullmatch(country) else "",
        provider_provenance=_provenance(provider_cfg, feed_key),
        extractor_version=extractor_version_for(provider_cfg),
        default_rail=default_rail(provider_cfg),
    )


# ── Reading the archive ───────────────────────────────────────────────────


def _census_of(fmt: str) -> Census:
    return GtfsCensus() if fmt == "gtfs" else NetexCensus()


def _with_census(error: ExtractError, census: Census) -> ExtractError:
    """`error` with what the reader saw, when its reason is one of
    `CENSUS_REASONS`; else `error` as it is."""
    if error.reason not in CENSUS_REASONS:
        return error
    return ExtractError(error.reason, error.detail, summary=census.summary(), census=census.full())


def _read_stations(
    fmt: str, archive_path: Path, provider_id: str, census: Census, default_rail: bool = False
) -> dict[str, Station]:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            if fmt == "gtfs":
                return read_gtfs(
                    zf,
                    provider_id,
                    census if isinstance(census, GtfsCensus) else None,
                    default_rail=default_rail,
                )
            return read_netex(
                zf, census if isinstance(census, NetexCensus) else None, default_rail=default_rail
            )
    except ArchiveError as exc:
        raise _with_census(ExtractError(exc.reason, exc.detail), census) from None
    except (
        OSError,
        EOFError,
        zipfile.BadZipFile,
        zlib.error,
        csv.Error,
        RuntimeError,  # NotImplementedError too (a Deflate64 member)
        ValueError,
    ) as exc:
        raise ExtractError("bad_archive", type(exc).__name__) from None


# ── The cache ─────────────────────────────────────────────────────────────


def extracts_dir(session_id: str) -> Path:
    """Where a session's extracts are kept: `inbox/<sid>/_extracts/`, beside
    `_fetch_state/` and out of the timetable folders every build reads."""
    return settings.inbox_dir / session_id / "_extracts"


def _cache_name(meta: _Meta) -> str:
    """`<provider id>.<archive sha256>.<16 hex>.csv.gz`, the 16 hex of the
    extractor version (rail by default included), the feed key and the
    provider's columns: an extract is made again when any of them changes,
    never for the same archive."""
    ident = json.dumps(
        [
            meta.extractor_version,
            meta.feed_key,
            meta.provider_id,
            meta.provider_label,
            meta.provider_country_iso,
            meta.provider_provenance,
        ],
        separators=(",", ":"),
    )
    return f"{meta.provider_id}.{meta.feed_version}.{_sixteen_hex(ident)}.csv.gz"


def _counts_path(final: Path) -> Path:
    return final.with_name(final.name + _COUNTS_SUFFIX)


def _write_counts(final: Path, summary: str) -> None:
    """Keep the extract's summary (`ExtractResult.summary`) beside it.
    Best-effort."""
    if not summary:
        return
    with contextlib.suppress(OSError):
        _counts_path(final).write_text(json.dumps({"summary": summary}), encoding="utf-8")


def _read_counts(final: Path) -> str:
    """The summary kept beside a cached extract ('' when none, or not of
    the summary's form)."""
    try:
        with _counts_path(final).open("rb") as fh:
            record = json.loads(fh.read(_FAILED_MAX_BYTES))
    except (OSError, ValueError):
        return ""
    summary = record.get("summary") if isinstance(record, dict) else None
    return summary if isinstance(summary, str) and SUMMARY.fullmatch(summary) else ""


def _describe(
    path: Path, meta: _Meta, dropped: Counter[str], summary: str | None = None
) -> ExtractResult:
    """The result for a written extract: its size, sha256 and rows, read back
    (a whole gzip stream, or an exception); `summary` None: the one kept
    beside it, if any."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(MEGABYTE):
            digest.update(chunk)
    with gzip.open(path, "rb") as fh:
        rows = sum(1 for _ in fh) - 1
    if rows < 1:
        raise ExtractError("cache", "an extract without a row")
    if summary is None:
        summary = _read_counts(path)
    return ExtractResult(
        path_gz=path,
        feed_key=meta.feed_key,
        feed_version=meta.feed_version,
        extractor_version=meta.extractor_version,
        rows=rows,
        bytes=path.stat().st_size,
        body_sha256=digest.hexdigest(),
        dropped=dict(dropped),
        summary=summary,
    )


def _cached(path: Path, meta: _Meta) -> ExtractResult | None:
    """The cached extract at `path`, or None. A file that cannot be read back
    whole (cut short by a crash, damaged) is removed, so that it is made
    again instead of poisoning that archive for ever."""
    if not path.is_file():
        return None
    try:
        return _describe(path, meta, Counter())
    except (OSError, EOFError, zlib.error, ExtractError):  # gzip.BadGzipFile is an OSError
        log.warning("station extract cache damaged, made again: provider=%s", meta.provider_id)
        path.unlink(missing_ok=True)
        return None


def _prune(cache_dir: Path, provider_id: str, keep: Path) -> None:
    """Remove the provider's older extracts, their kept summaries and the
    remembered failures: one file kept per provider (with its summary)."""
    kept = {keep, _counts_path(keep)}
    patterns = (
        f"{provider_id}.*.csv.gz",
        f"{provider_id}.*{_FAILED_SUFFIX}",
        f"{provider_id}.*{_COUNTS_SUFFIX}",
    )
    for pattern in patterns:
        for old in cache_dir.glob(pattern):
            if old not in kept:
                old.unlink(missing_ok=True)


# ── Remembered failures ───────────────────────────────────────────────────

# The reason words of a child that passed one of its limits, one per limit,
# so that the log, the remembered failure and the feed status say which:
# memory (`CHILD_MEMORY_BYTES`, a `MemoryError` in the child), processor
# time (`CHILD_CPU_SECONDS`, the child killed by SIGXCPU) or wall clock
# (`CHILD_WALL_SECONDS`, the parent stopped waiting). `stopped`: the child
# was ended from outside by a signal that asks a process to end (SIGKILL, as
# the kernel's out-of-memory killer or an operator sends it, SIGTERM, SIGINT,
# SIGHUP). `crashed`: any other signal (SIGSEGV, SIGBUS, SIGABRT, ...), the
# child's own fault, named in the detail. `resource_limit`, the one word of
# earlier releases for the first four, is no longer given.
MEMORY_LIMIT = "memory_limit"
CPU_LIMIT = "cpu_limit"
WALL_CLOCK_LIMIT = "wall_clock_limit"
STOPPED = "stopped"
CRASHED = "crashed"
_STOP_SIGNALS = frozenset({signal.SIGKILL, signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
# Their details.
OUT_OF_MEMORY = "out of memory"
PROCESSOR_TIME = "processor time"
WALL_CLOCK = "wall-clock time"

# The reasons the same archive would give again, read by the same extractor
# under the same bounds: remembered, so that it is not read again. A child
# stopped from outside is not. One past its wall clock (a busy machine) or
# one that crashed (an archive that makes the reader crash does so every
# time; a crash under a passing memory shortage may not) is remembered only
# the second time in a row with the same reason (`TRIES`, `COUNTED_REASONS`;
# a child that fails otherwise in between starts the count again), so that a
# national archive does not hold the machine 45 minutes at every pass.
# `busy`, `cache` and `error` are never remembered.
REMEMBERED_REASONS = frozenset(
    {
        "bad_archive",
        "input_too_large",
        "no_stop_place",
        "no_rail_stop",
        "too_large",
        "too_many_rows",
        "line_too_long",
        "cell_too_long",
        MEMORY_LIMIT,
        CPU_LIMIT,
    }
)
COUNTED_REASONS = frozenset({WALL_CLOCK_LIMIT, CRASHED})
_FAILED_SUFFIX = ".failed"
# Beside an extract, its summary when it has one (`ExtractResult.summary`):
# what rail by default made rail and, since #371, the places read through a
# quay, for every provider. The name is kept so existing files stay valid.
_COUNTS_SUFFIX = ".rail-default.json"
TRIES = 2
_FAILED_MAX_BYTES = 1024
# The code an extraction runs: a change to any of these files (a reader
# fixed without a new `EXTRACTOR_VERSION`) makes every remembered failure
# be tried again.
_READER_MODULES = (
    station_extract_common,
    station_extract_gtfs,
    station_extract_netex,
    station_extract_skip,
)


@functools.cache
def _code_signature() -> str:
    """The sha256 of the extraction's own code (this module and the readers),
    or of their names when a file cannot be read."""
    digest = hashlib.sha256()
    for module in (sys.modules[__name__], *_READER_MODULES):
        try:
            digest.update(Path(str(module.__file__)).read_bytes())
        except OSError:
            digest.update(module.__name__.encode())
    return digest.hexdigest()


def _limits_signature() -> str:
    """The 16 hex of every bound an extraction runs under, and of its code:
    a remembered failure counts only under the bounds and code it was met
    with."""
    common, gtfs, netex = station_extract_common, station_extract_gtfs, station_extract_netex
    bounds = [
        common.MEMBER_MAX_BYTES,
        common.ARCHIVE_MAX_BYTES,
        common.RATIO_MAX,
        common.RATIO_FLOOR_BYTES,
        common.STATIONS_MAX,
        common.RAW_CODES_MAX,
        gtfs.RECORD_MAX,
        gtfs.FIELDS_MAX,
        gtfs.ID_MAX,
        gtfs.CALLED_STOPS_MAX,
        netex.XML_DEPTH_MAX,
        netex.XML_TEXT_MAX,
        netex.XML_TOKEN_MAX,
        netex.TARGET_ELEMENTS_MAX,
        netex.GROUPS_MAX,
        netex.ASSIGNMENTS_MAX,
        netex.QUAYS_MAX,
        netex.STOP_PLACES_MAX,
        netex.POINTS_MAX,
        CHILD_MEMORY_BYTES,
        CHILD_CPU_SECONDS,
        CHILD_WALL_SECONDS,
        MAX_ROWS,
        EXTRACT_MAX_BYTES,
        UNPACKED_MAX_BYTES,
        _code_signature(),
    ]
    return _sixteen_hex(json.dumps(bounds))


def _failed_path(final: Path) -> Path:
    return final.with_name(final.name + _FAILED_SUFFIX)


def _failure(final: Path, fmt: str) -> dict[str, Any] | None:
    """The failure recorded for the extract `final` read as `fmt`, when it
    was met under the current bounds and code; else None (none, other
    bounds, another format, unreadable)."""
    try:
        with _failed_path(final).open("rb") as fh:
            record = json.loads(fh.read(_FAILED_MAX_BYTES))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("limits") != _limits_signature()
        or record.get("format") != fmt
    ):
        return None
    return record


def _remembered(final: Path, fmt: str) -> str | None:
    """The reason remembered for the extract `final` read as `fmt`, or None."""
    found = _remembered_failure(final, fmt)
    return found[0] if found is not None else None


def _remembered_failure(final: Path, fmt: str) -> tuple[str, str] | None:
    """The reason remembered for the extract `final` read as `fmt` and its
    summary ('' when none was kept, or when it is not of the summary's
    form), or None."""
    record = _failure(final, fmt)
    if record is None:
        return None
    reason = record.get("reason")
    if not isinstance(reason, str) or reason not in _REMEMBERABLE:
        return None
    if reason in COUNTED_REASONS:
        tries = record.get("tries")
        if not isinstance(tries, int) or tries < TRIES:
            return None  # one wall-clock stop or crash only: tried again
    summary = record.get("summary")
    return reason, summary if isinstance(summary, str) and SUMMARY.fullmatch(summary) else ""


_REMEMBERABLE = REMEMBERED_REASONS | COUNTED_REASONS


def _remember(final: Path, provider_id: str, fmt: str, error: ExtractError) -> None:
    """Keep the reason of a failure the same archive would meet again, in
    place of the provider's older files; a wall-clock stop or a crash is
    counted, and remembered from the `TRIES`-th in a row with the same
    reason. Best-effort."""
    record: dict[str, Any] = {"reason": error.reason, "limits": _limits_signature(), "format": fmt}
    if error.summary and SUMMARY.fullmatch(error.summary):
        record["summary"] = error.summary
    if error.reason in COUNTED_REASONS:
        before = _failure(final, fmt)
        same = before is not None and before.get("reason") == error.reason
        tries = before.get("tries") if before is not None and same else None
        record["tries"] = (tries if isinstance(tries, int) else 0) + 1
    elif error.reason not in REMEMBERED_REASONS:
        # Not remembered (stopped from outside, an error): a count begun
        # before is not "in a row" any more.
        before = _failure(final, fmt)
        if before is not None and "tries" in before:
            _failed_path(final).unlink(missing_ok=True)
        return
    target = _failed_path(final)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=final.parent, prefix=f".{provider_id}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        Path(tmp_name).replace(target)
    except OSError:
        return
    _prune(final.parent, provider_id, target)


# Temporary files and archive snapshots older than this are left-overs of a
# process that died; each warm removes them.
STALE_SECONDS = 3600


def _sweep(cache_dir: Path) -> None:
    """Remove the temporary files (`.<pid>.*.tmp`) and snapshots
    (`.<pid>.*.snap`) left more than `STALE_SECONDS` ago. The age is that of
    the last change of the file's inode (`st_ctime`, or `st_mtime` when
    later): a snapshot is a hard link to the slot, so its `st_mtime` is the
    slot's download time, possibly hours old, while making the link changes
    `st_ctime`; a snapshot still waiting for the lock is never taken for a
    left-over."""
    limit = time.time() - STALE_SECONDS
    for pattern in (".*.tmp", ".*.snap"):
        for old in cache_dir.glob(pattern):
            with contextlib.suppress(OSError):
                status = old.stat()
                if max(status.st_ctime, status.st_mtime) < limit:
                    old.unlink()


# ── The extract, in the calling process ───────────────────────────────────


@dataclass(frozen=True)
class _Plan:
    """What an extraction needs, checked before any work."""

    fmt: str
    meta: _Meta
    final: Path


def _plan(
    provider_cfg: Mapping[str, Any],
    archive_path: Path,
    archive_sha256: str,
    extracted_at: datetime,
    cache_dir: Path | None,
) -> _Plan:
    if _SHA256.fullmatch(archive_sha256) is None:
        raise ExtractError("bad_sha256", "archive_sha256 is not 64 small hex digits")
    feed_key = feed_key_for(provider_cfg, upload_sha256=archive_sha256)
    if feed_key is None:
        raise ExtractError("no_feed_key", "this provider sends no extract")
    timetable = provider_cfg.get("timetable")
    fmt = str(timetable.get("format") or "") if isinstance(timetable, Mapping) else ""
    if fmt not in ("gtfs", "netex_nordic", "netex_epip"):
        raise ExtractError("unsupported_format", "not GTFS or NeTEx")
    meta = _meta(provider_cfg, feed_key, archive_sha256, extracted_at)
    folder = cache_dir if cache_dir is not None else archive_path.parent.parent / "_extracts"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExtractError("cache", type(exc).__name__) from None
    return _Plan(fmt, meta, folder / _cache_name(meta))


def _make(plan: _Plan, archive_path: Path) -> ExtractResult:
    """Read the archive and write the extract of `plan`, atomically."""
    meta = plan.meta
    census = _census_of(plan.fmt)
    stations = _read_stations(plan.fmt, archive_path, meta.provider_id, census, meta.default_rail)
    census.stations = len(stations)
    dropped: Counter[str] = Counter()
    fd, tmp_name = tempfile.mkstemp(
        dir=plan.final.parent, prefix=f".{meta.provider_id}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        rows = _write(stations, meta, tmp, dropped)
        tmp.replace(plan.final)
        _write_counts(plan.final, census.default_summary())
    except ExtractError as exc:
        census.dropped.update({word: dropped[word] for word in ROW_DROPS if dropped[word]})
        raise _with_census(exc, census) from None
    finally:
        tmp.unlink(missing_ok=True)
    del stations
    _prune(plan.final.parent, meta.provider_id, plan.final)
    result = _describe(plan.final, meta, dropped, census.default_summary())
    log.info(
        "station extract written: provider=%s format=%s rows=%d bytes=%d dropped=%s",
        meta.provider_id,
        plan.fmt,
        rows,
        result.bytes,
        ",".join(f"{reason}:{count}" for reason, count in sorted(dropped.items())) or "none",
    )
    return result


def build_extract(
    provider_cfg: Mapping[str, Any],
    archive_path: Path,
    archive_sha256: str,
    *,
    extracted_at: datetime,
    cache_dir: Path | None = None,
) -> ExtractResult:
    """Make the stop extract of `archive_path` **in the calling process**:
    for the child process of `ensure_extract` and for the tests only. Every
    other caller uses `ensure_extract`, which bounds the memory and time an
    archive can take.

    `archive_sha256` is the sha256 of the bytes VIATOR downloaded (or that
    the operator uploaded): `FetchResult.sha256`, kept in the fetch state as
    `sha256`, never the sha256 of the slot file when VIATOR re-wrapped it.
    `extracted_at` must carry its time zone (written in UTC). `cache_dir`
    holds the extracts, by archive sha256, `EXTRACTOR_VERSION`, feed key and
    provider columns (default: `_extracts/` of the session inbox the archive
    sits in, `inbox/<sid>/<subdir>/<file>`); a valid cached extract is
    returned as it is, a damaged one made again.

    Raises `ExtractError` only: when no extract can be made, or when it would
    pass one of MSMM's limits or a bound of the readers. Nothing is written
    then.
    """
    try:
        plan = _plan(provider_cfg, archive_path, archive_sha256, extracted_at, cache_dir)
        return _cached(plan.final, plan.meta) or _make(plan, archive_path)
    except ExtractError:
        raise
    except MemoryError:
        raise ExtractError(MEMORY_LIMIT, OUT_OF_MEMORY) from None
    except Exception as exc:
        raise ExtractError("error", type(exc).__name__) from None


# ── The extract, in a child process ───────────────────────────────────────

# The child's limits (review of pull request 4): an archive can take at most
# this much address space, processor time and wall-clock time, and the child
# runs at a lower priority than the web process.
CHILD_MEMORY_BYTES = 2 * 1024 * MEGABYTE
# Measured peaks at MSMM's 200,000-row cap: about 830 MiB RSS (GTFS) and
# 730 MiB (NeTEx), so 2 GiB of address space leaves some 2.4 times that; a
# national-shaped NeTEx of 4 GB unpacked peaks at about 200 MiB, a GTFS of
# 40 million stop times at about 100 MiB (what is kept grows with the rail
# stations, not with the bytes). An export that gives every journey its own
# scheduled stop points has as many points and assignments as calls: the
# NeTEx reader keeps some 250 bytes for each (1.2 KB before the fix of
# 15 October, which spent the 2 GiB near 1.6 million of them); a crafted
# file reaching all its caps at once would still pass 2 GiB, and ends
# `memory_limit` (`station_extract_netex`, "Memory").
# Processor time (`scripts/
# bench_station_extract.py`, one core): NeTEx at about 60 MB/s, so a national
# archive of 2 GB zipped (some 40 GB unpacked) takes about 11 minutes, 17 if
# it packs 30 times; GTFS at about 0.7 million stop times a second, so
# 100 million take under 3 minutes. 40 minutes leave twice the slowest of
# those on a slower core; the child works niced, one at a time, in the
# background (nothing waits for it: the send passes take what is ready), so
# its wall clock is 45 minutes. A child past its processor time fails the
# same way again (remembered, `REMEMBERED_REASONS`), so the margin matters.
CHILD_CPU_SECONDS = 40 * 60
CHILD_WALL_SECONDS = 45 * 60
CHILD_NICE = 10
# How long `ensure_extract` waits for another extraction to end by default.
WAIT_SECONDS = CHILD_WALL_SECONDS + 60
# One extraction at a time: in this process, and on the machine (a lock file
# in the inbox, which the web process and the worker share).
_ONE_HERE = threading.Lock()
_LOCK_NAME = ".station-extract.lock"


@dataclass(frozen=True)
class _Limits:
    memory: int
    cpu: int
    nice: int


_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """Linux: the child is killed when its parent dies (even by SIGKILL), so
    no orphan extraction runs on. Elsewhere, nothing."""
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


_OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")


def _first_to_kill() -> None:
    """Linux: ask the kernel's out-of-memory killer to take this child first
    (1000, the most; raising one's own score needs no privilege), so that a
    machine short of memory ends the extraction, which is tried again,
    rather than the database or a graph build. Elsewhere, or refused,
    nothing."""
    with contextlib.suppress(OSError):
        _OOM_SCORE_ADJ.write_text("1000")


def _limit_self(limits: _Limits) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core file of a crash
    _die_with_parent()
    _first_to_kill()
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory, limits.memory))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu, limits.cpu + 30))
    os.nice(limits.nice)


def _child_main(
    conn: Any, target: Callable[..., ExtractResult], args: tuple[Any, ...], limits: _Limits
) -> None:
    """The child process: set its limits, run `target`, send back the result
    or ("error", reason, detail)."""
    try:
        _limit_self(limits)
        conn.send(("ok", target(*args)))
    except ExtractError as exc:
        conn.send(("error", exc.reason, exc.detail, exc.summary, exc.census))
    except MemoryError:
        conn.send(("error", MEMORY_LIMIT, OUT_OF_MEMORY))
    except Exception as exc:  # any other end is heard as a child that sent nothing
        conn.send(("error", "error", type(exc).__name__))
    finally:
        conn.close()


def _run_child(target: Callable[..., ExtractResult], args: tuple[Any, ...]) -> ExtractResult:
    """Run `target(*args)` in a fresh (spawned, never forked) child under the
    limits above; any end of the child surfaces as a result or `ExtractError`."""
    limits = _Limits(CHILD_MEMORY_BYTES, CHILD_CPU_SECONDS, CHILD_NICE)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_main, args=(sender, target, args, limits), daemon=True)
    message: Any = None
    try:
        child.start()
        sender.close()
        if receiver.poll(CHILD_WALL_SECONDS):
            with contextlib.suppress(EOFError, OSError):
                message = receiver.recv()
        else:
            message = ("error", WALL_CLOCK_LIMIT, WALL_CLOCK)
    except Exception as exc:
        raise ExtractError("error", type(exc).__name__) from None
    finally:
        receiver.close()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
    if message is None:
        raise _silent_end(child.exitcode)
    if message[0] == "ok":
        return message[1]  # type: ignore[no-any-return]
    raise _child_error(message)


def _child_error(message: tuple[Any, ...]) -> ExtractError:
    """The error a child sent: ("error", reason, detail), with the summary
    and the census of what the reader saw when it sent them."""
    reason, detail, summary, census = (*message[1:], "", "")[:4]
    return ExtractError(reason, detail, summary=summary, census=census)


def _silent_end(exitcode: int | None) -> ExtractError:
    """A child that ended without a word: killed by its processor-time
    signal (SIGXCPU, at `CHILD_CPU_SECONDS`), ended from outside (`stopped`:
    `_STOP_SIGNALS`), killed by its own fault (`crashed`: any other signal,
    named), or ended otherwise (`error`, with its exit code)."""
    if exitcode is None or exitcode >= 0:
        return ExtractError("error", f"the child ended with {exitcode}")
    try:
        number = signal.Signals(-exitcode)
    except ValueError:
        return ExtractError(CRASHED, f"the child ended with signal {-exitcode}")
    if number == signal.SIGXCPU:
        return ExtractError(CPU_LIMIT, PROCESSOR_TIME)
    reason = STOPPED if number in _STOP_SIGNALS else CRASHED
    return ExtractError(reason, f"the child ended with {number.name}")


@contextlib.contextmanager
def _machine_lock(lock_path: Path, deadline: float) -> Iterator[None]:
    """One extraction at a time on the machine: an exclusive lock on a file
    the web process and the worker share; `busy` past `deadline`."""
    if not _ONE_HERE.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise ExtractError("busy", "another extraction runs")
    try:
        try:
            handle = lock_path.open("a")
        except OSError:
            yield  # no shared folder to lock in: one at a time in this process only
            return
        with handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise ExtractError("busy", "another extraction runs") from None
                    time.sleep(1)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _ONE_HERE.release()


def ensure_extract(
    provider_cfg: Mapping[str, Any],
    archive_path: Path,
    archive_sha256: str,
    *,
    extracted_at: datetime,
    session_id: str,
    cache_dir: Path | None = None,
    wait_seconds: float | None = None,
    cache_only: bool = False,
) -> ExtractResult:
    """The stop extract of `archive_path`: the one entry point for every
    caller (the web process, the worker). With `cache_only`, only a valid
    cached extract is returned, else `ExtractError` `not_cached`: no
    extraction is started or waited for (the worker before a graph build).

    A valid cached extract (`extracts_dir(session_id)` unless `cache_dir` is
    given) is returned at once. Otherwise the extract is made in a spawned
    child process with `CHILD_MEMORY_BYTES` of address space,
    `CHILD_CPU_SECONDS` of processor time, `CHILD_WALL_SECONDS` of wall-clock
    time and a lower priority, one extraction at a time on the machine; a
    caller that would wait more than `wait_seconds` (default
    `WAIT_SECONDS`) for another one gets `busy`. The arguments are those of
    `build_extract`. Raises `ExtractError` only: `memory_limit`,
    `cpu_limit` or `wall_clock_limit` when the child passed that limit,
    `stopped` when a signal from outside ended it, `crashed` when another
    signal killed it.
    """
    folder = cache_dir if cache_dir is not None else extracts_dir(session_id)
    try:
        plan = _plan(provider_cfg, archive_path, archive_sha256, extracted_at, folder)
        _known(plan)
        if cache_only:
            raise ExtractError("not_cached", "no valid cached extract")
        deadline = time.monotonic() + (WAIT_SECONDS if wait_seconds is None else wait_seconds)
        with _machine_lock(settings.inbox_dir / _LOCK_NAME, deadline):
            _known(plan)  # made, or refused, while this one waited
            args = (dict(provider_cfg), archive_path, archive_sha256)
            try:
                return _run_child(_child_build, (*args, extracted_at, folder))
            except ExtractError as exc:
                _log_census(plan, exc)
                _remember(plan.final, plan.meta.provider_id, plan.fmt, exc)
                raise
    except _Known as known:
        if known.result is not None:
            return known.result
        raise ExtractError(known.reason, "remembered", summary=known.summary) from None
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError("error", type(exc).__name__) from None


def _log_census(plan: _Plan, error: ExtractError) -> None:
    """The whole census of a feed that gave no extract, in the log of the
    process that asked for it (counts and enumerated words only)."""
    if error.census:
        log.warning(
            "station extract census: provider=%s format=%s reason=%s %s",
            plan.meta.provider_id,
            plan.fmt,
            error.reason,
            error.census,
        )


class _Known(Exception):  # a way out of the checks, not an error
    """What is known of an extract without making it: the cached extract, or
    the reason remembered for the archive (with its summary)."""

    def __init__(self, result: ExtractResult | None, reason: str = "", summary: str = "") -> None:
        super().__init__(reason)
        self.result = result
        self.reason = reason
        self.summary = summary


def _known(plan: _Plan) -> None:
    """Raise `_Known` when the extract of `plan` is cached or its failure
    remembered; return when it must be made."""
    cached = _cached(plan.final, plan.meta)
    if cached is not None:
        raise _Known(cached)
    found = _remembered_failure(plan.final, plan.fmt)
    if found is not None:
        raise _Known(None, *found)


def extract_known(provider_cfg: Mapping[str, Any], archive_sha256: str, *, session_id: str) -> bool:
    """Whether the extract of an archive is in the cache, or its failure
    remembered: what the send pass asks to send those feeds first. Cheap
    (the file is not read back; `ensure_extract` checks it); never raises."""
    try:
        plan = _plan(
            provider_cfg, Path(), archive_sha256, datetime.now(UTC), extracts_dir(session_id)
        )
        return plan.final.is_file() or _remembered(plan.final, plan.fmt) is not None
    except Exception:  # an ordering hint only
        return False


def _child_build(
    provider_cfg: dict[str, Any],
    archive_path: Path,
    archive_sha256: str,
    extracted_at: datetime,
    cache_dir: Path,
) -> ExtractResult:
    return build_extract(
        provider_cfg, archive_path, archive_sha256, extracted_at=extracted_at, cache_dir=cache_dir
    )


# ── At refresh time ───────────────────────────────────────────────────────


def _same_file(fd: int, path: Path) -> bool:
    try:
        before, now = os.fstat(fd), path.stat()
    except OSError:
        return False
    return (before.st_dev, before.st_ino) == (now.st_dev, now.st_ino)


@contextlib.contextmanager
def provider_archive_snapshot(
    session_id: str,
    provider_cfg: Mapping[str, Any],
    *,
    upload_sha256: str | None = None,
) -> Iterator[tuple[Path, str] | None]:
    """The provider's timetable slot file and the sha256 of the bytes behind
    it, checked to belong together, or None when there is none yet.

    For a `url` or `nap` provider the sha256 is the one of the last download,
    kept in the fetch state (`FetchResult.sha256`, also kept on a 304); an
    upload into the slot forgets that state. For an `upload` provider it is
    `upload_sha256`, the sha256 of the uploaded file (`Upload.sha256`).

    The slot is opened first, then the state read, then the slot hard-linked
    to a snapshot (`.<pid>.<random>.snap` in the session's `_extracts/`,
    removed on leaving). The pair is used only when the snapshot is the very
    file opened first (`os.fstat` of the open file against the snapshot's
    `stat`) and is the file the state describes: its size, then its sha256
    (streamed), against the state's `slot_size_bytes` and `slot_sha256`
    (`feed_fetch`; for an upload, `upload_sha256`). A refresh dispatches the
    new file before it saves its state: a snapshot taken between the two
    holds the new file and reads the old sha256, and is refused
    (`archive_changed`) instead of being labelled with the wrong version; the
    next warm takes it. The extraction reads the snapshot, which no refresh
    can replace."""
    with _snapshot(session_id, provider_cfg, upload_sha256) as (found, _reason):
        yield found


@contextlib.contextmanager
def _snapshot(
    session_id: str, provider_cfg: Mapping[str, Any], upload_sha256: str | None
) -> Iterator[tuple[tuple[Path, str] | None, str]]:
    """`provider_archive_snapshot`, with the reason word when there is none:
    `skipped` (no slot file or no sha256 yet), `snapshot_failed` (the hard
    link could not be made: another device, a permission), `slot_changed`
    (a refresh replaced the slot meanwhile), `archive_changed` (the snapshot
    is not the file the state describes: a refresh dispatched a new file and
    has not saved its state yet) or `archive_unverified` (a state written
    before this release, of a re-wrapped file, which cannot be checked; the
    next download of a new file writes what is needed)."""
    found = _slot_and_source(session_id, provider_cfg)
    fd = _open_slot(found[0]) if found is not None else None
    if found is None or fd is None:
        yield None, "skipped"
        return
    slot, source, state_name = found
    snapshot: Path | None = None
    reason = "skipped"
    try:
        expected = _expected(session_id, source, state_name, upload_sha256)
        if expected is not None:
            snapshot, reason = _link_snapshot(fd, slot, session_id, str(provider_cfg["id"]))
    finally:
        os.close(fd)
    try:
        if snapshot is not None and expected is not None:
            reason = _verify(snapshot, expected)
        if snapshot is not None and expected is not None and not reason:
            yield (snapshot, expected.sha256), ""
        else:
            yield None, reason
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Expected:
    """What the slot must hold: `sha256` labels the extract (the download's,
    or the upload's), `slot_sha256` and `slot_size` describe the slot file
    itself (a re-wrapped `.xml.gz` differs from its download); `legacy` when
    the fetch state predates `slot_sha256`."""

    sha256: str
    slot_sha256: str
    slot_size: int | None
    legacy: bool = False


def _expected(
    session_id: str, source: str, state_name: str, upload_sha256: str | None
) -> _Expected | None:
    if source == "upload":
        if upload_sha256 is None or _SHA256.fullmatch(upload_sha256) is None:
            return None
        return _Expected(upload_sha256, upload_sha256, None)
    state = feed_fetch.load_state(settings.inbox_dir / session_id / "_fetch_state", state_name)
    sha = state.get("sha256")
    if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
        return None
    slot_sha, slot_size = state.get("slot_sha256"), state.get("slot_size_bytes")
    if isinstance(slot_sha, str) and _SHA256.fullmatch(slot_sha) and isinstance(slot_size, int):
        return _Expected(sha, slot_sha, slot_size)
    size = state.get("size_bytes")
    return _Expected(sha, sha, size if isinstance(size, int) else None, legacy=True)


def _verify(snapshot: Path, expected: _Expected) -> str:
    """'' when the snapshot is the file the state describes (its size, then
    its sha256, streamed), else the reason word. Hashing a national archive
    takes seconds: it runs on the warm thread or in the worker, never in a
    request thread, and only when no cached extract of that sha256 exists."""
    try:
        size = snapshot.stat().st_size
        if expected.slot_size is not None and size != expected.slot_size:
            return "archive_unverified" if expected.legacy else "archive_changed"
        digest = hashlib.sha256()
        with snapshot.open("rb") as fh:
            while chunk := fh.read(MEGABYTE):
                digest.update(chunk)
    except OSError:
        return "snapshot_failed"
    return "" if digest.hexdigest() == expected.slot_sha256 else "archive_changed"


def _open_slot(slot: Path) -> int | None:
    try:
        return os.open(slot, os.O_RDONLY)
    except OSError:
        return None


def _slot_and_source(
    session_id: str, provider_cfg: Mapping[str, Any]
) -> tuple[Path, str, str] | None:
    """The slot file of the provider, its source and its fetch-state name."""
    pid = str(provider_cfg.get("id") or "")
    timetable = provider_cfg.get("timetable")
    if not isinstance(timetable, Mapping) or _PROVIDER_ID.fullmatch(pid) is None:
        return None
    fmt = str(timetable.get("format") or "")
    details = ingestion.TIMETABLE_FORMAT_DETAILS.get(fmt)
    source = str(timetable.get("source") or "")
    if details is None or source not in ("url", "nap", "upload"):
        return None
    slot = (
        settings.inbox_dir
        / session_id
        / details["subdir"]
        / ingestion.staged_filename_for_format(pid, fmt)
    )
    return slot, source, f"provider[{pid}].timetable({fmt})"


def _link_snapshot(fd: int, slot: Path, session_id: str, pid: str) -> tuple[Path | None, str]:
    """A hard link to the slot, kept only when it is the file open as `fd`;
    else None and the reason word."""
    folder = extracts_dir(session_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = folder / f".{pid}.{os.urandom(6).hex()}.snap"
        os.link(slot, snapshot)
    except OSError:
        return None, "snapshot_failed"
    if _same_file(fd, snapshot):
        return snapshot, ""
    snapshot.unlink(missing_ok=True)
    return None, "slot_changed"


# What is in progress in this process: (session, provider, archive sha256).
_IN_FLIGHT: set[tuple[str, str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def _cached_for(
    session_id: str, provider: Mapping[str, Any], upload_sha256: str | None
) -> ExtractResult | str | None:
    """The cached extract of the sha256 the state names, or the reason
    remembered for it, if any: then the slot is neither snapshotted nor
    hashed (an extract of that sha256 was made, or refused, from a verified
    snapshot before)."""
    found = _slot_and_source(session_id, provider)
    expected = _expected(session_id, found[1], found[2], upload_sha256) if found else None
    if expected is None:
        return None
    try:
        plan = _plan(provider, Path(), expected.sha256, datetime.now(UTC), extracts_dir(session_id))
        _known(plan)
    except ExtractError:
        return None
    except _Known as known:
        return known.result if known.result is not None else known.reason
    return None


def _warm_one(
    session_id: str, provider: Mapping[str, Any], upload_sha256: str | None
) -> ExtractResult | str:
    """The extract of one provider, or the reason word there is none."""
    pid = str(provider.get("id") or "")
    cached = _cached_for(session_id, provider, upload_sha256)
    if cached is not None:
        return cached
    with _snapshot(session_id, provider, upload_sha256) as (found, reason):
        if found is None:
            return reason
        if feed_key_for(provider, upload_sha256=found[1]) is None:
            return "skipped"
        archive, sha = found
        key = (session_id, pid, sha)
        with _IN_FLIGHT_LOCK:
            if key in _IN_FLIGHT:
                return "in_flight"
            _IN_FLIGHT.add(key)
        try:
            return ensure_extract(
                provider,
                archive,
                sha,
                extracted_at=datetime.now(UTC),
                session_id=session_id,
            )
        except ExtractError as exc:
            return exc.reason
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(key)


def _slot_size(session_id: str, provider: Mapping[str, Any]) -> int:
    """The size of the provider's timetable file, 0 when there is none: the
    warm makes the smallest extracts first, so that one national archive
    never keeps the small feeds of the session waiting."""
    found = _slot_and_source(session_id, provider)
    try:
        return found[0].stat().st_size if found is not None else 0
    except OSError:
        return 0


def warm_session_extracts(
    session_id: str,
    config: Mapping[str, Any],
    *,
    only_provider: str | None = None,
    upload_sha256: Mapping[str, str] | None = None,
) -> dict[str, ExtractResult | str]:
    """Make, or find in the cache, the extract of every provider of the session
    that has a feed key and a timetable file: provider id -> its result, or
    the reason word when there is none (`skipped`, `in_flight` when the same
    archive is being made, or an `ExtractError` reason). Run after a refresh
    or an upload (on the warm thread, `queue_warm`), so that sending the extracts
    later never waits for one to be made. The smallest timetable files go
    first. Never raises; one line per provider in the log, with the session
    and provider ids and a reason word."""
    outcome: dict[str, ExtractResult | str] = {}
    try:
        providers = ingestion.normalize_providers(dict(config))
    except ValueError:
        log.info("station extract skipped: sid=%s reason=config", session_id)
        return outcome
    with contextlib.suppress(OSError):
        _sweep(extracts_dir(session_id))
    uploads = upload_sha256 or {}
    for provider in sorted(providers, key=lambda p: _slot_size(session_id, p)):
        pid = provider["id"]
        if only_provider is not None and pid != only_provider:
            continue
        try:
            result = _warm_one(session_id, provider, uploads.get(pid))
        except Exception as exc:  # one provider never stops the others
            result = "error"
            log.warning(
                "station extract failed: sid=%s provider=%s error=%s",
                session_id,
                pid,
                type(exc).__name__,
            )
        outcome[pid] = result
        _log_outcome(session_id, pid, result)
    return outcome


# The reasons a provider has no extract that are not a failure.
_SKIP_REASONS = frozenset(
    {
        "skipped",
        "in_flight",
        "snapshot_failed",
        "slot_changed",
        "archive_changed",
        "archive_unverified",
    }
)


def _log_outcome(session_id: str, pid: str, result: ExtractResult | str) -> None:
    """One line per provider: counts and reason words, never a name or code.
    What the extraction cleaned or dropped (`ExtractResult.dropped`, made in
    the child, whose own log lines go nowhere) is logged here."""
    if isinstance(result, ExtractResult):
        dropped = ",".join(f"{k}:{v}" for k, v in sorted(result.dropped.items())) or "none"
        log.info(
            "station extract ready: sid=%s provider=%s rows=%d bytes=%d dropped=%s",
            session_id,
            pid,
            result.rows,
            result.bytes,
            dropped,
        )
    elif result in _SKIP_REASONS:
        log.info("station extract skipped: sid=%s provider=%s reason=%s", session_id, pid, result)
    else:
        log.warning(
            "station extract not written: sid=%s provider=%s reason=%s", session_id, pid, result
        )


# ── The warm queue of the web process ─────────────────────────────────────

# Warms run on one thread of their own, never in the request thread pool (a
# warm can wait for the machine's lock for many minutes). The queue holds at
# most `QUEUE_MAX` distinct jobs; a job already waiting is replaced by the
# newer one (its configuration is the newest), never doubled.
QUEUE_MAX = 64
_Job = tuple[str, str | None]
_QUEUE: queue.Queue[_Job] = queue.Queue()
_PENDING: dict[_Job, tuple[dict[str, Any], dict[str, str]]] = {}
_PENDING_LOCK = threading.Lock()
_WORKER: list[threading.Thread] = []


def queue_warm(
    session_id: str,
    config: Mapping[str, Any],
    *,
    only_provider: str | None = None,
    upload_sha256: Mapping[str, str] | None = None,
) -> bool:
    """Queue `warm_session_extracts` for the warm thread and return at once:
    True when queued (or merged into the same job already waiting), False
    when the queue is full (logged; the next refresh queues it again)."""
    job: _Job = (session_id, only_provider)
    with _PENDING_LOCK:
        if job in _PENDING:
            uploads = _PENDING[job][1]
            _PENDING[job] = (dict(config), {**uploads, **(upload_sha256 or {})})
            return True
        if len(_PENDING) >= QUEUE_MAX:
            log.warning("station extract not queued: sid=%s reason=queue_full", session_id)
            return False
        _PENDING[job] = (dict(config), dict(upload_sha256 or {}))
        _QUEUE.put(job)
        if not _WORKER or not _WORKER[0].is_alive():
            worker = threading.Thread(target=_warm_loop, name="station-extract-warm", daemon=True)
            _WORKER[:] = [worker]
            worker.start()
    return True


def _warm_loop() -> None:
    while True:
        job = _QUEUE.get()
        try:
            _run_job(job)
        finally:
            _QUEUE.task_done()


def _run_job(job: _Job) -> None:
    with _PENDING_LOCK:
        taken = _PENDING.pop(job, None)
    if taken is None:
        return
    config, uploads = taken
    try:
        warm_session_extracts(job[0], config, only_provider=job[1], upload_sha256=uploads)
    except Exception as exc:  # the warm thread never dies
        log.warning("station extract warm failed: sid=%s error=%s", job[0], type(exc).__name__)
