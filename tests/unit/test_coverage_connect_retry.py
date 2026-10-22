"""Unit tests for `_call_with_connect_retry` — the 2026-07-01 eu19 fix.

Before this, a MOTIS/OTP session restarting mid-run (autoheal or
otherwise) caused every pair scheduled during its 90-180s cold-boot
window to fail *instantly* with `httpx.ConnectError` and get persisted
as a wrong 'error' cell. `_call_with_connect_retry` retries on
connection-level errors only (ConnectError, RemoteProtocolError and,
since #388, ConnectTimeout), with growing backoff, giving the session a
real chance to come back before the pair is given up on.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.network_coverage import runner
from app.network_coverage.hubs import Hub


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The real backoff delays are 5s/15s/40s — tests must not actually
    wait that long. Patch runner's asyncio.sleep and record calls so
    tests can also assert the backoff schedule was honoured."""
    calls: list[float] = []

    async def _fake_sleep(delay_s: float) -> None:
        calls.append(delay_s)

    monkeypatch.setattr(runner.asyncio, "sleep", _fake_sleep)
    return calls


# The backoff schedule, pinned here on purpose: 5 s, 15 s, 40 s, each with
# +-20 % jitter (#388 review). The ranges do not overlap, so a changed or
# reordered delay fails the check.
_SCHEDULE_S = (5.0, 15.0, 40.0)


def _assert_backoff(calls: list[float], n: int) -> None:
    """`calls` are the first `n` backoff delays, each within +-20 %."""
    assert len(calls) == n
    for got, base in zip(calls, _SCHEDULE_S[:n], strict=True):
        assert base * 0.8 <= got <= base * 1.2, (got, base)


@pytest.mark.asyncio
async def test_succeeds_immediately_without_retry_on_happy_path():
    calls = 0

    async def _fetch():
        nonlocal calls
        calls += 1
        return "ok"

    result = await runner._call_with_connect_retry(_fetch)

    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_on_connect_error_then_succeeds(_no_real_sleep):
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("All connection attempts failed")
        return "recovered"

    result = await runner._call_with_connect_retry(_fetch)

    assert result == "recovered"
    assert attempts == 3
    # Two failures → two backoff sleeps, matching the first two entries
    # of the configured delay schedule.
    _assert_backoff(_no_real_sleep, 2)


@pytest.mark.asyncio
async def test_gives_up_and_raises_after_exhausting_all_retries(_no_real_sleep):
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("All connection attempts failed")

    with pytest.raises(httpx.ConnectError):
        await runner._call_with_connect_retry(_fetch)

    # len(delays) retries + the final attempt after the last sleep.
    assert attempts == len(runner._CONNECT_RETRY_DELAYS_S) + 1
    _assert_backoff(_no_real_sleep, 3)


@pytest.mark.asyncio
async def test_retries_on_remote_protocol_error_then_succeeds(_no_real_sleep):
    """2026-07-02 — a session bounce lands mid-request instead of before
    it: MOTIS/OTP accepts the TCP connection then drops it without
    replying, which httpx surfaces as RemoteProtocolError rather than
    ConnectError. Same retry treatment applies."""
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return "recovered"

    result = await runner._call_with_connect_retry(_fetch)

    assert result == "recovered"
    assert attempts == 2
    _assert_backoff(_no_real_sleep, 1)


@pytest.mark.asyncio
async def test_does_not_retry_non_connect_errors(_no_real_sleep):
    """A timeout, a bad response, or any other failure isn't caused by a
    session bounce — retrying wouldn't change the outcome, it would just
    make a genuinely-broken pair take longer to report as such."""
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise httpx.TimeoutException("timed out")

    with pytest.raises(httpx.TimeoutException):
        await runner._call_with_connect_retry(_fetch)

    assert attempts == 1
    assert _no_real_sleep == []


