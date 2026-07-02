"""Async coverage runner — iterates pairs, persists results, updates
the run's progress counters (v0.1.27).

Bounded concurrency: pairs run 5-at-a-time by default. Sequential is
clean and predictable but takes ~42 minutes for 506 directional pairs;
5-way parallelism cuts that to ~10 min while staying well below OTP's
internal queue depth and our `MAX_CONCURRENT_REBUILDS` semaphore (which
isn't on the journey path, but we want to leave headroom for live
journey searches submitted by operators in parallel).

The runner reuses `app.journey.planner_dispatch` to call the engine-
appropriate `fetch_plan` (OTP or MOTIS) — same plumbing the live journey
UI uses. Each pair is recorded into the
existing `journey_searches` + `journey_search_executions` +
`journey_trips` infrastructure so the v0.1.26 trip-card UI works as the
click-cell drilldown out of the box.

State machine:
  pending (created)
    → running (background task started)
      → completed (all pairs processed)
      | failed (unexpected exception escaped the loop)
      | cancelled (operator stopped via API — not yet wired in v0.1.27)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from .. import config_service
from ..db import SessionLocal
from ..journey import planner_dispatch, recorder
from ..models import (
    JourneySearchExecution,
    JourneyTrip,
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ..models import Session as SessionRow
from ..models.sessions import SessionState
from . import external_verify  # PR-E — auto-verify-on-completion sweep
from .alignment import compute_alignment  # PR-196a — graduated heatmap scorer
from .hubs import Hub  # static HUBS used as fallback inside _load_active_hubs

# PR #36 — valid coverage-run modes.
MODE_SINGLE_SESSION = "single_session"
MODE_FANOUT = "fanout"
VALID_MODES = (MODE_SINGLE_SESSION, MODE_FANOUT)

# Placeholder label stored on `network_coverage_runs.session_label` for
# fanout runs (which have no single session to label). The sidebar /
# matrix UI renders this verbatim, so it doubles as a "this is a fanout
# run" badge.
FANOUT_SESSION_LABEL = "fanout"

log = logging.getLogger(__name__)

# ─────────────────── operator-tunable knobs (platform_config) ───────────────────
#
# The seven runner knobs below live in `app.config_schema.CONFIG_SCHEMA`
# (keys `COVERAGE_*`) so operators can tune them from /admin/config
# without code changes or redeploys.
#
# At the start of every execute_run() we snapshot the current DB values
# into a `CoverageConfig` and thread it through every helper. The
# snapshot is FROZEN for the run's lifetime — editing /admin/config
# mid-run does not perturb the in-flight job (avoids the
# "half-the-pairs-used-old-timeout" failure mode that would otherwise
# make post-mortems painful).
#
# Defaults below are bit-identical to the prior hardcoded module
# constants — see the design history baked into each field's comment.
# `CONFIG_SCHEMA` enforces the same bounds.
#
# `_VERIFY_STATUSES` is intentionally NOT tunable — it's part of the
# behaviour contract with the matrix UI (which cell colourings get the
# external-verify treatment), not an operational knob.

# v0.1.29.2 background on `num_itineraries` / `search_window_seconds`:
# Originally shipped as 50/24h in v0.1.29 to give "full-day visibility
# per pair" but that exceeded OTP's 60s apiProcessingTimeout for
# long-haul pairs on a France-wide multi-NAP graph (RAPTOR's per-call
# work scales near-quadratically with searchWindow on dense networks).
# v0.1.29.2 reduces the window to 4h (matching the v0.1.27 baseline
# that completed cleanly) but keeps numItineraries at 50 — so we still
# get ALL alternatives within a 4-hour departure window. Operators
# who tune the window up should expect ~quadratic wallclock growth.

# Module-level defaults — also serve as the fallback CoverageConfig used
# by unit tests that exercise the per-pair helpers directly without
# standing up a DB. The keep-as-default approach lets us refactor
# without breaking the existing patch-the-module-constant idiom in
# test_coverage_external_verify_sweep.py.
_DEFAULT_PAIR_PARALLELISM = 5
_DEFAULT_PER_PAIR_TIMEOUT_MS = 60_000
_DEFAULT_COVERAGE_NUM_ITINERARIES = 50
_DEFAULT_COVERAGE_SEARCH_WINDOW_SECONDS = 14_400  # 4h
_DEFAULT_VERIFY_PARALLELISM = 2
_DEFAULT_VERIFY_TIMEOUT_S = 30.0
_DEFAULT_VERIFY_SLEEP_BETWEEN_MS = 500

# PR-3 — K-slot time-slicing defaults. K=6 with a 24h window puts a
# slot every 4 hours, matching the legacy single-call window size so
# each slot's RAPTOR work is comparable to the v0.1.29.2 baseline.
# numItineraries=10 per slot caps a single slot at 10x what the live
# UI uses — keeps each call well under OTP's 60s ceiling on dense
# graphs even when the slot lands on a busy commuter peak.
_DEFAULT_SLOT_COUNT = 6
_DEFAULT_NUM_ITINERARIES_PER_SLOT = 10
_DEFAULT_SLOT_TIMEOUT_MS = 20_000
_DEFAULT_WITHIN_PAIR_PARALLELISM = 3
_DEFAULT_WINDOW_START_LOCAL = "00:00"
_DEFAULT_WINDOW_END_LOCAL = "24:00"
_DEFAULT_WINDOW_TIMEZONE = "UTC"

# "24:00" sentinel — accepted as the day-window upper bound so the form
# can offer a full-day default without the operator typing "00:00" and
# losing the "ends at midnight" semantic. Translated to (next_day,
# 00:00) at execute time so the actual UTC slot grid does the right
# thing.
_END_OF_DAY_SENTINEL = "24:00"

# Cell statuses that trigger an external-verify pass. 'ok' is
# excluded because we don't need ÖBB to confirm a route VIATOR
# already returned. 'skipped' is excluded because those cells never
# actually got queried — no signal in asking ÖBB. 'no_route' is the
# canonical click-to-verify case; 'timeout' and 'error' are added
# in PR-E so a flaky OTP run can be disambiguated from real gaps.
# Intentionally NOT operator-tunable — see module comment.
_VERIFY_STATUSES = ("no_route", "timeout", "error")


@dataclass(frozen=True)
class CoverageConfig:
    """Snapshot of operator-tunable runner knobs, read once at
    execute_run start and frozen for the run's lifetime.

    Every field maps 1:1 to a `COVERAGE_*` key in
    `app.config_schema.CONFIG_SCHEMA`. Defaults on this dataclass match
    the schema defaults (= the prior hardcoded module constants), so
    constructing `CoverageConfig()` with no DB is a valid fallback —
    used by unit tests that exercise per-pair helpers in isolation.
    """

    num_itineraries: int = _DEFAULT_COVERAGE_NUM_ITINERARIES
    search_window_seconds: int = _DEFAULT_COVERAGE_SEARCH_WINDOW_SECONDS
    pair_timeout_ms: int = _DEFAULT_PER_PAIR_TIMEOUT_MS
    pair_parallelism: int = _DEFAULT_PAIR_PARALLELISM
    verify_parallelism: int = _DEFAULT_VERIFY_PARALLELISM
    verify_timeout_s: float = _DEFAULT_VERIFY_TIMEOUT_S
    verify_sleep_ms: int = _DEFAULT_VERIFY_SLEEP_BETWEEN_MS
    # PR-3 — K-slot time-slicing.
    slot_count: int = _DEFAULT_SLOT_COUNT
    num_itineraries_per_slot: int = _DEFAULT_NUM_ITINERARIES_PER_SLOT
    slot_timeout_ms: int = _DEFAULT_SLOT_TIMEOUT_MS
    within_pair_parallelism: int = _DEFAULT_WITHIN_PAIR_PARALLELISM
    default_window_start: str = _DEFAULT_WINDOW_START_LOCAL
    default_window_end: str = _DEFAULT_WINDOW_END_LOCAL
    default_timezone: str = _DEFAULT_WINDOW_TIMEZONE


def _load_coverage_config(db: DbSession) -> CoverageConfig:
    """Read every `COVERAGE_*` platform_config row into a frozen
    CoverageConfig. Falls back to dataclass defaults for any key that
    config_service can't resolve (defence in depth — schema defaults
    already cover the missing-row case, but a typed gap here would
    cascade into a runtime crash in the per-pair loop)."""
    cfg = config_service.get_all(db)
    return CoverageConfig(
        num_itineraries=int(cfg.get("COVERAGE_NUM_ITINERARIES", _DEFAULT_COVERAGE_NUM_ITINERARIES)),
        search_window_seconds=int(
            cfg.get(
                "COVERAGE_SEARCH_WINDOW_SECONDS",
                _DEFAULT_COVERAGE_SEARCH_WINDOW_SECONDS,
            )
        ),
        pair_timeout_ms=int(cfg.get("COVERAGE_PAIR_TIMEOUT_MS", _DEFAULT_PER_PAIR_TIMEOUT_MS)),
        pair_parallelism=int(cfg.get("COVERAGE_PAIR_PARALLELISM", _DEFAULT_PAIR_PARALLELISM)),
        verify_parallelism=int(cfg.get("COVERAGE_VERIFY_PARALLELISM", _DEFAULT_VERIFY_PARALLELISM)),
        verify_timeout_s=float(cfg.get("COVERAGE_VERIFY_TIMEOUT_S", _DEFAULT_VERIFY_TIMEOUT_S)),
        verify_sleep_ms=int(cfg.get("COVERAGE_VERIFY_SLEEP_MS", _DEFAULT_VERIFY_SLEEP_BETWEEN_MS)),
        # PR-3 — K-slot + day-window + timezone defaults.
        slot_count=int(cfg.get("COVERAGE_SLOT_COUNT", _DEFAULT_SLOT_COUNT)),
        num_itineraries_per_slot=int(
            cfg.get("COVERAGE_NUM_ITINERARIES_PER_SLOT", _DEFAULT_NUM_ITINERARIES_PER_SLOT)
        ),
        slot_timeout_ms=int(cfg.get("COVERAGE_SLOT_TIMEOUT_MS", _DEFAULT_SLOT_TIMEOUT_MS)),
        within_pair_parallelism=int(
            cfg.get("COVERAGE_WITHIN_PAIR_PARALLELISM", _DEFAULT_WITHIN_PAIR_PARALLELISM)
        ),
        default_window_start=str(
            cfg.get("COVERAGE_DEFAULT_WINDOW_START", _DEFAULT_WINDOW_START_LOCAL)
        ),
        default_window_end=str(cfg.get("COVERAGE_DEFAULT_WINDOW_END", _DEFAULT_WINDOW_END_LOCAL)),
        default_timezone=str(cfg.get("COVERAGE_DEFAULT_TIMEZONE", _DEFAULT_WINDOW_TIMEZONE)),
    )


# ─────────────────────── cooperative-cancel registry ───────────────────────
#
# PR-1 — Stop button. Each in-flight `execute_run` registers an
# `asyncio.Event` keyed by run_id in `_CANCEL_EVENTS`. The
# POST /runs/{id}/stop endpoint sets that event; the per-pair loop checks
# it before each pair and exits cleanly when set. Cells already processed
# before the click stay in the DB — partial results are valuable.
_CANCEL_EVENTS: dict[uuid.UUID, asyncio.Event] = {}


def register_cancel(run_id: uuid.UUID) -> asyncio.Event:
    """Register (or reuse) the cancel event for `run_id`. Idempotent."""
    ev = _CANCEL_EVENTS.get(run_id)
    if ev is None:
        ev = asyncio.Event()
        _CANCEL_EVENTS[run_id] = ev
    return ev


def clear_cancel(run_id: uuid.UUID) -> None:
    """Drop the cancel event from the registry. Safe to call multiple times."""
    _CANCEL_EVENTS.pop(run_id, None)


def is_cancelled(run_id: uuid.UUID) -> bool:
    """True when the cancel event for `run_id` is set."""
    ev = _CANCEL_EVENTS.get(run_id)
    return ev is not None and ev.is_set()


def request_cancel(run_id: uuid.UUID) -> bool:
    """Public entry point for the POST /stop endpoint. Returns True when
    a live cancel event was found and set, False when no event was
    registered. Does NOT flip status — runner owns terminal-state writes."""
    ev = _CANCEL_EVENTS.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


# ────────────────────── PR-187 — DB-status cancel check ──────────────────────
#
# `_CANCEL_EVENTS` is a module-local dict, so it is PROCESS-local. A SQL
# `UPDATE network_coverage_runs SET status='cancelled'` issued by an operator
# (via psql), an orphan-cleanup script, or a sibling uvicorn worker has ZERO
# effect on the in-memory event the runner consults. In one incident this
# caused the runner to keep hammering MOTIS for 4+ hours after operators
# believed the run was cancelled.
#
# `_is_cancelled_in_db` does a cheap `SELECT status FROM network_coverage_runs
# WHERE id=:rid` and treats any non-'running'/'pending' state as a cancel
# signal (covers operator UPDATEs to 'cancelled', orphan-cleanup writes of
# 'failed', etc.). To keep the per-pair hot loop from hammering the DB, each
# run gets a small TTL cache of the last DB result that survives 3 seconds —
# plenty short to honour an operator click within one full pair's wall time,
# long enough to amortise the SELECT across the K within-pair slot calls.

_DB_CANCEL_CACHE_TTL_SECONDS = 3.0
_DB_CANCEL_TERMINAL_STATUSES = frozenset(("cancelled", "failed", "completed"))


def _is_cancelled_in_db(
    run_id: uuid.UUID,
    cache: dict[uuid.UUID, tuple[float, bool]],
) -> bool:
    """True iff the run's DB status is a non-running terminal state
    ('cancelled', 'failed', 'completed').

    Uses a per-run 3s TTL cache (`cache` is owned by the caller and lives
    for the duration of `execute_run`) to avoid hammering the DB inside
    the per-pair hot loop. The cache entry is `(expires_at_monotonic, value)`.
    A cache miss / expiry opens a short-lived `SessionLocal()` session,
    fetches the row's `status`, and stores the result.

    Defensive: any exception (DB down, schema drift) returns False — we'd
    rather keep running than crash the runner on a check that's meant to
    be cheap and best-effort. The in-memory `_CANCEL_EVENTS` path remains
    the primary signal; this is a backstop for cross-process cancels.
    """
    now = time.monotonic()
    hit = cache.get(run_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        with SessionLocal() as db:
            row = db.execute(
                select(NetworkCoverageRun.status).where(NetworkCoverageRun.id == run_id)
            ).scalar_one_or_none()
    except Exception:
        log.debug("PR-187 DB cancel check failed for run %s; assuming not cancelled", run_id)
        return False
    cancelled = row is not None and row in _DB_CANCEL_TERMINAL_STATUSES
    cache[run_id] = (now + _DB_CANCEL_CACHE_TTL_SECONDS, cancelled)
    return cancelled


# ─────────────────── PR-3 — day window + K-slot helpers ────────────────────
#
# The runner now slices each pair into K time-slots covering a per-run
# day window in the run's IANA timezone. Helpers below are all DB-free
# and unit-tested in `tests/unit/test_coverage_slicing.py` so they can
# be exercised without the full execute_run pipeline.


@dataclass(frozen=True)
class ResolvedWindow:
    """Resolved per-run day-window — UTC anchor instants + the IANA zone
    used to compute them. Threaded through `_fetch_plan_sliced` so the
    K-slot grid + the trip-belongs filter agree on the same boundaries.

    `start_utc` / `end_utc` bracket the calendar-day window in UTC; the
    K time-slot boundaries are computed by even subdivision between
    them. `tz_name` is preserved so the trip filter can re-localise
    `first_transit_leg_departure_utc` back to the same wall-clock zone
    the operator selected (matters for cross-midnight night-train
    windows like 18:00-06:00).
    """

    start_utc: datetime
    end_utc: datetime
    tz_name: str


def _default_reference_date(tz_name: str | None) -> date:
    """Tomorrow in `tz_name`, falling back to UTC for unknown zones.

    Extracted so `create_run` stays under Sonar's cognitive-complexity
    threshold (the nested try/except for ZoneInfo + the date arithmetic
    pushed the calling site over otherwise)."""
    try:
        tz_for_ref = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz_for_ref = ZoneInfo("UTC")
    return (datetime.now(tz_for_ref) + timedelta(days=1)).date()


def _resolve_timezone(tz_name: str | None, cfg: CoverageConfig) -> ZoneInfo:
    """Return a ZoneInfo for `tz_name`, falling back to the cfg default
    and finally to UTC. Unknown zones log a warning but never raise —
    the runner must always make progress on a coverage run.
    """
    candidate = tz_name or cfg.default_timezone or "UTC"
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "PR-3 unknown timezone %r — falling back to UTC for day-window resolution",
            candidate,
        )
        return ZoneInfo("UTC")


def _parse_hhmm(value: str | None, default: str) -> tuple[int, bool]:
    """Parse "HH:MM" into hours-from-midnight, returning (hours_minutes
    as total minutes, is_end_of_day_sentinel).

    The "24:00" sentinel returns (1440, True) so callers can recognise
    "midnight of the NEXT day" semantics — Postgres TIME forbids 24:00
    so we can't round-trip the literal through the DB. Anything that
    fails to parse falls back to `default` (also parsed) — defence in
    depth, the API layer validates upstream.
    """
    candidate = (value or default or "00:00").strip()
    if candidate == _END_OF_DAY_SENTINEL:
        return 24 * 60, True
    try:
        hh_str, mm_str = candidate.split(":", 1)
        hh = int(hh_str)
        mm = int(mm_str)
        if not (0 <= hh <= 23) or not (0 <= mm <= 59):
            raise ValueError
        return hh * 60 + mm, False
    except (ValueError, AttributeError):
        # Default fallback — re-parse the default which is operator-
        # supplied via platform_config, so it MAY also be bogus; final
        # fallback is 0 minutes so the runner never crashes.
        if value is None or value == default:
            return 0, False
        return _parse_hhmm(default, "00:00")


def _resolve_run_window(
    *,
    window_start_local: dtime | str | None,
    window_end_local: dtime | str | None,
    window_timezone: str | None,
    reference_date_value: date | None,
    cfg: CoverageConfig,
) -> ResolvedWindow:
    """Compose the per-run day-window into a (start_utc, end_utc) pair.

    Accepts either `datetime.time` (the ORM column type) or "HH:MM"
    string for the bounds so callers from both the runner (ORM rows)
    and the unit tests (strings) can use the same helper.

    Rules:
      - NULL bound → fall back to cfg.default_window_start/end
      - "24:00" string is honoured as end-of-day (= next-day 00:00)
      - bound start >= bound end → treated as a cross-midnight window
        (e.g. 18:00-06:00 = 12-hour night-train slice), end_utc is
        rolled forward by one day
      - reference_date NULL → today's date in the resolved timezone
        (matches `create_run`'s "tomorrow" default at run-create time;
        execute time only sees this branch if the row was inserted
        without a date for some reason)
    """
    tz = _resolve_timezone(window_timezone, cfg)

    def _to_string_bound(b: dtime | str | None, default: str) -> str:
        if b is None:
            return default
        if isinstance(b, dtime):
            return f"{b.hour:02d}:{b.minute:02d}"
        return b

    start_minutes, _ = _parse_hhmm(
        _to_string_bound(window_start_local, cfg.default_window_start),
        cfg.default_window_start,
    )
    end_minutes, end_is_sentinel = _parse_hhmm(
        _to_string_bound(window_end_local, cfg.default_window_end),
        cfg.default_window_end,
    )

    if reference_date_value is None:
        # Use today in the resolved tz — same fallback `create_run` uses
        # for the "tomorrow" persistence default, but here we don't bump
        # by 1 day because execute_run shouldn't silently shift the
        # window from what create_run captured. If a future code path
        # bypasses create_run and lands here we still produce a usable
        # window.
        reference_date_value = datetime.now(tz).date()

    start_local = datetime.combine(
        reference_date_value,
        dtime(hour=start_minutes // 60, minute=start_minutes % 60),
        tzinfo=tz,
    )

    # "24:00" sentinel == 1440 minutes == midnight of the NEXT day.
    # A cross-midnight window like 18:00-06:00 also needs the next-day
    # bump on end_utc; we detect that as "end <= start" once both are
    # in 0-1440 minutes and not the sentinel.
    end_day = reference_date_value
    if end_is_sentinel or (not end_is_sentinel and end_minutes <= start_minutes):
        end_day = reference_date_value + timedelta(days=1)
    end_minutes_clamped = 0 if end_is_sentinel else end_minutes
    end_local = datetime.combine(
        end_day,
        dtime(hour=end_minutes_clamped // 60, minute=end_minutes_clamped % 60),
        tzinfo=tz,
    )
    return ResolvedWindow(
        start_utc=start_local.astimezone(UTC),
        end_utc=end_local.astimezone(UTC),
        tz_name=str(tz),
    )


def _slot_boundaries(window: ResolvedWindow, slot_count: int) -> list[datetime]:
    """Return slot_count+1 UTC datetimes spanning [start_utc, end_utc].

    slot_count=1 returns [start_utc, end_utc] — i.e. one slot covering
    the whole window, matching the legacy single-call behaviour.
    """
    if slot_count < 1:
        slot_count = 1
    total_seconds = (window.end_utc - window.start_utc).total_seconds()
    slot_seconds = total_seconds / slot_count
    return [window.start_utc + timedelta(seconds=i * slot_seconds) for i in range(slot_count)] + [
        window.end_utc
    ]


def _coverage_dedup_key(trip: dict[str, Any]) -> tuple[str, str, tuple[str, ...], str]:
    """4-tuple identity for a trip in the K-slot dedup pass.

    (from_stop_id_norm, first_transit_leg_dep_utc_minute, route_signature,
    to_stop_id_norm) where:
      - from/to stop ids come from the first/last TRANSIT leg (NOT the
        itinerary endpoints, which on a walk-then-train trip would be
        the operator's coords)
      - dep is truncated to the minute (departure times that differ by
        seconds are the same train in practice)
      - route_signature is the tuple of (route_short_name or mode) for
        every transit leg in order — same train sequence == same route

    A walk-only trip (no transit legs) returns ("", "", (), "") which
    naturally collapses all walk-only trips to one entry — they're not
    distinguishable as separate "services" anyway.
    """
    legs = trip.get("legs") or []
    transit_legs = [
        lg for lg in legs if (lg.get("mode") or "").upper() not in ("WALK", "TRANSFER", "")
    ]
    if not transit_legs:
        return ("", "", (), "")

    first = transit_legs[0]
    last = transit_legs[-1]
    from_stop = _normalise_stop_id(first.get("from_stop_id"))
    to_stop = _normalise_stop_id(last.get("to_stop_id"))

    dep = trip.get("first_transit_leg_departure_utc") or first.get("departure") or ""
    dep_minute = _truncate_iso_to_minute(dep)

    route_sig = tuple(
        (lg.get("route_short_name") or lg.get("mode") or "").strip().upper() for lg in transit_legs
    )
    return (from_stop, dep_minute, route_sig, to_stop)


def _normalise_stop_id(stop_id: str | Any) -> str:
    """Strip the OTP/MOTIS feed-id prefix so cross-engine dedup works.

    OTP stop ids are `<feed>:<local>`; MOTIS uses `<feed>_<local>`. A
    train surfaced by both engines should dedup, so we collapse both
    forms onto `<local>` — the local part is the canonical identifier
    that survives engine swap. Missing / None returns "" so the
    dedup tuple stays a plain string."""
    if not stop_id:
        return ""
    s = str(stop_id)
    # OTP form first (colon), then MOTIS (underscore). Prefer the
    # right-most separator so a `<feed>_<local_with_underscores>` id
    # keeps the locally-meaningful portion intact.
    if ":" in s:
        return s.rsplit(":", 1)[-1]
    if "_" in s:
        return s.rsplit("_", 1)[-1]
    return s


def _truncate_iso_to_minute(value: str) -> str:
    """`YYYY-MM-DDTHH:MM:SS+00:00` → `YYYY-MM-DDTHH:MM+00:00`. Defensive
    against malformed input — returns the string unchanged if the seconds
    field isn't where we expect it."""
    if not value or "T" not in value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M%z")


def _trip_belongs_to_window(trip: dict[str, Any], window: ResolvedWindow) -> bool:
    """True iff the trip's FIRST TRANSIT LEG departure (UTC) falls in
    [window.start_utc, window.end_utc).

    Trips with no `first_transit_leg_departure_utc` (walk-only, or a
    client that hasn't been upgraded to emit the field) return False —
    they have no boarding event to anchor against the window. The
    runner treats those as "doesn't count" rather than guessing.
    """
    dep_str = trip.get("first_transit_leg_departure_utc")
    if not dep_str or not isinstance(dep_str, str):
        return False
    try:
        dep_utc = datetime.fromisoformat(dep_str)
    except (TypeError, ValueError):
        return False
    if dep_utc.tzinfo is None:
        dep_utc = dep_utc.replace(tzinfo=UTC)
    return window.start_utc <= dep_utc < window.end_utc


async def _fetch_plan_sliced(
    *,
    engine: str,
    session_id: str,
    origin: Hub,
    dest: Hub,
    window: ResolvedWindow,
    cfg: CoverageConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """K-slot fetch_plan dispatch + dedup. Returns ``(last_raw, deduped_trips)``.

    K = `cfg.slot_count`. K=1 is bit-identical to the legacy single
    fetch_plan call (= the rollback flag — set COVERAGE_SLOT_COUNT=1 in
    /admin/config to revert PR-3 behaviour). K>1 fires `cfg.within_pair_
    parallelism` concurrent calls, each anchored at a slot boundary,
    each requesting `cfg.num_itineraries_per_slot` itineraries within
    `slot_seconds` seconds, then filters every returned trip on
    `_trip_belongs_to_window` and deduplicates on `_coverage_dedup_key`.

    `last_raw` is the raw response from the chronologically-last slot
    (proxy for "give me one raw payload I can introspect" — used by
    the recorder for the journey_search_executions row). Persisting
    all K raws would multiply storage by K with no analytical gain
    (the deduped trip list is the canonical artifact).

    Per-slot exceptions are captured so a single timed-out slot
    doesn't poison the whole pair — the rest still contribute their
    trips and the pair gets the partial coverage it deserves. If
    EVERY slot raises, the first exception is re-raised so the caller
    sees an error status (matches the legacy single-call behaviour).
    """
    boundaries = _slot_boundaries(window, cfg.slot_count)

    # K=1 parity: single call with the full window, no dedup, no filter.
    # This branch is the operator's emergency rollback to pre-PR-3 behaviour
    # — IMPORTANT not to add any deviation here (filter, dedup, etc.) or
    # the "set slot_count=1" rollback story breaks.
    if cfg.slot_count == 1:
        raw, trips = await planner_dispatch.planner_for_engine(engine).fetch_plan(
            session_id=session_id,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=boundaries[0],
            timeout_ms=cfg.pair_timeout_ms,
            num_itineraries=cfg.num_itineraries,
            search_window_seconds=cfg.search_window_seconds,
        )
        return raw, trips

    semaphore = asyncio.Semaphore(cfg.within_pair_parallelism)
    slot_seconds = int((window.end_utc - window.start_utc).total_seconds() / cfg.slot_count)

    async def _one_slot(slot_start: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with semaphore:
            try:
                raw, trips = await asyncio.wait_for(
                    planner_dispatch.planner_for_engine(engine).fetch_plan(
                        session_id=session_id,
                        from_lat=origin.lat,
                        from_lon=origin.lon,
                        to_lat=dest.lat,
                        to_lon=dest.lon,
                        when=slot_start,
                        timeout_ms=cfg.slot_timeout_ms,
                        num_itineraries=cfg.num_itineraries_per_slot,
                        search_window_seconds=slot_seconds,
                    ),
                    timeout=cfg.slot_timeout_ms / 1000.0,
                )
                return raw, trips
            except Exception as exc:
                # Per-slot tolerance: log and return empty so the rest of
                # the slots still contribute. The caller re-raises only
                # when every slot failed.
                log.debug(
                    "PR-3 slot at %s for %s -> %s failed: %s",
                    slot_start.isoformat(),
                    origin.id,
                    dest.id,
                    exc,
                )
                raise

    results = await asyncio.gather(
        *(_one_slot(b) for b in boundaries[:-1]),
        return_exceptions=True,
    )
    return _merge_slot_results(results, window)


def _accumulate_slot_trips(
    trips: list[dict[str, Any]],
    window: ResolvedWindow,
    deduped: dict[tuple[str, str, tuple[str, ...], str], dict[str, Any]],
) -> None:
    """Merge one slot's trips into the accumulating dedup map (in place).

    Extracted from `_merge_slot_results` so the parent's nested for/if
    chain stays under Sonar's cognitive-complexity ceiling. Walk-only
    trips and trips whose first transit-leg boards outside the run's
    day window are dropped here so the caller can stay agnostic to the
    filter rules — see `_trip_belongs_to_window` for the exact rule."""
    for trip in trips:
        if not _trip_belongs_to_window(trip, window):
            continue
        key = _coverage_dedup_key(trip)
        if key not in deduped:
            deduped[key] = trip


def _merge_slot_results(
    results: list[Any],
    window: ResolvedWindow,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Filter + dedup K-slot outcomes into one ``(last_raw, trips)`` pair.

    Extracted from `_fetch_plan_sliced` so that function stays under
    Sonar's cognitive-complexity ceiling. Re-raises the first slot
    exception ONLY when no slot succeeded — otherwise partial coverage
    is more useful than an error status (matches PR-E's
    one-bad-cell-doesn't-abort-the-run posture)."""
    deduped: dict[tuple[str, str, tuple[str, ...], str], dict[str, Any]] = {}
    last_raw: dict[str, Any] = {}
    first_exc: BaseException | None = None
    successes = 0
    for outcome in results:
        if isinstance(outcome, BaseException):
            if first_exc is None:
                first_exc = outcome
            continue
        successes += 1
        raw, trips = outcome
        if raw:
            last_raw = raw  # last successful payload wins
        _accumulate_slot_trips(trips, window, deduped)
    if successes == 0 and first_exc is not None:
        raise first_exc
    return last_raw, list(deduped.values())


def _load_active_hubs(db: DbSession, countries: list[str] | None = None) -> list[Hub]:
    """v0.1.31 — read the active hub list from `network_coverage_hubs`.

    Falls back to the static `app/network_coverage/hubs.py` HUBS list
    when the DB table is empty (fresh install pre-migration, or dev
    environments). Logs a warning the first time the fallback fires
    so it's visible in operator logs but doesn't crash the runner.

    Hubs are returned in (country, sort_order, id) order — the same
    sort the matrix UI uses, so result rows index cleanly to cells
    without any client-side reordering.
    """
    q = select(NetworkCoverageHub).where(NetworkCoverageHub.is_active.is_(True))
    if countries:
        # Country codes are stored uppercase on insert (see HubCreate
        # normalisation); we uppercase the filter list here so an operator
        # who types lowercase 'fr' in the form still matches.
        q = q.where(NetworkCoverageHub.country.in_([c.upper() for c in countries]))
    q = q.order_by(
        NetworkCoverageHub.country,
        NetworkCoverageHub.sort_order,
        NetworkCoverageHub.id,
    )
    rows = db.execute(q).scalars().all()
    if rows:
        return [
            Hub(id=r.id, name=r.name, short=r.short, region=r.region or "", lat=r.lat, lon=r.lon)
            for r in rows
        ]
    # Country filter that returns nothing is a programming/UI error — the
    # caller should have validated the codes before creating the run. We
    # do NOT silently fall back to the static HUBS in that case (that
    # would build the matrix against the wrong country set and confuse
    # the operator).
    if countries:
        return []
    # Empty-table fallback — keeps the runner working in tests and
    # the brief migration window before seed completes.
    log.warning(
        "network_coverage_hubs table empty; falling back to static "
        "app/network_coverage/hubs.py — run alembic upgrade to seed the table"
    )
    from .hubs import HUBS  # local import to keep the module import graph small

    return list(HUBS)


def _hub_pairs(hubs: list[Hub], direction: str) -> list[tuple[Hub, Hub]]:
    """Build the pair list from a runtime hub list (v0.1.31).

    Mirrors the static `all_pairs()` / `unordered_pairs()` from
    `hubs.py` but operates on a dynamic input — needed once the hub
    list comes from the DB instead of a module-level constant.
    """
    if direction == "both":
        return [(a, b) for a in hubs for b in hubs if a.id != b.id]
    if direction == "single":
        return [(a, b) for i, a in enumerate(hubs) for b in hubs[i + 1 :]]
    raise ValueError(f"direction must be 'both' or 'single', got {direction!r}")


def _hub_set_signature(hubs: list[Hub]) -> str:
    """Generate a stable identifier for the hub set used at run time.

    v0.1.27/28 baked the hub set into a string like 'fr-major-26'. With
    DB-backed hubs (v0.1.31) the set isn't a fixed name — it's whatever
    was active at run creation. We hash the slug list so historical
    runs can be compared / grouped: two runs with the same hub composition
    get the same signature, even if individual hubs were edited later.

    Format: "live:<count>:<sha256[:8]>" — count is human-readable, hash
    is the canonical equality marker.
    """
    import hashlib

    slugs = sorted(h.id for h in hubs)
    digest = hashlib.sha256(",".join(slugs).encode("utf-8")).hexdigest()[:8]
    return f"live:{len(hubs)}:{digest}"


def create_run(
    db: DbSession,
    *,
    actor_user_id: uuid.UUID | None,
    session_id: str | None,
    depart_at: datetime,
    direction: str = "both",
    mode: str = MODE_SINGLE_SESSION,
    countries: list[str] | None = None,
    verify_externally: bool = False,
    window_start_local: dtime | None = None,
    window_end_local: dtime | None = None,
    window_timezone: str | None = None,
    reference_date_value: date | None = None,
) -> NetworkCoverageRun:
    """Create a pending coverage run and return it.

    Caller is responsible for kicking off the background task that
    processes the run (see `execute_run`).

    v0.1.31 — hubs read from `network_coverage_hubs` (operator-editable)
    instead of the static `hubs.py` constant.

    PR #36 — `mode` controls per-pair query distribution:
      'single_session'  — query the run's `session_id` (legacy behaviour;
                          `session_id` is required and is stored verbatim
                          as the `session_label`)
      'fanout'          — query every serving + include_in_fanout session
                          at execute time, merge results by trip_signature
                          (the live `/api/journey/fanout` pattern). The
                          API layer enforces `session_id is None` for
                          fanout-mode runs.

    PR-E — `verify_externally`, when True, triggers a Phase-3 sweep that
    asks ÖBB HAFAS about every `no_route`/`timeout`/`error` cell and
    persists the verdict to the result row's `external_*` columns. Default
    False keeps the legacy click-to-verify behaviour.
    """
    if direction not in ("both", "single"):
        raise ValueError(f"direction must be 'both' or 'single', got {direction!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if mode == MODE_SINGLE_SESSION and not session_id:
        raise ValueError("mode='single_session' requires a non-empty session_id")
    if mode == MODE_FANOUT and session_id is not None:
        raise ValueError("mode='fanout' must not specify a session_id")

    # Normalise countries to uppercase + drop empty list — empty means
    # "no filter" so the run row stores NULL rather than [], keeping
    # post-hoc "did this run filter?" checks simple (`countries is None`).
    norm_countries = [c.upper() for c in countries] if countries else None
    hubs = _load_active_hubs(db, countries=norm_countries)
    if not hubs:
        if norm_countries:
            raise ValueError(
                f"No active hubs match countries={norm_countries!r} — "
                "add hubs with those country codes or relax the filter"
            )
        raise ValueError("No active hubs configured — add some via the manage-hubs UI")
    pairs = _hub_pairs(hubs, direction)

    # session_label is the per-row UI hint. Fanout runs use a fixed
    # placeholder so the sidebar can badge them distinctly.
    label = session_id if mode == MODE_SINGLE_SESSION else FANOUT_SESSION_LABEL
    assert label is not None  # narrowing for mypy — guaranteed by mode validation above

    # PR-3 — resolve `reference_date` to a concrete calendar day NOW so
    # the row carries the operator's intent verbatim. Default is
    # "tomorrow in the resolved timezone" — gives a same-day result for
    # operators in any zone without needing to think about UTC drift.
    if reference_date_value is None:
        reference_date_value = _default_reference_date(window_timezone)

    run = NetworkCoverageRun(
        actor_user_id=actor_user_id,
        session_id=session_id,
        session_label=label,
        depart_at=depart_at,
        hub_set=_hub_set_signature(hubs),
        direction=direction,
        status="pending",
        total_pairs=len(pairs),
        mode=mode,
        countries=norm_countries,
        verify_externally=verify_externally,
        # PR-3 — per-run day window. NULL across the board reproduces
        # pre-PR-3 behaviour at execute time (runner falls back to the
        # platform_config defaults).
        window_start_local=window_start_local,
        window_end_local=window_end_local,
        window_timezone=window_timezone,
        reference_date=reference_date_value,
    )
    db.add(run)
    db.flush()
    return run


def _resolve_session_engine(db: DbSession, session_id: str | None) -> str:
    """Read a session's engine in a fresh DB query. 'otp' default for
    rows the operator deleted between coverage-run create and execute."""
    if session_id is None:
        return "otp"
    row = db.get(SessionRow, session_id)
    if row is None:
        return "otp"
    return getattr(row, "engine", "otp") or "otp"


def _snapshot_fanout_sessions(db: DbSession) -> tuple[list[str], dict[str, str]]:
    """Snapshot the eligible-for-fanout session ids + their engines.

    Done once at run start so a session going down mid-run doesn't change
    the pair load partway through; per-pair execution consults this
    instead of the live DB."""
    sessions = list(
        db.execute(
            select(SessionRow)
            .where(SessionRow.state == SessionState.SERVING.value)
            .where(SessionRow.include_in_fanout.is_(True))
            .order_by(SessionRow.id)
        )
        .scalars()
        .all()
    )
    return (
        [s.id for s in sessions],
        {s.id: (getattr(s, "engine", "otp") or "otp") for s in sessions},
    )


@dataclass(frozen=True)
class _Phase1Snapshot:
    """Frozen result of Phase-1 setup. Per-pair execution reads everything
    it needs from this snapshot (no further DB lookups during Phase 2)."""

    run_mode: str
    session_id_for_pairs: str | None
    engine_for_pairs: str
    fanout_session_ids: list[str]
    engine_by_session: dict[str, str]
    depart_at_for_pairs: datetime
    pairs: list[tuple[Hub, Hub]]
    cfg: CoverageConfig
    window: ResolvedWindow


def _resolve_run_mode_targets(
    db: DbSession, run: NetworkCoverageRun
) -> tuple[str | None, str, list[str], dict[str, str]]:
    """Resolve the session(s) and engine(s) the runner will call into.
    Mutates `run.status='failed'` inline when the run's mode is
    unsatisfiable so the outer Phase-1 helper can short-circuit cleanly."""
    if run.mode == MODE_SINGLE_SESSION:
        if run.session_id is None:
            log.warning("run %s mode=single_session has no session_id — aborting", run.id)
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            db.commit()
            return (None, "otp", [], {})
        return (run.session_id, _resolve_session_engine(db, run.session_id), [], {})
    fanout_ids, engines = _snapshot_fanout_sessions(db)
    if not fanout_ids:
        log.warning(
            "run %s mode=fanout has no serving fanout-enabled sessions — aborting",
            run.id,
        )
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        db.commit()
        return (None, "otp", [], {})
    return (None, "otp", fanout_ids, engines)


def _phase1_snapshot_and_start(run_id: uuid.UUID) -> _Phase1Snapshot | None:
    """Phase-1 of `execute_run` — validate the run, flip to running,
    snapshot the inputs the per-pair loop needs (cfg + window + hubs)."""
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            log.warning("network-coverage run %s not found — aborting", run_id)
            return None
        if run.status not in ("pending", "running"):
            log.info(
                "network-coverage run %s already in terminal state %s — skipping",
                run_id,
                run.status,
            )
            return None
        run.status = "running"
        run.started_at = datetime.now(UTC)
        db.commit()
        cfg = _load_coverage_config(db)
        session_id_for_pairs, engine_for_pairs, fanout_session_ids, engine_by_session = (
            _resolve_run_mode_targets(db, run)
        )
        if run.status == "failed":
            return None
        # PR-3 — resolve the run's day-window into a (start_utc, end_utc)
        # pair now so every per-pair call sees the same grid. Per-run
        # column NULL → fall back to platform_config defaults via cfg.
        window = _resolve_run_window(
            window_start_local=run.window_start_local,
            window_end_local=run.window_end_local,
            window_timezone=run.window_timezone,
            reference_date_value=run.reference_date,
            cfg=cfg,
        )
        hubs_now = _load_active_hubs(db, countries=run.countries)
        pairs = _hub_pairs(hubs_now, run.direction)
        return _Phase1Snapshot(
            run_mode=run.mode,
            session_id_for_pairs=session_id_for_pairs,
            engine_for_pairs=engine_for_pairs,
            fanout_session_ids=fanout_session_ids,
            engine_by_session=engine_by_session,
            depart_at_for_pairs=run.depart_at,
            pairs=pairs,
            cfg=cfg,
            window=window,
        )


async def _process_pair_with_cancel(
    *,
    run_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    snap: _Phase1Snapshot,
    origin: Hub,
    dest: Hub,
    cancel_cache: dict[uuid.UUID, tuple[float, bool]] | None = None,
) -> None:
    """PR-1 — per-pair coroutine with cooperative cancel. Checked before
    AND inside the semaphore so a Stop click propagates to both queued
    and in-flight pairs.

    PR-187 — also consults the DB status (with a 3s per-run TTL cache)
    so a SQL `UPDATE network_coverage_runs SET status='cancelled'` from
    an operator / orphan-cleanup / sibling uvicorn worker is honoured.
    Without this, the in-memory `_CANCEL_EVENTS` is process-local and
    cross-process cancels are invisible to the runner.

    `cancel_cache` is owned by the caller (`execute_run` allocates a
    fresh empty dict for each run) so a stale cache entry can never leak
    across runs. Default `None` keeps direct unit-test callers working
    without threading a cache through every call site."""
    cache = cancel_cache if cancel_cache is not None else {}
    if is_cancelled(run_id) or _is_cancelled_in_db(run_id, cache):
        return
    async with semaphore:
        if is_cancelled(run_id) or _is_cancelled_in_db(run_id, cache):
            return
        if snap.run_mode == MODE_FANOUT:
            await _execute_pair_fanout(
                run_id=run_id,
                session_ids=snap.fanout_session_ids,
                engine_by_session=snap.engine_by_session,
                origin=origin,
                dest=dest,
                depart_at=snap.depart_at_for_pairs,
                cfg=snap.cfg,
                window=snap.window,
            )
            return
        assert snap.session_id_for_pairs is not None
        await _execute_pair(
            run_id=run_id,
            session_id=snap.session_id_for_pairs,
            engine=snap.engine_for_pairs,
            origin=origin,
            dest=dest,
            depart_at=snap.depart_at_for_pairs,
            cfg=snap.cfg,
            window=snap.window,
        )


def _mark_run_failed(run_id: uuid.UUID) -> None:
    """Stamp `status='failed' + finished_at=now()` in its own short txn.
    Used by `execute_run` when the per-pair gather raises."""
    with SessionLocal() as db:
        db.execute(
            update(NetworkCoverageRun)
            .where(NetworkCoverageRun.id == run_id)
            .values(status="failed", finished_at=datetime.now(UTC))
        )
        db.commit()


def _persist_cancelled_run(*, run_id: uuid.UUID, elapsed_s: float) -> None:
    """PR-1 — terminal-state writer for operator-cancelled runs.
    Recomputes counters from the partial cell set, stamps finished_at,
    attaches a `cancelled_by_operator` marker, flips status."""
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            return
        run.finished_at = datetime.now(UTC)
        rows = (
            db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
            .scalars()
            .all()
        )
        run.completed_pairs = len(rows)
        run.ok_pairs = sum(1 for r in rows if r.status == "ok")
        run.no_route_pairs = sum(1 for r in rows if r.status == "no_route")
        run.error_pairs = sum(1 for r in rows if r.status not in ("ok", "no_route", "skipped"))
        run.summary = {
            **(run.summary or {}),
            "elapsed_seconds": elapsed_s,
            "cancelled_by_operator": True,
            "cancelled_at": datetime.now(UTC).isoformat(),
        }
        run.status = "cancelled"
        db.commit()


async def _finalise_completed_run(
    *, run_id: uuid.UUID, elapsed_s: float, cfg: CoverageConfig
) -> None:
    """Phase-3 — recompute summary counters, optionally run the PR-E
    external-verify sweep, flip to 'completed' in one transaction."""
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            return
        run.finished_at = datetime.now(UTC)
        rows = (
            db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
            .scalars()
            .all()
        )
        run.completed_pairs = len(rows)
        run.ok_pairs = sum(1 for r in rows if r.status == "ok")
        run.no_route_pairs = sum(1 for r in rows if r.status == "no_route")
        run.error_pairs = sum(1 for r in rows if r.status not in ("ok", "no_route", "skipped"))

        verify_counters = await _maybe_run_external_verify_sweep(db=db, run=run, rows=rows, cfg=cfg)

        run.summary = {
            "elapsed_seconds": elapsed_s,
            "median_response_ms": _median(
                [r.response_ms for r in rows if r.response_ms is not None]
            ),
            "p95_response_ms": _percentile(
                [r.response_ms for r in rows if r.response_ms is not None], 0.95
            ),
            "external_verified_count": verify_counters["verified"],
            "external_ok_count": verify_counters["ok"],
            "external_no_route_count": verify_counters["no_route"],
            "external_error_count": verify_counters["error"],
        }
        # Flip status LAST so a partially-completed sweep that exits
        # via an unexpected exception leaves the run in 'running' and
        # mark_orphaned_runs catches it at next startup.
        run.status = "completed"
        db.commit()


async def execute_run(run_id: uuid.UUID) -> None:
    """Drive a pending coverage run to completion.

    Designed to be called from a FastAPI BackgroundTask. Manages its own
    DB session lifecycle so the request's transaction can commit + return
    immediately. Idempotent in the loose sense: re-running on a completed
    run is a no-op.

    PR-1 — wraps the whole body in try/finally so the cancel-event
    registration set by `register_cancel(run_id)` is always cleared, even
    when execute_run exits via an exception, early-return, or terminal-
    state short-circuit.
    """
    log.info("network-coverage run %s starting", run_id)
    started = time.monotonic()
    register_cancel(run_id)
    try:
        snap = _phase1_snapshot_and_start(run_id)
        if snap is None or not snap.pairs:
            log.error("run %s has no pairs to execute", run_id)
            return

        # Phase 2: process pairs with bounded concurrency. Each pair gets
        # its own short-lived DB session.
        semaphore = asyncio.Semaphore(snap.cfg.pair_parallelism)
        # PR-187 — per-run TTL cache for the DB-status cancel check.
        # Lives only for the duration of this execute_run call (no leak
        # across runs); shared across every pair coroutine so the cheap
        # SELECT is amortised.
        cancel_cache: dict[uuid.UUID, tuple[float, bool]] = {}
        try:
            await asyncio.gather(
                *(
                    _process_pair_with_cancel(
                        run_id=run_id,
                        semaphore=semaphore,
                        snap=snap,
                        origin=o,
                        dest=d,
                        cancel_cache=cancel_cache,
                    )
                    for o, d in snap.pairs
                ),
                return_exceptions=False,
            )
        except Exception:
            log.exception("network-coverage run %s failed mid-loop", run_id)
            _mark_run_failed(run_id)
            return

        elapsed_s = time.monotonic() - started

        # PR-1 — if a Stop click set the cancel event mid-Phase-2, persist
        # the partial state under status='cancelled' instead of running
        # Phase 3. The cells already processed survive.
        if is_cancelled(run_id):
            _persist_cancelled_run(run_id=run_id, elapsed_s=elapsed_s)
            log.info(
                "network-coverage run %s cancelled by operator after %.1fs",
                run_id,
                elapsed_s,
            )
            return

        log.info(
            "network-coverage run %s completed in %.1fs (%d pairs)",
            run_id,
            elapsed_s,
            len(snap.pairs),
        )
        await _finalise_completed_run(run_id=run_id, elapsed_s=elapsed_s, cfg=snap.cfg)
    finally:
        clear_cancel(run_id)


async def _maybe_run_external_verify_sweep(
    *,
    db: DbSession,
    run: NetworkCoverageRun,
    rows: Sequence[NetworkCoverageResult],
    cfg: CoverageConfig | None = None,
) -> dict[str, int]:
    """PR-E — opt-in entry point for the external-verify sweep. Returns
    the zero-fill counters if the run didn't opt in or has no candidate
    cells, otherwise dispatches to `_run_external_verify_sweep` and
    returns its counters.

    Extracted from `execute_run` so that function stays under Sonar's
    cognitive-complexity ceiling — the verify-sweep dispatch alone
    introduced multiple branches that pushed Phase-3 over the limit.

    `cfg` is PR-2's operator-tunable knob bundle. Defaults to a fresh
    `CoverageConfig()` (= prior hardcoded constants) when called from a
    test that doesn't thread one in.

    PR-196a — the candidate filter (no_route / timeout / error only) was
    REMOVED. Today's "Show only where ÖBB disagrees" UI broke on every
    status='ok' cell because PR-E left their `external_ok` NULL, the
    binary filter hid every NULL, and the operator saw a white matrix.
    The graduated heatmap shipped here needs every cell scored to colour
    it, so we sweep them all. Rate limiting stays in
    `_run_external_verify_sweep` via the existing Semaphore + sleep —
    sweeping a full 650-pair run takes longer but stays under ÖBB's
    courtesy cap (~1 req/s) by construction.
    """
    cfg = cfg or CoverageConfig()
    zero = {"verified": 0, "ok": 0, "no_route": 0, "error": 0}
    if not getattr(run, "verify_externally", False):
        return zero
    # PR-196a — sweep every non-skipped row so the heatmap can score
    # ok-cells too (the bug being fixed). 'skipped' rows stay out
    # because they never got a real VIATOR answer to compare against.
    candidate_rows = [r for r in rows if r.status != "skipped"]
    log.info(
        "PR-196a external-verify sweep starting for run %s - %d candidate cells",
        run.id,
        len(candidate_rows),
    )
    if not candidate_rows:
        return zero
    counters = await _run_external_verify_sweep(db=db, run=run, rows=candidate_rows, cfg=cfg)
    log.info("PR-196a external-verify sweep done for run %s - %s", run.id, counters)
    return counters


def _fetch_viator_trips_for_search(
    db: DbSession, search_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    """PR-196a — pull the VIATOR-side trip dicts for one search_id.

    Used by the alignment scorer to compare against ÖBB's itineraries.
    Returns the canonical (legs[], duration_seconds, num_transfers,
    departure_at, arrival_at, modes) shape `_fetch_trips_by_search`
    emits in the admin API — the alignment scorer only reads `legs`,
    but threading the full dict keeps the data contract identical to
    what the modal renders. Empty list when search_id is NULL or the
    JOIN finds no rows (status='ok' but no trips = the alignment
    treats VIATOR as one-sided empty)."""
    if search_id is None:
        return []
    rows = (
        db.execute(
            select(JourneyTrip)
            .join(JourneySearchExecution, JourneyTrip.execution_id == JourneySearchExecution.id)
            .where(JourneySearchExecution.search_id == search_id)
            .order_by(JourneyTrip.rank_in_response)
        )
        .scalars()
        .all()
    )
    return [
        {
            "duration_seconds": t.duration_seconds,
            "num_transfers": t.num_transfers,
            "departure_at": t.departure_at.isoformat() if t.departure_at else None,
            "arrival_at": t.arrival_at.isoformat() if t.arrival_at else None,
            "modes": t.modes,
            "legs": t.legs or [],
        }
        for t in rows
    ]


def _persist_alignment_on_row(
    row: NetworkCoverageResult,
    viator_trips: list[dict[str, Any]],
    itineraries: list[external_verify.VerifyItinerary],
) -> None:
    """PR-196a — score the (VIATOR, ÖBB) pair, persist itineraries +
    score + tier onto the row in-place. Extracted from the per-cell
    coroutine so the sweep stays under Sonar's cognitive-complexity
    ceiling."""
    score, tier = compute_alignment(viator_trips, itineraries)
    # JSONB column — Pydantic dump keeps the shape stable across
    # writes / reads. `mode="json"` resolves any datetime / UUID
    # fields to ISO strings so the column round-trips losslessly.
    row.external_itineraries = [it.model_dump(mode="json") for it in itineraries]
    row.external_alignment_score = score
    row.external_alignment_tier = tier


async def _run_external_verify_sweep(
    *,
    db: DbSession,
    run: NetworkCoverageRun,
    rows: list[NetworkCoverageResult],
    cfg: CoverageConfig | None = None,
) -> dict[str, int]:
    """PR-E — sweep `rows` through ÖBB HAFAS and mutate each row's
    external_* columns in place on the attached ORM objects. Caller's
    db.commit() flushes everything in the same txn as status='completed'.

    Best-effort per cell: any exception is caught + logged and the cell
    is marked with external_error='sweep_exception' so one bad cell
    can't abort the run. Bounded concurrency via Semaphore + a short
    sleep inside each slot keeps us under ÖBB's documented soft cap.

    `cfg` is PR-2's operator-tunable knob bundle (verify_parallelism,
    verify_timeout_s, verify_sleep_ms). Defaults to `CoverageConfig()`
    when called directly from a test that doesn't supply one.

    PR-196a — additionally fetches VIATOR-side trips per cell, computes
    a graduated alignment score + tier via `compute_alignment`, and
    persists those (plus the per-itinerary ÖBB detail) onto the same
    row so the matrix UI's viridis heatmap renders without a second
    round-trip.

    Returns rollup counters for run.summary."""
    cfg = cfg or CoverageConfig()
    counters: dict[str, int] = {"verified": 0, "ok": 0, "no_route": 0, "error": 0}
    semaphore = asyncio.Semaphore(cfg.verify_parallelism)
    sleep_seconds = cfg.verify_sleep_ms / 1000.0

    async with httpx.AsyncClient(timeout=cfg.verify_timeout_s) as client:

        async def _verify_one(row: NetworkCoverageResult) -> None:
            async with semaphore:
                try:
                    origin_hub = db.get(NetworkCoverageHub, row.origin_hub_id)
                    dest_hub = db.get(NetworkCoverageHub, row.dest_hub_id)
                    if origin_hub is None or dest_hub is None:
                        # Hub was soft-deleted (or never existed) between
                        # the original run and the verify sweep — same
                        # surface as the click-verify endpoint's 404 path
                        # but persisted so the matrix UI can render it
                        # rather than the operator hitting a stale state.
                        row.external_source = external_verify._SOURCE_OEBB_HAFAS
                        row.external_error = "hub_missing"
                        row.external_verified_at = datetime.now(UTC)
                        counters["error"] += 1
                        counters["verified"] += 1
                        return
                    verdict = await external_verify.verify_via_oebb_hafas(
                        from_lat=origin_hub.lat,
                        from_lon=origin_hub.lon,
                        to_lat=dest_hub.lat,
                        to_lon=dest_hub.lon,
                        depart_at=run.depart_at,
                        client=client,
                    )
                    row.external_source = verdict.source
                    row.external_ok = verdict.ok
                    row.external_num_connections = verdict.num_connections
                    row.external_best_duration_seconds = verdict.best_duration_seconds
                    row.external_best_transfers = verdict.best_transfers
                    row.external_error = verdict.error
                    row.external_verified_at = datetime.now(UTC)
                    # PR-196a — score the (VIATOR, ÖBB) overlap + persist
                    # itineraries / score / tier for the heatmap. Errored
                    # verdicts get an empty ÖBB side ("we couldn't ask")
                    # which the scorer maps to no_service / one_sided
                    # based on whether VIATOR returned anything.
                    viator_trips = _fetch_viator_trips_for_search(db, row.journey_search_id)
                    _persist_alignment_on_row(row, viator_trips, verdict.itineraries)
                    counters["verified"] += 1
                    if verdict.error is not None:
                        counters["error"] += 1
                    elif verdict.ok:
                        counters["ok"] += 1
                    else:
                        counters["no_route"] += 1
                except Exception:
                    # Broad-except mirrors the recorder-write-failure
                    # escape valve elsewhere in this file — one bad
                    # cell never aborts the run. The exception is
                    # captured + persisted so operators can see WHICH
                    # cell broke and why in the matrix UI.
                    log.exception(
                        "PR-E external verify failed for cell %s->%s in run %s",
                        row.origin_hub_id,
                        row.dest_hub_id,
                        run.id,
                    )
                    row.external_source = external_verify._SOURCE_OEBB_HAFAS
                    row.external_error = "sweep_exception"
                    row.external_verified_at = datetime.now(UTC)
                    counters["error"] += 1
                    counters["verified"] += 1
                # Throttle inside the slot so concurrent slots don't
                # burst-saturate HAFAS at startup. Effective rate
                # ≈ parallelism / sleep_seconds = 2 / 0.5 = 4 req/s
                # ceiling, but each verify is two round-trips so wall
                # time per slot is closer to 3-5s, yielding ~0.6-1
                # verify/s observed. Comfortably under ÖBB's tolerance.
                await asyncio.sleep(sleep_seconds)

        # return_exceptions=False is safe because _verify_one swallows
        # everything internally; a bug that escapes is genuinely
        # exceptional and SHOULD bubble up to the Phase-3 commit-or-
        # abort logic above.
        await asyncio.gather(*(_verify_one(r) for r in rows), return_exceptions=False)

    return counters


# 2026-07-01 eu19 incident: a MOTIS/OTP session that flips unhealthy
# mid-run gets killed and takes 90-180s to cold-boot back up (see
# CLAUDE.md). Before this retry, every pair scheduled during that window
# failed *instantly* with httpx.ConnectError (the port simply isn't
# listening yet) and got persisted as a wrong 'error' cell — turning one
# transient container bounce into dozens of misleading matrix cells.
# Growing backoff gives the session a real chance to come back before
# giving up; cost is near-zero for the overwhelming common case (no
# ConnectError at all, so no retry ever happens).
_CONNECT_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 40.0)

_CONNECT_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    # 2026-07-02 — eu19 sweep post-healthcheck-fix still showed a handful
    # of "Server disconnected without sending a response" failures: the
    # TCP connection is accepted but MOTIS/OTP drops it before replying,
    # which surfaces as RemoteProtocolError rather than ConnectError. Same
    # class of transient session-bounce symptom (a restart landing mid-
    # request instead of before it), so it gets the same retry treatment.
    httpx.RemoteProtocolError,
)


async def _call_with_connect_retry[T](fetch: Callable[[], Awaitable[T]]) -> T:
    """Retry `fetch()` on connection-level session-bounce errors only
    (`_CONNECT_RETRY_EXCEPTIONS`), with growing backoff.

    Every other exception (timeout, HTTP error, bad response shape, ...)
    propagates on the first attempt — those aren't caused by a session
    bounce and retrying wouldn't change the outcome, it would just make a
    genuinely-broken pair take longer to report as such.
    """
    for delay_s in _CONNECT_RETRY_DELAYS_S:
        try:
            return await fetch()
        except _CONNECT_RETRY_EXCEPTIONS:
            await asyncio.sleep(delay_s)
    return await fetch()


async def _execute_pair(
    *,
    run_id: uuid.UUID,
    session_id: str,
    engine: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    cfg: CoverageConfig | None = None,
    window: ResolvedWindow | None = None,
) -> None:
    """Run a single A→B search and persist the result row.

    Wraps the engine-appropriate `fetch_plan` (the same call the live
    journey UI makes) so coverage results are bit-equivalent to what an
    operator would see via the Search page — modulo the lat/lon coords
    which come from the hub preset rather than master_stations geocoding.
    `engine` is passed in (snapshotted at Phase 1) so this hot per-pair
    function doesn't pay a DB lookup; see `execute_run`.

    `cfg` is PR-2's operator-tunable knob bundle. Defaults to a fresh
    `CoverageConfig()` (= prior hardcoded constants) when called from a
    test that doesn't thread one in.

    `window` is PR-3's resolved per-run day-window. When supplied, the
    fetch goes through `_fetch_plan_sliced` (K time-slot dispatch +
    filter + dedup); when None, falls back to the legacy single
    fetch_plan call so direct unit-test callers keep working. The
    runner always supplies `window`; only tests rely on the None path.
    """
    cfg = cfg or CoverageConfig()
    started = time.monotonic()
    status = "error"
    response_ms = 0
    num_itineraries: int | None = None
    best_duration_seconds: int | None = None
    best_num_transfers: int | None = None
    best_operators: str | None = None
    error_message: str | None = None
    journey_search_id: uuid.UUID | None = None
    raw: dict[str, Any] = {}
    trips: list[dict[str, Any]] = []

    async def _fetch() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if window is not None:
            # PR-3 — K-slot dispatch via the resolved per-run window.
            return await _fetch_plan_sliced(
                engine=engine,
                session_id=session_id,
                origin=origin,
                dest=dest,
                window=window,
                cfg=cfg,
            )
        return await planner_dispatch.planner_for_engine(engine).fetch_plan(
            session_id=session_id,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=depart_at,
            timeout_ms=cfg.pair_timeout_ms,
            # Legacy path — operator-tunable via COVERAGE_NUM_ITINERARIES
            # / COVERAGE_SEARCH_WINDOW_SECONDS.
            num_itineraries=cfg.num_itineraries,
            search_window_seconds=cfg.search_window_seconds,
        )

    try:
        raw, trips = await _call_with_connect_retry(_fetch)
        response_ms = int((time.monotonic() - started) * 1000)
        num_itineraries = len(trips)
        if trips:
            status = "ok"
            best = min(trips, key=lambda t: t.get("duration_seconds") or 1 << 30)
            best_duration_seconds = int(best.get("duration_seconds") or 0)
            best_num_transfers = int(best.get("num_transfers") or 0)
            seen_ops: list[str] = []
            for lg in best.get("legs", []):
                feed = lg.get("feed_id")
                if feed and feed not in seen_ops:
                    seen_ops.append(feed)
            best_operators = ",".join(seen_ops) if seen_ops else None
        else:
            status = "no_route"
    except Exception as exc:  # broad: persist as "error" + message
        response_ms = int((time.monotonic() - started) * 1000)
        # Distinguish timeout from generic error for the matrix colouring.
        cls_name = type(exc).__name__
        status = "timeout" if "Timeout" in cls_name or "timeout" in cls_name.lower() else "error"
        error_message = f"{cls_name}: {exc}"[:500]
        log.warning(
            "coverage run %s pair %s→%s failed: %s",
            run_id,
            origin.id,
            dest.id,
            error_message,
        )

    # Persist into journey_searches + journey_trips (best-effort, so the
    # click-cell drilldown can reuse the v0.1.26 trip-card UI).
    #
    # v0.1.29.4 — endpoint must be one of ('plan','compare','fanout') and
    # requested_time_kind must be one of ('depart_at','arrive_by') per the
    # CheckConstraints on journey_searches. v0.1.27 shipped with
    # endpoint='network-coverage' / requested_time_kind='depart' which
    # silently violated both constraints — every coverage row's INSERT
    # rolled back, leaving journey_search_id NULL on every result and
    # the modal forever showing "No linked journey-search row". The
    # coverage-vs-live distinction is preserved via
    # network_coverage_results.journey_search_id (the FK from the
    # coverage row pins it back to its parent run), so flattening
    # endpoint='plan' loses no analytical signal.
    with SessionLocal() as db:
        try:
            search = recorder.begin_search(
                db,
                user_id=None,  # ran from a background task
                ip=None,
                endpoint="plan",
                origin_lat=origin.lat,
                origin_lon=origin.lon,
                origin_label=origin.name,
                dest_lat=dest.lat,
                dest_lon=dest.lon,
                dest_label=dest.name,
                requested_time_kind="depart_at",
                requested_time=depart_at,
                modes="TRANSIT,WALK",
            )
            session_row = db.get(SessionRow, session_id)
            if session_row is not None:
                recorder.record_execution(
                    db,
                    search_id=search.id,
                    session_id=session_id,
                    graph_snapshot_id=None,
                    status=status,
                    response_ms=response_ms,
                    raw_response=raw or None,
                    error_message=error_message,
                    trips=trips,
                )
            recorder.finish_search(
                db,
                search,
                total_response_ms=response_ms,
                total_trips_unique=len(trips),
                status=status,
            )
            journey_search_id = search.id
            db.commit()
        except Exception as recorder_exc:
            log.exception(
                "coverage run %s pair %s→%s: recorder write failed (continuing)",
                run_id,
                origin.id,
                dest.id,
            )
            db.rollback()
            # v0.1.29.4 — surface the recorder failure into the matrix row
            # so the operator can see *why* the drilldown link is missing
            # (e.g. CheckConstraint violation) instead of the generic
            # "recorder write failed" message in the modal. Don't clobber
            # an existing OTP-side error_message; append/keep both.
            recorder_msg = f"[recorder] {type(recorder_exc).__name__}: {recorder_exc}"[:500]
            error_message = f"{error_message} | {recorder_msg}" if error_message else recorder_msg

    # Always persist the coverage result, even when the recorder write
    # failed — the matrix is the canonical artifact. Shared with the
    # fanout path so both modes write the result row + counters update
    # via exactly one code path (Sonar duplication metric was flagging
    # the previous copy-paste).
    _persist_pair_coverage_result(
        run_id=run_id,
        origin=origin,
        dest=dest,
        status=status,
        response_ms=response_ms,
        num_itineraries=num_itineraries,
        best_duration_seconds=best_duration_seconds,
        best_num_transfers=best_num_transfers,
        best_operators=best_operators,
        error_message=error_message,
        journey_search_id=journey_search_id,
    )


# ─────────────────── fanout helpers (PR #36) ───────────────────
#
# `_execute_pair_fanout` is intentionally a thin orchestrator that calls
# these four helpers in sequence. Sonar's cognitive-complexity rule was
# (rightly) screaming at the original 230-line body — extracting the
# four phases (query / merge / record / persist) makes each phase
# testable in isolation and brings the orchestrator under the rule's
# threshold. The persistence helper is shared with `_execute_pair`
# (single-session) to also kill the structural duplication that Sonar
# flagged at 3.1% on this PR.

# Type alias for the per-session tuple — keeps signatures readable.
_FanoutSub = tuple[str, str, dict[str, Any], list[dict[str, Any]], int]


async def _query_one_session_for_pair(
    *,
    sid: str,
    engine: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    run_id: uuid.UUID,
    cfg: CoverageConfig | None = None,
    window: ResolvedWindow | None = None,
) -> _FanoutSub:
    """One fetch_plan call for one (session, pair). Tolerates its own
    exception so a planner container being down doesn't poison the pair —
    the caller treats the returned status as "this session contributed
    nothing useful" and moves on. `engine` is the snapshotted backend
    for this session (see `execute_run`).

    `cfg` is PR-2's operator-tunable knob bundle (timeout, num itineraries,
    search window). Defaults to `CoverageConfig()` for direct test use.

    `window` is PR-3's resolved per-run day-window — when supplied the
    call goes through `_fetch_plan_sliced` (K-slot dispatch); when None
    the legacy single fetch_plan call is used."""
    cfg = cfg or CoverageConfig()
    sub_start = time.monotonic()

    async def _fetch() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if window is not None:
            return await _fetch_plan_sliced(
                engine=engine,
                session_id=sid,
                origin=origin,
                dest=dest,
                window=window,
                cfg=cfg,
            )
        return await planner_dispatch.planner_for_engine(engine).fetch_plan(
            session_id=sid,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=depart_at,
            timeout_ms=cfg.pair_timeout_ms,
            num_itineraries=cfg.num_itineraries,
            search_window_seconds=cfg.search_window_seconds,
        )

    try:
        raw, trips = await _call_with_connect_retry(_fetch)
        response_ms = int((time.monotonic() - sub_start) * 1000)
        sub_status = "ok" if trips else "no_route"
        return sid, sub_status, raw, trips, response_ms
    except Exception as exc:
        response_ms = int((time.monotonic() - sub_start) * 1000)
        cls_name = type(exc).__name__
        sub_status = (
            "timeout" if "Timeout" in cls_name or "timeout" in cls_name.lower() else "error"
        )
        log.warning(
            "coverage run %s pair %s→%s session %s failed: %s: %s",
            run_id,
            origin.id,
            dest.id,
            sid,
            cls_name,
            exc,
        )
        return sid, sub_status, {}, [], response_ms


def _derive_fanout_status(*, any_ok: bool, any_error_or_timeout: bool) -> str:
    """Pair-level status from the per-session outcomes. Priority is
    ok > error/timeout > no_route — "ok" wins as soon as any session
    returned itineraries (the whole point of fanout)."""
    if any_ok:
        return "ok"
    if any_error_or_timeout:
        return "error"
    return "no_route"


def _merge_one_trip_into_signatures(
    *,
    sid: str,
    trip: dict[str, Any],
    sig: str,
    by_signature: dict[str, dict[str, Any]],
    operators_union: list[str],
) -> None:
    """Merge one trip into the by-signature map and update the operator
    union. Same-signature trips collapse; the shortest-duration wins the
    'best' slot for that signature."""
    slot = by_signature.setdefault(sig, {"signature": sig, "session_ids": [], "best": trip})
    if sid not in slot["session_ids"]:
        slot["session_ids"].append(sid)
    if trip["duration_seconds"] < slot["best"]["duration_seconds"]:
        slot["best"] = trip
    for lg in trip.get("legs", []):
        feed = lg.get("feed_id")
        if feed and feed not in operators_union:
            operators_union.append(feed)


def _merge_fanout_results(
    per_session: list[_FanoutSub],
) -> tuple[str, dict[str, dict[str, Any]], list[str], list[str]]:
    """Aggregate the per-session outputs into one pair-level result.

    Returns ``(status, by_signature, sessions_with_trips, operators_union)``.
    Mirrors the live `/api/journey/fanout` endpoint's merge pattern.
    """
    sessions_with_trips: list[str] = []
    any_ok = False
    any_error_or_timeout = False
    by_signature: dict[str, dict[str, Any]] = {}
    operators_union: list[str] = []

    with SessionLocal() as db:
        from ..journey.signature import trip_signature

        for sid, sub_status, _raw, trips, _ms in per_session:
            if sub_status == "ok":
                any_ok = True
                sessions_with_trips.append(sid)
            elif sub_status in ("error", "timeout"):
                any_error_or_timeout = True

            for trip in trips:
                sig = trip_signature(db, session_id=sid, legs=trip.get("legs", []))
                _merge_one_trip_into_signatures(
                    sid=sid,
                    trip=trip,
                    sig=sig,
                    by_signature=by_signature,
                    operators_union=operators_union,
                )

    status = _derive_fanout_status(any_ok=any_ok, any_error_or_timeout=any_error_or_timeout)
    return status, by_signature, sessions_with_trips, operators_union


def _record_fanout_journey_search(
    *,
    run_id: uuid.UUID,
    per_session: list[_FanoutSub],
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    response_ms: int,
    status: str,
    total_trips_unique: int,
) -> tuple[uuid.UUID | None, str | None]:
    """Persist the journey_searches umbrella row + per-session executions
    so the matrix click-cell drilldown can reuse the v0.1.26 trip-card UI.

    Best-effort: if the recorder write fails the pair still gets a
    coverage_results row, with the recorder error surfaced in
    `error_message` so the operator can see *why* the modal link is
    missing instead of the generic "recorder write failed" message.

    Returns ``(journey_search_id, error_message_or_None)``.
    """
    with SessionLocal() as db:
        try:
            search = recorder.begin_search(
                db,
                user_id=None,
                ip=None,
                endpoint="fanout",
                origin_lat=origin.lat,
                origin_lon=origin.lon,
                origin_label=origin.name,
                dest_lat=dest.lat,
                dest_lon=dest.lon,
                dest_label=dest.name,
                requested_time_kind="depart_at",
                requested_time=depart_at,
                modes="TRANSIT,WALK",
            )
            for sid, sub_status, raw, trips, _ms in per_session:
                session_row = db.get(SessionRow, sid)
                if session_row is None:
                    continue
                recorder.record_execution(
                    db,
                    search_id=search.id,
                    session_id=sid,
                    graph_snapshot_id=None,
                    status=sub_status,
                    response_ms=_ms,
                    raw_response=raw or None,
                    error_message=None,
                    trips=trips,
                )
            recorder.finish_search(
                db,
                search,
                total_response_ms=response_ms,
                total_trips_unique=total_trips_unique,
                status=status,
            )
            journey_search_id = search.id
            db.commit()
            return journey_search_id, None
        except Exception as recorder_exc:
            log.exception(
                "coverage run %s pair %s→%s: recorder write failed (continuing)",
                run_id,
                origin.id,
                dest.id,
            )
            db.rollback()
            return None, f"[recorder] {type(recorder_exc).__name__}: {recorder_exc}"[:500]


def _persist_pair_coverage_result(
    *,
    run_id: uuid.UUID,
    origin: Hub,
    dest: Hub,
    status: str,
    response_ms: int,
    num_itineraries: int | None,
    best_duration_seconds: int | None,
    best_num_transfers: int | None,
    best_operators: str | None,
    error_message: str | None,
    journey_search_id: uuid.UUID | None,
    session_ids: list[str] | None = None,
) -> None:
    """Write one NetworkCoverageResult row and increment the run-level
    counters. Shared by `_execute_pair` (single-session) and
    `_execute_pair_fanout` so the matrix wire-format stays bit-equivalent
    across both code paths and the counter updates are written in exactly
    one place.

    `session_ids` is fanout-only (list of sessions that returned ≥1 trip);
    `_execute_pair` passes None.
    """
    with SessionLocal() as db:
        try:
            db.add(
                NetworkCoverageResult(
                    run_id=run_id,
                    origin_hub_id=origin.id,
                    dest_hub_id=dest.id,
                    status=status,
                    response_ms=response_ms,
                    num_itineraries=num_itineraries,
                    best_duration_seconds=best_duration_seconds,
                    best_num_transfers=best_num_transfers,
                    best_operators=best_operators,
                    session_ids=session_ids,
                    error_message=error_message,
                    journey_search_id=journey_search_id,
                )
            )
            db.execute(
                update(NetworkCoverageRun)
                .where(NetworkCoverageRun.id == run_id)
                .values(
                    completed_pairs=NetworkCoverageRun.completed_pairs + 1,
                    ok_pairs=NetworkCoverageRun.ok_pairs + (1 if status == "ok" else 0),
                    no_route_pairs=NetworkCoverageRun.no_route_pairs
                    + (1 if status == "no_route" else 0),
                    error_pairs=NetworkCoverageRun.error_pairs
                    + (1 if status not in ("ok", "no_route", "skipped") else 0),
                )
            )
            db.commit()
        except Exception:
            log.exception(
                "coverage run %s pair %s→%s: result write failed",
                run_id,
                origin.id,
                dest.id,
            )
            db.rollback()


async def _execute_pair_fanout(
    *,
    run_id: uuid.UUID,
    session_ids: list[str],
    engine_by_session: dict[str, str],
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    cfg: CoverageConfig | None = None,
    window: ResolvedWindow | None = None,
) -> None:
    """PR #36 — run an A→B search against EVERY fanout-enabled session and
    persist one merged result row.

    Thin orchestrator: delegates to four helpers (query / merge / record /
    persist). The orchestration is the only thing here; each phase is
    independently testable.

    Behaviour vs `_execute_pair`:
      1. Calls the engine-appropriate `fetch_plan` once per session in
         parallel via `asyncio.gather`. Each call is independent.
      2. Trips merge by `trip_signature` so the same itinerary surfaced
         by multiple sessions counts once; shortest-duration wins the
         "best" slot.
      3. `session_ids` on the result row records WHICH sessions returned
         ≥1 trip — that's the per-cell coverage signal PR #36 is built
         around.

    `cfg` is PR-2's operator-tunable knob bundle. Defaults to
    `CoverageConfig()` for direct test use.
    """
    cfg = cfg or CoverageConfig()
    pair_start = time.monotonic()

    per_session = await asyncio.gather(
        *(
            _query_one_session_for_pair(
                sid=sid,
                engine=engine_by_session.get(sid, "otp"),
                origin=origin,
                dest=dest,
                depart_at=depart_at,
                run_id=run_id,
                cfg=cfg,
                window=window,
            )
            for sid in session_ids
        )
    )

    status, by_signature, sessions_with_trips, operators_union = _merge_fanout_results(per_session)

    best_trip: dict[str, Any] | None = (
        min(
            (slot["best"] for slot in by_signature.values()),
            key=lambda t: t.get("duration_seconds") or 1 << 30,
        )
        if by_signature
        else None
    )
    response_ms = int((time.monotonic() - pair_start) * 1000)

    journey_search_id, error_message = _record_fanout_journey_search(
        run_id=run_id,
        per_session=per_session,
        origin=origin,
        dest=dest,
        depart_at=depart_at,
        response_ms=response_ms,
        status=status,
        total_trips_unique=len(by_signature),
    )

    _persist_pair_coverage_result(
        run_id=run_id,
        origin=origin,
        dest=dest,
        status=status,
        response_ms=response_ms,
        num_itineraries=len(by_signature) if by_signature else 0,
        best_duration_seconds=(int(best_trip.get("duration_seconds") or 0) if best_trip else None),
        best_num_transfers=(int(best_trip.get("num_transfers") or 0) if best_trip else None),
        best_operators=",".join(operators_union) if operators_union else None,
        session_ids=sessions_with_trips or None,
        error_message=error_message,
        journey_search_id=journey_search_id,
    )


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    sorted_v = sorted(values)
    return sorted_v[len(sorted_v) // 2]


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    sorted_v = sorted(values)
    idx = min(int(p * len(sorted_v)), len(sorted_v) - 1)
    return sorted_v[idx]


def get_run_with_results(
    db: DbSession, run_id: uuid.UUID
) -> tuple[NetworkCoverageRun | None, list[NetworkCoverageResult]]:
    """Fetch one run + all its results — used by the GET endpoint."""
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        return None, []
    results = (
        db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
        .scalars()
        .all()
    )
    return run, list(results)


def list_recent_runs(db: DbSession, *, limit: int = 20) -> list[NetworkCoverageRun]:
    """Most-recent runs first — for the sidebar list on the admin page."""
    return list(
        db.execute(
            select(NetworkCoverageRun).order_by(NetworkCoverageRun.started_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )


def mark_orphaned_runs_as_failed(db: DbSession) -> int:
    """v0.1.29.3 — terminate any runs left mid-flight by a web restart.

    `execute_run` is scheduled via FastAPI BackgroundTasks, which are
    strictly in-process to the web container. When the container
    restarts (deploy, OOM, manual `docker compose up -d` after pulling
    a new image) any currently-running coverage task dies with no DB
    state cleanup — the row stays in `status='running'` forever, the
    UI keeps showing it on the sidebar, and the operator has no signal
    that the work was actually abandoned.

    This helper is called from the FastAPI startup hook: by the time
    uvicorn is accepting requests, no `execute_run` from a previous
    container can still be alive, so anything in `running` or `pending`
    status is by definition orphaned. We flip them to `failed` and
    stamp `summary.orphaned_by_restart=true` so the matrix view can
    explain the state instead of showing a phantom progress bar.

    Returns the number of runs marked. Caller is responsible for the
    txn boundary (commit/rollback) — keeps this composable with
    startup-hook error handling.
    """
    orphans = list(
        db.execute(
            select(NetworkCoverageRun).where(NetworkCoverageRun.status.in_(("running", "pending")))
        )
        .scalars()
        .all()
    )
    if not orphans:
        return 0
    now = datetime.now(UTC)
    for run in orphans:
        run.status = "failed"
        run.finished_at = now
        run.summary = {
            **(run.summary or {}),
            "orphaned_by_restart": True,
            "orphaned_at": now.isoformat(),
            "completed_pairs_at_restart": run.completed_pairs or 0,
            "total_pairs_at_restart": run.total_pairs or 0,
        }
    return len(orphans)
