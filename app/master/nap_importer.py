"""Bulk-import providers from a National Access Point (NAP) catalogue.

Operator workflow problem we solve:

    With v0.1.6's per-session multi-provider model, building a "France-wide
    every-rail-operator" demonstrator means clicking "+ Add provider" and
    pasting URLs ~50 times. Worse, those URLs change as publishers
    re-organise their open-data sites — the manual list goes stale fast.

The French NAP at transport.data.gouv.fr exposes a JSON API listing every
public-transport dataset with their canonical resource URLs. Same pattern
exists for German `mobilithek.info`, Swedish Trafiklab, etc. — all use
DCAT-AP-style metadata. This module fetches that catalogue, filters it by
country / mode, picks the best resource per dataset (GTFS preferred for
OTP routing; NeTEx-Nordic / EPIP if no GTFS; NeTEx-FR archive-only
because OTP can't read it), and emits provider entries ready to drop into
`session.config.sources.providers[]`.

Two pure functions for the heart of the logic, easy to unit-test:

    fetch_datasets(nap_url)            — async fetch + cache
    select_resource(dataset)           — pick best resource per dataset
    classify_modes(dataset)            — heuristic mode detection
    make_provider_from_dataset(...)    — build the provider dict

Plus the top-level `import_from_nap()` orchestrator that ties them
together and returns a structured (added, skipped, warnings) result for
the API endpoint to surface in the UI.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Default NAP URL — French National Access Point. Swap for other countries
# (mobilithek.info, samtrafiken.se, etc.) by passing nap_url explicitly.
DEFAULT_FR_NAP_URL = "https://transport.data.gouv.fr/api/datasets"

# Heuristic mode detection — the NAP API doesn't expose modes structurally,
# so we substring-match dataset titles + tags. Lower-cased for case-insens.
# Lists are intentionally permissive to catch operator naming variations.
_MODE_KEYWORDS: dict[str, set[str]] = {
    "rail": {
        # SNCF brands
        "tgv", "ouigo", "intercité", "intercite", "ter ",
        # Operators (in French context)
        "sncf", "trenitalia", "eurostar", "renfe", "thalys", "lyria",
        # Generic French rail terms
        "train", "ferré", "ferrov", "ferroviaire", "ave",
    },
    "urban": {
        # Île-de-France
        "transilien", "rer ", "métro", "metro",
        "idfm", "ile-de-france mobilités", "ile-de-france mobilites",
        # Generic urban transit
        "tramway", "tram ", "réseau urbain", "reseau urbain",
        "ratp", "métropole", "metropole",
    },
    "bus": {
        # Bus / coach
        "bus", "autocar", "interurbain", "flixbus", "blablabus",
        "ouibus", "macron",
    },
    "bike": {
        "vélo", "velo", "vae ", "bicycle", "vls",
    },
}

# Format preference order for OTP routing. The first format on this list
# that a dataset publishes wins. NeTEx-FR is intentionally LAST because
# OTP 2.9 can't read it — but we still emit it as `archive-only` if it's
# the only thing available, so the operator knows the dataset exists.
_FORMAT_PRIORITY: list[str] = [
    "GTFS",
    "NeTEx",  # Often actually Nordic / EPIP — distinguish by schema_name
]

# Mapping of resource format → OTP-side timetable format. We coerce the
# upstream's loose format strings ("GTFS", "gtfs", "GTFS-RT", etc.)
# into the `gtfs|netex_nordic|netex_epip|netex_fr` enum we use internally.
def _normalise_format(fmt: str | None) -> str | None:
    if not fmt:
        return None
    f = fmt.strip().lower()
    if f in ("gtfs", "gtfs-rt", "gtfsrt", "gtfs rt"):
        # GTFS-RT isn't a timetable format on its own — it's a real-time
        # overlay. Skip resources that are RT-only here; the caller handles
        # them via `select_gtfs_rt_urls()`.
        if "rt" in f:
            return None
        return "gtfs"
    if "netex" in f:
        # The NAP doesn't reliably distinguish profiles by `format` alone
        # — it usually says just "NeTEx". The schema_name field (if
        # present) will tell us which profile. Default to "netex_fr" since
        # ~all French NAP NeTEx is the FR profile; callers detecting
        # Nordic/EPIP set it explicitly.
        return "netex_fr"
    return None


def _normalise_country(covered_area: Any) -> str | None:
    """Extract ISO-2 country code from a dataset's `covered_area` field.

    The NAP returns covered_area as either a list `[{type, nom, insee}]`
    or sometimes a single dict. INSEE codes for countries map to ISO-2
    (FR, DE, IT, ES, ...). Multi-country datasets get the first one;
    operators can rename in the UI afterwards.
    """
    if isinstance(covered_area, list) and covered_area:
        first = covered_area[0]
    elif isinstance(covered_area, dict):
        first = covered_area
    else:
        return None
    if not isinstance(first, dict):
        return None
    insee = (first.get("insee") or "").strip().upper()
    if len(insee) == 2 and insee.isalpha():
        return insee
    return None


def classify_modes(dataset: dict[str, Any]) -> set[str]:
    """Return the set of modes this dataset appears to cover.

    Heuristic — substring-matches the dataset's title against per-mode
    keyword lists. False positives are tolerable (operator can de-select);
    false negatives are not (a real rail dataset slipping through the
    filter would mean missing data). So lists err on the permissive side.

    A dataset returning {"rail", "urban"} would match both `rail` and
    `urban` mode filters. The empty set means no recognised mode — caller
    skips when filtering by mode.
    """
    haystack = " ".join([
        (dataset.get("title") or "").lower(),
        (dataset.get("slug") or "").lower(),
        " ".join((tag or "").lower() for tag in (dataset.get("tags") or [])),
    ])
    modes: set[str] = set()
    for mode, keywords in _MODE_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            modes.add(mode)
    return modes


def select_resource(dataset: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the best routing resource from a dataset's `resources[]` array.

    Returns (resource_dict, vita_format) where vita_format is one of:
        gtfs, netex_nordic, netex_epip, netex_fr, None.

    Selection order:
      1. GTFS (any updated copy) — best for OTP, ratified format
      2. NeTEx with `schema_name` indicating Nordic / EPIP → those profiles
         OTP CAN read
      3. NeTEx without profile metadata → assume NeTEx-FR (most common on
         the French NAP) and return as `netex_fr` so the caller can decide
         to skip it (current default — OTP can't route NeTEx-FR)

    Returns (None, None) when no usable resource exists. Skips RT-only
    feeds (those are wired separately via gtfs_rt fields, not as the
    timetable resource).
    """
    resources = dataset.get("resources") or []
    if not isinstance(resources, list):
        return (None, None)

    # First pass: prefer GTFS, most-recently-updated.
    gtfs_candidates = []
    netex_candidates = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        fmt = _normalise_format(r.get("format"))
        if fmt == "gtfs":
            gtfs_candidates.append(r)
        elif fmt and fmt.startswith("netex"):
            netex_candidates.append(r)

    if gtfs_candidates:
        # Pick the most-recently-updated GTFS resource (the publisher
        # sometimes lists multiple, e.g. snapshot vs current).
        best = max(gtfs_candidates, key=lambda r: r.get("updated") or "")
        return (best, "gtfs")

    if netex_candidates:
        # Look at schema_name to disambiguate Nordic / EPIP / FR profiles.
        for r in netex_candidates:
            schema = (r.get("schema_name") or "").lower()
            if "nordic" in schema:
                return (r, "netex_nordic")
            if "epip" in schema:
                return (r, "netex_epip")
        # No profile metadata — assume NeTEx-FR (most common on French
        # NAP). Caller handles archive-only behaviour.
        best = max(netex_candidates, key=lambda r: r.get("updated") or "")
        return (best, "netex_fr")

    return (None, None)