@pytest.mark.parametrize(
    "exc_type",
    [httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
    ids=["read-timeout", "write-timeout", "pool-timeout"],
)
@pytest.mark.asyncio
async def test_does_not_retry_read_write_or_pool_timeouts(_no_real_sleep, exc_type):
    """#388 — only the connect timeout joined the retried errors. A read
    timeout is the pair's own answer (the engine took the request and is
    too slow), a write timeout means the engine stopped reading, and a
    pool timeout would be contention inside VIATOR: none is retried."""
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise exc_type("zz")

    with pytest.raises(exc_type):
        await runner._call_with_connect_retry(_fetch)

    assert attempts == 1
    assert _no_real_sleep == []


@pytest.mark.asyncio
async def test_cancellation_during_backoff_sleep_propagates(monkeypatch):
    """A task cancellation (e.g. web shutdown) while a pair waits in the
    backoff must stop the pair at once: the retry must neither swallow the CancelledError nor
    try the engine again. Its own `asyncio.sleep` stub replaces the
    autouse one."""
    attempts = 0
    sleeping = asyncio.Event()
    never = asyncio.Event()
    delays: list[float] = []

    async def _blocking_sleep(delay_s: float) -> None:
        delays.append(delay_s)
        sleeping.set()
        await never.wait()

    monkeypatch.setattr(runner.asyncio, "sleep", _blocking_sleep)

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("zz")

    task = asyncio.ensure_future(runner._call_with_connect_retry(_fetch))
    # Bounded, like the wait below: if the ConnectTimeout were not retried
    # the task would end without sleeping, and this test must fail, not hang.
    await asyncio.wait_for(sleeping.wait(), timeout=5)
    task.cancel()
    # Bounded wait: a retry that swallowed the cancellation would block in
    # its next backoff sleep, and this test must fail, not hang. Releasing
    # the stub afterwards lets such a task run to its end at teardown.
    try:
        done, _pending = await asyncio.wait({task}, timeout=5)
    finally:
        never.set()

    assert task in done
    assert task.cancelled()
    assert attempts == 1
    _assert_backoff(delays, 1)


@pytest.mark.asyncio
async def test_cancellation_during_the_engine_call_propagates(_no_real_sleep):
    """A task cancellation (e.g. web shutdown) while the engine call is in
    flight is not a connection error: the task is cancelled, the engine
    was called once and no backoff sleep happens."""
    attempts = 0
    calling = asyncio.Event()
    release = asyncio.Event()

    async def _fetch():
        nonlocal attempts
        attempts += 1
        calling.set()
        await release.wait()
        return "late"

    task = asyncio.ensure_future(runner._call_with_connect_retry(_fetch))
    await asyncio.wait_for(calling.wait(), timeout=5)
    task.cancel()
    # Bounded: a retry that swallowed the cancellation would call the
    # engine again and wait there; releasing it lets that task end.
    try:
        done, _pending = await asyncio.wait({task}, timeout=5)
    finally:
        release.set()

    assert task in done
    assert task.cancelled()
    assert attempts == 1
    assert _no_real_sleep == []


@pytest.mark.asyncio
async def test_backoff_delays_are_jittered_within_twenty_percent(monkeypatch, _no_real_sleep):
    """The jitter draws from the module's PRNG, between 0.8 and 1.2 of
    each delay; pinned at both ends."""
    draws = iter([0.8, 1.2, 1.0])
    seen: list[tuple[float, float]] = []

    def _uniform(low: float, high: float) -> float:
        seen.append((low, high))
        return next(draws)

    monkeypatch.setattr(runner._BACKOFF_RANDOM, "uniform", _uniform)

    async def _fetch():
        raise httpx.ConnectError("zz")

    with pytest.raises(httpx.ConnectError):
        await runner._call_with_connect_retry(_fetch)

    assert seen == [(0.8, 1.2)] * 3
    assert _no_real_sleep == pytest.approx([4.0, 18.0, 40.0])


@pytest.mark.asyncio
async def test_a_stopped_run_skips_the_backoff_and_the_retry(monkeypatch, _no_real_sleep):
    """Stop pressed before the first failure: the connection error is
    raised at once, without sleeping or calling the engine again."""
    run_id = uuid.uuid4()
    monkeypatch.setattr(runner, "is_cancelled", lambda rid: rid == run_id)
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("zz")

    with pytest.raises(httpx.ConnectTimeout):
        await runner._call_with_connect_retry(_fetch, run_id)

    assert attempts == 1
    assert _no_real_sleep == []


@pytest.mark.asyncio
async def test_a_run_stopped_during_the_backoff_is_not_retried(monkeypatch):
    """Stop pressed while the pair waits in the backoff: after the sleep
    the engine is not called again."""
    run_id = uuid.uuid4()
    stopped = False
    monkeypatch.setattr(runner, "is_cancelled", lambda rid: stopped and rid == run_id)

    async def _sleep_then_stop(_delay_s: float) -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(runner.asyncio, "sleep", _sleep_then_stop)
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("zz")

    with pytest.raises(httpx.ConnectError):
        await runner._call_with_connect_retry(_fetch, run_id)

    assert attempts == 1


@pytest.mark.asyncio
async def test_another_runs_stop_does_not_cut_the_retry(monkeypatch, _no_real_sleep):
    other = uuid.uuid4()
    monkeypatch.setattr(runner, "is_cancelled", lambda rid: rid == other)
    attempts = 0

    async def _fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("zz")
        return "ok"

    assert await runner._call_with_connect_retry(_fetch, uuid.uuid4()) == "ok"
    assert attempts == 2


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        ([httpx.ConnectTimeout("a"), httpx.ConnectError("b")], 0),
        ([httpx.ConnectTimeout("a"), TimeoutError()], 1),
        ([httpx.ConnectError("a"), httpx.ReadTimeout("b"), TimeoutError()], 1),
        ([TimeoutError(), httpx.ConnectTimeout("b")], 0),
    ],
    ids=["all-connect", "connect-then-outer-timeout", "first-non-connect", "non-connect-first"],
)
def test_slot_failure_reports_a_connection_error_only_when_every_slot_had_one(failures, expected):
    assert runner._slot_failure(failures) is failures[expected]


