"""#392 — the health endpoints answer even when every request thread is busy,
and a run ended by a web restart is named in the web log at startup.

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
import uuid
from typing import Any

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


# ───────────────────────────── orphaned runs at startup ─────────────────────────────


class _Log:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.exceptions: list[str] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))

    def exception(self, event: str, **_fields: Any) -> None:
        self.exceptions.append(event)


class _Db:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> _Db:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


def _patch(
    monkeypatch: pytest.MonkeyPatch, run_ids: list[uuid.UUID] | Exception
) -> tuple[_Log, _Db]:
    from app.network_coverage import runner

    log, db = _Log(), _Db()

    def _mark(_db: Any) -> list[uuid.UUID]:
        if isinstance(run_ids, Exception):
            raise run_ids
        return run_ids

    monkeypatch.setattr(main, "log", log)
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(runner, "mark_orphaned_runs_as_failed", _mark)
    return log, db


def test_startup_names_the_orphaned_runs_with_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [uuid.UUID(int=99), uuid.UUID(int=199)]
    log, db = _patch(monkeypatch, ids)

    main._fail_orphaned_coverage_runs()

    ((event, fields),) = log.warnings
    assert event == "network_coverage.orphans_marked_failed"
    assert fields["count"] == 2
    assert fields["run_ids"] == [str(i) for i in ids]
    for cause in ("deploy", "autoheal", "manual restart"):
        assert cause in fields["hint"]
    assert db.commits == 1


def test_startup_is_quiet_without_orphaned_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    log, db = _patch(monkeypatch, [])

    main._fail_orphaned_coverage_runs()

    assert log.warnings == []
    assert db.commits == 0


def test_startup_survives_a_failed_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    log, db = _patch(monkeypatch, RuntimeError("zz-99"))

    main._fail_orphaned_coverage_runs()

    assert log.exceptions == ["network_coverage.orphan_cleanup_failed"]
    assert log.warnings == []
    assert db.commits == 0
