"""Integration: the database steps #397 moved off web's event loop stay off
it, one test per step the loop-health harness does not drive, and the
held-connection limiter that keeps those steps from waiting behind the sync
request threads when the pool is full (review round 1 of #405).

Each test records, for every SQL statement, whether it ran on the event-loop
thread. A step moved back onto the loop runs its statements there, and the
test fails (checked by putting each step back on the loop, one at a time).

Real PostgreSQL, the fresh database and the fakes of
tests/integration/test_web_loop_health.py. Invented values only (ZZ ids,
numbers with 99). Skips when Postgres is down.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session

from tests.integration.test_web_loop_health import (
    _EMAIL,
    _USER,
    SIDS,
    _install_fakes,
    _postgres_or_skip,
    fresh_db,
)

__all__ = ["fresh_db"]  # the fixture, imported for the tests below

_ORIGIN, _HUB, _DEST = "9999991", "9999993", "9999992"
_JOURNEY = {
    "from": {"lat": 45.099, "lon": 9.099, "label": "ZZ A", "uic": _ORIGIN},
    "to": {"lat": 45.999, "lon": 9.999, "label": "ZZ B", "uic": _DEST},
    "depart_at": "2099-09-09T09:09:00Z",
}


class _SqlThreads:
    """Every SQL statement, sorted by the thread that ran it."""

    def __init__(self) -> None:
        self.loop_thread = threading.get_ident()
        self.on_loop: list[str] = []
        self.off_loop: list[str] = []

    def __call__(self, _conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        line = " ".join(statement.split())
        if threading.get_ident() == self.loop_thread:
            self.on_loop.append(line)
        else:
            self.off_loop.append(line)

    def ran_off_loop(self, fragment: str) -> bool:
        return any(fragment in line for line in self.off_loop)


@contextmanager
def _sql_threads() -> Iterator[_SqlThreads]:
    """Call on the event loop's thread (the test's)."""
    threads = _SqlThreads()
    event.listen(Engine, "before_cursor_execute", threads)
    try:
        yield threads
    finally:
        event.remove(Engine, "before_cursor_execute", threads)


@asynccontextmanager
async def _signed_in() -> AsyncIterator[httpx.AsyncClient]:
    """The app over ASGI, signed in as the seeded platform admin."""
    from app.auth import tokens
    from app.main import app
    from app.settings import settings

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://zz.invalid"
    ) as client:
        client.cookies.set(
            settings.jwt_cookie_name, tokens.issue_jwt(_USER, _EMAIL, "platform_admin")
        )
        yield client


def _run_sql(sql: str, **params: Any) -> None:
    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _scalar(sql: str, **params: Any) -> Any:
    from app.db import engine

    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


class _NoTrips:
    """An engine that finds nothing: the fanout falls back to the federated
    planner."""

    async def fetch_plan(self, **_kw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        await asyncio.sleep(0)
        return {"itineraries": []}, []


# ─────────────────────── the moved steps, one test each ───────────────────────


async def test_osm_refresh_finishes_off_the_loop(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`refresh_osm`'s end (the session row re-read under its lock, the
    staleness mark, the audit row, the commit) runs in a worker thread."""
    from app.api.admin import sessions

    sid = SIDS[0]
    await asyncio.to_thread(
        _run_sql,
        "UPDATE sessions SET config = CAST(:c AS jsonb) WHERE id = :s",
        c='{"sources": {"osm_pbf": "https://zz.invalid/zz-99.osm.pbf"}}',
        s=sid,
    )

    async def _fetched(_client: Any, _db: Any, _sid: str, _staging: Any, task: Any) -> Any:
        await asyncio.sleep(0)
        return {"status": "fetched", "key": task.label}

    monkeypatch.setattr(sessions, "_refresh_one_task", _fetched)
    async with _signed_in() as client:
        with _sql_threads() as sql:
            r = await client.post(f"/api/sessions/{sid}/sources/osm/refresh")
    assert r.status_code == 200, r.text
    assert [f["key"] for f in r.json()["fetched"]] == ["osm_pbf"]
    assert sql.on_loop == []
    assert sql.ran_off_loop("FOR UPDATE")  # the re-read under the row lock
    assert sql.ran_off_loop("INSERT INTO audit_events")
    audited = await asyncio.to_thread(
        _scalar,
        "SELECT count(*) FROM audit_events WHERE action = 'session.osm.refreshed' AND target_id = :s",
        s=sid,
    )
    assert audited == 1