# ─────────────────────── wiring into the pair-execution call sites ───────────────────────
#
# The two production call sites (`_execute_pair`'s single-session path and
# `_query_one_session_for_pair`'s fanout path) were never exercised by any
# existing test — those were always monkeypatched away wholesale by callers
# testing the layer above. Moving their fetch logic into a `_fetch` closure
# introduced genuinely new lines with zero coverage; these tests close that
# gap by calling the real functions with the network/DB boundaries stubbed.


def _origin_dest():
    origin = Hub(id="o", name="Origin", short="O", region="", lat=0.0, lon=0.0)
    dest = Hub(id="d", name="Dest", short="D", region="", lat=1.0, lon=1.0)
    return origin, dest


def _resolved_window():
    return runner.ResolvedWindow(
        start_utc=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        tz_name="UTC",
    )


class _NoopFakeDb:
    """Minimal SessionLocal stand-in for `_execute_pair`'s recorder block —
    `db.get` returns None so the `session_row is not None` branch is
    skipped, keeping the fixture focused on the _fetch closure under test."""

    def get(self, *_a, **_kw):
        return None

    def commit(self):
        pass

    def rollback(self):
        pass


class _NoopFakeSessionLocal:
    def __call__(self):
        return self

    def __enter__(self):
        return _NoopFakeDb()

    def __exit__(self, *_exc):
        return False


def _patch_execute_pair_db_layer(monkeypatch):
    monkeypatch.setattr(runner, "SessionLocal", _NoopFakeSessionLocal())
    monkeypatch.setattr(
        runner.recorder, "begin_search", lambda *_a, **_kw: SimpleNamespace(id=uuid.uuid4())
    )
    monkeypatch.setattr(runner.recorder, "record_execution", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner.recorder, "finish_search", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner, "_persist_pair_coverage_result", MagicMock())


