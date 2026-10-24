"""#392 — the health endpoints answer even when every request thread is busy,
and web's startup no longer fails coverage runs (they execute in the worker).

`/healthz/version` is the image HEALTHCHECK (10 s timeout, 3 retries). A sync
handler runs in anyio's request thread pool (40 threads); while sync handlers
hold them all (for instance waiting for a database connection) a sync health
handler waits too. The health handlers are `async def` returning constants:
they run on the event loop and need no thread. Measured here by holding the
whole pool and asking for both endpoints.

Invented values only (ZZ, numbers with 99).
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import anyio
import anyio.to_thread
import httpx
import pytest

from app import main


@pytest.mark.parametrize("path", ["/healthz", "/healthz/version"])
def test_health_endpoints_are_async_and_depend_on_nothing(path: str) -> None:
    (route,) = [r for r in main.app.routes if getattr(r, "path", None) == path]
    assert inspect.iscoroutinefunction(route.endpoint)  # type: ignore[attr-defined]
    assert route.dependant.dependencies == []  # type: ignore[attr-defined]
    assert route.dependant.query_params == []  # type: ignore[attr-defined]


async def test_health_answers_while_every_request_thread_is_busy() -> None:
    limiter = anyio.to_thread.current_default_thread_limiter()
    saved = limiter.total_tokens
    release = threading.Event()
    limiter.total_tokens = 1
    try:
        # Hold the pool's only thread, as 40 stuck sync handlers would.
        holder = asyncio.ensure_future(anyio.to_thread.run_sync(release.wait))
        await asyncio.sleep(0.05)
        assert limiter.borrowed_tokens == 1
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://zz.invalid") as client:
            for path in ("/healthz", "/healthz/version"):
                r = await asyncio.wait_for(client.get(path), timeout=2.0)
                assert r.status_code == 200
    finally:
        release.set()
        await holder
        limiter.total_tokens = saved


# ─────────────────────── no coverage-run cleanup at startup ───────────────────────


def test_web_startup_no_longer_fails_coverage_runs() -> None:
    """#392 (durable part) — a coverage run executes in the worker's coverage
    executor, which resumes a run whose heartbeat went stale. Web's startup
    must not touch coverage runs any more: failing them there is what ended
    the runs of 21 and 22 Oct."""
    source = inspect.getsource(main._startup)
    assert "runner" not in source
    assert "orphan" not in source
    assert not hasattr(main, "_fail_orphaned_coverage_runs")
    from app.network_coverage import runner

    assert not hasattr(runner, "mark_orphaned_runs_as_failed")
