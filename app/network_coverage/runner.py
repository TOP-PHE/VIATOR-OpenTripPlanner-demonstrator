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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from .. import config_service
from ..db import SessionLocal
from ..journey import planner_dispatch, recorder
from ..models import (
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ..models import Session as SessionRow
from ..models.sessions import SessionState
from . import external_verify  # PR-E — auto-verify-on-completion sweep
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
    )


# ─────────────────────── cooperative-cancel registry ───────────────────────
#
# PR-1 — Stop button. Each in-flight `execute_run` registers an
# `asyncio.Event` keyed by run_id in `_CANCEL_EVENTS`. The
# POST /runs/{id}/stop endpoint sets that event; the per-pair loop checks
# it before each pair and exits cleanly when set. Cells that already
# processed before the click stay in the DB — partial results are
# valuable. The endpoint NEVER touches `network_coverage_runs.status`
# directly; that write happens in `execute_run`'s post-loop branch once
# the runner has observed the signal, so the DB row's terminal-state
# guarantee (status='running' until the worker says otherwise) holds.
#
# Module-level dict because:
#  - FastAPI BackgroundTasks runs in the same process as the API,
#  - all coverage runs are single-process (no celery / no multi-worker
#    scheduling for this feature),
#  - swapping in a Redis-backed signal later is a one-helper change if
#    we ever go multi-process.
_CANCEL_EVENTS: dict[uuid.UUID, asyncio.Event] = {}


def register_cancel(run_id: uuid.UUID) -> asyncio.Event:
    """Register (or reuse) the cancel event for `run_id`.

    Idempotent: if a previous registration is still present (e.g. the
    worker died and is being relaunched on the same run id — unlikely
    in practice but defensive), the existing event is returned so a
    Stop click in the meantime still fires for the new worker."""
    ev = _CANCEL_EVENTS.get(run_id)
    if ev is None:
        ev = asyncio.Event()
        _CANCEL_EVENTS[run_id] = ev
    return ev


def clear_cancel(run_id: uuid.UUID) -> None:
    """Drop the cancel event from the registry. Safe to call multiple
    times — used in `execute_run`'s `finally` to keep the dict bounded."""
    _CANCEL_EVENTS.pop(run_id, None)


def is_cancelled(run_id: uuid.UUID) -> bool:
    """True when the cancel event for `run_id` is set. Cheap O(1) dict
    lookup — called from inside the per-pair hot loop."""
    ev = _CANCEL_EVENTS.get(run_id)
    return ev is not None and ev.is_set()


def request_cancel(run_id: uuid.UUID) -> bool:
    """Public entry point for the POST /stop endpoint. Returns True when
    a live cancel event was found and set, False when no event was
    registered (i.e. the run isn't actually being processed in this
    worker — caller should re-check the DB row state and 409 if needed).

    Does NOT flip `status='cancelled'` on the row — that's the runner's
    job once it has observed the signal and finished the in-flight
    write, so we don't end up with status='cancelled' but the runner
    still mid-pair persisting a fresh row."""
    ev = _CANCEL_EVENTS.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


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
    """Frozen result of Phase-1 setup: everything the per-pair loop needs.

    Extracted from `execute_run` so the outer function stays under Sonar's
    cognitive-complexity ceiling. Per-pair execution reads everything it
    needs from this snapshot (no further DB lookups during Phase 2).
    """

    run_mode: str
    session_id_for_pairs: str | None
    engine_for_pairs: str
    fanout_session_ids: list[str]
    engine_by_session: dict[str, str]
    depart_at_for_pairs: datetime
    pairs: list[tuple[Hub, Hub]]
    cfg: CoverageConfig


def _resolve_run_mode_targets(
    db: DbSession, run: NetworkCoverageRun
) -> tuple[str | None, str, list[str], dict[str, str]]:
    """Resolve the session(s) and engine(s) the runner will call into.

    Mutates `run.status='failed'` (and stamps `finished_at`, commits) when
    the run's mode is unsatisfiable so the outer Phase-1 helper can short-
    circuit cleanly. Returns
    (session_id_for_pairs, engine_for_pairs, fanout_session_ids, engine_by_session)
    — single_session populates the first two, fanout the last two.
    """
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
    """Phase-1 of `execute_run` — validate the run, flip to running, snapshot
    the inputs the per-pair loop needs (incl. the frozen CoverageConfig).

    Returns None when the run is unprocessable (missing, already terminal,
    single_session-with-no-session_id, fanout-with-no-eligible-sessions).
    The caller's `finally` will still drop the cancel-event registration.

    v0.1.31 — hubs are re-read at execute time (not snapshotted at
    create_run), so operators who edit the hub list between create and
    execute see results consistent with the live matrix UI.
    """
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
        # PR-2 — freeze the tunables NOW. Editing /admin/config mid-run
        # must not perturb the in-flight job.
        cfg = _load_coverage_config(db)
        session_id_for_pairs, engine_for_pairs, fanout_session_ids, engine_by_session = (
            _resolve_run_mode_targets(db, run)
        )
        if run.status == "failed":
            return None
        hubs_now = _load_active_hubs(db)
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
        )


