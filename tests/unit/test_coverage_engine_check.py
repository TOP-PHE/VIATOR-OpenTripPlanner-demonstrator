"""The coverage runner's engine check: a run never asks a stopped engine.

22-24 Nov 2026 — eu19's MOTIS had stopped (its container could no longer start)
and an AT/DE/FR run, through 7 resumes, asked every pair of the stopped engine:
only error cells. A run now checks before its pairs that each engine it asks
answers (`_check_engines_before_pairs`), and a single-session run whose engine
stops answering is stopped (`_count_pair_outcome`, COVERAGE_ENGINE_DOWN_PAIRS).
Both end the run failed with a readable reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.config_schema import CONFIG_SCHEMA, coerce, default_for
from app.journey import motis_client, otp_client
from app.network_coverage import runner
from app.network_coverage.hubs import Hub

ORIGIN = Hub(id="zz-a", name="ZZ A", short="ZZA", region="", lat=45.99, lon=9.99)
DEST = Hub(id="zz-b", name="ZZ B", short="ZZB", region="", lat=46.99, lon=10.99)
SID = "zz-sid"


def _snapshot(
    *,
    cfg: runner.CoverageConfig | None = None,
    fanout: dict[str, str] | None = None,
    todo: list[tuple[Hub, Hub]] | None = None,
) -> runner._Phase1Snapshot:
    return runner._Phase1Snapshot(
        run_mode=runner.MODE_FANOUT if fanout else runner.MODE_SINGLE_SESSION,
        session_id_for_pairs=None if fanout else SID,
        engine_for_pairs="motis",
        fanout_session_ids=list(fanout or {}),
        engine_by_session=dict(fanout or {}),
        depart_at_for_pairs=datetime(2099, 9, 9, 9, 0, tzinfo=UTC),
        pairs=[(ORIGIN, DEST), (DEST, ORIGIN)],
        cfg=cfg or runner.CoverageConfig(),
        window=None,  # type: ignore[arg-type]
        todo_pairs=todo,
    )


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    yield
    runner._UNREACHABLE_PAIRS.clear()
    runner._ENGINE_DOWN.clear()
    runner._CANCEL_EVENTS.clear()
    runner._ABANDONED.clear()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The runner's sleeps, recorded and not waited."""
    calls: list[float] = []

    async def _sleep(delay_s: float) -> None:
        calls.append(delay_s)

    monkeypatch.setattr(runner.asyncio, "sleep", _sleep)
    return calls


@pytest.fixture
def not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's Stop check without the database: the in-process Stop only."""

    async def _stopped(run_id: uuid.UUID, _cache: Any) -> bool:
        return runner.is_cancelled(run_id)

    monkeypatch.setattr(runner, "_run_stopped", _stopped)


def _engines(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[bool]]) -> list[str]:
    """Each session's engine answers the checks in turn as listed; returns the sessions asked."""
    asked: list[str] = []

    async def _answers(_engine: str, session_id: str) -> bool:
        asked.append(session_id)
        return answers[session_id].pop(0)

    monkeypatch.setattr(runner, "_engine_answers", _answers)
    return asked


# ─────────────────────────── what is asked ───────────────────────────


def test_each_engine_is_asked_where_its_container_health_check_asks() -> None:
    assert motis_client.health_url("eu19-transit-motis") == "http://motis-eu19-transit-motis:8080/"
    assert otp_client.health_url("zz-otp") == "http://otp-zz-otp:8080/otp/"


def _mock_engine(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[str]:
    asked: list[str] = []
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return handler(request)

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(runner.httpx, "AsyncClient", _client)
    return asked


@pytest.mark.parametrize("status", [200, 404, 503])
async def test_any_http_answer_means_the_engine_runs(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    asked = _mock_engine(monkeypatch, lambda _r: httpx.Response(status))
    assert await runner._engine_answers("motis", SID) is True
    assert asked == [f"http://motis-{SID}:8080/"]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno -2] Name or service not known"),
        httpx.ConnectTimeout("zz"),
        httpx.ReadTimeout("zz"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
)
async def test_a_transport_error_means_the_engine_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, error: httpx.TransportError
) -> None:
    def _fail(_r: httpx.Request) -> httpx.Response:
        raise error

    asked = _mock_engine(monkeypatch, _fail)
    assert await runner._engine_answers("otp", SID) is False
    assert asked == [f"http://otp-{SID}:8080/otp/"]


