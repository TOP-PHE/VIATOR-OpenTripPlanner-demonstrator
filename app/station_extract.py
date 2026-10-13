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

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .feed_resolvers import validate_resolver

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
    source = str(timetable.get("source") or ("url" if timetable.get("url") else "upload"))
    key: str | None = None
    if source == "nap":
        try:
            resolver = validate_resolver(timetable.get("resolver"), "resolver")
        except ValueError:
            return None
        key = resolver_identity(resolver)
    elif source == "url":
        url = str(timetable.get("url") or "").strip()
        key = url_identity(url) if url else None
    elif source == "upload":
        if upload_sha256 is not None and _SHA256.fullmatch(upload_sha256):
            key = f"upload:{upload_sha256}"
    if key is None or FEED_KEY.fullmatch(key) is None:
        return None
    return key


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
    raise NotImplementedError("build_extract lands in the next commit")
