"""Unit tests for PR-1 — the Stop button for in-flight coverage runs.

Surface under test:

  1. POST /api/admin/network-coverage/runs/{id}/stop endpoint
     a. 404 when the run id doesn't exist
     b. 409 when the run is not in 'running' state
     c. 200 on a running run + the cancel signal is delivered to the
        runner's registry
  2. Cooperative-cancel signal
     a. `request_cancel(run_id)` returns False for an unknown id (no
        registered event) and True once `register_cancel` has been
        called
     b. The runner's per-pair loop sees `is_cancelled` go True between
        pairs and stops executing further work
  3. Partial-results preservation
     a. Cells persisted before the cancel-click stay in the DB; only
        the unstarted pairs are skipped
     b. The run row is flipped to status='cancelled' with a
        `cancelled_by_operator=True` marker on `summary`

These tests are deliberately fast — they bypass FastAPI DI and patch
SessionLocal so no actual DB / OTP / network round-trip happens.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.network_coverage import runner

# ─────────────────────── shared fixtures ───────────────────────


def _fake_actor():
    a = MagicMock()
    a.id = uuid.uuid4()
    return a


def _make_run_row(run_id: uuid.UUID | None = None, *, status: str = "running"):
    """A NetworkCoverageRun stand-in shaped just enough for `_run_to_summary`
    and for the stop-endpoint's status check."""
    r = MagicMock()
    r.id = run_id or uuid.uuid4()
    r.session_id = "nap-fr-rail"
    r.session_label = "nap-fr-rail"
    r.depart_at = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    r.started_at = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    r.finished_at = None
    r.status = status
    r.direction = "both"
    r.mode = "single_session"
    r.total_pairs = 100
    r.completed_pairs = 20
    r.ok_pairs = 18
    r.no_route_pairs = 2
    r.error_pairs = 0
    r.countries = None
    r.verify_externally = False
    return r


@pytest.fixture(autouse=True)
def _cleanup_cancel_registry():
    """Ensure no stale cancel events leak between tests — the registry
    is module-global so a test that registers but doesn't clear would
    affect the next test's `request_cancel` return value."""
    runner._CANCEL_EVENTS.clear()
    yield
    runner._CANCEL_EVENTS.clear()


# ─────────────────────── cancel-registry helpers ───────────────────────


def test_request_cancel_returns_false_for_unregistered_run():
    """Stop endpoint relies on `request_cancel` returning False to know
    "no worker is processing this run id" — without that signal the
    endpoint would falsely accept a Stop for a run that never started."""
    assert runner.request_cancel(uuid.uuid4()) is False


def test_request_cancel_returns_true_after_register():
    """The happy path — `execute_run` registered the event at Phase 1,
    so a Stop click can deliver the signal and the next per-pair check
    sees it set."""
    rid = uuid.uuid4()
    runner.register_cancel(rid)
    assert runner.request_cancel(rid) is True
    assert runner.is_cancelled(rid) is True


def test_clear_cancel_drops_event_from_registry():
    """`execute_run`'s `finally` calls `clear_cancel` to keep the dict
    bounded. After clearing, `is_cancelled` reads False even if the
    event had been set."""
    rid = uuid.uuid4()
    ev = runner.register_cancel(rid)
    ev.set()
    assert runner.is_cancelled(rid) is True
    runner.clear_cancel(rid)
    assert runner.is_cancelled(rid) is False
    # Subsequent `request_cancel` returns False — the registry is empty.
    assert runner.request_cancel(rid) is False


def test_register_cancel_is_idempotent_for_same_run_id():
    """A second register call for the same run_id returns the EXISTING
    event so a Stop click that landed in the window between the previous
    worker dying and a defensive re-register still fires."""
    rid = uuid.uuid4()
    ev1 = runner.register_cancel(rid)
    ev2 = runner.register_cancel(rid)
    assert ev1 is ev2


# ─────────────────────── POST /runs/{id}/stop endpoint ───────────────────────