# ─────────────────────── the check before the pairs ───────────────────────


async def test_an_engine_that_answers_is_asked_once(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    asked = _engines(monkeypatch, {SID: [True]})
    assert await runner._engines_not_answering(uuid.uuid4(), _snapshot()) == []
    assert asked == [SID]
    assert sleeps == []


async def test_an_engine_restarting_gets_its_time_to_come_back(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    asked = _engines(monkeypatch, {SID: [False, False, True]})
    assert await runner._engines_not_answering(uuid.uuid4(), _snapshot()) == []
    assert asked == [SID, SID, SID]
    assert sleeps == [runner._ENGINE_PROBE_DELAY_S] * 2


async def test_an_engine_silent_at_each_check_is_down(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    _engines(monkeypatch, {SID: [False] * runner._ENGINE_PROBE_ATTEMPTS})
    down = await runner._engines_not_answering(uuid.uuid4(), _snapshot())
    assert down == [(SID, "motis")]
    assert len(sleeps) == runner._ENGINE_PROBE_ATTEMPTS - 1


async def test_fanout_checks_every_session_and_asks_again_only_the_silent_ones(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    asked = _engines(monkeypatch, {"zz-otp": [True], "zz-motis": [False, False, False]})
    snap = _snapshot(fanout={"zz-otp": "otp", "zz-motis": "motis"})
    assert await runner._engines_not_answering(uuid.uuid4(), snap) == [("zz-motis", "motis")]
    assert asked.count("zz-otp") == 1
    assert asked.count("zz-motis") == 3


async def test_a_stop_during_the_wait_ends_the_check(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)
    asked = _engines(monkeypatch, {SID: [False, False, False]})

    async def _sleep_then_stop(_delay_s: float) -> None:
        runner.request_cancel(run_id)

    monkeypatch.setattr(runner.asyncio, "sleep", _sleep_then_stop)
    assert await runner._engines_not_answering(run_id, _snapshot()) == []
    assert asked == [SID]


async def test_a_silent_engine_fails_the_run_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    _engines(monkeypatch, {SID: [False] * 3})
    failed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *a: failed.append(a))
    run_id = uuid.uuid4()

    assert await runner._check_engines_before_pairs(run_id, _snapshot(), "zz:99:a") is False

    [(failed_id, executor_id, reason)] = failed
    assert (failed_id, executor_id) == (run_id, "zz:99:a")
    assert f"session {SID} (MOTIS)" in reason
    assert "did not answer" in reason
    assert "3 checks over 40 s" in reason


async def test_the_reason_names_each_silent_session_of_a_fanout(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    _engines(monkeypatch, {"zz-1": [False] * 3, "zz-2": [False] * 3})
    failed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *a: failed.append(a))
    snap = _snapshot(fanout={"zz-1": "otp", "zz-2": "motis"})

    assert await runner._check_engines_before_pairs(uuid.uuid4(), snap, None) is False
    assert "sessions zz-1 (OTP), zz-2 (MOTIS)" in failed[0][2]


async def test_a_resumed_run_with_no_pair_left_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _engines(monkeypatch, {})
    assert await runner._check_engines_before_pairs(uuid.uuid4(), _snapshot(todo=[]), None)
    assert asked == []


async def test_a_run_whose_claim_was_lost_is_not_written(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], not_stopped: None
) -> None:
    _engines(monkeypatch, {SID: [False] * 3})
    failed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *a: failed.append(a))
    run_id = uuid.uuid4()
    runner._ABANDONED.add(run_id)

    assert await runner._check_engines_before_pairs(run_id, _snapshot(), "zz:99:a") is False
    assert failed == []


# ─────────────────────── an engine that stops mid-run ───────────────────────


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("[Errno -2] Name or service not known")


def test_the_run_stops_at_the_set_number_of_unreachable_pairs_in_a_row() -> None:
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)
    cfg = runner.CoverageConfig(engine_down_pairs=3)

    runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    assert not runner.is_cancelled(run_id)
    assert run_id not in runner._ENGINE_DOWN

    runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    assert runner.is_cancelled(run_id)
    reason = runner._ENGINE_DOWN[run_id]
    assert f"session {SID} stopped answering" in reason
    assert "3 pairs in a row" in reason
    assert "ConnectError" in reason
    assert "COVERAGE_ENGINE_DOWN_PAIRS = 3" in reason


