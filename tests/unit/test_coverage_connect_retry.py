"""Unit tests for `_call_with_connect_retry` — the 2026-07-01 eu19 fix.

Before this, a MOTIS/OTP session restarting mid-run (autoheal or
otherwise) caused every pair scheduled during its 90-180s cold-boot
window to fail *instantly* with `httpx.ConnectError` and get persisted
as a wrong 'error' cell. `_call_with_connect_retry` retries on
ConnectError only, with growing backoff, giving the session a real
chance to come back before the pair is given up on.
"""

from __future__ import annotations

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
    assert _no_real_sleep == list(runner._CONNECT_RETRY_DELAYS_S[:2])


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
    assert _no_real_sleep == list(runner._CONNECT_RETRY_DELAYS_S)


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
    assert _no_real_sleep == [runner._CONNECT_RETRY_DELAYS_S[0]]
