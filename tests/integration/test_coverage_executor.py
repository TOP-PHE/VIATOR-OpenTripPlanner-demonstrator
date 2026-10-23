"""Integration: coverage runs execute in the worker's executor and resume
after a restart (issue #392, durable part).

Real runner, real PostgreSQL 16, fake MOTIS (the #392 harness of
test_coverage_web_health.py: 8 ZZ hubs, 56 pairs, deterministic itineraries).
Checked here:

- web's create route makes a `pending` run and executes nothing;
- the claim is atomic: a row being claimed is skipped, and of many racing
  executors exactly one wins;
- an executor killed mid-run leaves the run `running`; once its heartbeat is
  stale another executor resumes it, asks only the pairs without a result,
  and ends with exact counters, no duplicate and no missing pair, and the
  same results as an uninterrupted run;
- a live heartbeat keeps the run from being taken; a stale one does not; a
  lost claim stops the old holder without a terminal write;
- Stop during the pairs, during the completion phase, and of a queued run;
- the resume limit marks the run failed with its reason;
- the release at a graceful shutdown makes the run claimable at once;
- the `viator_coverage_*` gauges read the claim state.

Invented values only (ZZ hubs, numbers with 99).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text

from tests.integration.test_coverage_web_health import (
    HUB_COUNT,
    ITINERARIES_PER_SLOT,
    PAIR_PARALLELISM,
    SID,
    SLOT_LATENCY_S,
    WITHIN_PAIR_PARALLELISM,
    _assert_results_unchanged,
    _counts,
    _create_run,
    _FakeMotis,
    _wait_for_pairs,
    fresh_db,  # noqa: F401  (pytest fixture)
)

PAIRS = HUB_COUNT * (HUB_COUNT - 1)
EXEC_A = "zz-host:99:aaaa"
EXEC_B = "zz-host:99:bbbb"


@pytest.fixture
def engine(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeMotis]:  # noqa: F811
    from app.journey import planner_dispatch
    from app.network_coverage import runner

    fake = _FakeMotis()
    monkeypatch.setattr(planner_dispatch, "planner_for_engine", lambda _engine: fake)
    cfg = runner.CoverageConfig(
        pair_parallelism=PAIR_PARALLELISM, within_pair_parallelism=WITHIN_PAIR_PARALLELISM
    )
    monkeypatch.setattr(runner, "_load_coverage_config", lambda _db: cfg)
    yield fake


def _session() -> Any:
    from app.db import SessionLocal

    return SessionLocal()


def _run_row(run_id: uuid.UUID) -> Any:
    from app.models import NetworkCoverageRun

    with _session() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        db.expunge(run)
        return run


def _sql(statement: str, **params: Any) -> None:
    with _session() as db:
        db.execute(text(statement), params)
        db.commit()


def _claim(executor_id: str, **kw: Any) -> Any:
    from app.network_coverage import executor

    with _session() as db:
        return executor.claim_next_run(db, executor_id=executor_id, **kw)


def _results(run_id: uuid.UUID) -> dict[tuple[str, str], tuple[Any, ...]]:
    from app.models import NetworkCoverageResult

    with _session() as db:
        rows = db.execute(
            select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id)
        ).scalars()
        return {
            (r.origin_hub_id, r.dest_hub_id): (
                r.status,
                r.num_itineraries,
                r.best_duration_seconds,
                r.best_num_transfers,
                r.best_operators,
            )
            for r in rows
        }


def _journey_searches() -> int:
    from app.models import JourneySearch

    with _session() as db:
        return int(db.execute(select(func.count()).select_from(JourneySearch)).scalar_one())


async def _drain_db_executor() -> None:
    """Wait for the runner's DB threads to finish what a cancelled task
    left them (shielded pair writes), as a killed process would not: one
    job per thread, held at a barrier, runs only once every thread is free."""
    from app.network_coverage import runner

    loop = asyncio.get_running_loop()
    await asyncio.sleep(0.2)  # shielded writes of the cancelled task get submitted
    barrier = threading.Barrier(runner._DB_THREADS)
    await asyncio.gather(
        *(
            loop.run_in_executor(runner._DB_EXECUTOR, barrier.wait, 30)
            for _ in range(runner._DB_THREADS)
        )
    )


# ───────────────────────────── web creates, never executes ─────────────────────────────


def test_web_route_creates_a_pending_run_and_does_not_execute_it(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    from app.api.admin import network_coverage as api
    from app.models import NetworkCoverageResult, NetworkCoverageRun
    from app.network_coverage import runner

    def _never(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("web executed a coverage run")

    monkeypatch.setattr(runner, "execute_run", _never)
    assert "bg" not in inspect.signature(api.create_run).parameters
    actor = type("Actor", (), {"id": uuid.UUID("99999999-9999-4999-8999-999999999992")})()
    body = api.RunCreate(
        session_id=SID, depart_at=datetime(2099, 9, 9, 0, 0, tzinfo=UTC), direction="both"
    )
    with _session() as db:
        summary = api.create_run(body=body, db=db, actor=actor)  # type: ignore[arg-type]
    assert summary.status == "pending"
    assert summary.display_status == "queued"
    with _session() as db:
        run = db.get(NetworkCoverageRun, uuid.UUID(summary.id))
        assert run is not None
        assert (run.status, run.claimed_by, run.heartbeat_at) == ("pending", None, None)
        assert run.total_pairs == PAIRS
        assert db.execute(select(func.count()).select_from(NetworkCoverageResult)).scalar() == 0
    assert engine.calls == 0


# ─────────────────────────────────── atomic claim ───────────────────────────────────


def test_a_row_being_claimed_is_skipped_not_taken_twice(engine: _FakeMotis) -> None:
    from app.network_coverage import executor

    run_id = _create_run()
    holder = _session()
    try:
        locked = executor.select_claimable(holder, executor.STALE_AFTER_SECONDS)
        assert locked is not None and locked.id == run_id
        # Another executor, while the first has the row locked: nothing to take.
        assert _claim(EXEC_B) is None
    finally:
        holder.rollback()
        holder.close()
    claim = _claim(EXEC_B)
    assert claim is not None and claim.run_id == run_id and claim.resumed is False


def test_racing_executors_only_one_wins(engine: _FakeMotis) -> None:
    run_id = _create_run()
    barrier = threading.Barrier(8)
    won: list[Any] = []

    def _race(i: int) -> None:
        barrier.wait()
        executor_id = f"zz-host:99:{i:04d}"
        claim = _claim(executor_id)
        if claim is not None:
            won.append((executor_id, claim))

    threads = [threading.Thread(target=_race, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    ((winner, claim),) = won
    assert claim.run_id == run_id
    run = _run_row(run_id)
    assert (run.status, run.claimed_by, run.resume_count) == ("running", winner, 0)


# ─────────────────────────────── stale and fresh heartbeats ───────────────────────────────


def test_fresh_heartbeat_is_not_taken_stale_one_is(engine: _FakeMotis) -> None:
    run_id = _create_run()
    assert _claim(EXEC_A) is not None
    assert _claim(EXEC_B) is None  # fresh: not taken
    _sql(
        "UPDATE network_coverage_runs SET heartbeat_at = now() - interval '179 seconds' "
        "WHERE id = :r",
        r=run_id,
    )
    assert _claim(EXEC_B) is None  # still inside the 180 s
    _sql(
        "UPDATE network_coverage_runs SET heartbeat_at = now() - interval '181 seconds' "
        "WHERE id = :r",
        r=run_id,
    )
    claim = _claim(EXEC_B)
    assert claim is not None and claim.resumed is True and claim.resume_count == 1
    run = _run_row(run_id)
    assert run.claimed_by == EXEC_B
    assert run.summary["resume_count"] == 1
    (resume,) = run.summary["resumes"]
    assert resume["previous_executor"] == EXEC_A
    assert resume["last_heartbeat_at"] is not None


def test_a_live_heartbeat_keeps_the_run(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executor A beats every 0.1 s on a slow run; B, for which 1 s is
    stale, never gets it."""
    from app.network_coverage import executor

    monkeypatch.setattr("tests.integration.test_coverage_web_health.SLOT_LATENCY_S", 0.5)
    run_id = _create_run()
    a = executor.CoverageExecutor(EXEC_A, heartbeat_s=0.1, stale_after_s=1.0)

    async def _scenario() -> list[Any]:
        assert (await a.poll_once()) is not None
        seen = []
        for _ in range(6):
            await asyncio.sleep(0.4)
            seen.append(await asyncio.to_thread(_claim, EXEC_B, stale_after_s=1.0))
        await _stop_with_stop_click(run_id, a)
        return seen

    assert asyncio.run(_scenario()) == [None] * 6
    assert _run_row(run_id).claimed_by == EXEC_A


