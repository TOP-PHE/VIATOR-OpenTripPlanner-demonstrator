"""#392 — a coverage run makes no blocking call on the web's event loop.

The runner is a FastAPI background task on the web's loop. Its database work
(phase 1, the DB cancel check, the recorder, the result row and counters, the
fan-out merge, the terminal writes) goes through `runner._off_loop` (a small
dedicated executor), and the completion phase runs on a private loop in a
thread of its own (`runner._run_on_private_loop`). These tests pin, without a
database, that each of those calls runs off the loop thread, in the same
order and with the same arguments as before; the timing harness on a real
PostgreSQL is tests/integration/test_coverage_web_health.py.

Invented values only (ZZ hubs and sessions, numbers with 99).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.network_coverage import runner
from app.network_coverage.hubs import Hub

ORIGIN = Hub(id="zz-a", name="ZZ A", short="ZZA", region="", lat=45.99, lon=9.99)
DEST = Hub(id="zz-b", name="ZZ B", short="ZZB", region="", lat=46.99, lon=10.99)
DEPART = datetime(2099, 9, 9, 9, 0, tzinfo=UTC)

_marker: contextvars.ContextVar[str] = contextvars.ContextVar("zz_marker", default="unset")


class _Calls:
    """Records (name, thread id, kwargs) of each patched call."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, dict[str, Any]]] = []

    def names(self) -> list[str]:
        return [name for name, _t, _kw in self.items]

    def threads(self) -> set[int]:
        return {t for _n, t, _kw in self.items}

    def kwargs(self, name: str) -> dict[str, Any]:
        return next(kw for n, _t, kw in self.items if n == name)

    def recorder(self, name: str, result: Any = None) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            self.items.append((name, threading.get_ident(), {"args": args, **kwargs}))
            return result

        return _call


# ───────────────────────────── the two helpers ─────────────────────────────


async def test_off_loop_runs_in_a_coverage_thread_with_the_callers_context() -> None:
    loop_thread = threading.get_ident()
    token = _marker.set("zz-99")
    try:
        seen = await runner._off_loop(
            lambda a, *, b: (
                threading.get_ident(),
                threading.current_thread().name,
                a + b,
                _marker.get(),
            ),
            90,
            b=9,
        )
    finally:
        _marker.reset(token)
    thread, name, value, marker = seen
    assert thread != loop_thread
    assert name.startswith("coverage-db")
    assert (value, marker) == (99, "zz-99")


async def test_off_loop_raises_the_calls_own_exception() -> None:
    def _boom() -> None:
        raise LookupError("zz-99")

    with pytest.raises(LookupError, match="zz-99"):
        await runner._off_loop(_boom)


def test_the_coverage_executor_is_small() -> None:
    # A run holds at most this many of the web's 15 pooled DB connections.
    assert runner._DB_EXECUTOR._max_workers == runner._DB_THREADS == 2


async def test_private_loop_runs_the_coroutine_on_another_thread_and_loop() -> None:
    web_loop = asyncio.get_running_loop()
    web_thread = threading.get_ident()

    async def _phase() -> tuple[int, asyncio.AbstractEventLoop, str]:
        time.sleep(0.01)  # a blocking call holds the private loop only
        await asyncio.sleep(0)
        return threading.get_ident(), asyncio.get_running_loop(), "zz-99"

    thread, loop, value = await runner._run_on_private_loop(_phase())
    assert thread != web_thread
    assert loop is not web_loop
    assert value == "zz-99"


# ───────────────────────────── the cancel check ─────────────────────────────


async def test_run_stopped_answers_a_stop_click_without_the_db(monkeypatch) -> None:
    rid = uuid.uuid4()
    calls = _Calls()
    monkeypatch.setattr(runner, "_is_cancelled_in_db", calls.recorder("db", result=False))
    runner.register_cancel(rid)
    runner.request_cancel(rid)
    try:
        assert await runner._run_stopped(rid, {}) is True
    finally:
        runner.clear_cancel(rid)
    assert calls.items == []


async def test_run_stopped_answers_a_fresh_cache_entry_on_the_loop(monkeypatch) -> None:
    rid = uuid.uuid4()
    calls = _Calls()
    monkeypatch.setattr(runner, "_is_cancelled_in_db", calls.recorder("db", result=False))
    cache = {rid: (time.monotonic() + 99, True)}
    assert await runner._run_stopped(rid, cache) is True
    assert calls.items == []


