"""#392 (durable part) — the coverage executor's pieces that need no database:
the worker's SIGTERM release, the executor loop's error paths, the page's
display status, the snapshot of the runner knobs, the Prometheus gauges and
their alert rules. The database behaviour (atomic claim, heartbeat, resume,
Stop, resume limit) is tested on PostgreSQL in
tests/integration/test_coverage_executor.py.

Invented values only (ZZ, numbers with 99).
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from app.network_coverage import executor, runner

ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────────── worker: start and SIGTERM ─────────────────────────────


def test_worker_starts_the_executor_and_releases_at_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker

    events: list[Any] = []

    class _FakeExecutor:
        def start(self) -> None:
            events.append("start")

        def release(self) -> list[uuid.UUID]:
            events.append("release")
            return []

    handlers: dict[int, Any] = {}
    monkeypatch.setattr(executor, "CoverageExecutor", _FakeExecutor)
    monkeypatch.setattr(worker.signal, "signal", lambda s, h: handlers.__setitem__(s, h))
    monkeypatch.setattr(worker.os, "_exit", lambda code: events.append(("exit", code)))

    worker._start_coverage_executor()
    assert events == ["start"]
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert events == ["start", "release", ("exit", 128 + signal.SIGTERM)]


def test_worker_main_starts_the_coverage_executor() -> None:
    import inspect

    from app import worker

    assert "_start_coverage_executor()" in inspect.getsource(worker.main)


# ───────────────────────────────── executor loop ─────────────────────────────────


@contextmanager
def _no_db() -> Any:
    yield MagicMock()


def test_new_executor_ids_are_unique_and_bounded() -> None:
    a, b = executor.new_executor_id(), executor.new_executor_id()
    assert a != b
    assert len(a) <= 128
    assert a.count(":") >= 2


def test_read_limits_uses_the_platform_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor.config_service,
        "get_all",
        lambda _db: {"COVERAGE_MAX_CONCURRENT_RUNS": 2, "COVERAGE_MAX_RESUMES": 9},
    )
    assert executor.read_limits(MagicMock()) == executor.Limits(2, 9)
    monkeypatch.setattr(executor.config_service, "get_all", lambda _db: {})
    assert executor.read_limits(MagicMock()) == executor.Limits(1, 5)


async def test_poll_once_respects_the_concurrency_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = executor.CoverageExecutor("zz:99:a", session_factory=_no_db)
    monkeypatch.setattr(executor, "read_limits", lambda _db: executor.Limits(1, 5))
    claims: list[int] = []

    def _claim(**_kw: Any) -> None:
        claims.append(1)

    monkeypatch.setattr(executor, "claim_next_run", lambda _db, **kw: _claim(**kw))
    ex._active[uuid.uuid4()] = asyncio.ensure_future(asyncio.sleep(0))
    assert await ex.poll_once() is None
    assert claims == []
    ex._active.clear()
    assert await ex.poll_once() is None
    assert claims == [1]


async def test_serve_survives_a_failing_poll_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = executor.CoverageExecutor("zz:99:a", poll_s=0.01, session_factory=_no_db)
    polls: list[int] = []

    async def _boom() -> None:
        polls.append(1)
        if len(polls) == 3:
            ex.stop()
        raise RuntimeError("zz-99 database not migrated yet")

    monkeypatch.setattr(ex, "poll_once", _boom)
    await asyncio.wait_for(ex.serve(), timeout=5)
    assert len(polls) == 3


async def test_execute_settles_a_run_that_ended_without_a_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = executor.CoverageExecutor("zz:99:a", heartbeat_s=60, session_factory=_no_db)
    settled: list[uuid.UUID] = []

    async def _crash(_run_id: uuid.UUID, *, executor_id: str) -> None:
        raise RuntimeError("zz-99")

    monkeypatch.setattr(runner, "execute_run", _crash)
    monkeypatch.setattr(executor, "settle_unfinished", lambda _db, rid, _ex: settled.append(rid))
    claim = executor.Claim(run_id=uuid.uuid4(), resumed=False, resume_count=0)
    ex._active[claim.run_id] = asyncio.ensure_future(asyncio.sleep(0))
    await ex._execute(claim)
    assert settled == [claim.run_id]
    assert ex.active_run_ids == []


async def test_a_cancelled_execution_is_never_settled_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = executor.CoverageExecutor("zz:99:a", heartbeat_s=60, session_factory=_no_db)
    settled: list[uuid.UUID] = []

    async def _forever(_run_id: uuid.UUID, *, executor_id: str) -> None:
        await asyncio.sleep(99)

    monkeypatch.setattr(runner, "execute_run", _forever)
    monkeypatch.setattr(executor, "settle_unfinished", lambda _db, rid, _ex: settled.append(rid))
    claim = executor.Claim(run_id=uuid.uuid4(), resumed=True, resume_count=1)
    task = asyncio.ensure_future(ex._execute(claim))
    ex._active[claim.run_id] = task
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled == []
    assert ex.active_run_ids == []


async def test_heartbeat_failure_is_retried_and_a_takeover_abandons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = executor.CoverageExecutor("zz:99:a", heartbeat_s=0.01, session_factory=_no_db)
    beats: list[int] = []

    def _beat(_db: Any, _rid: uuid.UUID, _ex: str) -> bool:
        beats.append(1)
        if len(beats) == 1:
            raise RuntimeError("zz-99 database blip")
        return False

    abandoned: list[uuid.UUID] = []
    monkeypatch.setattr(executor, "heartbeat", _beat)
    monkeypatch.setattr(executor, "claim_taken_over", lambda _db, _rid, _ex: True)
    monkeypatch.setattr(runner, "abandon_run", abandoned.append)
    rid = uuid.uuid4()
    await asyncio.wait_for(ex._heartbeat_loop(rid), timeout=5)
    assert len(beats) == 2
    assert abandoned == [rid]


def test_release_is_bounded_and_survives_a_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken() -> Any:
        raise RuntimeError("zz-99 database down")

    ex = executor.CoverageExecutor("zz:99:a", session_factory=_broken)
    assert ex.release(timeout_s=1) == []


def test_heartbeat_age_and_resume_record() -> None:
    now = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
    run = SimpleNamespace(
        heartbeat_at=now - timedelta(seconds=199),
        claimed_by="zz:99:a",
        completed_pairs=99,
        resume_count=0,
        summary={"zz": 99},
    )
    assert executor._heartbeat_age_s(run, now) == 199  # type: ignore[arg-type]
    executor._record_resume(run, now, crash=True)  # type: ignore[arg-type]
    assert run.resume_count == 1
    assert run.summary["zz"] == 99
    assert run.summary["resume_count"] == 1
    assert run.summary["crash_resume_count"] == 1
    assert run.summary["release_resume_count"] == 0
    first = run.summary["resumes"][0]
    assert first["kind"] == "crash"
    assert first["previous_executor"] == "zz:99:a"
    assert first["completed_pairs"] == 99
    run.claimed_by = None
    executor._record_resume(run, now, crash=False)  # type: ignore[arg-type]
    assert run.resume_count == 2
    assert run.summary["crash_resume_count"] == 1
    assert run.summary["release_resume_count"] == 1
    assert run.summary["resumes"][1]["kind"] == "release"
    run.heartbeat_at = None
    assert executor._heartbeat_age_s(run, now) is None  # type: ignore[arg-type]
    assert executor._crash_resume_count({"crash_resume_count": "zz"}) == 0
    assert executor._crash_resume_count(None) == 0


# ─────────────────────────────── runner: snapshot, summary ───────────────────────────────


def test_config_snapshot_round_trip_and_bad_shapes() -> None:
    from dataclasses import asdict

    cfg = runner.CoverageConfig(slot_count=9, pair_parallelism=2)
    assert runner._config_from_snapshot(asdict(cfg)) == cfg
    assert runner._config_from_snapshot({**asdict(cfg), "zz_gone_knob": 99}) == cfg
    assert runner._config_from_snapshot(None) is None
    assert runner._config_from_snapshot("zz") is None


def test_completion_keeps_the_resume_record_only() -> None:
    summary = {"resume_count": 2, "resumes": [{"zz": 99}], "cancelled_by_operator": True}
    assert runner._kept_resume_summary(summary) == {"resume_count": 2, "resumes": [{"zz": 99}]}
    assert runner._kept_resume_summary(None) == {}


def test_counters_from_statuses() -> None:
    assert runner._counter_values(["ok", "ok", "no_route", "timeout", "error", "skipped"]) == {
        "completed_pairs": 6,
        "ok_pairs": 2,
        "no_route_pairs": 1,
        "error_pairs": 2,
    }


def test_elapsed_of_a_resumed_run_counts_from_its_first_start() -> None:
    import time

    snap = SimpleNamespace(resumed=True, started_at=datetime.now(UTC) - timedelta(seconds=99))
    assert 99 <= runner._run_elapsed_s(snap, time.monotonic()) < 120  # type: ignore[arg-type]
    fresh = SimpleNamespace(resumed=False, started_at=None)
    assert runner._run_elapsed_s(fresh, time.monotonic() - 9) >= 9  # type: ignore[arg-type]


async def test_an_abandoned_run_writes_no_terminal_state(monkeypatch: pytest.MonkeyPatch) -> None:
    rid = uuid.uuid4()
    snap = runner._Phase1Snapshot(
        run_mode=runner.MODE_SINGLE_SESSION,
        session_id_for_pairs="zz-s",
        engine_for_pairs="motis",
        fanout_session_ids=[],
        engine_by_session={},
        depart_at_for_pairs=datetime(2099, 9, 9, tzinfo=UTC),
        pairs=[(MagicMock(), MagicMock())],
        cfg=runner.CoverageConfig(),
        window=None,  # type: ignore[arg-type]
    )
    writes: list[str] = []
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: snap)

    async def _pair(**_kw: Any) -> None:
        runner.abandon_run(rid)

    async def _stopped(_rid: uuid.UUID, _cache: Any) -> bool:
        return True

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair)
    monkeypatch.setattr(runner, "_run_stopped", _stopped)
    monkeypatch.setattr(runner, "_persist_cancelled_run", lambda **_kw: writes.append("c"))
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *_a: writes.append("f"))
    await runner.execute_run(rid, executor_id="zz:99:a")
    assert writes == []
    assert not runner.is_abandoned(rid)


# ─────────────────────────────── web: display status ───────────────────────────────


@pytest.mark.parametrize(
    ("status", "heartbeat_s", "holder", "stop", "shown"),
    [
        ("pending", None, None, False, "queued"),
        ("running", 9, "zz:99:a", False, "running"),
        ("running", 199, "zz:99:a", False, "resuming"),
        ("running", None, "zz:99:a", False, "resuming"),
        # Released at a deploy: no holder, heartbeat = the release time.
        ("running", 9, None, False, "resuming"),
        ("running", 9, "zz:99:a", True, "stopping"),
        ("completed", 9, "zz:99:a", True, "completed"),
        ("failed", None, None, False, "failed"),
    ],
)
def test_display_status(
    status: str, heartbeat_s: int | None, holder: str | None, stop: bool, shown: str
) -> None:
    from app.api.admin.network_coverage import _display_status

    now = datetime.now(UTC)
    run = SimpleNamespace(
        status=status,
        claimed_by=holder,
        heartbeat_at=None if heartbeat_s is None else now - timedelta(seconds=heartbeat_s),
        cancel_requested_at=now if stop else None,
    )
    assert _display_status(run)[0] == shown


@pytest.mark.parametrize(
    ("status", "summary", "reason"),
    [
        ("failed", {"failure_reason": "zz-99 why"}, "zz-99 why"),
        ("failed", {"orphaned_by_restart": True}, "web restarted during the run"),
        ("failed", None, None),
        ("failed", {"failure_reason": ""}, None),
        ("completed", {"failure_reason": "zz-99 stale"}, None),
    ],
)
def test_failure_reason_of_a_run(status: str, summary: Any, reason: str | None) -> None:
    from app.api.admin.network_coverage import _failure_reason

    got = _failure_reason(SimpleNamespace(status=status, summary=summary))
    if reason is None:
        assert got is None
    else:
        assert got is not None
        assert got.startswith(reason)


def test_run_summary_carries_the_executor_fields() -> None:
    from app.api.admin.network_coverage import _run_to_summary

    now = datetime.now(UTC)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        session_id="zz-s",
        session_label="zz-s",
        depart_at=now,
        started_at=now,
        finished_at=None,
        status="running",
        direction="both",
        mode="single_session",
        total_pairs=99,
        completed_pairs=9,
        ok_pairs=9,
        no_route_pairs=0,
        error_pairs=0,
        heartbeat_at=now - timedelta(seconds=299),
        cancel_requested_at=None,
        resume_count=2,
    )
    summary = _run_to_summary(run)
    assert summary.display_status == "resuming"
    assert summary.resume_count == 2
    assert summary.heartbeat_age_seconds is not None
    assert summary.heartbeat_age_seconds >= 299
    assert summary.failure_reason is None


def test_page_shows_the_display_status_and_stops_queued_runs() -> None:
    page = (ROOT / "app/templates/admin/network_coverage.html").read_text(encoding="utf-8")
    assert "function runStatusPill(run)" in page
    assert "run.display_status || run.status" in page
    assert "const reason = run.failure_reason || '';" in page
    assert 'class="cov-fail-reason"' in page
    assert "const stopBtn = runIsLive(r)" in page
    for shown in ("queued", "resuming", "stopping"):
        assert f'.cov-pill[data-status="{shown}"]' in page


# ─────────────────────────────── metrics and alert rules ───────────────────────────────


def test_coverage_gauges_from_one_row() -> None:
    from app.metrics import _COVERAGE_GAUGES, _coverage_metrics

    db = MagicMock()
    db.execute.return_value.one.return_value = (1, 2, 99.5, None, 9)

    @contextmanager
    def _factory() -> Any:
        yield db

    values = {m.name: m.samples[0].value for m in _coverage_metrics(_factory)}
    assert list(values) == [name for name, _ in _COVERAGE_GAUGES]
    assert list(values.values()) == [1.0, 2.0, 99.5, 0.0, 9.0]

    db.execute.return_value.one.return_value = (1, 2)
    assert list(_coverage_metrics(_factory)) == []


def test_coverage_gauges_swallow_db_errors() -> None:
    from app.metrics import _coverage_metrics

    def _broken() -> Any:
        raise ConnectionError("zz-99")

    assert list(_coverage_metrics(_broken)) == []


def test_alert_rules_read_the_gauges_web_exports() -> None:
    from app.metrics import _COVERAGE_GAUGES
    from tests.unit.test_monitoring_config import VIATOR_WEB_METRICS

    # #397 added web's own event-loop gauge beside the coverage gauges.
    assert set(VIATOR_WEB_METRICS) == {name for name, _ in _COVERAGE_GAUGES} | {
        "viator_web_event_loop_lag_seconds"
    }
    rules = yaml.safe_load((ROOT / "docker/prometheus/rules/coverage.yml").read_text())
    exprs = {r["alert"]: r["expr"] for g in rules["groups"] for r in g["rules"]}
    assert exprs["CoverageRunHeartbeatStale"] == "viator_coverage_heartbeat_age_seconds > 900"
    assert "viator_coverage_runs_running == 0" in exprs["CoverageRunNotClaimed"]
    # The alert threshold stays well beyond the executor's stale limit.
    assert executor.STALE_AFTER_SECONDS * 3 < 900


def test_ci_runs_the_alert_rule_unit_tests() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in ci["jobs"]["python"]["steps"] if s.get("name", "").startswith("promtool")
    )
    assert 'test rules "/etc/prometheus/tests/$(basename "$f")"' in step["run"]
    tests = yaml.safe_load((ROOT / "docker/prometheus/tests/coverage_test.yml").read_text())
    assert tests["rule_files"] == ["../rules/coverage.yml"]


async def test_settle_failure_is_logged_and_the_run_released_from_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = executor.CoverageExecutor("zz:99:a", heartbeat_s=60, session_factory=_no_db)

    async def _done(_run_id: uuid.UUID, *, executor_id: str) -> None:
        return None

    def _broken(_db: Any, _rid: uuid.UUID, _ex: str) -> bool:
        raise RuntimeError("zz-99")

    monkeypatch.setattr(runner, "execute_run", _done)
    monkeypatch.setattr(executor, "settle_unfinished", _broken)
    claim = executor.Claim(run_id=uuid.uuid4(), resumed=False, resume_count=0)
    await ex._execute(claim)
    assert ex.active_run_ids == []


def test_start_runs_serve_in_a_daemon_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = executor.CoverageExecutor("zz:99:a", poll_s=0.01, session_factory=_no_db)
    polls: list[int] = []

    async def _poll() -> None:
        polls.append(1)
        ex.stop()

    monkeypatch.setattr(ex, "poll_once", _poll)
    thread = ex.start()
    thread.join(timeout=5)
    assert thread.daemon
    assert not thread.is_alive()
    assert polls == [1]


def test_hubs_by_ids_keep_the_snapshot_order_and_fall_back_to_the_static_list() -> None:
    from app.network_coverage.hubs import HUBS

    row = SimpleNamespace(id="zz-hub-9", name="ZZ 9", short="ZZ9", region=None, lat=45.9, lon=9.9)
    db = MagicMock()
    db.execute.return_value.scalars.return_value = [row]
    hubs = runner._hubs_by_ids(db, [HUBS[0].id, "zz-gone-99", "zz-hub-9"])
    assert [h.id for h in hubs] == [HUBS[0].id, "zz-hub-9"]
    assert hubs[1].region == ""


async def test_completion_commits_nothing_once_the_claim_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(), summary={"resume_count": 1}, verify_externally=False, status="running"
    )
    db = MagicMock()
    db.get.return_value = run
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.scalar_one_or_none.return_value = "zz:99:other"

    @contextmanager
    def _session() -> Any:
        yield db

    monkeypatch.setattr(runner, "SessionLocal", _session)
    await runner._finalise_completed_run(
        run_id=run.id, elapsed_s=9.0, cfg=runner.CoverageConfig(), executor_id="zz:99:a"
    )
    assert run.status == "running"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


# ─────────────────────────────── review round 1 (#402) ───────────────────────────────


async def test_a_failing_abandoned_run_is_not_marked_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair loop raises after the claim was lost: no failure write, the
    run belongs to the other executor (S4: the abandon check before
    `_mark_run_failed`)."""
    rid = uuid.uuid4()
    snap = runner._Phase1Snapshot(
        run_mode=runner.MODE_SINGLE_SESSION,
        session_id_for_pairs="zz-s",
        engine_for_pairs="motis",
        fanout_session_ids=[],
        engine_by_session={},
        depart_at_for_pairs=datetime(2099, 9, 9, tzinfo=UTC),
        pairs=[(MagicMock(), MagicMock())],
        cfg=runner.CoverageConfig(),
        window=None,  # type: ignore[arg-type]
    )
    failed: list[Any] = []

    async def _pair(**_kw: Any) -> None:
        runner.abandon_run(rid)
        raise RuntimeError("zz-99 after the takeover")

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair)
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *a: failed.append(a))
    try:
        assert await runner._run_pairs(rid, snap, "zz:99:a") is False
    finally:
        runner._ABANDONED.discard(rid)
        runner.clear_cancel(rid)
    assert failed == []


