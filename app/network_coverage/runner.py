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
  pending (created by web; queued until an executor claims it)
    → running (claimed by the worker's coverage executor, #392)
      → completed (all pairs processed)
      | failed (unexpected exception escaped the loop, or resume limit)
      | cancelled (operator stopped via API)
    A `running` run whose executor stopped (stale heartbeat) is claimed
    again and resumed: the pairs that already have a result are skipped
    (`app/network_coverage/executor.py`).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session as DbSession

from .. import config_service
from ..db import SessionLocal
from ..journey import planner_dispatch, recorder
from ..models import (
    JourneySearch,
    JourneySearchExecution,
    JourneyTrip,
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ..models import Session as SessionRow
from ..models.sessions import SessionState
from . import external_verify  # PR-E — auto-verify-on-completion sweep
from .alignment import (
    compute_alignment,  # PR-196a — graduated heatmap scorer
    filter_trips_from_depart_at,  # apples-to-apples VIATOR/ÖBB window
)
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

# ──────────────── #392 — no blocking work on the run's event loop ────────────────
#
# `execute_run` runs on the event loop of the worker's coverage executor
# (app/network_coverage/executor.py), the loop that also keeps the run's
# heartbeat. Before #392 it was a FastAPI background task on web's loop, the
# one that answers every request and the image HEALTHCHECK. Every database
# call of the runner is synchronous SQLAlchemy, and the recorder alone makes
# two to three SELECTs per leg of every trip (the trip signature). Made on
# the loop, six pairs finishing together held it for seconds: on 22 Oct the
# health checks took 9-17 s during a run, and the harness
# (tests/integration/test_coverage_web_health.py: 56 pairs, 6 at a time)
# measured 30 590 statements on the loop and 1-3 s of loop lag. In the
# worker a held loop would delay the heartbeat instead.
#
# So the runner never calls the database from a coroutine: every blocking
# call goes through `_off_loop` (a small dedicated executor), and the
# completion phase, whose verify sweep shares one ORM session across its
# cells, runs whole on a private event loop in a thread of its own
# (`_run_on_private_loop`). The engine calls stay on the executor's loop:
# they are awaited I/O.
#
# Two threads, shared by every run of the process. The recorder's work is
# mostly Python (ORM objects, statement building) and holds the GIL, so more
# threads do not write faster: the harness measured the same run time and
# loop lag with 1, 2 and 4. Two let one pair's database round trips overlap
# another's Python work, and the runs hold at most two of the worker's
# fifteen pooled connections (SQLAlchemy's default pool, 5 + 10 overflow),
# so the executor's poll and heartbeat keep theirs.
_DB_THREADS = 2
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=_DB_THREADS, thread_name_prefix="coverage-db")


async def _off_loop[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run the blocking `fn(*args, **kwargs)` on the coverage DB executor and
    await its result, so the event loop stays free meanwhile. The calls of
    one pair are awaited one after the other, so they keep their order.

    The caller's context variables (structlog's request-free bindings, the
    OpenTelemetry span) go with the call, as `asyncio.to_thread` does. A
    cancelled await does not stop a call that has started (a write ends in
    commit or rollback), but drops one still queued: writes that must
    happen together go in one call, shielded (`_write_pair`)."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(_DB_EXECUTOR, call)


async def _run_on_private_loop[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run `coro` to its end on a new event loop in a thread of its own.

    For a phase that interleaves its own awaited I/O with blocking calls on
    one shared ORM session (the completion phase and its verify sweep): the
    blocking calls then hold that private loop, never the run's. The thread
    is the default executor's (`asyncio.to_thread`), not `_DB_EXECUTOR`'s,
    so a sweep of hours does not take one of the pair writers."""
    return await asyncio.to_thread(asyncio.run, coro)


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


def _elapsed_ms(start: float) -> int:
    """Milliseconds since `start`, a `time.monotonic()` reading."""
    return int((time.monotonic() - start) * 1000)


def _failure_status(exc: BaseException) -> str:
    """Cell status for a failed query: "timeout" when the exception class
    names a timeout (httpx.ReadTimeout, asyncio.TimeoutError, ...), so the
    matrix colours it apart from a generic "error"."""
    return "timeout" if "timeout" in type(exc).__name__.lower() else "error"


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


# #392 — the run's executor lost its claim (another executor took the run
# after this one's heartbeat went stale). Its pairs stop like a Stop, but it
# writes no terminal state: the run belongs to the other executor now.
_ABANDONED: set[uuid.UUID] = set()


def abandon_run(run_id: uuid.UUID) -> None:
    """Stop `run_id` in this process without any terminal write (the claim
    was lost). Called on the run's event loop by the executor's heartbeat."""
    _ABANDONED.add(run_id)
    request_cancel(run_id)


def is_abandoned(run_id: uuid.UUID) -> bool:
    """True once `abandon_run(run_id)` was called in this process."""
    return run_id in _ABANDONED


class RunNeedsResume(Exception):
    """#392 — the run cannot go on in this execution, but it has not failed:
    a transient database error kept it from starting, or pairs still have no
    result row at the completion. The executor leaves it `running` and
    claimed, with no terminal write; once its heartbeat is stale an executor
    resumes it (a crash resume, bounded by COVERAGE_MAX_RESUMES) and asks the
    pairs that have no result."""


# #392 — database errors worth a retry: the connection dropped or the server
# is restarting. Anything else (a constraint, a bug) is not retried.
_TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (OperationalError, InterfaceError)
# Waits between the attempts of the start of a run (phase 1) and of a pair's
# write. The total stays short: a longer outage is left to the resume.
_PHASE1_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0)
_WRITE_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.0, 2.0)
# The result-row guard of `network_coverage_results` (20260505_1500).
_UNIQUE_RESULT_CONSTRAINT = "unique_result_per_run_pair"


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

# #392 — runs whose row carries a Stop click (`cancel_requested_at`, written
# by web, which no longer shares a process with the run). Filled by the DB
# check in an executor thread (a set add is atomic); `_run_stopped` turns it
# into the in-process Stop on the run's loop, so the run ends `cancelled`
# with `cancelled_by_operator` exactly as the in-process Stop did.
_STOP_REQUESTED_IN_DB: set[uuid.UUID] = set()


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

    #392 — a Stop click written by web (`cancel_requested_at`) also answers
    True, and the run goes into `_STOP_REQUESTED_IN_DB` (see there).

    Defensive: any exception (DB down, schema drift) returns False — we'd
    rather keep running than crash the runner on a check that's meant to
    be cheap and best-effort. The in-memory `_CANCEL_EVENTS` path remains
    the primary signal; this is a backstop for cross-process cancels.
    """
    now = time.monotonic()
    fresh = _fresh_cancel_answer(run_id, cache, now)
    if fresh is not None:
        return fresh
    try:
        with SessionLocal() as db:
            row = db.execute(
                select(NetworkCoverageRun.status, NetworkCoverageRun.cancel_requested_at).where(
                    NetworkCoverageRun.id == run_id
                )
            ).one_or_none()
    except Exception:
        log.debug("PR-187 DB cancel check failed for run %s; assuming not cancelled", run_id)
        return False
    stop_clicked = row is not None and row[1] is not None
    if stop_clicked:
        _STOP_REQUESTED_IN_DB.add(run_id)
    cancelled = row is not None and (row[0] in _DB_CANCEL_TERMINAL_STATUSES or stop_clicked)
    cache[run_id] = (now + _DB_CANCEL_CACHE_TTL_SECONDS, cancelled)
    return cancelled


def _fresh_cancel_answer(
    run_id: uuid.UUID, cache: dict[uuid.UUID, tuple[float, bool]], now: float
) -> bool | None:
    """The cached DB cancel answer for `run_id` while it is fresh (3 s),
    else None. The one freshness rule of `_is_cancelled_in_db` and
    `_run_stopped`."""
    hit = cache.get(run_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    return None


async def _run_stopped(run_id: uuid.UUID, cache: dict[uuid.UUID, tuple[float, bool]]) -> bool:
    """The per-pair cancel check: the in-process Stop event, then the DB
    status (`_is_cancelled_in_db`, same 3 s cache). A fresh cache entry is
    answered here; only an expired one costs a trip to the DB executor, so
    the SELECT never runs on the event loop (#392). The Stop event is read
    again once the DB answer is back: a click made while the SELECT waited
    for a thread still stops this pair."""
    if is_cancelled(run_id):
        return True
    fresh = _fresh_cancel_answer(run_id, cache, time.monotonic())
    if fresh is not None:
        return fresh
    stopped_in_db = await _off_loop(_is_cancelled_in_db, run_id, cache)
    if run_id in _STOP_REQUESTED_IN_DB:
        request_cancel(run_id)  # web's Stop click: the in-process Stop, on this loop
    return stopped_in_db or is_cancelled(run_id)


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


def _anchor_reference_date(
    depart_at_local: datetime, start_minutes: int, end_minutes: int, end_is_sentinel: bool
) -> date:
    """The calendar day whose day-window contains `depart_at_local`.

    For an ordinary window (00:00-24:00, 06:00-22:00, ...) that is simply
    the local date. For a **cross-midnight** window — which
    `_resolve_run_window` explicitly supports, e.g. an 18:00-06:00
    night-train slice — a 02:00 departure belongs to the window that
    *opened the previous evening*, so the anchor day is one day earlier.
    Getting this wrong would compose a grid starting 16 hours after the
    train the operator asked about.

    A `depart_at_local` that falls in neither window (e.g. 10:00 with an
    18:00-06:00 window — the operator's departure sits outside their own
    window) degrades to the local date. The run still searches a coherent
    grid; it just doesn't contain `depart_at`, which is the operator's
    own configuration choice, not a bug we can resolve for them.
    """
    if not end_is_sentinel and end_minutes <= start_minutes:  # cross-midnight
        local_minutes = depart_at_local.hour * 60 + depart_at_local.minute
        if local_minutes < end_minutes:
            return depart_at_local.date() - timedelta(days=1)
    return depart_at_local.date()


def _anchor_depart_at_and_reference_date(
    depart_at: datetime,
    reference_date_value: date | None,
    window_start_local: dtime | str | None,
    window_end_local: dtime | str | None,
    window_timezone: str | None,
    cfg: CoverageConfig,
) -> tuple[datetime, date]:
    """Resolve `depart_at` to an instant and `reference_date` to the day
    whose window contains it — both read in the run's own timezone.

    Two invariants this establishes, neither of which held before:

    1. **`depart_at` is a wall-clock in the run's zone, not in UTC.** The
       admin form's `<input type="datetime-local">` posts a naive string
       with no offset. Stamping it UTC (the old `depart_at.replace(
       tzinfo=UTC)` in the API layer) silently reinterpreted "06:40" as
       06:40Z = 08:40 for a `Europe/Brussels` run. One zone per run:
       `depart_at`, the K-slot window, and `reference_date` now all read
       against `_resolve_timezone(window_timezone, cfg)`. A caller that
       supplies a tz-aware `depart_at` is respected as-is (it already
       names an instant). Every *consumer* of the stored instant must
       therefore localise before rendering a wall-clock — see
       `external_verify._build_trip_search_body` (ÖBB reads outDate /
       outTime as Vienna-local) and `_run_to_summary`'s `depart_at`.

    2. **`reference_date` is the day whose window contains `depart_at`.**
       The K-slot search grid is composed *exclusively* from
       `reference_date` + `window_*_local` (see `_resolve_run_window`);
       `depart_at` is never passed to the planner. Meanwhile every
       downstream comparison anchors on `depart_at`: the ÖBB verify sweep
       (`verify_via_oebb_hafas(..., depart_at=run.depart_at)`) and the
       trip filters in `_fetch_trips_by_search` /
       `alignment.filter_trips_from_depart_at`.

       Defaulting `reference_date` to "tomorrow at create time" let the
       two name different calendar days. An operator asking for
       2026-07-20 on 2026-07-08 got a matrix searched on 2026-07-09,
       whose trips then failed `departure_at >= depart_at` by 11 days —
       every cell rendered "no itineraries found" beside a non-zero
       itinerary count, and every alignment score was scored against a
       VIATOR side filtered down to nothing. The wrong day was invisible
       because the cell modal renders leg times as HH:MM with no date.

    An **explicitly supplied** `reference_date` that disagrees with
    `depart_at`'s anchor day is rejected (ValueError -> HTTP 400) rather
    than persisted. Honouring it would recreate exactly the defect above
    on a brand-new run: the grid searches one day, every depart_at-anchored
    comparison judges another, and the alignment sweep writes
    `one_sided_oebb` tiers for the whole matrix. Operators who want a
    different schedule day move `depart_at`, which is the field that
    actually names it.

    `app/models/network_coverage.py`'s `reference_date` docstring has
    always *claimed* create_run keeps the two consistent. This is the
    first version that actually does.
    """
    tz = _resolve_timezone(window_timezone, cfg)
    if depart_at.tzinfo is None:
        depart_at = depart_at.replace(tzinfo=tz)
    start_minutes, end_minutes, end_is_sentinel = _window_bounds_minutes(
        window_start_local, window_end_local, cfg
    )
    anchor = _anchor_reference_date(
        depart_at.astimezone(tz), start_minutes, end_minutes, end_is_sentinel
    )
    if reference_date_value is None:
        return depart_at, anchor
    if reference_date_value != anchor:
        raise ValueError(
            f"reference_date {reference_date_value.isoformat()} does not match the day-window "
            f"containing depart_at ({anchor.isoformat()}). The K-slot grid is searched on "
            f"reference_date while every comparison anchors on depart_at, so the two must agree. "
            f"Move the Departure to the day you want to search, or clear the Reference date."
        )
    return depart_at, reference_date_value


def expected_reference_date(run: NetworkCoverageRun, cfg: CoverageConfig) -> date:
    """The `reference_date` `create_run` would derive for this row today.

    Read-path twin of `_anchor_depart_at_and_reference_date`. Uses the
    run's OWN persisted window columns; only a NULL column falls back to
    `cfg`, and only the timezone fallback can move the answer (a
    day-level comparison is far less config-sensitive than window
    containment). The true fix for that residue is freezing the resolved
    window onto the run row — tracked separately.
    """
    tz = _resolve_timezone(run.window_timezone, cfg)
    start_minutes, end_minutes, end_is_sentinel = _window_bounds_minutes(
        run.window_start_local, run.window_end_local, cfg
    )
    return _anchor_reference_date(
        run.depart_at.astimezone(tz), start_minutes, end_minutes, end_is_sentinel
    )


def reference_date_matches_depart_at(run: NetworkCoverageRun, cfg: CoverageConfig) -> bool:
    """True when the run searched the day its own `depart_at` names.

    False for every run created before `_anchor_depart_at_and_reference_date`
    landed (their `reference_date` is "tomorrow at create time", unrelated
    to `depart_at`). Callers use this to decide whether `depart_at` is a
    meaningful anchor to filter that run's trips against.

    Deliberately a **calendar-day** comparison, not window containment: an
    operator who narrows the window to 06:00-22:00 and asks to depart at
    05:30 has made a correctly-anchored run whose depart_at simply
    precedes the first slot. Treating that as "wrong day" would badge a
    valid run and silently drop PR #221's trip filter.
    """
    return run.reference_date == expected_reference_date(run, cfg)


def _resolve_timezone(tz_name: str | None, cfg: CoverageConfig) -> ZoneInfo:
    """Return a ZoneInfo for `tz_name`, falling back to the cfg default
    and finally to UTC. Unknown zones log a warning but never raise —
    the runner must always make progress on a coverage run.
    """
    candidate = tz_name or cfg.default_timezone or "UTC"
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        # Line breaks stripped explicitly (CodeQL py/log-injection): %r
        # escapes them already, but the name comes from a run's config.
        log.warning(
            "PR-3 unknown timezone %r — falling back to UTC for day-window resolution",
            candidate.replace("\r", "").replace("\n", "")[:80],
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


def _window_bounds_minutes(
    window_start_local: dtime | str | None,
    window_end_local: dtime | str | None,
    cfg: CoverageConfig,
) -> tuple[int, int, bool]:
    """`(start_minutes, end_minutes, end_is_sentinel)` for a run's bounds.

    Accepts either `datetime.time` (the ORM column type) or "HH:MM" string
    so the runner (ORM rows), `create_run` (validated form input) and the
    unit tests can share one parser. NULL bound -> the cfg default.

    Shared by `_resolve_run_window` (which composes the K-slot grid) and
    `_anchor_reference_date` (which picks the day that grid anchors on),
    so the two can never disagree about where a cross-midnight window
    starts.
    """

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
    return start_minutes, end_minutes, end_is_sentinel


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
      - reference_date NULL → today's date in the resolved timezone.
        `create_run` always persists a concrete `reference_date` (the day
        whose window contains `depart_at` — see
        `_anchor_depart_at_and_reference_date`), so execute time only
        sees this branch if the row was inserted without a date for some
        reason.
    """
    tz = _resolve_timezone(window_timezone, cfg)
    start_minutes, end_minutes, end_is_sentinel = _window_bounds_minutes(
        window_start_local, window_end_local, cfg
    )

    if reference_date_value is None:
        # Today in the resolved tz. `create_run` always persists a concrete
        # reference_date (the day whose window contains depart_at), so
        # execute_run never takes this branch; it exists only so a future
        # code path that bypasses create_run still produces a usable
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
    Sonar's cognitive-complexity ceiling. Raises a slot exception ONLY
    when no slot succeeded — otherwise partial coverage is more useful
    than an error status (matches PR-E's one-bad-cell-doesn't-abort-the-
    run posture). Which exception is raised: see `_slot_failure`."""
    deduped: dict[tuple[str, str, tuple[str, ...], str], dict[str, Any]] = {}
    last_raw: dict[str, Any] = {}
    failures: list[BaseException] = []
    successes = 0
    for outcome in results:
        if isinstance(outcome, BaseException):
            failures.append(outcome)
            continue
        successes += 1
        raw, trips = outcome
        if raw:
            last_raw = raw  # last successful payload wins
        _accumulate_slot_trips(trips, window, deduped)
    # Declared optional, then narrowed: the slot's own exception (its exact
    # httpx or asyncio class) is re-raised as it is, never wrapped.
    failure: BaseException | None = _slot_failure(failures) if successes == 0 and failures else None
    if failure is not None:
        raise failure
    return last_raw, list(deduped.values())


def _slot_failure(failures: list[BaseException]) -> BaseException:
    """The exception a pair whose every slot failed reports (#388 review).

    `_call_with_connect_retry` wraps the whole K-slot fetch and retries
    it when this exception is a connection-level one. So a connection-
    level exception is reported only when EVERY slot failed with one:
    the engine accepted none of the slots. When any slot failed another
    way (an outer `wait_for` timeout, a read timeout, an HTTP error...),
    the engine did take requests and the pair's failure is its own: the
    first such exception is reported and the pair is not retried. Before,
    the first exception in slot order was raised, so the same failure
    was retried (minutes held on a pair slot) or not, by slot order."""
    for exc in failures:
        if not isinstance(exc, _CONNECT_RETRY_EXCEPTIONS):
            return exc
    return failures[0]


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

    #392 — nothing executes it here: the run stays `pending` until the
    worker's coverage executor claims it and calls `execute_run`.

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
    # the row carries the operator's intent verbatim. It defaults to the
    # calendar day of `depart_at`, read in the run's own timezone, so the
    # day the K-slot grid searches is the day the operator asked for —
    # see `_anchor_depart_at_and_reference_date` for why that used to be
    # "tomorrow" and what it broke.
    depart_at, reference_date_value = _anchor_depart_at_and_reference_date(
        depart_at,
        reference_date_value,
        window_start_local,
        window_end_local,
        window_timezone,
        _load_coverage_config(db),
    )

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
    # #392 — the pairs still to ask: `pairs` minus those that already have a
    # result row (a resumed run). None = all of `pairs` (a fresh run).
    todo_pairs: list[tuple[Hub, Hub]] | None = None
    # #392 — the run was claimed again after its executor stopped, and when
    # it first started (its elapsed time counts from there).
    resumed: bool = False
    started_at: datetime | None = None

    def pairs_to_run(self) -> list[tuple[Hub, Hub]]:
        """The pairs this execution asks: every pair, less the done ones."""
        return self.pairs if self.todo_pairs is None else self.todo_pairs


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


def _config_from_snapshot(data: Any) -> CoverageConfig | None:
    """The `CoverageConfig` frozen in a run's `execution_snapshot`, or None
    when there is none (or it no longer fits the dataclass)."""
    if not isinstance(data, dict):
        return None
    known = {f.name for f in fields(CoverageConfig)}
    try:
        return CoverageConfig(**{k: v for k, v in data.items() if k in known})
    except (TypeError, ValueError):
        return None


def _hubs_by_ids(db: DbSession, hub_ids: list[str]) -> list[Hub]:
    """The hubs named in a run's snapshot, in its order, active or not (a
    hub deactivated mid-run stays in the run). Ids no longer in the table
    come from the static fallback list, else are left out."""
    rows = {
        r.id: r
        for r in db.execute(
            select(NetworkCoverageHub).where(NetworkCoverageHub.id.in_(hub_ids))
        ).scalars()
    }
    from .hubs import HUBS  # local import, as in _load_active_hubs

    static = {h.id: h for h in HUBS}
    hubs: list[Hub] = []
    for hub_id in hub_ids:
        r = rows.get(hub_id)
        if r is not None:
            hubs.append(
                Hub(
                    id=r.id, name=r.name, short=r.short, region=r.region or "", lat=r.lat, lon=r.lon
                )
            )
        elif hub_id in static:
            hubs.append(static[hub_id])
    return hubs


def _frozen_inputs(db: DbSession, run: NetworkCoverageRun) -> tuple[CoverageConfig, list[Hub]]:
    """#392 — the run's knobs and hubs. Read from the platform config and
    the active hubs at its first execution and frozen onto the row
    (`execution_snapshot`), so a resumed run asks the same pairs with the
    same K-slot grid even if a knob or a hub changed in between."""
    frozen = run.execution_snapshot if isinstance(run.execution_snapshot, dict) else {}
    cfg = _config_from_snapshot(frozen.get("config"))
    hub_ids = frozen.get("hub_ids")
    if cfg is not None and isinstance(hub_ids, list):
        return cfg, _hubs_by_ids(db, [str(h) for h in hub_ids])
    cfg = _load_coverage_config(db)
    hubs = _load_active_hubs(db, countries=run.countries)
    run.execution_snapshot = {
        "hub_ids": [h.id for h in hubs],
        "config": asdict(cfg) if isinstance(cfg, CoverageConfig) else None,
    }
    db.commit()
    return cfg, hubs


def _counter_values(statuses: Sequence[str]) -> dict[str, int]:
    """The run's four counters recomputed from its result rows' statuses —
    the one rule of the recount (cancel, completion, resume)."""
    return {
        "completed_pairs": len(statuses),
        "ok_pairs": sum(1 for s in statuses if s == "ok"),
        "no_route_pairs": sum(1 for s in statuses if s == "no_route"),
        "error_pairs": sum(1 for s in statuses if s not in ("ok", "no_route", "skipped")),
    }


def _apply_counters(run: NetworkCoverageRun, statuses: Sequence[str]) -> None:
    for name, value in _counter_values(statuses).items():
        setattr(run, name, value)


def _done_pairs_and_recount(db: DbSession, run: NetworkCoverageRun) -> set[tuple[str, str]]:
    """#392 — the (origin, dest) pairs that already have a result row, and
    the counters made exact from those rows. A pair's result row and its
    counter increments are one transaction, so they agree already; the
    recount is idempotent and also covers rows older than that rule."""
    rows = db.execute(
        select(
            NetworkCoverageResult.origin_hub_id,
            NetworkCoverageResult.dest_hub_id,
            NetworkCoverageResult.status,
        ).where(NetworkCoverageResult.run_id == run.id)
    ).all()
    if rows:
        _apply_counters(run, [r[2] for r in rows])
        db.commit()
    return {(r[0], r[1]) for r in rows}


def _phase1_snapshot_and_start(run_id: uuid.UUID) -> _Phase1Snapshot | None:
    """Phase-1 of `execute_run` — validate the run, flip to running,
    snapshot the inputs the per-pair loop needs (cfg + window + hubs).

    #392 — the executor's claim has already set `running` and `started_at`;
    only a direct call on a `pending` run (tests) sets them here. On a
    resumed run the pairs that already have a result are left out."""
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
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC)
            db.commit()
        cfg, hubs_now = _frozen_inputs(db, run)
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
        pairs = _hub_pairs(hubs_now, run.direction)
        done = _done_pairs_and_recount(db, run)
        todo = [p for p in pairs if (p[0].id, p[1].id) not in done]
        if done:
            log.info(
                "coverage_executor.resume_skips run_id=%s done=%d to_run=%d",
                run_id,
                len(pairs) - len(todo),
                len(todo),
            )
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
            todo_pairs=todo,
            resumed=isinstance(run.resume_count, int) and run.resume_count > 0,
            started_at=run.started_at,
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
    if await _run_stopped(run_id, cache):
        return
    async with semaphore:
        if await _run_stopped(run_id, cache):
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


def _mark_run_failed(
    run_id: uuid.UUID, executor_id: str | None = None, reason: str | None = None
) -> None:
    """Stamp `status='failed' + finished_at=now()` in its own short txn.
    Used by `execute_run` when the per-pair gather raises. With
    `executor_id` (#392), only while that executor still holds the run;
    `reason` goes to `summary.failure_reason` (shown on the page)."""
    now = datetime.now(UTC)
    stmt = update(NetworkCoverageRun).where(NetworkCoverageRun.id == run_id)
    if executor_id is not None:
        stmt = stmt.where(NetworkCoverageRun.claimed_by == executor_id)
    extra = {"failure_reason": reason or "the run failed", "failed_at": now.isoformat()}
    summary = func.coalesce(NetworkCoverageRun.summary, literal({}, JSONB)).op("||")(
        literal(extra, JSONB)
    )
    with SessionLocal() as db:
        db.execute(stmt.values(status="failed", finished_at=now, summary=summary))
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
        _apply_counters(run, [r.status for r in rows])
        run.summary = {
            **(run.summary or {}),
            "elapsed_seconds": elapsed_s,
            "cancelled_by_operator": True,
            "cancelled_at": datetime.now(UTC).isoformat(),
        }
        run.status = "cancelled"
        db.commit()


# #392 — what the completion keeps of the summary it replaces: the record of
# the resumes the executor wrote at each claim of a running run.
_RESUME_SUMMARY_KEYS = (
    "resume_count",
    "crash_resume_count",
    "release_resume_count",
    "resumes",
)


def _kept_resume_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    return {k: summary[k] for k in _RESUME_SUMMARY_KEYS if k in summary}


def _still_claimed_by(db: DbSession, run_id: uuid.UUID, executor_id: str) -> bool:
    """#392 — True while `executor_id` holds the run, checked under a row
    lock so no claim can change hands before this transaction commits."""
    holder = db.execute(
        select(NetworkCoverageRun.claimed_by)
        .where(NetworkCoverageRun.id == run_id)
        .with_for_update()
    ).scalar_one_or_none()
    return holder == executor_id


def _locked_status_and_holder(db: DbSession, run_id: uuid.UUID) -> tuple[str | None, str | None]:
    """#406 — the run's `status` and `claimed_by` as committed now, read
    under a row lock so neither can change before this transaction commits
    ((None, None) when the row is gone)."""
    row = db.execute(
        select(NetworkCoverageRun.status, NetworkCoverageRun.claimed_by)
        .where(NetworkCoverageRun.id == run_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]


def _missing_pair_count(rows: Sequence[Any], expected: frozenset[tuple[str, str]] | None) -> int:
    """#392 — how many of the run's pairs have no result row (0 when the
    expected pair set is not given)."""
    if expected is None:
        return 0
    return len(expected - {(r.origin_hub_id, r.dest_hub_id) for r in rows})


def _hold_for_resume(
    db: DbSession, run: NetworkCoverageRun, missing: int, executor_id: str | None
) -> None:
    """#392 — pairs have no result row at the completion (their write failed
    after its retries): note it on the run, while this executor still holds
    it, and leave the run `running` for a resume, which asks those pairs."""
    if executor_id is not None and not _still_claimed_by(db, run.id, executor_id):
        db.rollback()
        return
    run.summary = {
        **(run.summary or {}),
        "missing_pairs": missing,
        "missing_pairs_at": datetime.now(UTC).isoformat(),
    }
    db.commit()


async def _finalise_completed_run(
    *,
    run_id: uuid.UUID,
    elapsed_s: float,
    cfg: CoverageConfig,
    executor_id: str | None = None,
    expected_pairs: frozenset[tuple[str, str]] | None = None,
) -> None:
    """Phase-3 — recompute summary counters, optionally run the PR-E
    external-verify sweep, flip to 'completed' in one transaction.

    #392 — idempotent, so resume-safe: nothing is written before the one
    commit, and an interrupted completion leaves the run `running`, to be
    resumed with no pair left and completed again. With `executor_id`, the
    commit happens only while that executor still holds the run.

    #406 — the commit happens only while the run is still `running`, read
    under the row lock just before the flip: a status written meanwhile (a
    `cancelled` set by SQL during the verify sweep) is kept.

    #392 — recount guard: with `expected_pairs` (the run's frozen pair set),
    the run is completed only when every pair has its result row; else it
    raises `RunNeedsResume` and the resume asks the missing pairs."""
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            return
        if run.status != "running":
            # #392 — ended meanwhile by someone else (a status written by
            # SQL, PR-187): nothing to complete, and no completed over it.
            log.warning("network-coverage run %s is %s: not completed", run_id, run.status)
            return
        rows = (
            db.execute(select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id))
            .scalars()
            .all()
        )
        _apply_counters(run, [r.status for r in rows])
        missing = _missing_pair_count(rows, expected_pairs)
        if missing:
            _hold_for_resume(db, run, missing, executor_id)
            raise RunNeedsResume(f"{missing} pairs have no result row yet")
        run.finished_at = datetime.now(UTC)

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
            **_kept_resume_summary(run.summary),
        }
        status, holder = _locked_status_and_holder(db, run_id)
        if status != "running":
            # #406 — ended by someone else during the verify sweep (a status
            # written by SQL, PR-187): keep it, write nothing over it.
            db.rollback()
            log.warning(
                "coverage_executor.status_changed run_id=%s status=%s phase=completion: "
                "kept, not completed",
                run_id,
                status,
            )
            return
        if executor_id is not None and holder != executor_id:
            db.rollback()
            log.warning(
                "coverage_executor.claim_lost run_id=%s executor=%s phase=completion",
                run_id,
                executor_id,
            )
            return
        # Flip status LAST so a partially-completed sweep that exits via
        # an unexpected exception leaves the run in 'running': the
        # executor resumes it once its heartbeat is stale (#392).
        run.status = "completed"
        db.commit()


def _run_elapsed_s(snap: _Phase1Snapshot, started: float) -> float:
    """The run's wall-clock time: this execution's, or since its first
    start for a resumed run (#392), whose earlier part ran elsewhere."""
    if snap.resumed and snap.started_at is not None:
        return max(0.0, (datetime.now(UTC) - snap.started_at).total_seconds())
    return time.monotonic() - started


async def _run_pairs(run_id: uuid.UUID, snap: _Phase1Snapshot, executor_id: str | None) -> bool:
    """Phase 2: the pairs still to ask, with bounded concurrency; each pair
    gets its own short-lived DB sessions. False when the loop failed (the
    run is then marked failed, unless its claim was lost)."""
    semaphore = asyncio.Semaphore(snap.cfg.pair_parallelism)
    # PR-187 — per-run TTL cache for the DB-status cancel check.
    # Lives only for the duration of this execute_run call (no leak
    # across runs); shared across every pair coroutine so the cheap
    # SELECT is amortised.
    cancel_cache: dict[uuid.UUID, tuple[float, bool]] = {}
    # #397 — one DB cancel check before the pairs start. It fills the cache,
    # so each pair's first check is answered from it; without it every pair
    # coroutine saw the empty cache at once and queued its own SELECT on the
    # 2-thread DB executor (~0.5 s at the start of a large run). The pairs
    # still check as before (fresh answers within 3 s, then the DB again).
    await _run_stopped(run_id, cancel_cache)
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
                for o, d in snap.pairs_to_run()
            ),
            return_exceptions=False,
        )
    except Exception as exc:
        log.exception("network-coverage run %s failed mid-loop", run_id)
        if not is_abandoned(run_id):
            reason = f"an error stopped the pairs: {type(exc).__name__}: {exc}"[:500]
            await _off_loop(_mark_run_failed, run_id, executor_id, reason)
        return False
    return True