async def test_run_stopped_reads_the_db_status_off_the_loop(monkeypatch) -> None:
    rid = uuid.uuid4()
    calls = _Calls()
    monkeypatch.setattr(runner, "_is_cancelled_in_db", calls.recorder("db", result=True))
    cache = {rid: (time.monotonic() - 1, False)}  # expired
    assert await runner._run_stopped(rid, cache) is True
    assert calls.names() == ["db"]
    assert calls.kwargs("db")["args"] == (rid, cache)
    assert threading.get_ident() not in calls.threads()


async def test_a_pair_is_skipped_when_the_db_says_the_run_ended(monkeypatch) -> None:
    rid = uuid.uuid4()
    monkeypatch.setattr(runner, "_is_cancelled_in_db", lambda _rid, _cache: True)

    async def _never(**_kw: Any) -> None:
        raise AssertionError("a pair ran after the run ended")

    monkeypatch.setattr(runner, "_execute_pair", _never)
    await runner._process_pair_with_cancel(
        run_id=rid,
        semaphore=asyncio.Semaphore(1),
        snap=_snapshot(),
        origin=ORIGIN,
        dest=DEST,
    )


# ─────────────────────────── single-session pair ───────────────────────────


class _Planner:
    def __init__(self, trips: list[dict[str, Any]] | None = None, exc: Exception | None = None):
        self.trips = trips or []
        self.exc = exc

    async def fetch_plan(self, **_kw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.exc is not None:
            raise self.exc
        return {"zz": 99}, self.trips


def _trip(duration: int, feed: str) -> dict[str, Any]:
    return {"duration_seconds": duration, "num_transfers": 1, "legs": [{"feed_id": feed}]}


async def test_execute_pair_writes_off_the_loop_in_order(monkeypatch) -> None:
    calls = _Calls()
    search_id = uuid.uuid4()
    trips = [_trip(9999, "zz-slow"), _trip(5999, "zz-fast")]
    monkeypatch.setattr(runner.planner_dispatch, "planner_for_engine", lambda _e: _Planner(trips))
    monkeypatch.setattr(
        runner, "_record_pair_journey_search", calls.recorder("record", (search_id, None))
    )
    monkeypatch.setattr(runner, "_persist_pair_coverage_result", calls.recorder("persist"))

    rid = uuid.uuid4()
    await runner._execute_pair(
        run_id=rid, session_id="zz-s", engine="motis", origin=ORIGIN, dest=DEST, depart_at=DEPART
    )

    assert calls.names() == ["record", "persist"]
    assert threading.get_ident() not in calls.threads()
    record = calls.kwargs("record")
    assert (record["status"], record["raw"], record["trips"]) == ("ok", {"zz": 99}, trips)
    assert record["error_message"] is None
    persist = calls.kwargs("persist")
    assert persist["status"] == "ok"
    assert persist["num_itineraries"] == 2
    assert (persist["best_duration_seconds"], persist["best_operators"]) == (5999, "zz-fast")
    assert persist["journey_search_id"] == search_id
    assert persist["run_id"] == rid


async def test_execute_pair_hands_the_recorders_error_to_the_result_row(monkeypatch) -> None:
    calls = _Calls()
    monkeypatch.setattr(
        runner.planner_dispatch,
        "planner_for_engine",
        lambda _e: _Planner(exc=ValueError("zz-99")),
    )
    monkeypatch.setattr(
        runner,
        "_record_pair_journey_search",
        calls.recorder("record", (None, "ValueError: zz-99 | [recorder] X: zz")),
    )
    monkeypatch.setattr(runner, "_persist_pair_coverage_result", calls.recorder("persist"))

    await runner._execute_pair(
        run_id=uuid.uuid4(),
        session_id="zz-s",
        engine="motis",
        origin=ORIGIN,
        dest=DEST,
        depart_at=DEPART,
    )

    assert calls.kwargs("record")["error_message"] == "ValueError: zz-99"
    assert calls.kwargs("record")["status"] == "error"
    persist = calls.kwargs("persist")
    assert persist["error_message"] == "ValueError: zz-99 | [recorder] X: zz"
    assert persist["journey_search_id"] is None
    assert persist["status"] == "error"


class _FailingDb:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.rolled_back = False

    def __enter__(self) -> _FailingDb:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, *_a: Any) -> None:
        return None

    def commit(self) -> None:
        if self.fail_on == "commit":
            raise RuntimeError("zz commit")

    def rollback(self) -> None:
        self.rolled_back = True