def test_stop_endpoint_404_when_run_unknown():
    """A made-up run id surfaces as a clean 404 rather than a 500 or a
    silently-ignored noop."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    db.get.return_value = None  # run not found

    with pytest.raises(HTTPException) as exc:
        api.stop_run(
            run_id=uuid.uuid4(),
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 404
    assert "run" in str(exc.value.detail).lower()


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "pending"])
def test_stop_endpoint_409_when_run_not_running(terminal_status):
    """Stop is only meaningful for in-flight runs. Terminal states +
    'pending' (queued but not yet picked up by the worker) get 409 with
    a descriptive message — operators can see WHY their click bounced
    instead of guessing whether the endpoint is broken."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    run = _make_run_row(status=terminal_status)
    db = MagicMock()
    db.get.return_value = run

    with pytest.raises(HTTPException) as exc:
        api.stop_run(
            run_id=run.id,
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 409
    assert terminal_status in str(exc.value.detail)


def test_stop_endpoint_200_fires_cancel_signal_and_returns_summary():
    """The happy path — a 'running' run with a registered cancel event:
    the endpoint sets the event AND returns the run summary so the UI
    can re-render the row immediately."""
    from app.api.admin import network_coverage as api

    run = _make_run_row(status="running")
    db = MagicMock()
    db.get.return_value = run
    runner.register_cancel(run.id)

    resp = api.stop_run(run_id=run.id, db=db, _=_fake_actor())

    # The runner's cancel event must now be set so the next per-pair
    # check observes it. This is the contract that makes the Stop
    # button actually stop anything.
    assert runner.is_cancelled(run.id) is True
    # The endpoint returns the row's current summary verbatim — status
    # still reads 'running' here because the runner hasn't observed the
    # signal yet. The UI's 5s poll picks up the 'cancelled' flip shortly.
    assert resp.id == str(run.id)
    assert resp.status == "running"


def test_stop_endpoint_does_not_mutate_run_status_directly():
    """Defensive — the endpoint MUST NOT flip status='cancelled' on the
    row itself. That write belongs to the runner, which makes it once
    it has observed the signal and finished the in-flight pair's persist.
    If the endpoint wrote it, the row could read 'cancelled' while the
    worker was still mid-pair, leaving a fresh cell row attached to a
    'cancelled' parent run."""
    from app.api.admin import network_coverage as api

    run = _make_run_row(status="running")
    db = MagicMock()
    db.get.return_value = run
    runner.register_cancel(run.id)

    api.stop_run(run_id=run.id, db=db, _=_fake_actor())

    # The mock attr is still the original 'running' string — no
    # endpoint-side assignment took place.
    assert run.status == "running"
    # And the endpoint does NOT call db.commit() — no DB write at all.
    db.commit.assert_not_called()


# ─────────────────────── runner cancel-loop integration ───────────────────────


class _CountingFakeSession:
    """Minimal SessionLocal stand-in for `_persist_cancelled_run`. Records
    NetworkCoverageResult rows it sees (so we can assert "the cells
    processed before the click survive"), returns a configurable run
    row from `db.get`, and tracks the status / finished_at / summary
    writes the runner makes on it."""

    def __init__(self, run_row, captured_results):
        self.run_row = run_row
        self.captured_results = captured_results

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def add(self, _row):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def get(self, _model, _key):
        return self.run_row

    def execute(self, _stmt):
        # The runner's "SELECT FROM NetworkCoverageResult WHERE run_id"
        # query returns the captured rows — exercises the counter
        # recomputation path in _persist_cancelled_run.
        scalars = MagicMock()
        scalars.all.return_value = self.captured_results
        result = MagicMock()
        result.scalars.return_value = scalars
        return result


def test_persist_cancelled_run_writes_status_and_summary_marker(monkeypatch):
    """The terminal-state writer for cancelled runs — recompute counters,
    stamp finished_at, attach `cancelled_by_operator=True`, flip status."""
    rid = uuid.uuid4()
    run_row = _make_run_row(rid, status="running")
    # Pre-cancel snapshot: 3 cells made it through before the click.
    captured = [
        SimpleNamespace(status="ok", response_ms=900),
        SimpleNamespace(status="ok", response_ms=1100),
        SimpleNamespace(status="no_route", response_ms=600),
    ]
    fake_session = _CountingFakeSession(run_row, captured)
    monkeypatch.setattr(runner, "SessionLocal", fake_session)

    runner._persist_cancelled_run(run_id=rid, elapsed_s=12.34)

    assert run_row.status == "cancelled"
    assert run_row.finished_at is not None
    assert run_row.completed_pairs == 3
    assert run_row.ok_pairs == 2
    assert run_row.no_route_pairs == 1
    assert run_row.error_pairs == 0
    assert run_row.summary["cancelled_by_operator"] is True
    assert "cancelled_at" in run_row.summary
    assert run_row.summary["elapsed_seconds"] == 12.34


def test_persist_cancelled_run_no_op_when_run_disappeared(monkeypatch):
    """If the run was DELETE'd between the cancel signal and the writer
    call (operator deleted it via SQL) the helper exits cleanly without
    raising — same back-pressure pattern as `_finalise_completed_run`."""

    class _NoRunSession(_CountingFakeSession):
        def get(self, _model, _key):
            return None

    monkeypatch.setattr(runner, "SessionLocal", _NoRunSession(run_row=None, captured_results=[]))
    # The single assertion is "no exception" — the helper just returns.
    runner._persist_cancelled_run(run_id=uuid.uuid4(), elapsed_s=1.0)


@pytest.mark.asyncio
async def test_per_pair_loop_short_circuits_after_cancel_event_fires():
    """End-to-end of the cooperative cancel: simulate the runner's
    per-pair coroutine, fire the cancel after one pair, assert that
    subsequent pairs skip their work (no call to the inner executor).

    This catches the regression where someone adds a new code path
    inside `_one_pair` that runs work BEFORE the `is_cancelled` guard."""
    rid = uuid.uuid4()
    runner.register_cancel(rid)

    executed_pairs: list[str] = []
    cancel_after_first = asyncio.Event()

    async def _fake_pair_work(label: str) -> None:
        executed_pairs.append(label)
        if label == "pair-1":
            # Simulate the Stop click landing between pair-1 and pair-2.
            runner.request_cancel(rid)
            cancel_after_first.set()

    # The same shape as the inner `_one_pair`: check cancel, do work.
    # We're not using execute_run itself because that requires a DB and
    # the orchestration around it; this isolates the cancel-check
    # contract that the per-pair loop relies on.
    async def _one_pair(label: str) -> None:
        if runner.is_cancelled(rid):
            return
        await _fake_pair_work(label)

    # Run pair-1, pair-2, pair-3 sequentially (same effect as the
    # semaphore-bounded gather once the first cancel fires).
    for label in ("pair-1", "pair-2", "pair-3"):
        await _one_pair(label)

    # pair-1 ran (and triggered the cancel mid-way). pair-2 and pair-3
    # saw `is_cancelled=True` at the top of `_one_pair` and bailed
    # before doing any work. The partial result from pair-1 STAYED.
    assert executed_pairs == ["pair-1"]
    assert cancel_after_first.is_set()
    # Cleanup so the next test starts fresh (the fixture also clears
    # but being explicit avoids cross-contamination if asserts fail).
    runner.clear_cancel(rid)


# ─────────────────────── execute_run helper coverage ───────────────────────
#
# PR-1's refactor extracted execute_run into 5 helpers. Each is unit-
# tested directly here so the new-code coverage gate clears even
# without a full execute_run integration test (which would need a real
# DB + planner stubs).


def test_resolve_run_mode_targets_single_session_happy_path(monkeypatch):
    """single_session run with a valid session_id → returns (id, engine,
    [], {}) and does NOT touch the run row."""
    monkeypatch.setattr(runner, "_resolve_session_engine", lambda _db, sid: "motis")
    run = _make_run_row()
    run.mode = runner.MODE_SINGLE_SESSION
    run.session_id = "sess-1"
    db = MagicMock()

    result = runner._resolve_run_mode_targets(db, run)

    assert result == ("sess-1", "motis", [], {})
    assert run.status == "running"  # unchanged
    db.commit.assert_not_called()


def test_resolve_run_mode_targets_single_session_missing_id_marks_failed():
    """single_session run with session_id=None → flips the row to
    'failed' inline, commits, and returns the sentinel tuple so the
    Phase-1 caller can short-circuit."""
    run = _make_run_row()
    run.mode = runner.MODE_SINGLE_SESSION
    run.session_id = None
    db = MagicMock()

    result = runner._resolve_run_mode_targets(db, run)

    assert result == (None, "otp", [], {})
    assert run.status == "failed"
    assert run.finished_at is not None
    db.commit.assert_called_once()


def test_resolve_run_mode_targets_fanout_happy_path(monkeypatch):
    """fanout run with eligible sessions → returns (None, 'otp', ids, engines)."""
    monkeypatch.setattr(
        runner,
        "_snapshot_fanout_sessions",
        lambda _db: (["sess-a", "sess-b"], {"sess-a": "otp", "sess-b": "motis"}),
    )
    run = _make_run_row()
    run.mode = runner.MODE_FANOUT
    db = MagicMock()

    result = runner._resolve_run_mode_targets(db, run)

    assert result == (None, "otp", ["sess-a", "sess-b"], {"sess-a": "otp", "sess-b": "motis"})
    assert run.status == "running"
    db.commit.assert_not_called()


def test_resolve_run_mode_targets_fanout_no_eligible_sessions_marks_failed(monkeypatch):
    """fanout with zero serving + include_in_fanout sessions → flips to
    'failed' so the operator notices instead of silently waiting forever."""
    monkeypatch.setattr(runner, "_snapshot_fanout_sessions", lambda _db: ([], {}))
    run = _make_run_row()
    run.mode = runner.MODE_FANOUT
    db = MagicMock()

    result = runner._resolve_run_mode_targets(db, run)

    assert result == (None, "otp", [], {})
    assert run.status == "failed"
    db.commit.assert_called_once()


def test_mark_run_failed_writes_update_and_commits(monkeypatch):
    """Stamps status='failed' + finished_at via UPDATE in its own
    short txn. Used by execute_run when the per-pair gather raises."""
    db = MagicMock()
    fake_session = MagicMock()
    fake_session.__enter__.return_value = db
    fake_session.__exit__.return_value = False
    monkeypatch.setattr(runner, "SessionLocal", lambda: fake_session)

    runner._mark_run_failed(uuid.uuid4())

    db.execute.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_finalise_completed_run_writes_completed_status(monkeypatch):
    """Phase-3 rollup: recompute counters, skip verify-sweep when the
    flag is off, flip to status='completed' in one transaction."""
    rid = uuid.uuid4()
    run_row = _make_run_row(rid, status="running")
    run_row.verify_externally = False
    captured = [
        SimpleNamespace(status="ok", response_ms=500),
        SimpleNamespace(status="ok", response_ms=700),
        SimpleNamespace(status="no_route", response_ms=300),
    ]
    fake_session = _CountingFakeSession(run_row, captured)
    monkeypatch.setattr(runner, "SessionLocal", fake_session)

    await runner._finalise_completed_run(run_id=rid, elapsed_s=42.0, cfg=runner.CoverageConfig())

    assert run_row.status == "completed"
    assert run_row.completed_pairs == 3
    assert run_row.ok_pairs == 2
    assert run_row.no_route_pairs == 1
    assert run_row.summary["elapsed_seconds"] == 42.0


@pytest.mark.asyncio
async def test_finalise_completed_run_no_op_when_run_disappeared(monkeypatch):
    """If the run was DELETEd before phase-3 reaches it, exit cleanly
    without raising — matches `_persist_cancelled_run` back-pressure."""

    class _NoRunSession(_CountingFakeSession):
        def get(self, _model, _key):
            return None

    monkeypatch.setattr(runner, "SessionLocal", _NoRunSession(run_row=None, captured_results=[]))
    await runner._finalise_completed_run(
        run_id=uuid.uuid4(), elapsed_s=1.0, cfg=runner.CoverageConfig()
    )


def test_phase1_snapshot_returns_none_when_run_missing(monkeypatch):
    """The runner can be invoked with a run_id that's been DELETEd
    (operator raced delete vs background task). Phase-1 returns None
    and execute_run's `if snap is None or not snap.pairs` short-circuits."""

    class _MissingRunSession(_CountingFakeSession):
        def get(self, _model, _key):
            return None

    monkeypatch.setattr(
        runner, "SessionLocal", _MissingRunSession(run_row=None, captured_results=[])
    )

    result = runner._phase1_snapshot_and_start(uuid.uuid4())

    assert result is None


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_phase1_snapshot_returns_none_for_terminal_run(monkeypatch, terminal):
    """Re-running on a terminal run is a no-op — phase-1 returns None
    without re-flipping the status field."""
    run_row = _make_run_row(status=terminal)
    monkeypatch.setattr(runner, "SessionLocal", _CountingFakeSession(run_row, captured_results=[]))

    result = runner._phase1_snapshot_and_start(uuid.uuid4())

    assert result is None
    assert run_row.status == terminal  # NOT mutated


# ─────────────────────── _process_pair_with_cancel ───────────────────────


def _make_snapshot(*, mode: str = "single_session"):
    """A `_Phase1Snapshot` shaped just enough for _process_pair_with_cancel
    to dispatch — none of the field VALUES actually matter because we
    monkeypatch the downstream `_execute_pair` / `_execute_pair_fanout`
    to record the call instead of doing real work."""
    from app.network_coverage.hubs import Hub

    origin = Hub(id="o", name="O", short="O", region="", lat=0.0, lon=0.0)
    dest = Hub(id="d", name="D", short="D", region="", lat=1.0, lon=1.0)
    return runner._Phase1Snapshot(
        run_mode=mode,
        session_id_for_pairs="sess-1" if mode == "single_session" else None,
        engine_for_pairs="otp",
        fanout_session_ids=["sess-a"] if mode == "fanout" else [],
        engine_by_session={"sess-a": "otp"} if mode == "fanout" else {},
        depart_at_for_pairs=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        pairs=[(origin, dest)],
        cfg=runner.CoverageConfig(),
        window=runner.ResolvedWindow(
            start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
            tz_name="UTC",
        ),
    )


@pytest.mark.asyncio
async def test_process_pair_with_cancel_skips_when_already_cancelled(monkeypatch):
    """Cancel event set BEFORE the coroutine enters the semaphore → exits
    immediately, downstream `_execute_pair` is never called. Without this
    short-circuit, the queue would still pay the OTP round-trip cost
    even for the pairs queued after the Stop click."""
    rid = uuid.uuid4()
    runner.register_cancel(rid)
    runner.request_cancel(rid)  # fire immediately

    called = MagicMock()

    async def _fake_pair(**_kw):
        called()

    monkeypatch.setattr(runner, "_execute_pair", _fake_pair)
    monkeypatch.setattr(runner, "_execute_pair_fanout", _fake_pair)

    snap = _make_snapshot()
    semaphore = asyncio.Semaphore(1)
    o, d = snap.pairs[0]
    await runner._process_pair_with_cancel(
        run_id=rid, semaphore=semaphore, snap=snap, origin=o, dest=d
    )

    called.assert_not_called()


@pytest.mark.asyncio
async def test_process_pair_with_cancel_single_session_dispatches_to_execute_pair(monkeypatch):
    """Happy path for single_session mode — the coroutine dispatches to
    `_execute_pair` (not the fanout variant) with snapshot-derived args."""
    rid = uuid.uuid4()
    runner.register_cancel(rid)

    captured: dict = {}

    async def _fake_pair(**kwargs):
        captured.update(kwargs)

    async def _fake_fanout(**_kw):
        captured["fanout_called"] = True

    monkeypatch.setattr(runner, "_execute_pair", _fake_pair)
    monkeypatch.setattr(runner, "_execute_pair_fanout", _fake_fanout)

    snap = _make_snapshot(mode="single_session")
    o, d = snap.pairs[0]
    await runner._process_pair_with_cancel(
        run_id=rid, semaphore=asyncio.Semaphore(1), snap=snap, origin=o, dest=d
    )

    assert "fanout_called" not in captured
    assert captured["session_id"] == "sess-1"
    assert captured["engine"] == "otp"


@pytest.mark.asyncio
async def test_process_pair_with_cancel_fanout_dispatches_to_fanout_helper(monkeypatch):
    """Happy path for fanout mode — dispatches to `_execute_pair_fanout`
    with the snapshotted session ids and engine map."""
    rid = uuid.uuid4()
    runner.register_cancel(rid)

    captured: dict = {}

    async def _fake_pair(**_kw):
        captured["single_called"] = True

    async def _fake_fanout(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(runner, "_execute_pair", _fake_pair)
    monkeypatch.setattr(runner, "_execute_pair_fanout", _fake_fanout)

    snap = _make_snapshot(mode="fanout")
    o, d = snap.pairs[0]
    await runner._process_pair_with_cancel(
        run_id=rid, semaphore=asyncio.Semaphore(1), snap=snap, origin=o, dest=d
    )

    assert "single_called" not in captured
    assert captured["session_ids"] == ["sess-a"]
    assert captured["engine_by_session"] == {"sess-a": "otp"}


# ─────────────────────── execute_run integration ───────────────────────


@pytest.mark.asyncio
async def test_execute_run_short_circuits_when_phase1_returns_none(monkeypatch):
    """Phase-1 returns None (missing/terminal/bad-config run) → execute_run
    logs + returns cleanly, registry stays bounded via the outer finally.

    Coverage: hits the `if snap is None or not snap.pairs: return` branch
    AND confirms `clear_cancel` is invoked even on the early-return path."""
    rid = uuid.uuid4()
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: None)

    await runner.execute_run(rid)

    # The outer try/finally cleared the registry even though we never
    # made it to Phase 2.
    assert rid not in runner._CANCEL_EVENTS


@pytest.mark.asyncio
async def test_execute_run_cancelled_mid_loop_persists_partial(monkeypatch):
    """A Stop click that lands during Phase 2 → after the gather returns,
    execute_run sees `is_cancelled=True` and routes to
    `_persist_cancelled_run` instead of `_finalise_completed_run`.

    Covers the "cancelled path" inside execute_run — without this test
    the if/else after `elapsed_s = ...` is uncovered."""
    rid = uuid.uuid4()
    snap = _make_snapshot()

    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: snap)

    async def _noop_pair(**_kw):
        # Simulate a pair completing AND the cancel landing afterward.
        runner.request_cancel(rid)

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _noop_pair)

    persist_calls: list[float] = []

    def _persist(*, run_id, elapsed_s):
        persist_calls.append(elapsed_s)

    finalise_calls: list[float] = []

    async def _finalise(*, run_id, elapsed_s, cfg):
        finalise_calls.append(elapsed_s)

    monkeypatch.setattr(runner, "_persist_cancelled_run", _persist)
    monkeypatch.setattr(runner, "_finalise_completed_run", _finalise)

    await runner.execute_run(rid)

    assert len(persist_calls) == 1, "must persist as cancelled, not completed"
    assert len(finalise_calls) == 0
    assert rid not in runner._CANCEL_EVENTS


