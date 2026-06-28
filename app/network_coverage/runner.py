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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..db import SessionLocal
from ..journey import planner_dispatch, recorder
from ..models import (
    NetworkCoverageHub,
    NetworkCoverageResult,
    NetworkCoverageRun,
)
from ..models import Session as SessionRow
from ..models.sessions import SessionState
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

# Bounded parallelism — number of pairs to run simultaneously.
# 5 keeps OTP comfortable while still cutting wallclock by ~5x.
# Adjust via NETWORK_COVERAGE_PARALLELISM platform_config in the future
# if operators want it tunable.
_PARALLELISM = 5

# Per-pair HTTP timeout. We use a generous 60s — coverage runs are not
# user-facing, and a slow OTP returning a real itinerary is more
# valuable signal than a fast timeout.
_PER_PAIR_TIMEOUT_MS = 60_000

# v0.1.29.2 — coverage search parameters. Originally I shipped 50/24h
# in v0.1.29 to give "full-day visibility per pair" but that exceeded
# OTP's 60s apiProcessingTimeout for long-haul pairs on a France-wide
# multi-NAP graph (Paris-Paris pairs worked fine because the routing
# is local; Paris→Lille / Paris→Strasbourg / cross-cutting pairs all
# timed out — RAPTOR's per-call work scales near-quadratically with
# searchWindow on dense networks).
#
# v0.1.29.2 reduces the window to 4h (matching the v0.1.27 baseline
# that completed cleanly) but keeps numItineraries at 50 — so we still
# get ALL alternatives within a 4-hour departure window, just not the
# whole 24h. For 08:00 depart that's 08:00-12:00, which catches the
# bulk of weekday TGV service for any pair (Paris-Lyon has ~7-8 TGVs
# in that window, Paris-Marseille ~3-4).
#
# For full-day visibility: queued for v0.1.30 — a "time-of-day sweep"
# button that runs the matrix at 06:00 / 10:00 / 14:00 / 18:00 / 22:00
# and stitches the per-pair counts. That's the right architecture for
# 24h coverage on a heavy graph; jamming it into a single OTP call was
# the v0.1.29 mistake.
_COVERAGE_NUM_ITINERARIES = 50
_COVERAGE_SEARCH_WINDOW_SECONDS = 14_400  # 4h — same as live UI baseline


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


