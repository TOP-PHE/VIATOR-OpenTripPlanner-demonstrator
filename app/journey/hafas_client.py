"""Async journey-level client for ÖBB's public HAFAS endpoint.

VIATOR's own routing runs on OpenTripPlanner (see `otp_client.py`); this
module talks to an *external* HAFAS backend (`fahrplan.oebb.at/bin/
mgate.exe`) so the journey UI can show ÖBB's reference engine side-by-
side with VIATOR's own results, as a validation oracle on the broader
DACH + cross-border + Eurostar/TGV/AVE/Iberian + Nordic-cross-border
network footprint Swiss OJP doesn't cover. See the module docstring on
`app/network_coverage/external_verify.py` for the 43-pair empirical
coverage validation that motivated picking ÖBB HAFAS as the reference.

This module is the **journey-level** counterpart of the coverage-cell
verify path. The HTTP plumbing (LocGeoPos → TripSearch two-step,
header set, error-code translation) lives in `external_verify` and is
shared verbatim — this file is the trip-dict normaliser layer that
turns HAFAS's `outConL[].secL[]` shape into VIATOR's canonical trip
dict (the same shape `otp_client._normalise` / `motis_client.
_itineraries_to_trips` / `ojp_client._normalise` emit). Once a HAFAS
connection has been normalised this way, the comparison panel renders
it through the same code path as the other engines.

Why a journey-level adapter on top of a coverage adapter
--------------------------------------------------------
`external_verify.verify_via_oebb_hafas` was built for the click-to-
verify modal on `no_route` cells: it asks "does ÖBB find anything?"
and returns a single yes/no `VerifyResult`. Journey-search reuses the
same backend but needs the full itinerary list (durations, transfers,
legs with operator badges, intermediate-station detail). PR refactor
extracted `external_verify.fetch_oebb_two_step` which returns both the
verdict *and* the parsed payload; this module consumes that payload.

Contract mirrors the other planner clients
------------------------------------------
`fetch_plan(...)` returns `(raw, trips)` where `trips` is the list of
canonical trip dicts. Transport / HTTP errors are mapped to an empty
trip list (HAFAS errors are returned via the `verdict.error` field
inside the raw payload, never raised) — the caller decides whether to
surface them as "unavailable" or "no_route" by checking `raw.status`.
PR-3's `first_transit_leg_departure_utc` is included so cross-engine
dedup / day-window filtering treats HAFAS itineraries identically to
the others.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..network_coverage import external_verify
from .canonical import CanonicalJourney
from .canonical_adapters import hafas_adapter
from .canonical_adapters.trip_dict import to_trip_dicts
from .trip_normalize import dedup_batch_and_track_latest_dep as _dedup_batch_and_track_latest_dep
from .trip_normalize import next_anchor_or_none as _next_anchor_or_none
from .trip_normalize import renumbered

log = logging.getLogger(__name__)


# HAFAS times are expressed in the operator's local timezone (ÖBB =
# Europe/Vienna). The journey UI's `datetime-local` input is naive, so a
# naive `when` is localised against this zone for the TripSearch's
# `outDate`/`outTime`. Trip-leg `departure` / `arrival` ISO strings
# coming back out of the normaliser are converted to UTC for consistency
# with the other clients.
_REFERENCE_TZ = "Europe/Vienna"


# ─────────────────────── public API ───────────────────────


async def fetch_plan(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int = 10_000,
    num_itineraries: int = 6,
    from_name: str | None = None,
    to_name: str | None = None,
) -> tuple[dict[str, Any], list[CanonicalJourney]]:
    """Call ÖBB HAFAS and return `(raw, trips)`.

    Mirrors `ojp_client.fetch_reference`'s signature — the journey API
    layer uses both interchangeably under different operator toggles.

    `raw` carries a small diagnostic dict (`format`, `status`,
    `from_lid`, `to_lid`, `error`) so the comparison panel can show
    "Reference (ÖBB HAFAS) · N itineraries · Mms" with proper status
    differentiation. The full HAFAS payload is intentionally NOT
    persisted — like OJP, this is a live-display-only comparison.

    Never raises: transport / parse failures land in `raw.status` and
    `raw.error`; `trips` comes back as `[]` in that case.
    """
    start = time.monotonic()
    # `num_itineraries` and `from_name` / `to_name` are accepted for
    # signature parity with the other journey clients but not threaded
    # all the way through yet — HAFAS's TripSearch returns ~5 by default
    # and naming is informational on the request only. They could be
    # plumbed in a future PR; for now Sonar's S1172 is suppressed by
    # explicit `_ = …` to keep the silence intentional.
    _ = num_itineraries, from_name, to_name

    depart_at = _localise_when(when)
    # Honour the operator-provided timeout (config_schema HAFAS_TIMEOUT_MS,
    # default 10s) — construct a client with that ceiling and hand it to the
    # two-step adapter. Without this the adapter falls back to its module-
    # local 30s constant, which is fine for coverage verification but too
    # generous for live-UI journey comparison.
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_ms / 1000.0)) as client:
        out = await external_verify.fetch_oebb_two_step(
            from_lat=from_lat,
            from_lon=from_lon,
            to_lat=to_lat,
            to_lon=to_lon,
            depart_at=depart_at,
            client=client,
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    raw: dict[str, Any] = {
        "format": "hafas-mgate",
        "from_lid": out.from_lid,
        "to_lid": out.to_lid,
        "response_ms": elapsed_ms,
    }
    verdict = out.verdict
    if verdict.error:
        # Network / parse / envelope failure — surface the error and
        # return no trips. The journey API maps `status='error'` and
        # the comparison panel renders the friendly message.
        raw["status"] = "error"
        raw["error"] = verdict.error
        return raw, []
    if not verdict.ok or out.payload is None:
        # Clean "no connections" answer (HAFAS H890 or empty outConL).
        raw["status"] = "no_route"
        return raw, []

    trips = hafas_adapter.journeys_from_trip_search(out.payload)
    raw["status"] = "ok" if trips else "no_route"
    return raw, trips


async def fetch_plan_paginated(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    when: datetime,
    timeout_ms: int = 10_000,
    from_name: str | None = None,
    to_name: str | None = None,
    target_window_seconds: int = 21600,
    max_pages: int = 4,
    total_timeout_ms: int = 20_000,
) -> tuple[dict[str, Any], list[CanonicalJourney]]:
    """Call `fetch_plan` repeatedly with successively later anchor times
    until HAFAS's coverage matches `target_window_seconds`, HAFAS runs
    out of connections, `total_timeout_ms` is spent, or `max_pages` is
    reached. Same `(raw, trips)` contract as `fetch_plan` —
    `raw['response_ms']` is summed across pages and `raw['pages']` is
    set when more than one page fired.

    **Why this exists** (v0.1.45). HAFAS's TripSearch is hardcoded to
    `numF=5` (see `external_verify._build_trip_search_body`) — the next
    5 connections from ONE anchor time, not a time span. An operator
    comparing the ÖBB panel against MOTIS/OTP's much wider
    `searchWindow` sees HAFAS's reference stop hours earlier than
    VIATOR's own results, which reads as "ÖBB found far less" when
    really it was only ever asked about a narrow slice of the day.

    Mirrors `ojp_client.fetch_reference_paginated` (identical problem,
    identical fix) — see that function's docstring for the general
    anchor-pagination algorithm; the per-page loop here is HAFAS-
    specific because `fetch_plan` never raises (errors surface via
    `raw['status']`), unlike `ojp_client.fetch_reference` which can
    raise `httpx.HTTPError` mid-flight.

    `total_timeout_ms` bounds the WHOLE paginated call, not each page.
    Without it a slow-but-not-erroring HAFAS could burn `max_pages` x
    `timeout_ms` (4 x 10 s = 40 s) while the journey concurrency
    semaphore is held — `/fanout` would stall far past the single-page
    latency callers were sized for. Each page's own timeout is clamped
    to the remaining budget so the total is a real ceiling.

    Stop conditions, evaluated each page:
    - **empty batch** (`no_route` or `error` status) → HAFAS exhausted
      or failed at this anchor; return whatever was collected so far.
      A page-2+ `error` is surfaced as `raw['partial']` + `raw['error']`
      rather than silently masquerading as a clean end-of-results.
    - **all trips were duplicates of earlier pages** → no forward
      progress, bail (boundary collision only).
    - **latest `departure_at` >= `when + target_window_seconds`** →
      coverage caught up to the target.
    - **budget spent** → `raw['pagination_stopped'] = 'budget'`.
    - **`max_pages` reached** → hard cap (default 4 pages x 5 results
      = 20 trips max, same ceiling as OJP's pagination).
    """
    target_end_ts = when.timestamp() + target_window_seconds
    anchor = when
    all_trips: list[CanonicalJourney] = []
    seen_fps: set[str] = set()
    total_ms = 0
    pages = 0
    first_raw: dict[str, Any] | None = None
    page_error: str | None = None
    stopped_reason: str | None = None
    started = time.monotonic()
    # dict[str, Any]: heterogeneous kwargs (floats, int, str|None) that
    # mypy can't unpack into fetch_plan's typed params without this.
    fetch_kwargs: dict[str, Any] = {
        "from_lat": from_lat,
        "from_lon": from_lon,
        "to_lat": to_lat,
        "to_lon": to_lon,
        "from_name": from_name,
        "to_name": to_name,
    }

    for _ in range(max_pages):
        page_timeout_ms = _page_timeout_or_none(
            pages=pages, started=started, total_timeout_ms=total_timeout_ms, timeout_ms=timeout_ms
        )
        if page_timeout_ms is None:
            stopped_reason = "budget"
            break

        pages += 1
        raw, batch = await fetch_plan(when=anchor, timeout_ms=page_timeout_ms, **fetch_kwargs)
        total_ms += int(raw.get("response_ms") or 0)
        if first_raw is None:
            first_raw = raw
        if not batch:
            page_error = _page_error_or_none(raw, all_trips, pages)
            break

        new_trips, latest_dep_ts = _dedup_batch_and_track_latest_dep(batch, seen_fps)
        if not new_trips:
            break  # all-dups -> no forward progress
        all_trips.extend(new_trips)
        next_anchor = _next_anchor_or_none(latest_dep_ts, target_end_ts)
        if next_anchor is None:
            break
        anchor = next_anchor

    return (
        _paginated_raw(
            first_raw=first_raw,
            total_ms=total_ms,
            pages=pages,
            page_error=page_error,
            stopped_reason=stopped_reason,
            has_trips=bool(all_trips),
        ),
        renumbered(all_trips),
    )


# Below this, a page has too little budget left to be worth issuing
# (HAFAS's two-step needs a LocGeoPos round-trip before TripSearch).
_MIN_PAGE_BUDGET_MS = 500


def _page_timeout_or_none(
    *, pages: int, started: float, total_timeout_ms: int, timeout_ms: int
) -> int | None:
    """This page's timeout in ms, or None when the overall budget is spent.

    Page 1 always runs — callers expect at least one attempt, so it gets
    a usable timeout even with a pathologically small `total_timeout_ms`.
    Every later page must fit inside what's left, which is what makes
    `total_timeout_ms` a real ceiling rather than a per-page hint.
    """
    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = total_timeout_ms - elapsed_ms
    if pages and remaining_ms < _MIN_PAGE_BUDGET_MS:
        return None
    return max(_MIN_PAGE_BUDGET_MS, min(timeout_ms, remaining_ms))


def _page_error_or_none(
    raw: dict[str, Any], all_trips: list[CanonicalJourney], pages: int
) -> str | None:
    """Distinguish "HAFAS cleanly ran out of connections" from "a later
    page blew up".

    The latter means our coverage is silently short of the target window,
    and the operator comparing column depths deserves to know. A page-1
    failure needs no special handling — `first_raw` already carries its
    error status through to the caller.
    """
    if raw.get("status") != "error" or not all_trips:
        return None
    page_error = str(raw.get("error") or "HAFAS pagination failed mid-flight")
    log.warning(
        "HAFAS pagination stopped at page %d (kept %d trips so far): %s",
        pages,
        len(all_trips),
        page_error,
    )
    return page_error


def _paginated_raw(
    *,
    first_raw: dict[str, Any] | None,
    total_ms: int,
    pages: int,
    page_error: str | None,
    stopped_reason: str | None,
    has_trips: bool,
) -> dict[str, Any]:
    """Assemble the paginated call's `raw` diagnostic dict from page 1's
    raw plus the whole run's bookkeeping."""
    raw_out = dict(first_raw or {})
    raw_out["response_ms"] = total_ms
    raw_out["status"] = "ok" if has_trips else raw_out.get("status", "no_route")
    if pages > 1:
        raw_out["pages"] = pages
    if page_error:
        raw_out["partial"] = True
        raw_out["error"] = page_error
    if stopped_reason:
        raw_out["pagination_stopped"] = stopped_reason
    return raw_out


# ─────────────────────── request-side helpers ───────────────────────


def _localise_when(when: datetime) -> datetime:
    """Render `when` as a Europe/Vienna-local datetime for HAFAS's TripSearch.

    `external_verify._build_trip_search_body` serialises `outDate` /
    `outTime` with bare `strftime("%Y%m%d")` / `strftime("%H%M%S")` —
    no UTC offset reaches the wire, and HAFAS interprets those fields
    in the operator's own zone (Europe/Vienna for ÖBB; see that
    function's docstring). So whatever wall-clock this returns IS what
    HAFAS searches for, regardless of the datetime's tzinfo.

    Two input shapes, both normalised to Vienna wall-clock:

    - **naive** (the journey UI's `datetime-local`) — the operator
      typed a local time; attach Vienna so "pick 12:51" searches 12:51.
    - **tz-aware** (`datetime.now(UTC)` from `_resolve_when`, and every
      pagination anchor from `trip_normalize.next_anchor_or_none`, which
      returns UTC) — CONVERT to Vienna so the same *instant* is
      searched. Passing a UTC-aware datetime through unchanged used to
      send UTC wall-clock mislabelled as Vienna local, shifting every
      search by the live offset (-2 h in CEST). Pagination made this
      acute: each anchor is derived from the previous page's UTC
      departure times, so the skew compounded across pages.

    Falls back to UTC if the zone fails to load (shouldn't happen on a
    normal OS — see ojp_client for the matching pattern).
    """
    tz: Any = UTC
    try:
        tz = ZoneInfo(_REFERENCE_TZ)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover
        log.warning("could not load %s — using UTC for HAFAS TripSearch", _REFERENCE_TZ)
    if when.tzinfo is not None:
        return when.astimezone(tz)
    return when.replace(tzinfo=tz)


# ─────────────────────── response normaliser ───────────────────────


def _normalise_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Today's trip dicts for a TripSearch answer, built through the canonical adapter
    (`canonical_adapters.hafas_adapter`, shared with coverage) and the temporary `trip_dict`
    bridge (#303 PR 3a). Defensive like the adapter: a malformed answer gives `[]`."""
    return to_trip_dicts(hafas_adapter.journeys_from_trip_search(payload))