async def _stop_with_stop_click(run_id: uuid.UUID, ex: Any) -> None:
    _sql("UPDATE network_coverage_runs SET cancel_requested_at = now() WHERE id = :r", r=run_id)
    for _ in range(400):
        if not ex.active_run_ids:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the run did not stop")


def test_a_lost_claim_stops_the_old_holder_without_a_terminal_write(engine: _FakeMotis) -> None:
    from app.network_coverage import executor

    run_id = _create_run()
    a = executor.CoverageExecutor(EXEC_A, heartbeat_s=0.1)

    async def _scenario() -> None:
        await a.poll_once()
        await _wait_for_pairs(run_id, 1)
        # Another executor took the run over (A's heartbeat was late).
        await asyncio.to_thread(
            _sql,
            "UPDATE network_coverage_runs SET claimed_by = :b WHERE id = :r",
            b=EXEC_B,
            r=run_id,
        )
        for _ in range(200):
            if not a.active_run_ids:
                break
            await asyncio.sleep(0.05)
        assert not a.active_run_ids
        await _drain_db_executor()

    asyncio.run(_scenario())
    run = _run_row(run_id)
    assert (run.status, run.claimed_by, run.finished_at) == ("running", EXEC_B, None)
    completed, rows = _counts(run_id)
    assert 0 < completed == sum(rows.values()) < PAIRS


