"""#397 — web's event-loop lag gauge and the shared MOTIS client.

`metrics.LoopLagMonitor` keeps `viator_web_event_loop_lag_seconds` at the
largest lag of the last minute (alert: docker/prometheus/rules/web.yml);
`motis_client` shares one httpx client per event loop instead of building
one (and its SSL context) per call. No database, no network: the MOTIS
calls go to an httpx.MockTransport. Invented values only (numbers with 99).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry, Gauge

from app import metrics
from app.journey import motis_client


def _gauge() -> Gauge:
    return Gauge("zz_loop_lag_seconds", "zz", registry=CollectorRegistry())


def _value(gauge: Gauge) -> float:
    return float(gauge._value.get())


# ───────────────────────────── the lag gauge ─────────────────────────────


def test_the_gauge_holds_the_largest_lag_of_the_window() -> None:
    gauge = _gauge()
    monitor = metrics.LoopLagMonitor(gauge, window_s=60.0)
    assert monitor.record(1000.0, 0.01) == pytest.approx(0.01)
    assert monitor.record(1010.0, 2.99) == pytest.approx(2.99)
    # The stall is still in the window.
    assert monitor.record(1050.0, 0.02) == pytest.approx(2.99)
    assert _value(gauge) == pytest.approx(2.99)
    # 60.5 s later it has left.
    assert monitor.record(1070.5, 0.03) == pytest.approx(0.03)
    assert _value(gauge) == pytest.approx(0.03)


def test_the_web_gauge_is_the_one_the_alert_reads() -> None:
    assert metrics.WEB_EVENT_LOOP_LAG_SECONDS._name == "viator_web_event_loop_lag_seconds"
    assert metrics.LOOP_LAG_MONITOR.gauge is metrics.WEB_EVENT_LOOP_LAG_SECONDS
    assert metrics.LOOP_LAG_WINDOW_S >= 60  # one rule evaluation (1 min) at least


async def test_a_blocked_loop_shows_in_the_gauge() -> None:
    gauge = _gauge()
    monitor = metrics.LoopLagMonitor(gauge, interval_s=0.01, window_s=60.0)
    monitor.start()
    try:
        await asyncio.sleep(0.05)
        assert _value(gauge) < 0.2
        time.sleep(0.3)  # blocking work on the loop, as a synchronous query would
        await asyncio.sleep(0.05)
        assert _value(gauge) >= 0.25
    finally:
        await monitor.stop()
    assert monitor._task is None


async def test_start_is_idempotent_and_stop_without_start_is_harmless() -> None:
    monitor = metrics.LoopLagMonitor(_gauge(), interval_s=0.01)
    await monitor.stop()
    monitor.start()
    task = monitor._task
    monitor.start()
    assert monitor._task is task
    await monitor.stop()
    assert task is not None and task.cancelled()


def test_a_task_left_on_another_loop_is_replaced_not_touched() -> None:
    """An app started again on a new loop without its shutdown (tests do
    that) gets a monitor on the new loop; stop() never awaits the old one."""
    monitor = metrics.LoopLagMonitor(_gauge(), interval_s=0.01)

    async def _start_only() -> Any:
        monitor.start()
        await asyncio.sleep(0.02)
        return monitor._task

    first = asyncio.run(_start_only())

    async def _start_and_stop() -> Any:
        monitor.start()
        second = monitor._task
        await monitor.stop()
        return second

    second = asyncio.run(_start_and_stop())
    assert second is not first
    assert second.cancelled()
    assert monitor._task is None


def test_web_starts_the_monitor_and_stops_it_at_shutdown(monkeypatch) -> None:
    """The app's startup handler starts the monitor on web's loop; shutdown
    stops it and closes the loop's MOTIS client."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import main

    monitor = metrics.LoopLagMonitor(_gauge(), interval_s=0.01)
    monkeypatch.setattr(metrics, "LOOP_LAG_MONITOR", monitor)
    closed: list[bool] = []

    async def _close() -> None:
        await asyncio.sleep(0)
        closed.append(True)

    monkeypatch.setattr(motis_client, "aclose_shared_client", _close)
    probe = FastAPI()
    probe.router.on_startup.append(main._start_loop_lag_monitor)
    probe.router.on_shutdown.append(main._shutdown)
    seen: dict[str, Any] = {}

    @probe.get("/zz")
    async def _zz() -> dict[str, bool]:
        await asyncio.sleep(0)
        seen["running"] = monitor._task is not None and not monitor._task.done()
        return {"ok": True}

    with TestClient(probe) as client:
        assert client.get("/zz").status_code == 200
    assert seen["running"] is True
    assert monitor._task is None
    assert closed == [True]