async def _phase1_with_retry(run_id: uuid.UUID) -> _Phase1Snapshot | None:
    """#392 — phase 1, retried on a transient database error (a DB blip as
    a resumed run starts must not fail a run of hours). Still failing after
    `_PHASE1_RETRY_DELAYS_S`, the run is left for a resume (`RunNeedsResume`)."""
    attempt = 0
    while True:
        try:
            return await _off_loop(_phase1_snapshot_and_start, run_id)
        except _TRANSIENT_DB_ERRORS as exc:
            if attempt >= len(_PHASE1_RETRY_DELAYS_S):
                raise RunNeedsResume(
                    f"a database error kept the run from starting: {type(exc).__name__}"
                ) from exc
            delay = _PHASE1_RETRY_DELAYS_S[attempt]
            attempt += 1
            log.warning(
                "coverage run %s: database error while starting, retry %d in %.1f s",
                run_id,
                attempt,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


async def execute_run(run_id: uuid.UUID, *, executor_id: str | None = None) -> None:
    """Drive a coverage run to completion.

    #392 — called by the worker's coverage executor once it holds the run
    (`executor_id`); never by web. Manages its own DB sessions. Idempotent
    in the loose sense: re-running on a completed run is a no-op, and
    re-running a `running` run (a resume) asks only the pairs that have no
    result yet. `executor_id=None` (tests) skips the claim checks. Raises
    `RunNeedsResume` when the run must be resumed rather than ended.

    PR-1 — wraps the whole body in try/finally so the cancel-event
    registration set by `register_cancel(run_id)` is always cleared, even
    when execute_run exits via an exception, early-return, or terminal-
    state short-circuit.
    """
    log.info("network-coverage run %s starting", run_id)
    started = time.monotonic()
    register_cancel(run_id)
    try:
        snap = await _phase1_with_retry(run_id)
        if snap is None or not snap.pairs:
            log.error("run %s has no pairs to execute", run_id)
            return
        if not await _run_pairs(run_id, snap, executor_id):
            return

        if executor_id is not None and not is_cancelled(run_id):
            # #392 — a Stop click written by web after the last pair's check
            # still counts, as the in-process Stop event did.
            await _run_stopped(run_id, {})
        elapsed_s = _run_elapsed_s(snap, started)

        if is_abandoned(run_id):
            # #392 — another executor holds the run now: no terminal write.
            log.warning("coverage_executor.claim_lost run_id=%s phase=pairs", run_id)
            return
        # PR-1 — if a Stop click set the cancel event mid-Phase-2, persist
        # the partial state under status='cancelled' instead of running
        # Phase 3. The cells already processed survive.
        if is_cancelled(run_id):
            await _off_loop(_persist_cancelled_run, run_id=run_id, elapsed_s=elapsed_s)
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
        # #392 — the completion phase (counter recount, optional verify sweep,
        # one commit) on a private loop: its blocking calls share one session.
        await _run_on_private_loop(
            _finalise_completed_run(
                run_id=run_id,
                elapsed_s=elapsed_s,
                cfg=snap.cfg,
                executor_id=executor_id,
                expected_pairs=frozenset((o.id, d.id) for o, d in snap.pairs),
            )
        )
    finally:
        clear_cancel(run_id)
        _STOP_REQUESTED_IN_DB.discard(run_id)
        _ABANDONED.discard(run_id)


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
    db: DbSession, search_id: uuid.UUID | None, depart_at: datetime
) -> list[dict[str, Any]]:
    """PR-196a — pull the VIATOR-side trip dicts for one search_id.

    Used by the alignment scorer to compare against ÖBB's itineraries.
    Returns the canonical (legs[], duration_seconds, num_transfers,
    departure_at, arrival_at, modes) shape `_fetch_trips_by_search`
    emits in the admin API — the alignment scorer only reads `legs`,
    but threading the full dict keeps the data contract identical to
    what the modal renders. Empty list when search_id is NULL or the
    JOIN finds no rows (status='ok' but no trips = the alignment
    treats VIATOR as one-sided empty).

    Filtered to `filter_trips_from_depart_at(..., depart_at)` — a
    cell's VIATOR trips span the whole K-slot day window (see that
    function's docstring), while ÖBB's `verify_via_oebb_hafas` call is
    a single forward-looking search anchored at `depart_at`. Without
    this, the scorer compared ÖBB's few depart_at-anchored itineraries
    against ALL of VIATOR's day-spanning ones and would rarely find a
    real match even when one exists."""
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
    trips = [
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
    return filter_trips_from_depart_at(trips, depart_at)


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
                    viator_trips = _fetch_viator_trips_for_search(
                        db, row.journey_search_id, run.depart_at
                    )
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
#
# Scope (#388 review): the retry wraps the WHOLE K-slot fetch
# (`_fetch_plan_sliced`) and fires only when every slot failed to connect
# (see `_slot_failure`). A connection error on only SOME slots is not
# retried: those slots' trips are missing from the pair's partial result,
# as before. A per-slot retry would eat into the slot budget and is left
# for a follow-up.
#
# Worst case for a pair whose engine accepts no connection: 4 attempts,
# each ceil(slots / slots in parallel) x the 5 s connect timeout (MOTIS
# and OTP alike), plus 60 s of backoff (+-20 % jitter). With the defaults
# (6 slots, 3 at a time) about 90-110 s; with 2 at a time about 110-130 s.
_CONNECT_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 40.0)

