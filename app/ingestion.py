"""Per-session ingestion dispatcher.

Routes uploaded files into the right per-session inbox subtree and decides
whether an OTP rebuild is needed for that session.

Layout:
  /data/inbox/<session_id>/{gtfs,osm,netex,archive,runtime,_staging}/

Multi-feed GTFS:
  Sessions can have multiple GTFS feeds (SNCF + IDFM + Trenitalia, etc.).
  Each feed lands at `inbox/<sid>/gtfs/<feed_id_lower>.zip`. The OTP entrypoint
  scans `gtfs/*.zip` at build time and generates a `build-config.json` with
  one transitFeeds entry per file (feedId = filename stem, uppercased). See
  docker/otp/entrypoint.sh.

  Single-feed sessions (legacy and the common case) stage at the default
  `gtfs.zip` filename, which the build-config generator turns into a
  feedId of `GTFS` — same routing behaviour as before.
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as DbSession

from .models import RebuildJob
from .settings import settings

# Which detected kinds get staged into OTP's build inbox vs. stored elsewhere.
STAGE_INTO_OTP_INBOX: dict[str, str] = {
    "GTFS": "gtfs",
    "NeTEx-Nordic": "netex",
    "NeTEx-EPIP": "netex",
    "OSM-PBF": "osm",
}

# Canonical default filename per kind. For GTFS, multi-feed sessions override
# this with a per-feed name (`<feed_id_lower>.zip`) — see `dispatch(...,
# staged_filename=)`. The entrypoint reads zips by glob from `gtfs/*.zip`
# regardless of name, and the build-config generator derives feedId from the
# stem.
STAGE_INTO_OTP_INBOX_FILENAME: dict[str, str] = {
    "GTFS": "gtfs.zip",
    "NeTEx-Nordic": "netex.zip",
    "NeTEx-EPIP": "netex.zip",
    "OSM-PBF": "osm.pbf",
}

# Feed IDs become OTP feedId namespaces on stop_ids (e.g. `SNCF:OCETrain-…`).
# OTP doesn't enforce a strict format but we keep ours alphanumeric-uppercase
# to avoid surprises with stop-id parsing in the journey UI. Filenames are
# the lowercased form (case-insensitive filesystems on macOS/Windows would
# otherwise collide on rename).
_FEED_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")
_COUNTRY_ISO_RE = re.compile(r"^[A-Z]{2}$")

# Timetable formats OTP can ingest natively. NeTEx-FR is intentionally not
# in this set — OTP doesn't read it, so accepting it for routing would be
# a footgun. Operators wanting NeTEx-FR archive-only (compliance) should
# still use the manual upload path with `declared_standard=NeTEx-FR-…`,
# which dispatches to `inbox/<sid>/archive/` and never touches OTP.
_OTP_TIMETABLE_FORMATS: frozenset[str] = frozenset({"gtfs", "netex_nordic", "netex_epip"})

# Where a provider's timetable content comes from (v0.1.37).
#   "url"    — download from timetable.url (the original behaviour)
#   "upload" — an operator file attached to this provider, landed at its
#              inbox slot via POST /{sid}/uploads?provider_id=...
#   "cross_border_filter" — a *derived* provider: it owns no feed of its own,
#              only a link (`derived_from`) to a national provider's slot in
#              another session plus filter params. Build/refresh runs
#              app.gtfs_cross_border_filter.filter_to_cross_border on the
#              linked national feed into this provider's slot. One source of
#              truth, no drift. See docs/provider-source-modes-design.md §12.
# A "server_file" mode (reference a pre-generated artifact) is still planned
# (§9 Phase 2) and is not yet a valid value.
_TIMETABLE_SOURCES: frozenset[str] = frozenset({"url", "upload", "cross_border_filter"})

# The OTP entrypoint's build-config generator reads each timetable file
# from one of these subdirs (the v0.1.4 single-feed code already did this
# for gtfs/; netex/ is now wired through the same path). Per-format inbox
# subdir + per-format `transitFeeds.type` value for build-config.json.
TIMETABLE_FORMAT_DETAILS: dict[str, dict[str, str]] = {
    "gtfs": {"subdir": "gtfs", "otp_type": "gtfs", "kind": "GTFS"},
    "netex_nordic": {"subdir": "netex", "otp_type": "netex", "kind": "NeTEx-Nordic"},
    "netex_epip": {"subdir": "netex", "otp_type": "netex", "kind": "NeTEx-EPIP"},
}

# Stored but does NOT trigger an OTP rebuild (Phase 6 — see strategy doc).
ARCHIVE_ONLY: set[str] = {
    "NeTEx-FR-Horaires",
    "NeTEx-FR-Arrets",
}

# Loaded into the database / staged for the OJP adapter (Phase 5).
LOAD_TO_DB: set[str] = {
    "SNCF-MCT",
    "SNCF-Stations",
}


def session_inbox(session_id: str | None) -> Path:
    """Return the per-session inbox dir, falling back to a `_phase1` bucket
    while the upload UI still lacks a session selector."""
    sid = session_id or "_phase1"
    return settings.inbox_dir / sid


def normalize_gtfs_sources(raw: object) -> list[dict[str, str]]:
    """Coerce `config.sources.gtfs` into a canonical multi-feed list.

    Accepts:
      - None / "" → []
      - str (legacy single URL) → [{"id": "GTFS", "url": <url>}]
      - list of {"id": str, "url": str} → returned as-is after validation

    Raises ValueError if the shape is wrong or any feed_id fails the regex.
    The "GTFS" default for the legacy single-string form keeps the OTP
    feedId stable across the migration: a graph that previously had a
    single feed with feedId="DEFAULT" (from the old hardcoded build-config)
    now has a single feed with feedId="GTFS" — small visible change in
    stop_id prefixes, but consistent with the new naming rule.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [{"id": "GTFS", "url": raw}]
    if not isinstance(raw, list):
        raise ValueError(f"config.sources.gtfs must be a string or list, got {type(raw).__name__}")
    out: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"feeds[{i}] must be an object with 'id' and 'url'")
        feed_id = (entry.get("id") or "").strip()
        url = (entry.get("url") or "").strip()
        if not feed_id or not _FEED_ID_RE.match(feed_id):
            raise ValueError(
                f"feeds[{i}].id={feed_id!r} must match /^[A-Z][A-Z0-9_-]{{1,15}}$/ "
                "(e.g. SNCF, IDFM, TRENITALIA, FR-SNCF)"
            )
        if feed_id in seen_ids:
            raise ValueError(f"feed id {feed_id!r} appears twice")
        seen_ids.add(feed_id)
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"feeds[{i}].url={url!r} must be an http(s) URL")
        out.append({"id": feed_id, "url": url})
    return out