@pytest.mark.asyncio
async def test_execute_pair_sliced_path_routes_through_fetch_plan_sliced(monkeypatch):
    """The runner always supplies `window` in production — `_execute_pair`'s
    _fetch closure must dispatch to `_fetch_plan_sliced` (K-slot fan-out),
    not the legacy single-call path."""
    captured: dict = {}

    async def _fake_sliced(**kwargs):
        captured.update(kwargs)
        return {}, [{"duration_seconds": 100, "num_transfers": 0, "legs": []}]

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    _patch_execute_pair_db_layer(monkeypatch)
    origin, dest = _origin_dest()

    await runner._execute_pair(
        run_id=uuid.uuid4(),
        session_id="sess-1",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        window=_resolved_window(),
    )

    assert captured["session_id"] == "sess-1"
    assert captured["engine"] == "motis"
    runner._persist_pair_coverage_result.assert_called_once()
    assert runner._persist_pair_coverage_result.call_args.kwargs["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_pair_legacy_path_routes_through_planner_dispatch(monkeypatch):
    """window=None (test-only path — see docstring) must still dispatch to
    `planner_dispatch.planner_for_engine(...).fetch_plan`, matching the
    pre-refactor behaviour."""
    captured: dict = {}

    class _FakePlanner:
        async def fetch_plan(self, **kwargs):
            captured.update(kwargs)
            return {}, []

    monkeypatch.setattr(
        runner.planner_dispatch, "planner_for_engine", lambda _engine: _FakePlanner()
    )
    _patch_execute_pair_db_layer(monkeypatch)
    origin, dest = _origin_dest()

    await runner._execute_pair(
        run_id=uuid.uuid4(),
        session_id="sess-1",
        engine="otp",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        window=None,
    )

    assert captured["session_id"] == "sess-1"
    runner._persist_pair_coverage_result.assert_called_once()
    assert runner._persist_pair_coverage_result.call_args.kwargs["status"] == "no_route"


@pytest.mark.asyncio
async def test_query_one_session_sliced_path_routes_through_fetch_plan_sliced(monkeypatch):
    captured: dict = {}

    async def _fake_sliced(**kwargs):
        captured.update(kwargs)
        return {}, [{"duration_seconds": 200, "num_transfers": 1, "legs": []}]

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    origin, dest = _origin_dest()

    sid, status, _raw, trips, _ms = await runner._query_one_session_for_pair(
        sid="sess-a",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        run_id=uuid.uuid4(),
        window=_resolved_window(),
    )

    assert captured["session_id"] == "sess-a"
    assert sid == "sess-a"
    assert status == "ok"
    assert len(trips) == 1


@pytest.mark.asyncio
async def test_query_one_session_legacy_path_routes_through_planner_dispatch(monkeypatch):
    captured: dict = {}

    class _FakePlanner:
        async def fetch_plan(self, **kwargs):
            captured.update(kwargs)
            return {}, []

    monkeypatch.setattr(
        runner.planner_dispatch, "planner_for_engine", lambda _engine: _FakePlanner()
    )
    origin, dest = _origin_dest()

    sid, status, _raw, trips, _ms = await runner._query_one_session_for_pair(
        sid="sess-a",
        engine="otp",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        run_id=uuid.uuid4(),
        window=None,
    )

    assert captured["session_id"] == "sess-a"
    assert sid == "sess-a"
    assert status == "no_route"
    assert trips == []


@pytest.mark.asyncio
async def test_query_one_session_retries_connect_error_via_shared_helper(
    monkeypatch, _no_real_sleep
):
    """Confirms the fanout path is wired to the same retry helper as the
    single-session path, not a separate ad-hoc copy."""
    attempts = 0

    async def _fake_sliced(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.ConnectError("All connection attempts failed")
        return {}, [{"duration_seconds": 50, "num_transfers": 0, "legs": []}]

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    origin, dest = _origin_dest()

    _sid, status, _raw, _trips, _ms = await runner._query_one_session_for_pair(
        sid="sess-a",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        run_id=uuid.uuid4(),
        window=_resolved_window(),
    )

    assert attempts == 2
    assert status == "ok"
    _assert_backoff(_no_real_sleep, 1)


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (httpx.ReadTimeout("zz"), "timeout"),
        (TimeoutError(), "timeout"),
        (httpx.ConnectTimeout("zz"), "timeout"),
        (httpx.ConnectError("zz"), "error"),
        (ValueError("zz"), "error"),
    ],
    ids=["httpx-read-timeout", "builtin-timeout", "connect-timeout", "connect-error", "other"],
)
def test_failure_status_names_timeouts_apart(exc: BaseException, status: str) -> None:
    assert runner._failure_status(exc) == status


def test_elapsed_ms_counts_whole_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.time, "monotonic", lambda: 12.3456)
    assert runner._elapsed_ms(10.0) == 2345


# ─────────────────────── connect timeout (#388) through `_execute_pair` ───────────────────────