# +-20 % jitter on each backoff delay so pairs that failed together (one
# engine hiccup under 6 parallel pairs) do not all retry in the same second.
_CONNECT_RETRY_JITTER = (0.8, 1.2)
# Not used for anything secret: a plain PRNG is right for spreading retries.
_BACKOFF_RANDOM = random.Random()  # noqa: S311  # nosec B311

_CONNECT_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    # 2026-07-02 — eu19 sweep post-healthcheck-fix still showed a handful
    # of "Server disconnected without sending a response" failures: the
    # TCP connection is accepted but MOTIS/OTP drops it before replying,
    # which surfaces as RemoteProtocolError rather than ConnectError. Same
    # class of transient session-bounce symptom (a restart landing mid-
    # request instead of before it), so it gets the same retry treatment.
    httpx.RemoteProtocolError,
    # 2026-10-22 (#388) — eu19 run with 6 pairs in parallel: one pair failed
    # with ConnectTimeout while MOTIS stayed up. A connect timeout means the
    # request never reached the engine (its accept queue was briefly full,
    # or it was busy for a moment), so like ConnectError it says nothing
    # about the pair itself. httpx makes it a TimeoutException, not a
    # ConnectError, so it must be named here on its own.
    #
    # Deliberately NOT retried, and why:
    # - ReadTimeout: the engine accepted the request and is too slow to
    #   answer it; that is the pair's own result and must report fast.
    # - WriteTimeout: the engine accepted the connection but stopped
    #   reading the request; retrying would only delay a broken pair.
    # - PoolTimeout: the planner clients open a fresh AsyncClient for each
    #   call (default pool, one request at a time), so the pool never makes
    #   a call wait; if it ever did, it would be contention inside VIATOR,
    #   not a sign of the engine's health.
    httpx.ConnectTimeout,
)


