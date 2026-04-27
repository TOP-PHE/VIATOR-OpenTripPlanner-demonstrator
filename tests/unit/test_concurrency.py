"""ConcurrencyGate behaviour: admit-or-reject + atomic counting + hot-swap."""

from __future__ import annotations

import pytest

from app.concurrency import ConcurrencyExceeded, ConcurrencyGate, semaphores


@pytest.mark.asyncio
async def test_admits_within_limit() -> None:
    gate = ConcurrencyGate("test", limit=2)
    # Nesting is intentional — we want to assert the counter at the inner level.
    async with gate.acquire_or_fail():  # noqa: SIM117
        async with gate.acquire_or_fail():
            assert gate.in_flight == 2


@pytest.mark.asyncio
async def test_rejects_when_full() -> None:
    gate = ConcurrencyGate("test", limit=1)
    async with gate.acquire_or_fail():
        with pytest.raises(ConcurrencyExceeded) as exc:
            async with gate.acquire_or_fail():
                pass
        assert exc.value.gate == "test"
        assert exc.value.limit == 1
    # After release, a new acquire succeeds.
    async with gate.acquire_or_fail():
        assert gate.in_flight == 1


@pytest.mark.asyncio
async def test_releases_on_exception() -> None:
    gate = ConcurrencyGate("test", limit=1)
    with pytest.raises(RuntimeError, match="boom"):
        async with gate.acquire_or_fail():
            raise RuntimeError("boom")
    assert gate.in_flight == 0
    # Limit was correctly released — next acquire works.
    async with gate.acquire_or_fail():
        assert gate.in_flight == 1


@pytest.mark.asyncio
async def test_set_limit_hot_swap_does_not_evict() -> None:
    """Reducing the limit while requests are in flight does not evict them."""
    gate = ConcurrencyGate("test", limit=3)
    # Nesting is intentional — we hot-swap the limit while three are in flight.
    async with gate.acquire_or_fail():  # noqa: SIM117
        async with gate.acquire_or_fail():
            async with gate.acquire_or_fail():
                assert gate.in_flight == 3
                gate.set_limit(1)  # in-flight requests stay
                assert gate.in_flight == 3
                # But new ones are rejected immediately.
                with pytest.raises(ConcurrencyExceeded):
                    async with gate.acquire_or_fail():
                        pass


def test_set_limit_rejects_zero() -> None:
    gate = ConcurrencyGate("test", limit=1)
    with pytest.raises(ValueError, match=">= 1"):
        gate.set_limit(0)


def test_reload_from_config_updates_all_gates() -> None:
    cfg = {
        "MAX_CONCURRENT_JOURNEYS": 50,
        "MAX_CONCURRENT_UPLOADS": 7,
        "MAX_CONCURRENT_REBUILDS": 2,
    }
    semaphores.reload_from_config(cfg)
    assert semaphores.journey.limit == 50
    assert semaphores.upload.limit == 7
    assert semaphores.rebuild.limit == 2
    assert semaphores.initialised