# ───────────────────────────── kill, resume, compare ─────────────────────────────


def test_kill_mid_run_then_resume_gives_the_uninterrupted_results(engine: _FakeMotis) -> None:
    from app.network_coverage import executor, runner

    reference = _create_run()
    b = executor.CoverageExecutor(EXEC_B, heartbeat_s=0.1, stale_after_s=0.5)

    async def _uninterrupted() -> None:
        assert (await b.poll_once()) is not None
        await asyncio.gather(*b._active.values())

    asyncio.run(_uninterrupted())
    _assert_results_unchanged(reference, engine)
    expected = _results(reference)
    searches_before = _journey_searches()

    resumed_run = _create_run()
    engine.calls = 0
    claim = _claim(EXEC_A)
    assert claim is not None and claim.run_id == resumed_run

    async def _killed() -> None:
        task = asyncio.create_task(runner.execute_run(resumed_run, executor_id=EXEC_A))
        await _wait_for_pairs(resumed_run, PAIR_PARALLELISM * 2)
        task.cancel()  # the worker dies: no terminal write, no release
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_db_executor()

    asyncio.run(_killed())
    run = _run_row(resumed_run)
    assert (run.status, run.claimed_by) == ("running", EXEC_A)
    done_at_kill, rows_at_kill = _counts(resumed_run)
    assert 0 < done_at_kill == sum(rows_at_kill.values()) < PAIRS
    calls_before_resume = engine.calls

    async def _resume() -> None:
        await asyncio.sleep(0.6)  # A's heartbeat is stale for B
        claim_b = await b.poll_once()
        assert claim_b is not None and claim_b.resumed is True
        await asyncio.gather(*b._active.values())

    asyncio.run(_resume())

    run = _run_row(resumed_run)
    assert run.status == "completed"
    assert run.resume_count == 1
    assert run.summary["resume_count"] == 1
    assert len(run.summary["resumes"]) == 1
    assert run.summary["resumes"][0]["completed_pairs"] == done_at_kill
    # Exact counters, every pair once, nothing re-asked.
    assert (run.completed_pairs, run.ok_pairs, run.no_route_pairs, run.error_pairs) == (
        PAIRS,
        PAIRS,
        0,
        0,
    )
    assert engine.calls - calls_before_resume == (PAIRS - done_at_kill) * 6
    assert _results(resumed_run) == expected
    assert _journey_searches() - searches_before == PAIRS
    with _session() as db:
        trips = db.execute(text("SELECT count(*) FROM journey_trips")).scalar_one()
    assert trips == 2 * PAIRS * 6 * ITINERARIES_PER_SLOT