def _backoff_delay_s(base_s: float) -> float:
    """One backoff delay: `base_s` with +-20 % jitter."""
    low, high = _CONNECT_RETRY_JITTER
    return base_s * _BACKOFF_RANDOM.uniform(low, high)


async def _call_with_connect_retry[T](
    fetch: Callable[[], Awaitable[T]], run_id: uuid.UUID | None = None
) -> T:
    """Retry `fetch()` on connection-level errors only
    (`_CONNECT_RETRY_EXCEPTIONS`: ConnectError, RemoteProtocolError,
    ConnectTimeout), with growing, jittered backoff.

    Every other exception (read, write or pool timeout, HTTP error, bad
    response shape, ...) propagates on the first attempt — those aren't
    caused by a session bounce or a full accept queue, and retrying
    wouldn't change the outcome, it would just make a genuinely-broken
    pair take longer to report as such.

    With `run_id`, a Stop of that run (its in-process cancel event) is
    checked before each backoff sleep and before each retry: the last
    connection error is raised at once instead of waiting and retrying.
    """
    for base_s in _CONNECT_RETRY_DELAYS_S:
        try:
            return await fetch()
        except _CONNECT_RETRY_EXCEPTIONS:
            if run_id is not None and is_cancelled(run_id):
                raise
            await asyncio.sleep(_backoff_delay_s(base_s))
            if run_id is not None and is_cancelled(run_id):
                raise
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
        raw, trips = await _call_with_connect_retry(_fetch, run_id)
        response_ms = _elapsed_ms(started)
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
        response_ms = _elapsed_ms(started)
        # Distinguish timeout from generic error for the matrix colouring.
        cls_name = type(exc).__name__
        status = _failure_status(exc)
        error_message = f"{cls_name}: {exc}"[:500]
        log.warning(
            "coverage run %s pair %s→%s failed: %s",
            run_id,
            origin.id,
            dest.id,
            error_message,
        )

    # Persist into journey_searches + journey_trips (best-effort, so the
    # click-cell drilldown can reuse the v0.1.26 trip-card UI), then the
    # result row. Always persist the coverage result, even when the recorder
    # write failed — the matrix is the canonical artifact. Both writes are
    # one off-loop unit (#392), see `_write_pair`.
    await _write_pair(
        _record_pair_journey_search,
        record_kwargs={
            "run_id": run_id,
            "session_id": session_id,
            "origin": origin,
            "dest": dest,
            "depart_at": depart_at,
            "status": status,
            "response_ms": response_ms,
            "raw": raw,
            "trips": trips,
            "error_message": error_message,
        },
        result_kwargs={
            "run_id": run_id,
            "origin": origin,
            "dest": dest,
            "status": status,
            "response_ms": response_ms,
            "num_itineraries": num_itineraries,
            "best_duration_seconds": best_duration_seconds,
            "best_num_transfers": best_num_transfers,
            "best_operators": best_operators,
        },
    )