def select_gtfs_rt_urls(dataset: dict[str, Any]) -> dict[str, str]:
    """Pick GTFS-RT URLs out of a dataset's resources, by type.

    Returns a dict like {"alerts_url": "https://...", "trip_updates_url": "..."}
    with whichever GTFS-RT resources we can identify. The NAP's
    `format` is usually "gtfs-rt" with no further breakdown; we
    sub-classify by URL keyword (alerts / trip-updates / vehicle-positions).
    Empty dict if none.
    """
    out: dict[str, str] = {}
    for r in dataset.get("resources") or []:
        if not isinstance(r, dict):
            continue
        fmt = (r.get("format") or "").strip().lower()
        if "rt" not in fmt:
            continue
        url = r.get("url") or ""
        url_lower = url.lower()
        if "alert" in url_lower and "alerts_url" not in out:
            out["alerts_url"] = url
        elif "trip" in url_lower and "trip_updates_url" not in out:
            out["trip_updates_url"] = url
        elif ("vehicle" in url_lower or "position" in url_lower) and "vehicle_positions_url" not in out:
            out["vehicle_positions_url"] = url
    return out


_PROVIDER_ID_RE = re.compile(r"[A-Z][A-Z0-9_-]{1,15}")


def slug_to_provider_id(text: str, *, existing: set[str] | None = None) -> str:
    """Generate a stable, regex-compliant provider id from a publisher name
    or dataset title.

    Tries in order:
      1. The first all-caps token (e.g. "SNCF Voyageurs" → "SNCF")
      2. Stripped-uppercased title with non-conformant chars dropped
      3. A truncated/hashed fallback if nothing else works

    Deduplicates against `existing` by appending `-2`, `-3`, ... until
    unique. Returns empty string only on truly pathological input.
    """
    existing = existing or set()
    text = (text or "").strip()
    if not text:
        return ""

    # Try 1: first all-caps token in the source text (e.g. SNCF, IDFM, RATP).
    for token in re.findall(r"[A-Z][A-Z0-9_-]+", text):
        candidate = token[:16]
        if _PROVIDER_ID_RE.fullmatch(candidate):
            return _dedupe(candidate, existing)

    # Try 2: build from full text by upper-casing + filtering allowed chars.
    cleaned = re.sub(r"[^A-Z0-9_-]", "", text.upper())
    if cleaned and len(cleaned) >= 2:
        candidate = cleaned[:16]
        if _PROVIDER_ID_RE.fullmatch(candidate):
            return _dedupe(candidate, existing)

    return ""


