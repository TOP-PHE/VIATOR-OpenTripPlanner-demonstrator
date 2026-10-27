"""Integration: the ÖBB check of a coverage run saves each answer as it
comes, records its progress, resumes where it stopped, and stops when the
run is ended.

Real runner and executor, real PostgreSQL 16, fake MOTIS (the #392 harness
of test_coverage_web_health.py: 8 ZZ hubs, 56 pairs) and a fake ÖBB HAFAS
that can hold every call from the Nth on, so the check can be looked at
half-way. The writer saves every 4 answers here. Checked:

- while the check runs, another session sees the saved answers on the
  result rows and the progress on the run, and the page's summary says
  "verifying"; at the end every cell has its answer and the counters;
- a completion that loses its executor half-way (a worker shutdown
  releases the run) saves what it had, and the resume asks ÖBB only about
  the cells without an answer, then completes with every answer;
- a `cancelled` written by SQL during the check (PR-187) stops it: ÖBB is
  asked about no more cells, the cancel is kept, the saved answers stay.

Invented values only (ZZ hubs, numbers with 99).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.test_coverage_web_health import (
    HUB_COUNT,
    PAIR_PARALLELISM,
    WITHIN_PAIR_PARALLELISM,
    _create_run,
    _FakeMotis,
    fresh_db,  # noqa: F401  (pytest fixture)
)

PAIRS = HUB_COUNT * (HUB_COUNT - 1)
EXEC_A = "zz-host:99:aaaa"
EXEC_B = "zz-host:99:bbbb"
VERIFY_PARALLELISM = 2
SAVE_EVERY = 4
# Calls from this one on are held: 20 answers (5 writes of 4) are saved by then.
HOLD_AT = 21
SAVED_AT_HOLD = HOLD_AT - 1


@pytest.fixture
def engine(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> _FakeMotis:  # noqa: F811
    from app.journey import planner_dispatch
    from app.network_coverage import runner, verify_progress

    fake = _FakeMotis()
    monkeypatch.setattr(planner_dispatch, "planner_for_engine", lambda _engine: fake)
    cfg = runner.CoverageConfig(
        pair_parallelism=PAIR_PARALLELISM,
        within_pair_parallelism=WITHIN_PAIR_PARALLELISM,
        verify_parallelism=VERIFY_PARALLELISM,
        verify_sleep_ms=0,
    )
    monkeypatch.setattr(runner, "_load_coverage_config", lambda _db: cfg)
    monkeypatch.setattr(verify_progress, "FLUSH_EVERY_CELLS", SAVE_EVERY)
    monkeypatch.setattr(verify_progress, "FLUSH_EVERY_S", 999.0)
    return fake


class _FakeOebb:
    """ÖBB HAFAS stand-in: one connection for every cell. From call
    `hold_at` on, each call waits for `release`; `held` is set once every
    verify slot waits, so every answer before them is saved by then."""

    def __init__(self, hold_at: int | None = None) -> None:
        self.calls = 0
        self.hold_at = hold_at
        self.waiting = 0
        self.held = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    async def __call__(self, **_kw: Any) -> Any:
        from app.network_coverage import external_verify

        with self._lock:
            self.calls += 1
            hold = self.hold_at is not None and self.calls >= self.hold_at
            if hold:
                self.waiting += 1
                if self.waiting == VERIFY_PARALLELISM:
                    self.held.set()
        if hold:
            assert await asyncio.to_thread(self.release.wait, 60)
        return external_verify.VerifyResult(source="fahrplan.oebb.at", ok=True, num_connections=1)


def _session() -> Any:
    from app.db import SessionLocal

    return SessionLocal()


def _sql(statement: str, **params: Any) -> None:
    with _session() as db:
        db.execute(text(statement), params)
        db.commit()


def _verifying_run(monkeypatch: pytest.MonkeyPatch, oebb: _FakeOebb) -> uuid.UUID:
    from app.network_coverage import external_verify

    monkeypatch.setattr(external_verify, "verify_via_oebb_hafas", oebb)
    run_id = _create_run()
    _sql("UPDATE network_coverage_runs SET verify_externally = true WHERE id = :r", r=run_id)
    return run_id


def _progress(run_id: uuid.UUID) -> dict[str, Any]:
    with _session() as db:
        row = db.execute(
            text(
                "SELECT status, verify_total, verify_done, verify_done_at_start, "
                "verify_started_at IS NOT NULL, verify_progress_at IS NOT NULL, finished_at, "
                "summary FROM network_coverage_runs WHERE id = :r"
            ),
            {"r": run_id},
        ).one()
        answered = db.execute(
            text(
                "SELECT count(*) FROM network_coverage_results "
                "WHERE run_id = :r AND external_verified_at IS NOT NULL "
                "AND external_ok AND external_alignment_tier IS NOT NULL"
            ),
            {"r": run_id},
        ).scalar_one()
    keys = ("status", "total", "done", "at_start", "started", "progressed", "finished", "summary")
    return {**dict(zip(keys, row, strict=True)), "answered": answered}


def _display_status(run_id: uuid.UUID) -> str:
    from app.api.admin.network_coverage import _run_to_summary
    from app.models import NetworkCoverageRun

    with _session() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        return _run_to_summary(run).display_status


def _assert_completed_with_every_answer(run_id: uuid.UUID, *, at_start: int) -> None:
    p = _progress(run_id)
    assert p["status"] == "completed"
    assert (p["total"], p["done"], p["at_start"]) == (PAIRS, PAIRS, at_start)
    assert p["answered"] == PAIRS
    assert p["summary"]["external_verified_count"] == PAIRS
    assert p["summary"]["external_ok_count"] == PAIRS


def test_answers_and_progress_are_visible_while_the_check_runs(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.network_coverage import executor

    oebb = _FakeOebb(hold_at=HOLD_AT)
    run_id = _verifying_run(monkeypatch, oebb)
    a = executor.CoverageExecutor(EXEC_A)
    seen: dict[str, Any] = {}

    async def _scenario() -> None:
        await a.poll_once()
        assert await asyncio.to_thread(oebb.held.wait, 120)
        seen.update(await asyncio.to_thread(_progress, run_id))
        seen["display"] = await asyncio.to_thread(_display_status, run_id)
        oebb.release.set()
        await asyncio.gather(*a._active.values())

    asyncio.run(_scenario())
    assert seen["status"] == "running"
    assert seen["finished"] is None  # the completion has not committed
    assert (seen["total"], seen["done"], seen["at_start"]) == (PAIRS, SAVED_AT_HOLD, 0)
    assert seen["started"] and seen["progressed"]
    assert seen["answered"] == SAVED_AT_HOLD  # visible to another session mid-check
    assert seen["display"] == "verifying"
    _assert_completed_with_every_answer(run_id, at_start=0)
    assert oebb.calls == PAIRS  # every cell asked once


def test_a_resumed_check_asks_only_the_cells_without_an_answer(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.network_coverage import executor, runner

    oebb = _FakeOebb(hold_at=HOLD_AT)
    run_id = _verifying_run(monkeypatch, oebb)
    with _session() as db:
        assert executor.claim_next_run(db, executor_id=EXEC_A) is not None

    async def _pairs_only() -> None:
        snap = await runner._off_loop(runner._phase1_snapshot_and_start, run_id)
        assert snap is not None
        runner.register_cancel(run_id)
        try:
            assert await runner._run_pairs(run_id, snap, EXEC_A)
        finally:
            runner.clear_cancel(run_id)

    asyncio.run(_pairs_only())

    def _complete_as_a() -> None:
        asyncio.run(
            runner._finalise_completed_run(
                run_id=run_id, elapsed_s=1.0, cfg=runner.CoverageConfig(), executor_id=EXEC_A
            )
        )

    first = threading.Thread(target=_complete_as_a)
    first.start()
    assert oebb.held.wait(120)
    # A graceful worker shutdown releases the run (#392) half-way through.
    _sql(
        "UPDATE network_coverage_runs SET claimed_by = NULL, heartbeat_at = now() WHERE id = :r",
        r=run_id,
    )
    oebb.release.set()
    first.join(120)
    assert not first.is_alive()
    stopped = _progress(run_id)
    assert stopped["status"] == "running"
    assert stopped["answered"] == stopped["done"] == SAVED_AT_HOLD
    assert oebb.calls < PAIRS  # the first attempt stopped asking

    calls_before = oebb.calls
    oebb.hold_at = None
    b = executor.CoverageExecutor(EXEC_B)

    async def _resume() -> None:
        assert (await b.poll_once()) is not None
        await asyncio.gather(*b._active.values())

    asyncio.run(_resume())
    assert oebb.calls - calls_before == PAIRS - SAVED_AT_HOLD
    _assert_completed_with_every_answer(run_id, at_start=SAVED_AT_HOLD)


def test_a_cancel_written_by_sql_during_the_check_stops_it(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.network_coverage import executor

    oebb = _FakeOebb(hold_at=HOLD_AT)
    run_id = _verifying_run(monkeypatch, oebb)
    a = executor.CoverageExecutor(EXEC_A)

    async def _scenario() -> None:
        await a.poll_once()
        assert await asyncio.to_thread(oebb.held.wait, 120)
        await asyncio.to_thread(
            _sql, "UPDATE network_coverage_runs SET status = 'cancelled' WHERE id = :r", r=run_id
        )
        oebb.release.set()
        await asyncio.gather(*a._active.values())

    asyncio.run(_scenario())
    p = _progress(run_id)
    assert p["status"] == "cancelled"  # kept (#406)
    assert p["finished"] is None  # nothing of the completion was committed
    assert "external_verified_count" not in (p["summary"] or {})
    assert p["answered"] == p["done"] == SAVED_AT_HOLD  # the answers saved stay
    # ÖBB was asked about no cell after the next write found the cancel: the
    # held calls, then at most one write's worth.
    assert oebb.calls <= HOLD_AT + SAVE_EVERY