# ─────────────────────────── the shared MOTIS client ───────────────────────────


def _install(monkeypatch, handler) -> list[Any]:
    """Every httpx.AsyncClient motis_client builds goes to a MockTransport;
    returns the list of the clients built."""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient
    built: list[Any] = []

    def factory(*args: Any, **kwargs: Any) -> Any:
        kwargs["transport"] = transport
        client = real_cls(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(motis_client.httpx, "AsyncClient", factory)
    return built


async def _plan(timeout_ms: int = 9900) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return await motis_client.fetch_plan(
        session_id="zz-99",
        from_lat=45.99,
        from_lon=9.99,
        to_lat=46.99,
        to_lon=10.99,
        when=datetime(2099, 9, 9, 9, 0, tzinfo=UTC),
        timeout_ms=timeout_ms,
        base_url="http://zz.invalid:8080",
    )


async def test_one_client_per_loop_for_every_call(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"itineraries": []})

    built = _install(monkeypatch, handler)
    try:
        for _ in range(3):
            assert await _plan() == ({"itineraries": []}, [])
        assert len(built) == 1
        assert len(seen) == 3
        # PR-188 still holds: every request says Connection: close.
        assert {r.headers["connection"].lower() for r in seen} == {"close"}
    finally:
        await motis_client.aclose_shared_client()
    assert built[0].is_closed


async def test_the_per_request_timeouts_are_unchanged(monkeypatch) -> None:
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"itineraries": []})

    _install(monkeypatch, handler)
    try:
        await _plan(timeout_ms=9900)
        await _plan(timeout_ms=1990)
    finally:
        await motis_client.aclose_shared_client()
    assert timeouts == [
        {"connect": 5.0, "read": 9.9, "write": 5.0, "pool": 5.0},
        {"connect": 5.0, "read": 1.99, "write": 5.0, "pool": 5.0},
    ]


async def test_the_client_caps_no_connections_and_keeps_none_idle(monkeypatch) -> None:
    """Like the per-call clients before: no pool to wait for under a large
    fan-out (no PoolTimeout), no idle connection kept."""
    built = _install(monkeypatch, lambda _r: httpx.Response(200, json={"itineraries": []}))
    try:
        await asyncio.gather(*(_plan() for _ in range(99)))
    finally:
        await motis_client.aclose_shared_client()
    assert len(built) == 1
    assert motis_client._LIMITS.max_connections is None
    assert motis_client._LIMITS.max_keepalive_connections == 0
    transport = httpx.AsyncHTTPTransport(limits=motis_client._LIMITS)
    try:
        pool = transport._pool
        assert pool._max_connections > 10_000  # None: no cap
        assert pool._max_keepalive_connections == 0
    finally:
        await transport.aclose()


def test_each_loop_gets_its_own_client(monkeypatch) -> None:
    """An AsyncClient belongs to its loop: web's, the coverage executor's and
    a private loop each get theirs; a closed client is replaced."""
    built = _install(monkeypatch, lambda _r: httpx.Response(200, json={"itineraries": []}))

    async def _twice() -> None:
        await _plan()
        await _plan()

    asyncio.run(_twice())
    asyncio.run(_twice())
    assert len(built) == 2

    async def _closed_then_again() -> None:
        await _plan()
        await motis_client.aclose_shared_client()
        await _plan()
        await motis_client.aclose_shared_client()

    asyncio.run(_closed_then_again())
    assert len(built) == 4
    assert all(client.is_closed for client in built[2:])


async def test_an_http_error_still_raises(monkeypatch) -> None:
    _install(monkeypatch, lambda _r: httpx.Response(500, json={"error": "zz"}))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await _plan()
    finally:
        await motis_client.aclose_shared_client()


async def test_the_per_request_line_is_debug(monkeypatch, caplog) -> None:
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"itineraries": []}))
    caplog.set_level(logging.DEBUG, logger=motis_client.log.name)
    try:
        await _plan()
    finally:
        await motis_client.aclose_shared_client()
    lines = [r for r in caplog.records if r.getMessage().startswith("motis.plan.request")]
    assert [r.levelno for r in lines] == [logging.DEBUG]