@pytest.mark.parametrize(
    "between",
    [None, httpx.ReadTimeout("zz"), RuntimeError("zz")],
    ids=["an answer", "a read timeout", "another error"],
)
def test_any_other_outcome_starts_the_count_again(between: BaseException | None) -> None:
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)
    cfg = runner.CoverageConfig(engine_down_pairs=3)

    for _ in range(2):
        runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    runner._count_pair_outcome(run_id, SID, cfg, between)
    for _ in range(2):
        runner._count_pair_outcome(run_id, SID, cfg, _connect_error())

    assert not runner.is_cancelled(run_id)
    assert runner._UNREACHABLE_PAIRS[run_id] == 2


def test_zero_never_stops_the_run() -> None:
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)
    cfg = runner.CoverageConfig(engine_down_pairs=0)
    for _ in range(50):
        runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    assert not runner.is_cancelled(run_id)
    assert run_id not in runner._ENGINE_DOWN


def test_a_stopped_run_does_not_count_its_last_pairs() -> None:
    """A Stop click cuts the pairs' retries short with their connection error:
    the run stays cancelled, not failed."""
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)
    runner.request_cancel(run_id)
    cfg = runner.CoverageConfig(engine_down_pairs=1)
    runner._count_pair_outcome(run_id, SID, cfg, _connect_error())
    assert run_id not in runner._ENGINE_DOWN


def test_each_run_counts_its_own_pairs() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    cfg = runner.CoverageConfig(engine_down_pairs=2)
    for run_id in (first, second):
        runner.register_cancel(run_id)
    runner._count_pair_outcome(first, SID, cfg, _connect_error())
    runner._count_pair_outcome(second, SID, cfg, _connect_error())
    assert not runner.is_cancelled(first)
    assert not runner.is_cancelled(second)


async def test_a_pair_that_cannot_reach_its_engine_is_counted(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """`_execute_pair` counts its connection error once its retries are spent,
    and still writes its error cell."""
    run_id = uuid.uuid4()
    runner.register_cancel(run_id)

    async def _down(**_kw: Any) -> Any:
        raise _connect_error()

    written: list[dict[str, Any]] = []

    async def _write(_record: Any, *, record_kwargs: Any, result_kwargs: dict[str, Any]) -> bool:
        written.append(result_kwargs)
        return True

    monkeypatch.setattr(runner, "_fetch_journeys_sliced", _down)
    monkeypatch.setattr(runner, "_write_pair", _write)

    await runner._execute_pair(
        run_id=run_id,
        session_id=SID,
        engine="motis",
        origin=ORIGIN,
        dest=DEST,
        depart_at=datetime(2099, 9, 9, 9, 0, tzinfo=UTC),
        cfg=runner.CoverageConfig(engine_down_pairs=1),
        window=runner.ResolvedWindow(
            start_utc=datetime(2099, 9, 9, tzinfo=UTC),
            end_utc=datetime(2099, 9, 10, tzinfo=UTC),
            tz_name="UTC",
        ),
    )

    assert len(sleeps) == len(runner._CONNECT_RETRY_DELAYS_S)
    assert [w["status"] for w in written] == ["error"]
    assert runner.is_cancelled(run_id)
    assert "COVERAGE_ENGINE_DOWN_PAIRS = 1" in runner._ENGINE_DOWN[run_id]


async def test_a_pair_that_gets_an_answer_starts_the_count_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()
    runner._UNREACHABLE_PAIRS[run_id] = 4

    async def _answer(**_kw: Any) -> Any:
        return {}, []

    async def _write(_record: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(runner, "_fetch_journeys_sliced", _answer)
    monkeypatch.setattr(runner, "_write_pair", _write)

    await runner._execute_pair(
        run_id=run_id,
        session_id=SID,
        engine="motis",
        origin=ORIGIN,
        dest=DEST,
        depart_at=datetime(2099, 9, 9, 9, 0, tzinfo=UTC),
        window=runner.ResolvedWindow(
            start_utc=datetime(2099, 9, 9, tzinfo=UTC),
            end_utc=datetime(2099, 9, 10, tzinfo=UTC),
            tz_name="UTC",
        ),
    )
    assert run_id not in runner._UNREACHABLE_PAIRS


# ─────────────────────────── the whole run ───────────────────────────


@pytest.fixture
def ends(monkeypatch: pytest.MonkeyPatch, not_stopped: None) -> dict[str, list[Any]]:
    """How the run ended: failed (with its reason), cancelled or completed."""
    seen: dict[str, list[Any]] = {"failed": [], "cancelled": [], "completed": []}
    monkeypatch.setattr(runner, "_mark_run_failed", lambda *a: seen["failed"].append(a[2]))
    monkeypatch.setattr(
        runner, "_persist_cancelled_run", lambda **kw: seen["cancelled"].append(kw["run_id"])
    )

    async def _finalise(*, run_id: uuid.UUID, **_kw: Any) -> None:
        seen["completed"].append(run_id)

    monkeypatch.setattr(runner, "_finalise_completed_run", _finalise)
    return seen


async def test_a_run_on_a_stopped_engine_asks_no_pair(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], ends: dict[str, list[Any]]
) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: _snapshot())
    _engines(monkeypatch, {SID: [False] * 3})
    pairs: list[Any] = []

    async def _pair(**kw: Any) -> None:
        pairs.append(kw)

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair)

    await runner.execute_run(run_id)

    assert pairs == []
    assert len(ends["failed"]) == 1
    assert "did not answer" in ends["failed"][0]
    assert ends["cancelled"] == ends["completed"] == []
    assert run_id not in runner._CANCEL_EVENTS