def gtfs_staged_filename(feed_id: str) -> str:
    """Canonical inbox filename for one GTFS feed in a multi-feed session.

    Lowercased so case-insensitive filesystems don't collide. The OTP
    entrypoint's build-config generator re-uppercases the stem to recover
    the operator-facing feedId.
    """
    return f"{feed_id.lower()}.zip"


def staged_filename_for_format(feed_id: str, fmt: str) -> str:
    """Canonical inbox filename for any timetable format (GTFS or NeTEx).

    Stem is the lowercased feed_id (case-insensitive FS safety); extension
    is always .zip — both GTFS and NeTEx ship as ZIP archives. The dispatch
    target subdir is determined by the format (`gtfs/` vs `netex/`), which
    keeps the entrypoint's build-config generator able to assign the right
    OTP `transitFeeds.type` value when scanning each subdir.
    """
    if fmt not in _OTP_TIMETABLE_FORMATS:
        raise ValueError(
            f"Unknown timetable format {fmt!r}. Valid: {sorted(_OTP_TIMETABLE_FORMATS)}"
        )
    return f"{feed_id.lower()}.zip"


# ──────────────────── Provider-bundle schema (v0.1.6) ────────────────────


def normalize_providers(raw_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Coerce `config.sources` into a canonical providers list.

    Three input shapes accepted, in order of precedence:

      A. v0.1.6 native — `sources.providers = [{...}, ...]`. Pass through
         after validation.
      B. v0.1.4 multi-feed — `sources.gtfs = [{id, url}, ...]` plus optional
         `sources.mct`, `sources.stations`. Lifted into one provider per
         GTFS entry; the first provider inherits any session-level mct /
         stations URLs (operator can move them around afterwards).
      C. Pre-v0.1.4 single-feed — `sources.gtfs = "<url>"`. Wrapped into a
         single provider with id="GTFS".

    Raises ValueError on schema problems with a UI-friendly message.

    Each provider in the returned list has:
      - id              str   /^[A-Z][A-Z0-9_-]{1,15}$/   — OTP feedId namespace
      - label           str   operator-facing name (often the railway's brand)
      - country_iso     str|None  uppercase 2-letter ISO; the country-gate
                                  check at session-save time refuses save
                                  when no master_stations rows exist for
                                  the declared country (v0.1.6)
      - timetable       dict   {format: gtfs|netex_nordic|netex_epip, url?: str}
      - gtfs_rt         dict   {alerts_url?, trip_updates_url?, vehicle_positions_url?}
      - mct_url         str|None
      - stations_csv_url str|None

    The reverse direction (legacy → canonical) does NOT mutate operator-
    saved data — it's a runtime convenience. Saving a provider list back
    via PATCH /api/sessions/{sid}/config writes the v0.1.6 shape.
    """
    sources = raw_config.get("sources") or {}
    if not isinstance(sources, dict):
        raise ValueError(f"config.sources must be an object, got {type(sources).__name__}")

    # Path A — v0.1.6 native shape.
    if "providers" in sources:
        raw_providers = sources["providers"]
        if not isinstance(raw_providers, list):
            raise ValueError("config.sources.providers must be a list of provider objects")
        seen_ids: set[str] = set()
        out_providers: list[dict[str, Any]] = []
        for i, p in enumerate(raw_providers):
            cleaned = _validate_provider(p, i)
            if cleaned["id"] in seen_ids:
                raise ValueError(f"provider id {cleaned['id']!r} appears twice")
            seen_ids.add(cleaned["id"])
            out_providers.append(cleaned)
        return out_providers

    # Path B/C — legacy lift. Reuse normalize_gtfs_sources for the GTFS
    # shape coercion (handles both list and string).
    feeds = normalize_gtfs_sources(sources.get("gtfs"))
    if not feeds:
        return []
    providers: list[dict[str, Any]] = []
    for i, feed in enumerate(feeds):
        provider: dict[str, Any] = {
            "id": feed["id"],
            "label": feed["id"],  # operator can rename via UI
            "country_iso": None,
            # Legacy feeds are always URL-sourced — set source explicitly so
            # the canonical shape is uniform (v0.1.37).
            "timetable": {"format": "gtfs", "source": "url", "url": feed["url"]},
            "gtfs_rt": {},
            "mct_url": None,
            "stations_csv_url": None,
        }
        # First provider inherits the session-level mct + stations CSV URLs
        # so a v0.1.4 SNCF session migrates cleanly. Subsequent providers
        # start empty (operator moves them around if they belong elsewhere).
        if i == 0:
            if isinstance(sources.get("mct"), str) and sources["mct"]:
                provider["mct_url"] = sources["mct"]
            if isinstance(sources.get("stations"), str) and sources["stations"]:
                provider["stations_csv_url"] = sources["stations"]
        providers.append(provider)
    return providers


def _validate_cross_border_filter(tt: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate the extra fields a `cross_border_filter` provider carries.

    A derived provider owns no feed: it links to a national provider's slot
    (`derived_from = {session_id, provider_id}`) and stores the filter params.
    Build/refresh runs `filter_to_cross_border` on the linked national feed.
    See docs/provider-source-modes-design.md §12.
    """
    df = tt.get("derived_from")
    if not isinstance(df, dict):
        raise ValueError(
            f"providers[{index}].timetable.derived_from must be an object with "
            "session_id + provider_id (the national feed this is filtered from)"
        )
    src_session = str(df.get("session_id") or "").strip()
    src_provider = str(df.get("provider_id") or "").strip()
    if not src_session:
        raise ValueError(
            f"providers[{index}].timetable.derived_from.session_id is required "
            "(the session whose national feed is filtered)"
        )
    if not _FEED_ID_RE.match(src_provider):
        raise ValueError(
            f"providers[{index}].timetable.derived_from.provider_id={src_provider!r} "
            "must be a provider id (e.g. RENFE) in the source session"
        )

    out: dict[str, Any] = {"derived_from": {"session_id": src_session, "provider_id": src_provider}}

    home_raw = tt.get("home_country")
    if home_raw not in (None, ""):
        home = str(home_raw).strip().upper()
        if not _COUNTRY_ISO_RE.match(home):
            raise ValueError(
                f"providers[{index}].timetable.home_country={home_raw!r} must be a "
                "2-letter ISO code (ES, FR, ...) — origin-country ownership for the filter"
            )
        out["home_country"] = home

    # rail_only defaults True (rail-only cross-border subset); accept explicit bool.
    rail_only = tt.get("rail_only", True)
    if not isinstance(rail_only, bool):
        raise ValueError(f"providers[{index}].timetable.rail_only must be true or false")
    out["rail_only"] = rail_only
    return out


def _validate_provider(raw: object, index: int) -> dict[str, Any]:
    """Validate one provider entry, returning the cleaned dict.

    Run on every save (via the session-config PATCH endpoint) and on
    every read where the migration from legacy shape happens. Strict
    enough to catch operator typos, lenient enough to keep optional
    fields actually optional (gtfs_rt, mct_url, stations_csv_url).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"providers[{index}] must be an object")

    # ── id ────────────────────────────────────────────────────────────
    feed_id = (raw.get("id") or "").strip()
    if not feed_id or not _FEED_ID_RE.match(feed_id):
        raise ValueError(
            f"providers[{index}].id={feed_id!r} must match "
            "/^[A-Z][A-Z0-9_-]{1,15}$/ (e.g. SNCF, IDFM, TRENITALIA, FR-SNCF)"
        )

    # ── label ─────────────────────────────────────────────────────────
    label = str(raw.get("label") or feed_id).strip() or feed_id

    # ── country_iso ──────────────────────────────────────────────────
    country_iso_raw = raw.get("country_iso")
    country_iso: str | None = None
    if country_iso_raw not in (None, ""):
        ci = str(country_iso_raw).strip().upper()
        if not _COUNTRY_ISO_RE.match(ci):
            raise ValueError(
                f"providers[{index}].country_iso={country_iso_raw!r} must be "
                "a 2-letter ISO code (FR, DE, IT, ...)"
            )
        country_iso = ci

    # ── timetable ────────────────────────────────────────────────────
    tt = raw.get("timetable")
    if not isinstance(tt, dict):
        raise ValueError(f"providers[{index}].timetable must be an object with format + url")
    fmt = (tt.get("format") or "").strip().lower()
    if fmt not in _OTP_TIMETABLE_FORMATS:
        raise ValueError(
            f"providers[{index}].timetable.format={fmt!r} must be one of "
            f"{sorted(_OTP_TIMETABLE_FORMATS)} "
            "(NeTEx-FR is intentionally excluded — OTP doesn't read it)"
        )
    url = (tt.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(
            f"providers[{index}].timetable.url={url!r} must be an http(s) URL "
            "(or empty if the operator will upload manually)"
        )
    # source discriminator (v0.1.37). Inferred for legacy configs that
    # predate the field: a URL present means "url", its absence means
    # "upload" (the operator will attach a file). See
    # docs/provider-source-modes-design.md.
    source_raw = (tt.get("source") or "").strip().lower()
    if source_raw and source_raw not in _TIMETABLE_SOURCES:
        raise ValueError(
            f"providers[{index}].timetable.source={source_raw!r} must be one of "
            f"{sorted(_TIMETABLE_SOURCES)}"
        )
    source = source_raw or ("url" if url else "upload")
    timetable: dict[str, Any] = {"format": fmt, "source": source}
    if url:
        timetable["url"] = url
    if source == "cross_border_filter":
        timetable.update(_validate_cross_border_filter(tt, index))

    # ── gtfs_rt (optional) ───────────────────────────────────────────
    gtfs_rt_raw = raw.get("gtfs_rt") or {}
    if not isinstance(gtfs_rt_raw, dict):
        raise ValueError(
            f"providers[{index}].gtfs_rt must be an object "
            "with optional alerts_url / trip_updates_url / vehicle_positions_url"
        )
    gtfs_rt: dict[str, Any] = {}
    for key in ("alerts_url", "trip_updates_url", "vehicle_positions_url"):
        v = (gtfs_rt_raw.get(key) or "").strip()
        if v:
            if not v.startswith(("http://", "https://")):
                raise ValueError(f"providers[{index}].gtfs_rt.{key}={v!r} must be an http(s) URL")
            gtfs_rt[key] = v

    # ── mct_url, stations_csv_url (optional) ─────────────────────────
    def _opt_url(key: str) -> str | None:
        v = (raw.get(key) or "").strip()
        if not v:
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"providers[{index}].{key}={v!r} must be an http(s) URL")
        return v

    # ── credential ids (v0.1.10, optional) ───────────────────────────
    # Each URL field can pair with a `<field>_credential_id` referencing
    # a row in user_credentials. We don't validate the UUID exists here
    # (that's the refresh-time concern; users delete credentials
    # asynchronously). We do validate it's at least UUID-shaped to catch
    # typos early.
    def _opt_cred_id(key: str) -> str | None:
        v = raw.get(key)
        if v is None or v == "":
            return None
        s = str(v).strip()
        # uuid.UUID() raises ValueError on bad input; we wrap it for a
        # field-prefixed message.
        try:
            uuid.UUID(s)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"providers[{index}].{key}={v!r} must be a UUID "
                f"(referencing a row in user_credentials)"
            ) from exc
        return s

    return {
        "id": feed_id,
        "label": label,
        "country_iso": country_iso,
        "timetable": timetable,
        "timetable_credential_id": _opt_cred_id("timetable_credential_id"),
        "gtfs_rt": gtfs_rt,
        # One credential applies to all three GTFS-RT URLs (alerts,
        # trip_updates, vehicle_positions) — they're virtually always on
        # the same domain with the same auth scheme. Per-URL credentials
        # would inflate the schema for negligible flexibility.
        "gtfs_rt_credential_id": _opt_cred_id("gtfs_rt_credential_id"),
        "mct_url": _opt_url("mct_url"),
        "mct_credential_id": _opt_cred_id("mct_credential_id"),
        "stations_csv_url": _opt_url("stations_csv_url"),
        "stations_csv_credential_id": _opt_cred_id("stations_csv_credential_id"),
    }


def dispatch(
    stored_path: Path,
    kind: str,
    db: DbSession,
    *,
    session_id: str | None = None,
    staged_filename: str | None = None,
) -> bool:
    """Move the stored file to its destination. Returns True if a rebuild was queued.

    `staged_filename` overrides the per-kind default (used by multi-feed GTFS
    to land each feed at `inbox/<sid>/gtfs/<feed_id_lower>.zip`). Other GTFS-
    family files (manual upload, single-source refresh) keep the canonical
    `gtfs.zip` name. For non-GTFS kinds the parameter is ignored.

    Rotation policy: when `staged_filename` is None (legacy single-feed flow)
    we rotate every existing non-`.old` file in the subdir to `.old` first —
    same as before. When a per-feed filename is given (multi-feed flow) we
    rotate ONLY the matching feed's prior file, so the other feeds in the
    same session stay live. This is the difference between "I'm replacing
    the only GTFS we have" and "I'm refreshing one of N feeds".
    """
    base = session_inbox(session_id)

    if kind in STAGE_INTO_OTP_INBOX:
        subdir = base / STAGE_INTO_OTP_INBOX[kind]
        subdir.mkdir(parents=True, exist_ok=True)
        target_name = staged_filename or STAGE_INTO_OTP_INBOX_FILENAME[kind]
        if staged_filename is None:
            # Legacy: rotate everything (single-feed sessions). Defensive
            # match against `.old` AND `.old.N` (v0.1.14 introduced numeric
            # OSM rotation generations) so we don't double-rotate already-
            # archived files into `osm.pbf.old.1.old` etc.
            _OLD_RE = re.compile(r"\.old(?:\.\d+)?$")
            for existing in subdir.iterdir():
                if existing.is_file() and not _OLD_RE.search(existing.name):
                    existing.rename(existing.with_suffix(existing.suffix + ".old"))
        else:
            # Multi-feed: rotate only the matching feed's prior file.
            existing_target = subdir / target_name
            if existing_target.exists():
                existing_target.rename(existing_target.with_suffix(existing_target.suffix + ".old"))
        target = subdir / target_name
        shutil.copy2(stored_path, target)
        _enqueue_rebuild(db, session_id=session_id, reason=f"new {kind} uploaded")
        return True

    if kind in ARCHIVE_ONLY:
        archive = base / "archive" / kind
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored_path, archive / stored_path.name)
        return False

    if kind in LOAD_TO_DB:
        runtime = base / "runtime" / kind
        runtime.mkdir(parents=True, exist_ok=True)
        target = runtime / f"latest{stored_path.suffix}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(stored_path, tmp)
        tmp.replace(target)
        return False

    raise ValueError(f"No dispatch rule for kind={kind}")


def _enqueue_rebuild(db: DbSession, *, session_id: str | None, reason: str) -> None:
    """Coalesce: skip if a pending job for the same session already exists."""
    pending = (
        db.query(RebuildJob)
        .filter(RebuildJob.status == "pending")
        .filter(RebuildJob.session_id == session_id)
        .first()
    )
    if pending is not None:
        return
    job = RebuildJob(
        session_id=session_id,
        status="pending",
        log=f"queued at {datetime.now(UTC).isoformat()} — {reason}\n",
    )
    db.add(job)
    db.commit()