class _Search:
    id = uuid.UUID("99999999-9999-4999-8999-999999999999")


@pytest.mark.parametrize(
    ("fail_on", "prior", "expected_id", "expected_message"),
    [
        ("begin", None, None, "[recorder] RuntimeError: zz begin"),
        ("begin", "ValueError: zz", None, "ValueError: zz | [recorder] RuntimeError: zz begin"),
        # As before #392: the id was taken before the commit failed.
        ("commit", None, _Search.id, "[recorder] RuntimeError: zz commit"),
    ],
)
def test_record_pair_journey_search_reports_a_failed_write(
    monkeypatch, fail_on, prior, expected_id, expected_message
) -> None:
    db = _FailingDb(fail_on)
    monkeypatch.setattr(runner, "SessionLocal", lambda: db)

    def _begin(_db: Any, **_kw: Any) -> _Search:
        if fail_on == "begin":
            raise RuntimeError("zz begin")
        return _Search()

    monkeypatch.setattr(runner.recorder, "begin_search", _begin)
    monkeypatch.setattr(runner.recorder, "finish_search", lambda *_a, **_kw: None)

    result = runner._record_pair_journey_search(
        run_id=uuid.uuid4(),
        session_id="zz-s",
        origin=ORIGIN,
        dest=DEST,
        depart_at=DEPART,
        status="ok",
        response_ms=99,
        raw={},
        trips=[],
        error_message=prior,
    )
    assert result == (expected_id, expected_message)
    assert db.rolled_back is True


def test_record_pair_journey_search_returns_the_search_id(monkeypatch) -> None:
    db = _FailingDb("none")
    monkeypatch.setattr(runner, "SessionLocal", lambda: db)
    begun: list[dict[str, Any]] = []

    def _begin(_db: Any, **kw: Any) -> _Search:
        begun.append(kw)
        return _Search()

    monkeypatch.setattr(runner.recorder, "begin_search", _begin)
    monkeypatch.setattr(runner.recorder, "finish_search", lambda *_a, **_kw: None)

    result = runner._record_pair_journey_search(
        run_id=uuid.uuid4(),
        session_id="zz-s",
        origin=ORIGIN,
        dest=DEST,
        depart_at=DEPART,
        status="no_route",
        response_ms=99,
        raw={},
        trips=[],
        error_message=None,
    )
    assert result == (_Search.id, None)
    assert begun[0]["endpoint"] == "plan"
    assert (begun[0]["origin_label"], begun[0]["dest_label"]) == ("ZZ A", "ZZ B")
    assert begun[0]["requested_time"] == DEPART
    assert db.rolled_back is False


# ───────────────────────────────── fan-out pair ─────────────────────────────────


async def test_fanout_pair_merges_records_and_writes_off_the_loop_in_order(monkeypatch) -> None:
    calls = _Calls()
    search_id = uuid.uuid4()
    monkeypatch.setattr(runner.planner_dispatch, "planner_for_engine", lambda _e: _Planner([]))
    monkeypatch.setattr(
        runner, "_merge_fanout_results", calls.recorder("merge", ("no_route", {}, [], []))
    )
    monkeypatch.setattr(
        runner, "_record_fanout_journey_search", calls.recorder("record", (search_id, None))
    )
    monkeypatch.setattr(runner, "_persist_pair_coverage_result", calls.recorder("persist"))

    await runner._execute_pair_fanout(
        run_id=uuid.uuid4(),
        session_ids=["zz-s1", "zz-s2"],
        engine_by_session={"zz-s1": "motis", "zz-s2": "otp"},
        origin=ORIGIN,
        dest=DEST,
        depart_at=DEPART,
    )

    assert calls.names() == ["merge", "record", "persist"]
    assert threading.get_ident() not in calls.threads()
    (per_session,) = calls.kwargs("merge")["args"]
    assert [sub[0] for sub in per_session] == ["zz-s1", "zz-s2"]
    assert calls.kwargs("persist")["journey_search_id"] == search_id
    assert calls.kwargs("persist")["status"] == "no_route"