async def _process_pair_with_cancel(
    *,
    run_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    snap: _Phase1Snapshot,
    origin: Hub,
    dest: Hub,
) -> None:
    """PR-1 — per-pair coroutine with cooperative cancel.

    Checked at the top (after the semaphore wait so the operator's Stop
    click propagates to queued-but-not-running pairs) AND once more inside
    the slot so a click that lands while we're waiting for the semaphore
    short-circuits the actual OTP/MOTIS call. Cells that already wrote
    their result rows before the click stay in the DB.

    Extracted from `execute_run`'s inner closure so the parent function's
    cognitive complexity stays under Sonar's ceiling.
    """
    if is_cancelled(run_id):
        return
    async with semaphore:
        if is_cancelled(run_id):
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
            )
            return
        assert snap.session_id_for_pairs is not None  # narrowed at Phase 1
        await _execute_pair(
            run_id=run_id,
            session_id=snap.session_id_for_pairs,
            engine=snap.engine_for_pairs,
            origin=origin,
            dest=dest,
            depart_at=snap.depart_at_for_pairs,
            cfg=snap.cfg,
        )


def _mark_run_failed(run_id: uuid.UUID) -> None:
    """Stamp `status='failed' + finished_at=now()` in its own short txn.

    Used by `execute_run` when the per-pair `asyncio.gather` raises — we
    keep the failure-write isolated from the in-flight pair-loop session
    so a transactional collision can't double-fault.
    """
    with SessionLocal() as db:
        db.execute(
            update(NetworkCoverageRun)
            .where(NetworkCoverageRun.id == run_id)
            .values(status="failed", finished_at=datetime.now(UTC))
        )
        db.commit()


def _persist_cancelled_run(*, run_id: uuid.UUID, elapsed_s: float) -> None:
    """PR-1 — terminal-state writer for an operator-cancelled run.

    Recomputes counter rollups from whatever cells made it into the
    results table (some pairs may have been mid-flight when the Stop
    click landed; those that completed their persist before the cancel-
    check fires keep their row). Stamps `finished_at`, attaches a
    `cancelled_by_operator` marker on `summary`, and flips status to
    'cancelled'. Single transaction so the matrix UI flips atomically.

    Mirrors the structure of `_finalise_completed_run` minus the verify
    sweep — running ÖBB HAFAS over the partial cell set when the
    operator just asked us to stop would be wrong.
    """
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
    """Phase-3 of `execute_run` — recompute summary counters, optionally
    run the PR-E external-verify sweep, and flip the run to 'completed'
    in a single transaction.

    Extracted from `execute_run` so the parent function stays under
    Sonar's cognitive-complexity ceiling.
    """
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

        # PR-E — external-verify sweep dispatch. Extracted to keep
        # execute_run under Sonar's CC=15 ceiling; the helper handles
        # the opt-in check, candidate filtering, logging, and zero-fill
        # counters in one place. ORM mutations on the rows are flushed
        # by the single db.commit() below, atomic with status='completed'.
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
        # via an unexpected exception (above the broad-except in the
        # helper) leaves the run in 'running' and mark_orphaned_runs
        # will catch it at next startup.
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
    state short-circuit. Phase-1 setup, the per-pair coroutine, and the
    failed/cancelled/completed terminal writes all live in dedicated
    helpers so this function stays under Sonar's cognitive-complexity
    ceiling.
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
        # its own short-lived DB session — no transaction stays open
        # across the network call to OTP.
        semaphore = asyncio.Semaphore(snap.cfg.pair_parallelism)
        try:
            await asyncio.gather(
                *(
                    _process_pair_with_cancel(
                        run_id=run_id,
                        semaphore=semaphore,
                        snap=snap,
                        origin=o,
                        dest=d,
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

        # PR-1 — if a Stop click set the cancel event at any point during
        # Phase 2, persist the partial state under status='cancelled'
        # instead of running Phase 3 (the verify sweep + status='completed'
        # write). The cells already processed survive.
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
    test that doesn't thread one in."""
    cfg = cfg or CoverageConfig()
    zero = {"verified": 0, "ok": 0, "no_route": 0, "error": 0}
    if not getattr(run, "verify_externally", False):
        return zero
    candidate_rows = [r for r in rows if r.status in _VERIFY_STATUSES]
    log.info(
        "PR-E external-verify sweep starting for run %s - %d candidate cells",
        run.id,
        len(candidate_rows),
    )
    if not candidate_rows:
        return zero
    counters = await _run_external_verify_sweep(db=db, run=run, rows=candidate_rows, cfg=cfg)
    log.info("PR-E external-verify sweep done for run %s - %s", run.id, counters)
    return counters


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


async def _execute_pair(
    *,
    run_id: uuid.UUID,
    session_id: str,
    engine: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
    cfg: CoverageConfig | None = None,
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

    try:
        raw, trips = await planner_dispatch.planner_for_engine(engine).fetch_plan(
            session_id=session_id,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=depart_at,
            timeout_ms=cfg.pair_timeout_ms,
            # v0.1.29 — full-day coverage mode (now operator-tunable via
            # COVERAGE_NUM_ITINERARIES / COVERAGE_SEARCH_WINDOW_SECONDS).
            num_itineraries=cfg.num_itineraries,
            search_window_seconds=cfg.search_window_seconds,
        )
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
) -> _FanoutSub:
    """One fetch_plan call for one (session, pair). Tolerates its own
    exception so a planner container being down doesn't poison the pair —
    the caller treats the returned status as "this session contributed
    nothing useful" and moves on. `engine` is the snapshotted backend
    for this session (see `execute_run`).

    `cfg` is PR-2's operator-tunable knob bundle (timeout, num itineraries,
    search window). Defaults to `CoverageConfig()` for direct test use."""
    cfg = cfg or CoverageConfig()
    sub_start = time.monotonic()
    try:
        raw, trips = await planner_dispatch.planner_for_engine(engine).fetch_plan(
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