def test_resume_after_the_pairs_runs_the_completion_again(engine: _FakeMotis) -> None:
    """Killed during the completion phase (all pairs written, status still
    running): the resume asks no pair and completes the run."""
    from app.network_coverage import executor, runner

    run_id = _create_run()
    assert _claim(EXEC_A) is not None

    async def _pairs_only() -> None:
        snap = await runner._off_loop(runner._phase1_snapshot_and_start, run_id)
        assert snap is not None
        runner.register_cancel(run_id)
        try:
            assert await runner._run_pairs(run_id, snap, EXEC_A)
        finally:
            runner.clear_cancel(run_id)

    asyncio.run(_pairs_only())
    assert _run_row(run_id).status == "running"
    engine.calls = 0
    _sql(
        "UPDATE network_coverage_runs SET heartbeat_at = NULL, claimed_by = NULL WHERE id = :r",
        r=run_id,
    )
    b = executor.CoverageExecutor(EXEC_B)

    async def _resume() -> None:
        assert (await b.poll_once()) is not None
        await asyncio.gather(*b._active.values())

    asyncio.run(_resume())
    assert engine.calls == 0
    _assert_results_unchanged(run_id, _FakeMotisCalls(PAIRS * 6))


class _FakeMotisCalls:
    def __init__(self, calls: int) -> None:
        self.calls = calls


# ─────────────────────────────────────── Stop ───────────────────────────────────────


def _stop(run_id: uuid.UUID) -> Any:
    from app.api.admin import network_coverage as api

    with _session() as db:
        return api.stop_run(run_id=run_id, db=db, _=None)  # type: ignore[arg-type]


def test_stop_during_the_pairs_ends_the_run_cancelled(engine: _FakeMotis) -> None:
    from app.network_coverage import executor

    run_id = _create_run()
    a = executor.CoverageExecutor(EXEC_A)

    async def _scenario() -> None:
        await a.poll_once()
        await _wait_for_pairs(run_id, PAIR_PARALLELISM)
        summary = await asyncio.to_thread(_stop, run_id)
        assert (summary.status, summary.display_status) == ("running", "stopping")
        await asyncio.gather(*a._active.values())

    asyncio.run(_scenario())
    calls_at_end = engine.calls
    run = _run_row(run_id)
    assert run.status == "cancelled"
    assert run.summary["cancelled_by_operator"] is True
    completed, rows = _counts(run_id)
    assert 0 < completed == sum(rows.values()) == run.ok_pairs < PAIRS
    asyncio.run(asyncio.sleep(SLOT_LATENCY_S * 3))
    assert engine.calls == calls_at_end


