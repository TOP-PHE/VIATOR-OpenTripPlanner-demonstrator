"""The stop extract of one timetable feed, for the station module (MSMM).

The station module builds its VIATOR station referential from the stops of
the timetables VIATOR already downloads (MSMM `docs/service-design.md`,
section 22). VIATOR never sends an archive: at refresh time it writes, per
feed version, one small file of the rail stations of that feed, and a later
step (MSMM pull request 5 on the VIATOR side) sends it to the module.

This module makes that file. Its public interface is small and stable:

    EXTRACTOR_VERSION
    feed_key_for(provider_cfg, *, upload_sha256=None) -> str | None
    build_extract(provider_cfg, archive_path, archive_sha256, *,
                  extracted_at, cache_dir=None) -> ExtractResult
    ExtractError (with a `reason` word)

The form of the file is the contract of MSMM 22.2, point 6, and MSMM's
`app/master/feed_intake.py::check_extract` is its reference: one bad item
refuses the whole extract there, so everything is cleaned or dropped here,
before it is written, and an extract that would pass a limit is not written
at all (`ExtractError`).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
import zipfile
import zlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import feed_fetch, ingestion, osm_geo, provider_catalogue
from .feed_resolvers import validate_resolver
from .settings import settings
from .station_extract_common import ArchiveError, Station
from .station_extract_gtfs import read_gtfs
from .station_extract_netex import read_netex

log = logging.getLogger(__name__)

# The version of the rules below. MSMM keeps one version of an archive per
# extractor version (MSMM 22.3, point 1): raise it whenever the rows written
# for the same archive would change, so the module receives the new extract.
EXTRACTOR_VERSION = "viator-stop-extract-1"

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
    `unsupported_format`, `bad_archive`, `no_rail_stop`, `too_large`,
    `too_many_rows`, `line_too_long`."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


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


# ── The feed key (MSMM 22.2, point 6) ──────────────────────────────────────


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
# extract, so none of them is ever passed here.
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
        # without an operator or authority) is the provider's own.
        dropped["operator_from_provider"] += 1
        operators = [meta.provider_id]
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
    iso = osm_geo.country_for_point(lat, lon) or ""
    return [
        FORMAT_VERSION,
        meta.feed_key,
        meta.feed_version,
        meta.extracted_at,
        EXTRACTOR_VERSION,
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
    # mtime 0 and no file name: the same rows give the same bytes.
    with (
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed,
    ):
        for record in [list(COLUMNS)] + [
            row
            for key in sorted(stations)
            if (row := _station_row(stations[key], meta, dropped)) is not None
        ]:
            if any(len(cell) > FIELD_MAX for cell in record):
                raise ExtractError("cell_too_long", "a cell is longer than MSMM reads")
            line = _csv_line(record)
            if len(line) > LINE_MAX:
                raise ExtractError("line_too_long", "a row is longer than MSMM reads")
            data = line.encode("utf-8")
            unpacked += len(data)
            if unpacked > UNPACKED_MAX_BYTES:
                raise ExtractError("too_large", "the extract unpacks past MSMM's cap")
            rows += 1
            if rows - 1 > MAX_ROWS:
                raise ExtractError("too_many_rows", "more rows than MSMM takes")
            packed.write(data)
    if rows <= 1:
        raise ExtractError("no_rail_stop", "no station-level stop with a rail call")
    if target.stat().st_size > EXTRACT_MAX_BYTES:
        raise ExtractError("too_large", "the gzip file is larger than MSMM takes")
    return rows - 1


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
        extracted_at=extracted_at.isoformat(timespec="seconds"),
        provider_id=provider_id,
        provider_label=_clean_label(str(provider_cfg.get("label") or ""), Counter()),
        provider_country_iso=country if _ISO2.fullmatch(country) else "",
        provider_provenance=_provenance(provider_cfg, feed_key),
    )


# ── Reading the archive ───────────────────────────────────────────────────


def _read_stations(fmt: str, archive_path: Path, provider_id: str) -> dict[str, Station]:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            if fmt == "gtfs":
                return read_gtfs(zf, provider_id)
            return read_netex(zf)
    except ArchiveError as exc:
        raise ExtractError(exc.reason, exc.detail) from None
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
    extractor version, the feed key and the provider's columns: an extract is
    made again when any of them changes, never for the same archive."""
    ident = json.dumps(
        [
            EXTRACTOR_VERSION,
            meta.feed_key,
            meta.provider_id,
            meta.provider_label,
            meta.provider_country_iso,
            meta.provider_provenance,
        ],
        separators=(",", ":"),
    )
    return f"{meta.provider_id}.{meta.feed_version}.{_sixteen_hex(ident)}.csv.gz"


def _describe(path: Path, meta: _Meta, dropped: Counter[str]) -> ExtractResult:
    """The result for a written extract: its size, sha256 and rows, read back."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(MEGABYTE):
            digest.update(chunk)
    with gzip.open(path, "rb") as fh:
        rows = sum(1 for _ in fh) - 1
    return ExtractResult(
        path_gz=path,
        feed_key=meta.feed_key,
        feed_version=meta.feed_version,
        extractor_version=EXTRACTOR_VERSION,
        rows=rows,
        bytes=path.stat().st_size,
        body_sha256=digest.hexdigest(),
        dropped=dict(dropped),
    )


def _prune(cache_dir: Path, provider_id: str, keep: Path) -> None:
    """Remove the provider's older extracts: one kept per provider."""
    for old in cache_dir.glob(f"{provider_id}.*.csv.gz"):
        if old != keep:
            old.unlink(missing_ok=True)


