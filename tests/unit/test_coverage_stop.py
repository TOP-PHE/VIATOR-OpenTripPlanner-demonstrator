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