def test_stop_during_the_completion_phase_lets_it_complete(
    engine: _FakeMotis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As before #392: once every pair is done the completion phase is not
    interrupted by a Stop click; the run ends completed with all its cells."""
    from app.network_coverage import executor, runner

    in_completion = threading.Event()
    real_sweep = runner._maybe_run_external_verify_sweep

    async def _slow_sweep(**kw: Any) -> dict[str, int]:
        in_completion.set()
        await asyncio.sleep(0.5)
        return await real_sweep(**kw)

    monkeypatch.setattr(runner, "_maybe_run_external_verify_sweep", _slow_sweep)
    run_id = _create_run()
    a = executor.CoverageExecutor(EXEC_A)

    async def _scenario() -> None:
        await a.poll_once()
        while not in_completion.is_set():
            await asyncio.sleep(0.02)
        await asyncio.to_thread(_stop, run_id)
        await asyncio.gather(*a._active.values())

    asyncio.run(_scenario())
    _assert_results_unchanged(run_id, engine)


def test_stop_of_a_queued_run_cancels_it_and_nothing_claims_it(engine: _FakeMotis) -> None:
    run_id = _create_run()
    summary = _stop(run_id)
    assert summary.status == "cancelled"
    run = _run_row(run_id)
    assert run.summary["cancelled_by_operator"] is True
    assert run.summary["cancelled_while_queued"] is True
    assert _claim(EXEC_A) is None
    assert engine.calls == 0


# ─────────────────────────────── resume limit, release ───────────────────────────────


def test_resume_limit_reached_marks_the_run_failed_with_its_reason(engine: _FakeMotis) -> None:
    run_id = _create_run()
    assert _claim(EXEC_A) is not None
    _sql(
        "UPDATE network_coverage_runs SET resume_count = 5, heartbeat_at = NULL WHERE id = :r",
        r=run_id,
    )
    assert _claim(EXEC_B, max_resumes=5) is None
    run = _run_row(run_id)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.summary["resume_limit_reached"] is True
    assert "COVERAGE_MAX_RESUMES = 5" in run.summary["failure_reason"]
    assert "6 times" in run.summary["failure_reason"]
    assert engine.calls == 0


def test_one_resume_under_the_limit_is_still_resumed(engine: _FakeMotis) -> None:
    run_id = _create_run()
    assert _claim(EXEC_A) is not None
    _sql(
        "UPDATE network_coverage_runs SET resume_count = 4, heartbeat_at = NULL WHERE id = :r",
        r=run_id,
    )
    claim = _claim(EXEC_B, max_resumes=5)
    assert claim is not None and claim.resume_count == 5


def test_release_makes_the_run_claimable_at_once(engine: _FakeMotis) -> None:
    from app.network_coverage import executor

    run_id = _create_run()
    a = executor.CoverageExecutor(EXEC_A)
    assert _claim(EXEC_A) is not None
    assert a.release() == [run_id]
    run = _run_row(run_id)
    assert (run.status, run.claimed_by, run.heartbeat_at) == ("running", None, None)
    claim = _claim(EXEC_B)
    assert claim is not None and claim.resumed is True


def test_a_run_ending_without_a_result_state_is_marked_failed(engine: _FakeMotis) -> None:
    from app.network_coverage import executor

    run_id = _create_run()
    _sql("""UPDATE network_coverage_runs SET countries = '["ZZ"]' WHERE id = :r""", r=run_id)
    _sql("UPDATE network_coverage_hubs SET is_active = FALSE")
    a = executor.CoverageExecutor(EXEC_A)

    async def _scenario() -> None:
        await a.poll_once()
        await asyncio.gather(*a._active.values())

    asyncio.run(_scenario())
    run = _run_row(run_id)
    assert run.status == "failed"
    assert "without a result state" in run.summary["failure_reason"]


# ─────────────────────────────────────── metrics ───────────────────────────────────────


def test_coverage_gauges_read_the_claim_state(engine: _FakeMotis) -> None:
    from app.db import SessionLocal
    from app.metrics import _coverage_metrics

    queued = _create_run()
    running = _create_run()
    _sql(
        "UPDATE network_coverage_runs SET started_at = now() - interval '99 seconds' WHERE id = :r",
        r=queued,
    )
    _sql(
        "UPDATE network_coverage_runs SET status = 'running', resume_count = 2, "
        "heartbeat_at = now() - interval '299 seconds' WHERE id = :r",
        r=running,
    )
    values = {m.name: m.samples[0].value for m in _coverage_metrics(SessionLocal)}
    assert values["viator_coverage_runs_queued"] == 1
    assert values["viator_coverage_runs_running"] == 1
    assert 99 <= values["viator_coverage_oldest_queued_age_seconds"] < 160
    assert 299 <= values["viator_coverage_heartbeat_age_seconds"] < 360
    assert values["viator_coverage_resumes_total"] == 2


def test_display_status_of_a_paused_run_is_resuming(engine: _FakeMotis) -> None:
    from app.api.admin.network_coverage import _display_status

    run_id = _create_run()
    assert _claim(EXEC_A) is not None
    assert _display_status(_run_row(run_id))[0] == "running"
    _sql(
        "UPDATE network_coverage_runs SET heartbeat_at = now() - interval '199 seconds' "
        "WHERE id = :r",
        r=run_id,
    )
    status, age = _display_status(_run_row(run_id))
    assert status == "resuming"
    assert age is not None and age > 180