async def test_a_run_needing_a_resume_is_left_claimed_not_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = executor.CoverageExecutor("zz:99:a", heartbeat_s=60, session_factory=_no_db)
    settled: list[uuid.UUID] = []
    noted: list[tuple[uuid.UUID, str]] = []

    async def _needs_resume(_run_id: uuid.UUID, *, executor_id: str) -> None:
        raise runner.RunNeedsResume("zz-99 pairs have no result row yet")

    def _note(_db: Any, rid: uuid.UUID, _ex: str, reason: str) -> None:
        noted.append((rid, reason))
        raise RuntimeError("zz-99 the note fails too")  # logged, nothing else

    monkeypatch.setattr(runner, "execute_run", _needs_resume)
    monkeypatch.setattr(executor, "settle_unfinished", lambda _db, rid, _ex: settled.append(rid))
    monkeypatch.setattr(executor, "note_interruption", _note)
    claim = executor.Claim(run_id=uuid.uuid4(), resumed=True, resume_count=1)
    ex._active[claim.run_id] = asyncio.ensure_future(asyncio.sleep(0))
    await ex._execute(claim)
    assert settled == []
    assert noted == [(claim.run_id, "zz-99 pairs have no result row yet")]
    assert ex.active_run_ids == []


def test_the_resume_limit_reason_names_the_missing_pairs_and_the_last_interruption() -> None:
    now = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
    run = SimpleNamespace(
        status="running",
        finished_at=None,
        claimed_by="zz:99:a",
        completed_pairs=99,
        summary={
            "crash_resume_count": 5,
            "missing_pairs": 9,
            "last_interruption": {"reason": "zz-99 a database error"},
        },
    )
    executor._fail_resume_limit(run, now, 5)  # type: ignore[arg-type]
    reason = run.summary["failure_reason"]
    assert run.status == "failed"
    assert "6 times" in reason
    assert "9 pairs still had no result row" in reason
    assert "last interruption: zz-99 a database error" in reason
    assert run.claimed_by is None


def test_the_claim_takes_a_released_run_as_a_release_resume() -> None:
    now = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        claimed_by=None,
        heartbeat_at=now,
        completed_pairs=9,
        resume_count=0,
        summary=None,
    )
    claim = executor._take(run, now, "zz:99:b")  # type: ignore[arg-type]
    assert claim.resumed is True
    assert claim.crash is False
    assert run.claimed_by == "zz:99:b"
    queued = SimpleNamespace(id=uuid.uuid4(), status="pending", claimed_by=None, resume_count=0)
    claim = executor._take(queued, now, "zz:99:b")  # type: ignore[arg-type]
    assert claim.resumed is False
    assert queued.status == "running"
    assert queued.started_at == now