def _zz_hubs():
    origin = Hub(id="ZZ-A", name="Zzstadt", short="ZZA", region="", lat=0.0, lon=0.0)
    dest = Hub(id="ZZ-B", name="Zzdorf", short="ZZB", region="", lat=1.0, lon=1.0)
    return origin, dest


async def _execute_zz_pair():
    origin, dest = _zz_hubs()
    await runner._execute_pair(
        run_id=uuid.uuid4(),
        session_id="zz-sess",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        window=_resolved_window(),
    )
    return runner._persist_pair_coverage_result.call_args.kwargs


@pytest.mark.asyncio
async def test_execute_pair_connect_timeout_then_success_gives_ok_cell(monkeypatch, _no_real_sleep):
    """The 22 Oct case: the engine does not accept the connection in time
    once, then answers. The cell must be `ok`, not a failure."""
    attempts = 0

    async def _fake_sliced(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("zz")
        return {}, [{"duration_seconds": 100, "num_transfers": 0, "legs": []}]

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    _patch_execute_pair_db_layer(monkeypatch)

    persisted = await _execute_zz_pair()

    assert attempts == 2
    assert persisted["status"] == "ok"
    assert persisted["error_message"] is None
    _assert_backoff(_no_real_sleep, 1)


@pytest.mark.asyncio
async def test_execute_pair_repeated_connect_timeouts_fail_after_last_retry(
    monkeypatch, _no_real_sleep
):
    """An engine that never accepts the connection: every backoff delay is
    waited, then the cell fails. `_failure_status` still files it under
    `timeout` (the class name says so), with the ConnectTimeout named in
    the message."""
    attempts = 0

    async def _fake_sliced(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("zz")

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    _patch_execute_pair_db_layer(monkeypatch)

    persisted = await _execute_zz_pair()

    assert attempts == len(runner._CONNECT_RETRY_DELAYS_S) + 1
    _assert_backoff(_no_real_sleep, 3)
    assert persisted["status"] == "timeout"
    assert persisted["error_message"].startswith("ConnectTimeout: ")


@pytest.mark.asyncio
async def test_execute_pair_read_timeout_is_not_retried(monkeypatch, _no_real_sleep):
    attempts = 0

    async def _fake_sliced(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("zz")

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    _patch_execute_pair_db_layer(monkeypatch)

    persisted = await _execute_zz_pair()

    assert attempts == 1
    assert _no_real_sleep == []
    assert persisted["status"] == "timeout"
    assert persisted["error_message"].startswith("ReadTimeout: ")


# ─────────────────────── real K-slot fetch (#388 review) ───────────────────────
#
# These run the real `_fetch_plan_sliced` and stub only the engine, so the
# slot merge decides whether the whole pair is retried.

_WINDOW_START = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


class _SlotPlanner:
    """Fake engine: slot 0 (the window start) does `first`, every other
    slot does `others` ("connect": ConnectTimeout, "hang": never answers,
    so the slot's outer wait_for fires, "empty": answers no trip)."""

    def __init__(self, first: str, others: str) -> None:
        self.first = first
        self.others = others
        self.calls = 0

    async def fetch_plan(self, *, when, **_kwargs):
        self.calls += 1
        mode = self.first if when == _WINDOW_START else self.others
        if mode == "connect":
            raise httpx.ConnectTimeout("zz")
        if mode == "hang":
            await asyncio.Event().wait()
        return {"zz": True}, []


async def _execute_sliced_zz_pair(monkeypatch, planner: _SlotPlanner):
    monkeypatch.setattr(runner.planner_dispatch, "planner_for_engine", lambda _engine: planner)
    _patch_execute_pair_db_layer(monkeypatch)
    origin, dest = _zz_hubs()
    await runner._execute_pair(
        run_id=uuid.uuid4(),
        session_id="zz-sess",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=_WINDOW_START,
        cfg=runner.CoverageConfig(slot_count=6, within_pair_parallelism=6, slot_timeout_ms=50),
        window=_resolved_window(),
    )
    return runner._persist_pair_coverage_result.call_args.kwargs


@pytest.mark.asyncio
async def test_sliced_pair_with_every_slot_failing_to_connect_is_retried(
    monkeypatch, _no_real_sleep
):
    planner = _SlotPlanner(first="connect", others="connect")

    persisted = await _execute_sliced_zz_pair(monkeypatch, planner)

    assert planner.calls == 6 * 4  # every slot, on each of the 4 attempts
    _assert_backoff(_no_real_sleep, 3)
    assert persisted["status"] == "timeout"
    assert persisted["error_message"].startswith("ConnectTimeout: ")


@pytest.mark.asyncio
async def test_sliced_pair_with_connect_timeout_and_slot_timeouts_is_not_retried(
    monkeypatch, _no_real_sleep
):
    """Slot 0 cannot connect, the others time out: the engine took those
    requests, so the pair's failure is its own and is not retried, whatever
    the slot order (#388 review)."""
    planner = _SlotPlanner(first="connect", others="hang")

    persisted = await _execute_sliced_zz_pair(monkeypatch, planner)

    assert planner.calls == 6
    assert _no_real_sleep == []
    assert persisted["status"] == "timeout"
    assert persisted["error_message"].startswith("TimeoutError")


@pytest.mark.asyncio
async def test_sliced_pair_with_one_slot_failing_to_connect_keeps_the_partial_result(
    monkeypatch, _no_real_sleep
):
    """Slot 0 cannot connect, the others answer: partial result as before,
    no retry (a per-slot retry is a follow-up)."""
    planner = _SlotPlanner(first="connect", others="empty")

    persisted = await _execute_sliced_zz_pair(monkeypatch, planner)

    assert planner.calls == 6
    assert _no_real_sleep == []
    assert persisted["status"] == "no_route"
    assert persisted["error_message"] is None


# ─────────────────────── OTP phase timeouts (#388 review) ───────────────────────


@pytest.mark.parametrize(
    ("timeout_ms", "read_s", "short_s"),
    [(60_000, 60.0, 5.0), (20_000, 20.0, 5.0), (3_000, 3.0, 3.0), (200, 1.0, 1.0)],
    ids=["coverage-legacy", "coverage-slot", "short-live", "below-floor"],
)
def test_otp_plan_timeout_shortens_connect_write_pool_only(timeout_ms, read_s, short_s):
    from app.journey import otp_client

    t = otp_client._plan_timeout(timeout_ms)

    assert (t.connect, t.read, t.write, t.pool) == (short_s, read_s, short_s, short_s)


@pytest.mark.asyncio
async def test_otp_fetch_plan_uses_the_phase_timeouts(monkeypatch):
    """The plan call itself builds its client with those timeouts."""
    from app.journey import otp_client

    real_client = httpx.AsyncClient
    seen: list[httpx.Timeout] = []

    def _client(*, timeout, **kwargs):
        seen.append(timeout)
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": {"planConnection": {"edges": []}}})
        )
        return real_client(timeout=timeout, transport=transport, **kwargs)

    monkeypatch.setattr(otp_client.httpx, "AsyncClient", _client)
    monkeypatch.setattr(otp_client, "_otp_base", lambda _sid: "http://zz-otp.invalid")

    _raw, trips = await otp_client.fetch_plan(
        session_id="zz-sess",
        from_lat=0.0,
        from_lon=0.0,
        to_lat=1.0,
        to_lon=1.0,
        when=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        timeout_ms=20_000,
    )

    assert trips == []
    assert seen == [httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)]


@pytest.mark.asyncio
async def test_both_call_sites_pass_the_run_id_to_the_retry(monkeypatch, _no_real_sleep):
    """A stopped run cuts the retry in the single-session and the fanout
    path alike, so both hand their run id to the retry helper."""
    monkeypatch.setattr(runner, "is_cancelled", lambda _rid: True)
    attempts = 0

    async def _fake_sliced(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("zz")

    monkeypatch.setattr(runner, "_fetch_plan_sliced", _fake_sliced)
    _patch_execute_pair_db_layer(monkeypatch)
    origin, dest = _zz_hubs()

    persisted = await _execute_zz_pair()
    _sid, status, _raw, _trips, _ms = await runner._query_one_session_for_pair(
        sid="zz-sess",
        engine="motis",
        origin=origin,
        dest=dest,
        depart_at=_WINDOW_START,
        run_id=uuid.uuid4(),
        window=_resolved_window(),
    )

    assert attempts == 2
    assert _no_real_sleep == []
    assert persisted["status"] == "timeout"
    assert status == "timeout"
