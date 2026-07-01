"""Unit tests for `_call_with_connect_retry` — the 2026-07-01 eu19 fix.

Before this, a MOTIS/OTP session restarting mid-run (autoheal or
otherwise) caused every pair scheduled during its 90-180s cold-boot
window to fail *instantly* with `httpx.ConnectError` and get persisted
as a wrong 'error' cell. `_call_with_connect_retry` retries on
ConnectError only, with growing backoff, giving the session a real
chance to come back before the pair is given up on.
"""

from __future__ import annotations

import httpx
import pytest

from app.network_coverage import runner


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