async def execute_run(run_id: uuid.UUID) -> None:
    """Drive a pending coverage run to completion.

    Designed to be called from a FastAPI BackgroundTask. Manages its own
    DB session lifecycle so the request's transaction can commit + return
    immediately. Idempotent in the loose sense: re-running on a completed
    run is a no-op.
    """
    log.info("network-coverage run %s starting", run_id)
    started = time.monotonic()
    pairs: list[tuple[Hub, Hub]] = []
    session_id_for_pairs: str | None = None
    depart_at_for_pairs: datetime | None = None
    run_mode: str = MODE_SINGLE_SESSION
    fanout_session_ids: list[str] = []
    # P1 MOTIS — engine snapshot read once at Phase 1 (alongside the session
    # ids). Per-pair helpers consult this instead of doing a DB lookup per
    # fetch_plan call. Single_session mode populates `engine_for_pairs`;
    # fanout mode populates `engine_by_session`.
    engine_for_pairs: str = "otp"
    engine_by_session: dict[str, str] = {}

    # Phase 1: snapshot the run inputs and flip to "running" in a short txn.
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            log.warning("network-coverage run %s not found — aborting", run_id)
            return
        if run.status not in ("pending", "running"):
            log.info(
                "network-coverage run %s already in terminal state %s — skipping",
                run_id,
                run.status,
            )
            return
        run.status = "running"
        run.started_at = datetime.now(UTC)
        db.commit()
        run_mode = run.mode
        if run_mode == MODE_SINGLE_SESSION:
            if run.session_id is None:
                log.warning("run %s mode=single_session has no session_id — aborting", run_id)
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                db.commit()
                return
            session_id_for_pairs = run.session_id
            engine_for_pairs = _resolve_session_engine(db, run.session_id)
        else:
            # Fanout — snapshot the eligible sessions (and their engines)
            # NOW; per-pair execution uses these without further DB lookups.
            fanout_session_ids, engine_by_session = _snapshot_fanout_sessions(db)
            if not fanout_session_ids:
                log.warning(
                    "run %s mode=fanout has no serving fanout-enabled sessions — aborting",
                    run_id,
                )
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                db.commit()
                return
        depart_at_for_pairs = run.depart_at
        # v0.1.31 — re-read active hubs from the DB at execute time.
        # We don't snapshot at create_run because the operator might
        # edit the hub list between create and execute (rare but
        # possible if the run was queued and processed minutes later);
        # using the latest active set keeps results consistent with
        # whatever the matrix UI is currently showing. total_pairs in
        # the run row was set at create time; if the operator edited
        # hubs in the meantime the actual count may differ — the
        # runner's per-pair counter handles divergence gracefully.
        hubs_now = _load_active_hubs(db)
        pairs = _hub_pairs(hubs_now, run.direction)

    if not pairs or depart_at_for_pairs is None:
        log.error("run %s has no pairs to execute", run_id)
        return

    # Phase 2: process pairs with bounded concurrency. Each pair gets
    # its own short-lived DB session — no transaction stays open
    # across the network call to OTP.
    semaphore = asyncio.Semaphore(_PARALLELISM)

    async def _one_pair(origin: Hub, dest: Hub) -> None:
        async with semaphore:
            if run_mode == MODE_FANOUT:
                await _execute_pair_fanout(
                    run_id=run_id,
                    session_ids=fanout_session_ids,
                    engine_by_session=engine_by_session,
                    origin=origin,
                    dest=dest,
                    depart_at=depart_at_for_pairs,
                )
            else:
                assert session_id_for_pairs is not None  # narrowed at Phase 1
                await _execute_pair(
                    run_id=run_id,
                    session_id=session_id_for_pairs,
                    engine=engine_for_pairs,
                    origin=origin,
                    dest=dest,
                    depart_at=depart_at_for_pairs,
                )

    try:
        await asyncio.gather(
            *(_one_pair(o, d) for o, d in pairs),
            return_exceptions=False,
        )
    except Exception:
        log.exception("network-coverage run %s failed mid-loop", run_id)
        with SessionLocal() as db:
            db.execute(
                update(NetworkCoverageRun)
                .where(NetworkCoverageRun.id == run_id)
                .values(status="failed", finished_at=datetime.now(UTC))
            )
            db.commit()
        return

    elapsed_s = time.monotonic() - started
    log.info(
        "network-coverage run %s completed in %.1fs (%d pairs)",
        run_id,
        elapsed_s,
        len(pairs),
    )

    # Phase 3: aggregate summary + flip to completed.
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        if run is None:
            return
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        # Compute summary counters one last time — the per-pair updates
        # incremented these but a final SUM is bug-resistant.
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
            "elapsed_seconds": elapsed_s,
            "median_response_ms": _median(
                [r.response_ms for r in rows if r.response_ms is not None]
            ),
            "p95_response_ms": _percentile(
                [r.response_ms for r in rows if r.response_ms is not None], 0.95
            ),
        }
        db.commit()


async def _execute_pair(
    *,
    run_id: uuid.UUID,
    session_id: str,
    engine: str,
    origin: Hub,
    dest: Hub,
    depart_at: datetime,
) -> None:
    """Run a single A→B search and persist the result row.

    Wraps the engine-appropriate `fetch_plan` (the same call the live
    journey UI makes) so coverage results are bit-equivalent to what an
    operator would see via the Search page — modulo the lat/lon coords
    which come from the hub preset rather than master_stations geocoding.
    `engine` is passed in (snapshotted at Phase 1) so this hot per-pair
    function doesn't pay a DB lookup; see `execute_run`.
    """
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
            timeout_ms=_PER_PAIR_TIMEOUT_MS,
            # v0.1.29 — full-day coverage mode (see module-level constants).
            num_itineraries=_COVERAGE_NUM_ITINERARIES,
            search_window_seconds=_COVERAGE_SEARCH_WINDOW_SECONDS,
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
) -> _FanoutSub:
    """One fetch_plan call for one (session, pair). Tolerates its own
    exception so a planner container being down doesn't poison the pair —
    the caller treats the returned status as "this session contributed
    nothing useful" and moves on. `engine` is the snapshotted backend
    for this session (see `execute_run`)."""
    sub_start = time.monotonic()
    try:
        raw, trips = await planner_dispatch.planner_for_engine(engine).fetch_plan(
            session_id=sid,
            from_lat=origin.lat,
            from_lon=origin.lon,
            to_lat=dest.lat,
            to_lon=dest.lon,
            when=depart_at,
            timeout_ms=_PER_PAIR_TIMEOUT_MS,
            num_itineraries=_COVERAGE_NUM_ITINERARIES,
            search_window_seconds=_COVERAGE_SEARCH_WINDOW_SECONDS,
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
    """
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