# ───────────────────────────────── execute_run ─────────────────────────────────


def _snapshot() -> Any:
    return runner._Phase1Snapshot(
        run_mode=runner.MODE_SINGLE_SESSION,
        session_id_for_pairs="zz-s",
        engine_for_pairs="motis",
        fanout_session_ids=[],
        engine_by_session={},
        depart_at_for_pairs=DEPART,
        pairs=[(ORIGIN, DEST)],
        cfg=runner.CoverageConfig(),
        window=None,  # type: ignore[arg-type]
    )


async def test_execute_run_phases_run_off_the_loop(monkeypatch) -> None:
    rid = uuid.uuid4()
    web_thread = threading.get_ident()
    web_loop = asyncio.get_running_loop()
    seen: dict[str, Any] = {}

    def _phase1(run_id: uuid.UUID) -> Any:
        seen["phase1"] = (run_id, threading.get_ident())
        return _snapshot()

    async def _pair(**_kw: Any) -> None:
        seen["pair_loop"] = asyncio.get_running_loop()

    async def _finalise(*, run_id: uuid.UUID, elapsed_s: float, cfg: Any) -> None:
        seen["finalise"] = (run_id, threading.get_ident(), asyncio.get_running_loop())

    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", _phase1)
    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair)
    monkeypatch.setattr(runner, "_finalise_completed_run", _finalise)

    await runner.execute_run(rid)

    assert seen["phase1"][0] == rid and seen["phase1"][1] != web_thread
    # The engine calls stay on the web loop: awaited I/O.
    assert seen["pair_loop"] is web_loop
    run_id, thread, loop = seen["finalise"]
    assert run_id == rid and thread != web_thread and loop is not web_loop
    assert rid not in runner._CANCEL_EVENTS


async def test_execute_run_terminal_writes_run_off_the_loop(monkeypatch) -> None:
    web_thread = threading.get_ident()
    calls = _Calls()
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: _snapshot())
    monkeypatch.setattr(runner, "_persist_cancelled_run", calls.recorder("cancelled"))
    monkeypatch.setattr(runner, "_mark_run_failed", calls.recorder("failed"))

    stopped = uuid.uuid4()

    async def _pair_then_stop(**_kw: Any) -> None:
        runner.request_cancel(stopped)

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair_then_stop)
    await runner.execute_run(stopped)

    crashed = uuid.uuid4()

    async def _crash(**_kw: Any) -> None:
        raise RuntimeError("zz-99")

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _crash)
    await runner.execute_run(crashed)

    assert calls.names() == ["cancelled", "failed"]
    assert calls.kwargs("cancelled")["run_id"] == stopped
    assert calls.kwargs("failed")["args"] == (crashed,)
    assert web_thread not in calls.threads()


# ───────────────────────────── orphaned runs at startup ─────────────────────────────


class _Run:
    def __init__(self, status: str) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.summary: dict[str, Any] | None = {"zz": 99}
        self.completed_pairs = 99
        self.total_pairs = 199
        self.finished_at: datetime | None = None


class _OrphanDb:
    def __init__(self, runs: list[_Run]) -> None:
        self.runs = runs

    def execute(self, _stmt: Any) -> Any:
        runs = self.runs

        class _Result:
            def scalars(self) -> Any:
                return self

            def all(self) -> list[_Run]:
                return runs

        return _Result()


def test_mark_orphaned_runs_returns_their_ids() -> None:
    runs = [_Run("running"), _Run("pending")]
    assert runner.mark_orphaned_runs_as_failed(_OrphanDb(runs)) == [r.id for r in runs]  # type: ignore[arg-type]
    for run in runs:
        assert run.status == "failed"
        assert run.summary is not None and run.summary["orphaned_by_restart"] is True
        assert run.summary["zz"] == 99
        assert run.summary["completed_pairs_at_restart"] == 99
    assert runner.mark_orphaned_runs_as_failed(_OrphanDb([])) == []  # type: ignore[arg-type]