def _dedupe(candidate: str, existing: set[str]) -> str:
    """Append `-2`, `-3`… until the candidate doesn't collide with existing."""
    if candidate not in existing:
        return candidate
    i = 2
    while True:
        # Reserve room for the suffix; trim base to keep within 16 chars.
        suffix = f"-{i}"
        base = candidate[: 16 - len(suffix)]
        attempt = f"{base}{suffix}"
        if attempt not in existing:
            return attempt
        i += 1
        if i > 99:
            # Pathological — give up rather than spinning forever.
            return ""


def make_provider_from_dataset(
    dataset: dict[str, Any],
    *,
    default_country: str | None = None,
    existing_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Translate one NAP dataset into one VIATOR provider entry.

    Returns the provider dict ready to append to `session.config.sources.
    providers[]`, OR None if the dataset has no usable resource (e.g.
    documentation-only, or only NeTEx-FR which we treat as archive-only).

    The caller is responsible for actually merging into the session config
    + handling duplicates by URL.
    """
    resource, fmt = select_resource(dataset)
    if resource is None or fmt is None:
        return None
    if fmt == "netex_fr":
        # OTP can't read NeTEx-FR — log + skip. Caller surfaces in `warnings`.
        return None

    publisher = (dataset.get("publisher") or {}).get("name") or ""
    title = dataset.get("title") or ""
    pid = slug_to_provider_id(publisher, existing=existing_ids or set()) \
        or slug_to_provider_id(title, existing=existing_ids or set())
    if not pid:
        return None

    country = _normalise_country(dataset.get("covered_area")) or default_country

    provider: dict[str, Any] = {
        "id": pid,
        "label": title or publisher or pid,
        "country_iso": country,
        "timetable": {"format": fmt, "url": resource.get("url") or ""},
        "gtfs_rt": select_gtfs_rt_urls(dataset),
    }
    return provider


# ───────────────────── module-level cache ─────────────────────
# Keyed by NAP URL → (timestamp, list-of-datasets). 5-minute TTL keeps the
# UI snappy (preview + actual import are usually <1 min apart) without
# exposing operators to stale data over hours. The cache lives in-process;
# the worker and web containers each maintain their own copy.

_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def fetch_datasets(nap_url: str = DEFAULT_FR_NAP_URL) -> list[dict[str, Any]]:
    """Fetch the NAP catalogue, with a 5-minute in-process cache.

    Returns the raw list of dataset dicts. No filtering happens here — the
    caller does it client-side via `classify_modes()` etc.

    Raises httpx.HTTPError on network failure. The /api/datasets endpoint
    can return MB of JSON for a national NAP; cache prevents us hammering
    it on every preview-then-confirm cycle.
    """
    now = time.time()
    cached = _cache.get(nap_url)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    log.info("fetching NAP catalogue from %s", nap_url)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(nap_url)
        r.raise_for_status()
        data = r.json()

    # Some NAPs return a top-level array, others wrap in {"data": [...]}.
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        datasets = data["data"]
    elif isinstance(data, list):
        datasets = data
    else:
        raise ValueError(f"NAP API at {nap_url} returned unexpected shape: {type(data).__name__}")

    _cache[nap_url] = (now, datasets)
    return datasets


# ───────────────────── top-level orchestrator ─────────────────────


async def import_from_nap(
    *,
    existing_providers: list[dict[str, Any]],
    nap_url: str = DEFAULT_FR_NAP_URL,
    country: str | None = None,
    modes: list[str] | None = None,
    include_publishers: list[str] | None = None,
    exclude_dataset_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch the NAP catalogue, filter, build providers, dedupe against existing.

    Returns:
        {
            "providers": [<new providers ready to merge into session config>],
            "skipped": [{"dataset": "name", "reason": "..."}, ...],
            "warnings": ["..."]
        }

    Caller is responsible for actually persisting `providers` into the
    session config (and triggering the staleness banner). This split keeps
    the importer pure — it builds the proposed list, the API endpoint
    decides whether to commit.

    Filters:
        country     ISO-2 — keep only datasets whose covered_area starts
                    with this code. None means no country filter.
        modes       List of {rail, urban, bus, bike}. A dataset matches
                    if any of its detected modes intersects this list.
                    None means no mode filter.
        include_publishers  Optional whitelist of publisher names (substring
                    match, case-insensitive). Use to limit to specific
                    operators (e.g. ["SNCF", "IDFM"]).
        exclude_dataset_ids Optional skip list of dataset IDs.

    The dedupe pass compares against existing_providers' timetable URLs +
    provider ids. A dataset whose URL is already in the session is silently
    skipped (no warning — that's the desired behaviour on re-imports).
    """
    datasets = await fetch_datasets(nap_url)

    existing_urls = {
        (p.get("timetable") or {}).get("url")
        for p in existing_providers
        if isinstance(p, dict)
    }
    existing_ids = {
        p.get("id") for p in existing_providers if isinstance(p, dict) and p.get("id")
    }

    new_providers: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        title = ds.get("title") or "(untitled)"
        ds_id = ds.get("id") or ""

        # Filter: dataset id exclude list.
        if exclude_dataset_ids and ds_id in exclude_dataset_ids:
            skipped.append({"dataset": title, "reason": "explicitly excluded"})
            continue

        # Filter: country.
        if country:
            ds_country = _normalise_country(ds.get("covered_area"))
            if ds_country and ds_country != country:
                # Different country — silently skip; common for national NAPs
                # to host cross-border datasets we don't want.
                continue

        # Filter: mode keywords.
        if modes:
            ds_modes = classify_modes(ds)
            if not (ds_modes & set(modes)):
                # No mode match — silently skip (would be noisy if logged).
                continue

        # Filter: publisher whitelist.
        if include_publishers:
            pub_name = ((ds.get("publisher") or {}).get("name") or "").lower()
            if not any(p.lower() in pub_name for p in include_publishers):
                continue

        # Build the provider candidate.
        provider = make_provider_from_dataset(
            ds,
            default_country=country,
            existing_ids=existing_ids | {p["id"] for p in new_providers},
        )
        if provider is None:
            # Inspect why it's None to give a useful warning.
            resource, fmt = select_resource(ds)
            if resource is None:
                skipped.append({"dataset": title, "reason": "no GTFS or NeTEx resource"})
            elif fmt == "netex_fr":
                skipped.append({"dataset": title, "reason": "NeTEx-FR only (OTP can't read it)"})
                warnings.append(
                    f"{title}: only NeTEx-FR available — archive-only, can't be used "
                    "for routing. Operator publishes no GTFS for this dataset."
                )
            else:
                skipped.append({"dataset": title, "reason": "couldn't generate provider id"})
            continue

        # Dedupe against existing session providers.
        if provider["timetable"]["url"] in existing_urls:
            skipped.append({"dataset": title, "reason": "already in session (same URL)"})
            continue
        if provider["id"] in existing_ids:
            # ID collision but URL is different — could happen if same
            # publisher publishes multiple datasets. Append numeric suffix.
            new_id = _dedupe(provider["id"], existing_ids | {p["id"] for p in new_providers})
            if new_id:
                provider["id"] = new_id
            else:
                skipped.append({"dataset": title, "reason": "couldn't dedupe provider id"})
                continue

        new_providers.append(provider)

    return {
        "providers": new_providers,
        "skipped": skipped,
        "warnings": warnings,
    }