async def test_a_run_whose_engine_stops_mid_run_fails_rather_than_cancels(
    monkeypatch: pytest.MonkeyPatch, ends: dict[str, list[Any]]
) -> None:
    run_id = uuid.uuid4()
    snap = _snapshot(cfg=runner.CoverageConfig(engine_down_pairs=2))
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: snap)
    _engines(monkeypatch, {SID: [True]})

    async def _unreachable_pair(**_kw: Any) -> None:
        runner._count_pair_outcome(run_id, SID, snap.cfg, _connect_error())

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _unreachable_pair)

    await runner.execute_run(run_id, executor_id="zz:99:a")

    assert len(ends["failed"]) == 1
    assert "stopped answering" in ends["failed"][0]
    assert ends["cancelled"] == ends["completed"] == []
    assert run_id not in runner._ENGINE_DOWN
    assert run_id not in runner._UNREACHABLE_PAIRS


async def test_a_run_whose_engine_answers_completes(
    monkeypatch: pytest.MonkeyPatch, ends: dict[str, list[Any]]
) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setattr(runner, "_phase1_snapshot_and_start", lambda _rid: _snapshot())
    _engines(monkeypatch, {SID: [True]})

    async def _pair(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(runner, "_process_pair_with_cancel", _pair)

    await runner.execute_run(run_id)

    assert ends["completed"] == [run_id]
    assert ends["failed"] == ends["cancelled"] == []


# ─────────────────────────── the setting ───────────────────────────


def test_the_setting_defaults_to_ten_pairs_and_zero_turns_it_off() -> None:
    assert default_for("COVERAGE_ENGINE_DOWN_PAIRS") == 10
    assert CONFIG_SCHEMA["COVERAGE_ENGINE_DOWN_PAIRS"]["min"] == 0
    assert coerce("COVERAGE_ENGINE_DOWN_PAIRS", "0") == 0
    assert runner.CoverageConfig().engine_down_pairs == 10


def test_the_setting_is_read_into_the_run_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner.config_service, "get_all", lambda _db: {"COVERAGE_ENGINE_DOWN_PAIRS": 25}
    )
    assert runner._load_coverage_config(None).engine_down_pairs == 25  # type: ignore[arg-type]


def test_a_run_frozen_before_the_setting_takes_the_default_on_a_resume() -> None:
    cfg = runner._config_from_snapshot({"pair_parallelism": 4})
    assert cfg is not None
    assert cfg.engine_down_pairs == 10