async def _write_pair(
    record: Callable[..., tuple[uuid.UUID | None, str | None]],
    *,
    record_kwargs: dict[str, Any],
    result_kwargs: dict[str, Any],
) -> bool:
    """Write one pair: its journey search (`record`, which returns the
    search id and the pair's error message), then its result row and
    counters (`_persist_pair_coverage_result`, shared by both modes so the
    matrix wire-format and the counter updates live in one place).

    Both writes are ONE call on the coverage DB executor, shielded: once
    started, a cancellation of the run's task (e.g. a worker shutdown) does
    not stop it halfway (#392). True when the pair has its result row."""
    return await asyncio.shield(
        _off_loop(_record_then_persist, record, record_kwargs, result_kwargs)
    )


def _record_then_persist(
    record: Callable[..., tuple[uuid.UUID | None, str | None]],
    record_kwargs: dict[str, Any],
    result_kwargs: dict[str, Any],
) -> bool:
    """The blocking body of `_write_pair`, in one executor thread.

    #392 — the journey search and the result row are ONE transaction (the
    recorder's part in a savepoint, so its failure still leaves the result
    row): a process killed in between commits neither, so a resumed run
    never finds an orphan search for a pair without a result and asks it
    once more with exactly one search in the end. A transient database
    error is retried (`_WRITE_RETRY_DELAYS_S`); if the write still fails
    the pair simply has no result row: the completion sees it missing and
    the run is resumed instead of completed (`RunNeedsResume`). A result
    row already there (written by an earlier holder of the run) counts as
    written; the second one is refused by the unique guard."""
    origin, dest = result_kwargs["origin"], result_kwargs["dest"]
    where = f"coverage run {result_kwargs['run_id']} pair {origin.id}→{dest.id}"
    for attempt in range(len(_WRITE_RETRY_DELAYS_S) + 1):
        try:
            _write_pair_once(record, record_kwargs, result_kwargs)
            return True
        except IntegrityError as exc:
            if _UNIQUE_RESULT_CONSTRAINT in str(exc):
                log.warning("%s: already has a result row (an earlier holder); kept", where)
                return True
            log.exception("%s: result write failed", where)
            return False
        except _TRANSIENT_DB_ERRORS:
            if attempt == len(_WRITE_RETRY_DELAYS_S):
                break
            log.warning("%s: database error on the result write, retrying", where, exc_info=True)
            time.sleep(_WRITE_RETRY_DELAYS_S[attempt])
        except Exception:
            log.exception("%s: result write failed", where)
            return False
    log.error(
        "%s: result write failed after %d attempts; the pair stays without a result "
        "and is asked again when the run resumes",
        where,
        len(_WRITE_RETRY_DELAYS_S) + 1,
    )
    return False