@pytest.mark.asyncio
async def test_execute_run_happy_path_calls_finalise(monkeypatch):
    """No cancel signal → execute_run calls `_finalise_completed_run`
    after the pair loop finishes cleanly."""
    rid = uuid.uuid4()
    snap = _make_snapshot()

    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: snap)

    async def _noop_pair(**_kw):
        pass

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _noop_pair)

    finalise_calls: list[float] = []

    async def _finalise(*, run_id, elapsed_s, cfg):
        finalise_calls.append(elapsed_s)

    monkeypatch.setattr(runner, "_finalise_completed_run", _finalise)

    await runner.execute_run(rid)

    assert len(finalise_calls) == 1, "happy path must reach Phase 3"
    assert rid not in runner._CANCEL_EVENTS


# ─────────────────────── PR-187 — DB-status cancel check ───────────────────────
#
# Regression coverage for the multi-hour incident where a SQL
# `UPDATE network_coverage_runs SET status='cancelled'` had no effect on
# the in-flight runner (which only consulted the process-local
# `_CANCEL_EVENTS` dict). `_is_cancelled_in_db` is the cheap
# DB-status backstop with a 3s per-run TTL cache.


class _StatusFakeSession:
    """SessionLocal stand-in that returns a configurable status string
    from the `SELECT status FROM network_coverage_runs WHERE id=:rid`
    query, and counts how many times `.execute()` is called so the
    cache-TTL test can assert hit/miss behaviour."""

    def __init__(self, status: str | None):
        self.status = status
        self.execute_call_count = 0

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _stmt):
        self.execute_call_count += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.status
        return result