# ── The extract ───────────────────────────────────────────────────────────


def build_extract(
    provider_cfg: Mapping[str, Any],
    archive_path: Path,
    archive_sha256: str,
    *,
    extracted_at: datetime,
    cache_dir: Path | None = None,
) -> ExtractResult:
    """Write the stop extract of `archive_path` and return what was written.

    `archive_sha256` is the sha256 of the bytes VIATOR downloaded (or that
    the operator uploaded): `FetchResult.sha256`, kept in the fetch state as
    `sha256`, never the sha256 of the slot file when VIATOR re-wrapped it.
    `extracted_at` must carry its time zone.

    `cache_dir` is where extracts are kept, by archive sha256 and
    `EXTRACTOR_VERSION`: a second call for the same archive, extractor and
    provider returns the file already there. By default it is `_extracts/`
    of the session inbox the archive sits in (`inbox/<sid>/<subdir>/<file>`).

    Raises `ExtractError` when no extract can be made or when the extract
    would pass one of MSMM's limits: nothing is written then.
    """
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
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / _cache_name(meta)
    if final.is_file():
        return _describe(final, meta, Counter())

    stations = _read_stations(fmt, archive_path, meta.provider_id)
    dropped: Counter[str] = Counter()
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{meta.provider_id}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        rows = _write(stations, meta, tmp, dropped)
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)
    del stations
    _prune(folder, meta.provider_id, final)
    result = _describe(final, meta, dropped)
    log.info(
        "station extract written: format=%s rows=%d bytes=%d dropped=%s",
        fmt,
        rows,
        result.bytes,
        ",".join(f"{reason}:{count}" for reason, count in sorted(dropped.items())) or "none",
    )
    return result


# ── At refresh time ───────────────────────────────────────────────────────


def provider_archive(
    session_id: str,
    provider_cfg: Mapping[str, Any],
    *,
    upload_sha256: str | None = None,
) -> tuple[Path, str] | None:
    """The provider's timetable slot file and the sha256 of the bytes behind
    it, or None when there is none yet.

    For a `url` or `nap` provider the sha256 is the one of the last download,
    kept in the fetch state (`FetchResult.sha256`, also kept on a 304); an
    upload into the slot forgets that state, so a stale sha256 is never paired
    with another file. For an `upload` provider it is `upload_sha256`, the
    sha256 of the uploaded file (`Upload.sha256`)."""
    pid = str(provider_cfg.get("id") or "")
    timetable = provider_cfg.get("timetable")
    if not isinstance(timetable, Mapping) or _PROVIDER_ID.fullmatch(pid) is None:
        return None
    fmt = str(timetable.get("format") or "")
    details = ingestion.TIMETABLE_FORMAT_DETAILS.get(fmt)
    if details is None:
        return None
    inbox = settings.inbox_dir / session_id
    slot = inbox / details["subdir"] / ingestion.staged_filename_for_format(pid, fmt)
    if not slot.is_file():
        return None
    source = timetable.get("source")
    if source == "upload":
        sha = upload_sha256
    elif source in ("url", "nap"):
        state = feed_fetch.load_state(inbox / "_fetch_state", f"provider[{pid}].timetable({fmt})")
        sha = state.get("sha256") if isinstance(state.get("sha256"), str) else None
    else:
        return None
    if sha is None or _SHA256.fullmatch(sha) is None:
        return None
    return slot, sha


def warm_session_extracts(
    session_id: str,
    config: Mapping[str, Any],
    *,
    only_provider: str | None = None,
    upload_sha256: Mapping[str, str] | None = None,
) -> dict[str, ExtractResult | str]:
    """Make, or find in the cache, the extract of every provider of the session
    that has a feed key and a timetable file: provider id -> its result, or
    the reason word when there is none. Run after a refresh or an upload
    (FastAPI background task), so that sending the extracts later never waits
    for one to be made. Never raises: a failure is logged as a reason word."""
    outcome: dict[str, ExtractResult | str] = {}
    try:
        providers = ingestion.normalize_providers(dict(config))
    except ValueError:
        return outcome
    uploads = upload_sha256 or {}
    for provider in providers:
        pid = provider["id"]
        if only_provider is not None and pid != only_provider:
            continue
        found = provider_archive(session_id, provider, upload_sha256=uploads.get(pid))
        if found is None or feed_key_for(provider, upload_sha256=found[1]) is None:
            outcome[pid] = "skipped"
            continue
        archive, sha = found
        try:
            outcome[pid] = build_extract(provider, archive, sha, extracted_at=datetime.now(UTC))
        except ExtractError as exc:
            log.warning("station extract not written: reason=%s", exc.reason)
            outcome[pid] = exc.reason
        except Exception as exc:  # a background task must never die on one feed
            log.warning("station extract failed: error=%s", type(exc).__name__)
            outcome[pid] = "error"
    return outcome