def _write_pair_once(
    record: Callable[..., tuple[uuid.UUID | None, str | None]],
    record_kwargs: dict[str, Any],
    result_kwargs: dict[str, Any],
) -> None:
    with SessionLocal() as db:
        journey_search_id, error_message = record(db=db, **record_kwargs)
        _persist_pair_coverage_result(
            db=db,
            **result_kwargs,
            error_message=error_message,
            journey_search_id=journey_search_id,
        )
        db.commit()


def _record_pair_journey_search(
    *,
    db: DbSession,
    run_id: uuid.UUID,
    session_id: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    status: str,
    response_ms: int,
    raw: dict[str, Any],
    trips: list[dict[str, Any]],
    error_message: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    """Single-session twin of `_record_fanout_journey_search`: write the
    journey_searches row, the execution and its trips for one pair.

    Returns ``(journey_search_id, error_message)``: the search id (None when
    the write failed) and the pair's error message, with the recorder's
    failure appended to it when the write failed. Blocking: called through
    `_off_loop`, never on the event loop (#392). #392 — writes in a savepoint
    of `db`, the transaction that also writes the result row (see
    `_record_then_persist`); a transient database error is raised so the
    whole pair write is retried.

    v0.1.29.4 — endpoint must be one of ('plan','compare','fanout') and
    requested_time_kind must be one of ('depart_at','arrive_by') per the
    CheckConstraints on journey_searches. v0.1.27 shipped with
    endpoint='network-coverage' / requested_time_kind='depart' which
    silently violated both constraints — every coverage row's INSERT
    rolled back, leaving journey_search_id NULL on every result and
    the modal forever showing "No linked journey-search row". The
    coverage-vs-live distinction is preserved via
    network_coverage_results.journey_search_id (the FK from the
    coverage row pins it back to its parent run), so flattening
    endpoint='plan' loses no analytical signal.
    """
    journey_search_id: uuid.UUID | None = None
    try:
        with db.begin_nested():
            search = _begin_coverage_search(
                db, endpoint="plan", origin=origin, dest=dest, depart_at=depart_at
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
    except _TRANSIENT_DB_ERRORS:
        raise
    except Exception as recorder_exc:
        log.exception(
            "coverage run %s pair %s→%s: recorder write failed (continuing)",
            run_id,
            origin.id,
            dest.id,
        )
        # v0.1.29.4 — surface the recorder failure into the matrix row
        # so the operator can see *why* the drilldown link is missing
        # (e.g. CheckConstraint violation) instead of the generic
        # "recorder write failed" message in the modal. Don't clobber
        # an existing OTP-side error_message; append/keep both.
        recorder_msg = f"[recorder] {type(recorder_exc).__name__}: {recorder_exc}"[:500]
        error_message = f"{error_message} | {recorder_msg}" if error_message else recorder_msg
    return journey_search_id, error_message


def _begin_coverage_search(
    db: DbSession, *, endpoint: str, origin: Hub, dest: Hub, depart_at: datetime
) -> JourneySearch:
    """The journey_searches row of one coverage pair (both modes): no user
    or IP (a background task), the hubs as endpoints, departing at the run's
    `depart_at`."""
    return recorder.begin_search(
        db,
        user_id=None,
        ip=None,
        endpoint=endpoint,
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
        raw, trips = await _call_with_connect_retry(_fetch, run_id)
        response_ms = _elapsed_ms(sub_start)
        sub_status = "ok" if trips else "no_route"
        return sid, sub_status, raw, trips, response_ms
    except Exception as exc:
        response_ms = _elapsed_ms(sub_start)
        cls_name = type(exc).__name__
        sub_status = _failure_status(exc)
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
    db: DbSession,
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

    Returns ``(journey_search_id, error_message_or_None)``. #392 — writes in
    a savepoint of `db`, as `_record_pair_journey_search` does.
    """
    try:
        with db.begin_nested():
            search = _begin_coverage_search(
                db, endpoint="fanout", origin=origin, dest=dest, depart_at=depart_at
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
        return search.id, None
    except _TRANSIENT_DB_ERRORS:
        raise
    except Exception as recorder_exc:
        log.exception(
            "coverage run %s pair %s→%s: recorder write failed (continuing)",
            run_id,
            origin.id,
            dest.id,
        )
        return None, f"[recorder] {type(recorder_exc).__name__}: {recorder_exc}"[:500]


def _persist_pair_coverage_result(
    *,
    db: DbSession,
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
    """Add one NetworkCoverageResult row and increment the run-level
    counters, in `db`'s transaction (#392: the caller commits it with the
    pair's journey search, see `_record_then_persist`). Shared by
    `_execute_pair` (single-session) and `_execute_pair_fanout` so the
    matrix wire-format stays bit-equivalent across both code paths and the
    counter updates are written in exactly one place.

    `session_ids` is fanout-only (list of sessions that returned ≥1 trip);
    `_execute_pair` passes None.
    """
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
            no_route_pairs=NetworkCoverageRun.no_route_pairs + (1 if status == "no_route" else 0),
            error_pairs=NetworkCoverageRun.error_pairs
            + (1 if status not in ("ok", "no_route", "skipped") else 0),
        )
    )


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

    # #392 — the merge (trip signatures read the DB), the recorder and the
    # result write are blocking: off the event loop, in this order.
    status, by_signature, sessions_with_trips, operators_union = await _off_loop(
        _merge_fanout_results, per_session
    )

    best_trip: dict[str, Any] | None = (
        min(
            (slot["best"] for slot in by_signature.values()),
            key=lambda t: t.get("duration_seconds") or 1 << 30,
        )
        if by_signature
        else None
    )
    response_ms = _elapsed_ms(pair_start)

    await _write_pair(
        _record_fanout_journey_search,
        record_kwargs={
            "run_id": run_id,
            "per_session": per_session,
            "origin": origin,
            "dest": dest,
            "depart_at": depart_at,
            "response_ms": response_ms,
            "status": status,
            "total_trips_unique": len(by_signature),
        },
        result_kwargs={
            "run_id": run_id,
            "origin": origin,
            "dest": dest,
            "status": status,
            "response_ms": response_ms,
            "num_itineraries": len(by_signature) if by_signature else 0,
            "best_duration_seconds": (
                int(best_trip.get("duration_seconds") or 0) if best_trip else None
            ),
            "best_num_transfers": (int(best_trip.get("num_transfers") or 0) if best_trip else None),
            "best_operators": ",".join(operators_union) if operators_union else None,
            "session_ids": sessions_with_trips or None,
        },
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