async def test_the_federated_planner_reads_coordinates_off_the_loop(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plan_federated`'s master_stations read runs in a worker thread."""
    from app.db import SessionLocal
    from app.journey import federated_planner
    from app.models import Session as SessionRow

    monkeypatch.setitem(federated_planner._SERVED_UICS_CACHE, SIDS[0], {_ORIGIN, _HUB})
    monkeypatch.setitem(federated_planner._SERVED_UICS_CACHE, SIDS[1], {_HUB, _DEST})
    db = SessionLocal()
    try:
        rows = await asyncio.to_thread(
            lambda: list(db.scalars(select(SessionRow).where(SessionRow.id.in_(SIDS))))
        )
        with _sql_threads() as sql:
            trips = await federated_planner.plan_federated(
                db,
                origin_uic=_ORIGIN,
                dest_uic=_DEST,
                when=datetime(2099, 9, 9, 9, 9, tzinfo=UTC),
                sessions=sorted(rows, key=lambda s: s.id),
                timeout_ms=9900,
            )
    finally:
        await asyncio.to_thread(db.close)
    assert trips == []  # no master_stations row, no request position: nothing to plan
    assert sql.on_loop == []
    assert sql.ran_off_loop("FROM master_stations")


async def test_the_trainline_upsert_runs_off_the_loop(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import SessionLocal
    from app.master import trainline

    async def _csv() -> str:
        await asyncio.sleep(0)
        return (
            "id;uic;name;country;latitude;longitude\n"
            "99001;9999901;ZZ Loop Nord;ZZ;45.099;9.099\n"
            "99002;9999902;ZZ Loop Sud;ZZ;45.199;9.199\n"
        )

    monkeypatch.setattr(trainline, "fetch_csv", _csv)
    db = SessionLocal()
    try:
        with _sql_threads() as sql:
            counts = await trainline.refresh(db)
    finally:
        await asyncio.to_thread(db.close)
    assert counts["added"] == 2
    assert sql.on_loop == []
    assert sql.ran_off_loop("INSERT INTO master_stations")


async def test_the_sessions_are_reloaded_off_the_loop_before_the_federated_fallback(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fanout's commit expires the session rows; the federated fallback
    reads them (ids, time zones) only after they were loaded again in a
    worker thread, never lazily on the loop."""
    from app.journey import federated_planner, planner_dispatch

    _install_fakes(monkeypatch)
    monkeypatch.setattr(planner_dispatch, "get_planner", lambda _s: _NoTrips())
    asked: list[list[str]] = []
    real = federated_planner.plan_federated

    async def _spy(db: Any, **kw: Any) -> list[dict[str, Any]]:
        asked.append([s.id for s in kw["sessions"]])
        return await real(db, **kw)

    monkeypatch.setattr(federated_planner, "plan_federated", _spy)
    async with _signed_in() as client:
        with _sql_threads() as sql:
            r = await client.post("/api/journey/fanout", json=_JOURNEY)
    assert r.status_code == 200, r.text
    assert r.json()["trips"] == []
    assert asked == [list(SIDS)]
    assert sql.on_loop == []
    assert sql.ran_off_loop("FROM sessions")


async def test_a_session_deleted_mid_fanout_gives_the_federated_empty_result(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session deleted between the fanout's commit and the federated
    fallback: the fallback's best-effort `[]`, not a 500."""
    from app.api import journey
    from app.journey import planner_dispatch

    _install_fakes(monkeypatch)
    monkeypatch.setattr(planner_dispatch, "get_planner", lambda _s: _NoTrips())
    real = journey._record_fanout

    def _record_then_delete(*args: Any, **kwargs: Any) -> Any:
        recorded = real(*args, **kwargs)
        gone = SIDS[1]
        _run_sql("DELETE FROM journey_search_executions WHERE session_id = :s", s=gone)
        _run_sql("DELETE FROM graph_snapshots WHERE session_id = :s", s=gone)
        _run_sql("DELETE FROM sessions WHERE id = :s", s=gone)
        return recorded

    monkeypatch.setattr(journey, "_record_fanout", _record_then_delete)
    async with _signed_in() as client:
        r = await client.post("/api/journey/fanout", json=_JOURNEY)
    assert r.status_code == 200, r.text
    assert r.json()["trips"] == []
    assert "federated_trips" not in r.json()


async def test_geocode_reads_off_the_loop_and_returns_its_connection(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MOTIS session is picked in a worker thread, and the request's
    pooled connection is back in the pool before the geocoder is asked
    (the #380 pattern: a typeahead must not hold one over a network call)."""
    from app.api import geocode
    from app.db import engine

    checked_out: list[int] = []

    async def _geocode(_sid: str, _q: str, _gone: Any = None) -> list[Any]:
        checked_out.append(engine.pool.checkedout())
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(geocode, "_fetch_motis_geocode", _geocode)
    async with _signed_in() as client:
        with _sql_threads() as sql:
            r = await client.get("/api/geocode", params={"q": "zz loop"})
    assert r.status_code == 200, r.text
    assert checked_out == [0]
    assert sql.on_loop == []
    assert sql.ran_off_loop("FROM sessions")


# ─────────────────────────── review nits, with a test ───────────────────────────


async def test_replay_with_an_unknown_engine_counts_an_error_per_search(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target session whose engine resolves to no planner: each replayed
    search fails as an engine error (as before #397), dry runs included,
    instead of a 500 for the whole request."""
    from app.journey import planner_dispatch

    _install_fakes(monkeypatch)
    async with _signed_in() as client:
        plan = await client.post("/api/journey/plan", json={**_JOURNEY, "session_id": SIDS[0]})
        assert plan.status_code == 200, plan.text

        def _unknown(_session: Any) -> Any:
            raise ValueError("Unknown session engine: 'zz-99'")

        monkeypatch.setattr(planner_dispatch, "get_planner", _unknown)
        snapshot_id = await asyncio.to_thread(
            _scalar, "SELECT id FROM graph_snapshots WHERE session_id = :s", s=SIDS[0]
        )
        body: dict[str, Any] = {
            "filter": {"session_id": SIDS[0], "since": "2000-01-01T00:00:00Z"},
            "against_graph_snapshot_id": str(snapshot_id),
        }
        dry = await client.post("/api/admin/replay", json={**body, "dry_run": True})
        wet = await client.post("/api/admin/replay", json=body)
    assert dry.status_code == 200, dry.text
    assert dry.json()["would_replay"] == 1
    assert wet.status_code == 200, wet.text
    assert wet.json()["queued"] == 1
    assert wet.json()["outcomes"]["errors"] == 1
    replayed = await asyncio.to_thread(
        _scalar,
        "SELECT status FROM journey_searches WHERE replay_of_search_id IS NOT NULL",
    )
    assert replayed == "error"


async def test_a_refresh_cancelled_during_its_claim_releases_it(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request cancelled while its claim thread runs: the claim it
    committed is ended as interrupted, so the next refresh of that scope is
    not refused (409) until the lease runs out."""
    from app import refresh_progress
    from app.api.admin import sessions

    sid, scope = SIDS[0], refresh_progress.OSM
    claimed, go = threading.Event(), threading.Event()
    real = refresh_progress.claim

    def _slow_claim(*args: Any, **kwargs: Any) -> Any:
        run = real(*args, **kwargs)
        claimed.set()
        go.wait(10)
        return run

    monkeypatch.setattr(refresh_progress, "claim", _slow_claim)
    task = asyncio.ensure_future(sessions._claim_off_loop(sid, scope, 1))
    assert await asyncio.to_thread(claimed.wait, 10)
    task.cancel()
    await asyncio.sleep(0.05)
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The claim thread ends the claim it made after the request had gone.
    deadline = time.monotonic() + 10
    state = None
    while state != refresh_progress.INTERRUPTED and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        state = await asyncio.to_thread(
            _scalar,
            "SELECT state FROM source_refresh_progress WHERE session_id = :s AND scope = :c",
            s=sid,
            c=scope,
        )
    assert state == refresh_progress.INTERRUPTED
    monkeypatch.setattr(refresh_progress, "claim", real)
    again = await asyncio.to_thread(sessions._claim_refresh, sid, scope, 1)
    await asyncio.to_thread(again.finish, refresh_progress.FINISHED)


# ─────────────────── the held-connection limiter (pool full) ───────────────────

# The lens-A review's scenario, scaled down: a pool of one connection (3 s
# timeout), four request threads. One async handler holds the connection
# across an await (an engine call); eight sync handlers then take every
# request thread, each waiting for the pool. The holder's commit must not
# queue behind them: in anyio's pool it waited 6.1 s and 7 of the 8 sync
# handlers timed out; on the held-step limiter it runs at once and all 8 are
# served.
_POOL_TIMEOUT_S = 3
_REQUEST_THREADS = 4
_SYNC_HANDLERS = 8
_ENGINE_CALL_S = 0.5


async def test_a_held_step_never_waits_behind_sync_handlers_on_a_full_pool() -> None:
    from app.db_steps import db_step, holds_connection

    url = _postgres_or_skip()
    eng = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=_POOL_TIMEOUT_S)
    limiter = anyio.to_thread.current_default_thread_limiter()
    tokens = limiter.total_tokens
    limiter.total_tokens = _REQUEST_THREADS

    def _sync_handler() -> str:
        try:
            with Session(eng) as db:
                db.execute(text("SELECT 1"))
            return "ok"
        except PoolTimeout:
            return "pool-timeout"

    async def _holder() -> tuple[float, bool]:
        db = Session(eng)
        t0 = time.monotonic()
        try:
            await db_step(Session.execute, db, text("SELECT 1"))  # takes the connection
            held = holds_connection(db)
            await asyncio.sleep(_ENGINE_CALL_S)  # the engine call
            await db_step(Session.commit, db)  # gives it back
        finally:
            db.close()
        return time.monotonic() - t0, held

    try:
        holder = asyncio.ensure_future(_holder())
        await asyncio.sleep(0.1)
        results = await asyncio.gather(
            *(anyio.to_thread.run_sync(_sync_handler) for _ in range(_SYNC_HANDLERS))
        )
        elapsed, held = await holder
    finally:
        limiter.total_tokens = tokens
        eng.dispose()
    assert held
    assert results == ["ok"] * _SYNC_HANDLERS
    assert elapsed < _ENGINE_CALL_S + _POOL_TIMEOUT_S / 2