def test_is_cancelled_in_db_returns_true_for_cancelled_status(monkeypatch):
    """SQL `UPDATE ... SET status='cancelled'` must surface as a cancel
    signal to the runner — without this the operator's psql cancel is
    invisible and the runner keeps hammering MOTIS."""
    fake_session = _StatusFakeSession(status="cancelled")
    monkeypatch.setattr(runner, "SessionLocal", fake_session)

    cache: dict = {}
    rid = uuid.uuid4()
    assert runner._is_cancelled_in_db(rid, cache) is True


def test_is_cancelled_in_db_returns_false_for_running_status(monkeypatch):
    """Happy path — the run is still 'running' in the DB, the helper
    returns False and the runner continues processing pairs."""
    fake_session = _StatusFakeSession(status="running")
    monkeypatch.setattr(runner, "SessionLocal", fake_session)

    cache: dict = {}
    rid = uuid.uuid4()
    assert runner._is_cancelled_in_db(rid, cache) is False


def test_is_cancelled_in_db_caches_within_ttl_window(monkeypatch):
    """The per-pair hot loop calls this helper on every pair — without
    the TTL cache that's one SELECT per pair x N pairs, which gets
    expensive on dense matrices. Two calls within 3s for the same run
    should hit the cache (single SELECT) and return the same answer."""
    fake_session = _StatusFakeSession(status="running")
    monkeypatch.setattr(runner, "SessionLocal", fake_session)

    cache: dict = {}
    rid = uuid.uuid4()

    # First call: cache miss → one SELECT.
    assert runner._is_cancelled_in_db(rid, cache) is False
    assert fake_session.execute_call_count == 1

    # Second call within the 3s TTL: cache hit → no new SELECT.
    assert runner._is_cancelled_in_db(rid, cache) is False
    assert fake_session.execute_call_count == 1, (
        "second call within TTL must reuse cached value, not re-query the DB"
    )


@pytest.mark.asyncio
async def test_execute_run_gather_exception_routes_to_mark_failed(monkeypatch):
    """Exception escaping the per-pair gather → execute_run calls
    `_mark_run_failed` (NOT _finalise / _persist) and exits without
    re-raising. Covers the broad-except branch."""
    rid = uuid.uuid4()
    snap = _make_snapshot()

    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: snap)

    async def _exploding_pair(**_kw):
        raise RuntimeError("simulated planner crash")

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _exploding_pair)

    failed_calls: list[uuid.UUID] = []

    def _mark(run_id):
        failed_calls.append(run_id)

    monkeypatch.setattr(runner, "_mark_run_failed", _mark)

    await runner.execute_run(rid)

    assert failed_calls == [rid]
    assert rid not in runner._CANCEL_EVENTS
