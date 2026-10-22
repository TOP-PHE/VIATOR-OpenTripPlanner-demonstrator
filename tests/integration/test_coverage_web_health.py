"""Integration: the web stays healthy while a coverage run is busy (issue #392).

A coverage run (`runner.execute_run`) is a FastAPI background task, so it
shares the web's event loop with every request, `GET /healthz/version` (the
image HEALTHCHECK: 10 s timeout, 3 retries) included. Before #392 the run
made its database calls synchronously on that loop: per pair, the recorder
write (two to three SELECTs per leg for the trip signature, then the trips),
the result row and the counters, the DB cancel check, and at the end the
counter recount and the verify sweep. While six pairs finished together the
loop answered nothing, and on 22 Oct the health checks took 9-17 s.

This harness runs the real runner code paths (phase 1, the K-slot fan-out,
the recorder, the result write, the cancel check, the completion) on a real
PostgreSQL, with a fake MOTIS client that waits like an engine and answers
MOTIS-shaped itineraries through the real `motis_client` normalisation.
While the run is busy it measures:

- how many SQL statements ran on the event-loop thread (must be none: the
  deterministic check);
- the event-loop lag seen by a ticker coroutine and the latency of
  `GET /healthz/version` asked from another thread, as Docker's curl asks
  from outside (timing check, with a bound scaled down from the 10 s
  HEALTHCHECK timeout);

and checks that the run's results and counters are those of the same run
before the change (the expected values are computed from the fake engine,
independently of the runner), and that both cancel paths (the Stop button,
a status written by SQL) behave as before.

Invented values only (ZZ hubs and stop ids, numbers with 99). Fresh-DB
harness like the other integration tests; skips when Postgres is down.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import OperationalError

from alembic import command

_USER = "99999999-9999-4999-8999-999999999992"
SID = "zz-cov-99"
HUB_COUNT = 8  # 8 hubs, both directions: 56 pairs
PAIR_PARALLELISM = 6  # production's COVERAGE_PAIR_PARALLELISM on 22 Oct
WITHIN_PAIR_PARALLELISM = 2
SLOT_LATENCY_S = 0.15  # one fake MOTIS slot call
ITINERARIES_PER_SLOT = 10
# The HEALTHCHECK timeout is 10 s; production stalled 9-17 s. The bound here
# is that timeout scaled down 10 times: 1.0 s. Measured on PostgreSQL 16 with
# this file's sizes, worst health latency: 1.9-3.0 s before #392 (the loop
# held by the recorder's SELECTs), 20-140 ms after (up to ~270 ms with
# coverage tracing and four CPU hogs on four cores). The deterministic check
# is the SQL count on the loop thread; this bound leaves shared CI runners
# room and still fails before #392.
HEALTH_LATENCY_BOUND_S = 1.0
LOOP_LAG_BOUND_S = 1.0


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping coverage web-health test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def fresh_db() -> str:
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    from app import config_service

    config_service.invalidate_cache()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash, role) "
                "VALUES (:u, 'zz-cov@zz.invalid', 'ZZ', 'x', 'platform_admin')"
            ),
            {"u": _USER},
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, category, state, engine, created_by) "
                "VALUES (:s, 'ZZ coverage', 'NAP', 'serving', 'motis', :u)"
            ),
            {"s": SID, "u": _USER},
        )
        # Invented hubs only: the migration's seed hubs are removed first.
        conn.execute(text("DELETE FROM network_coverage_hubs"))
        for i in range(HUB_COUNT):
            conn.execute(
                text(
                    "INSERT INTO network_coverage_hubs "
                    "(id, name, short, country, lat, lon, is_active, sort_order) "
                    "VALUES (:id, :name, :short, 'ZZ', :lat, :lon, TRUE, :i)"
                ),
                {
                    "id": f"zz-hub-{i}",
                    "name": f"ZZ Hub {i}",
                    "short": f"ZZ{i}",
                    "lat": 45.0 + i * 0.099,
                    "lon": 9.0 + i * 0.099,
                    "i": i,
                },
            )
    engine.dispose()
    return url


# ─────────────────────────── the fake MOTIS engine ───────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _place(stop: str, lat: float, lon: float) -> dict[str, Any]:
    return {"name": f"ZZ Stop {stop}", "stopId": f"zz_{stop}", "lat": lat, "lon": lon}


def _motis_raw(from_lat: float, from_lon: float, when: datetime) -> dict[str, Any]:
    """A MOTIS `/api/v6/plan` answer: ITINERARIES_PER_SLOT itineraries, each a
    walk, two rail legs and a walk, boarding inside the slot. Stop ids and
    route names are deterministic in (origin, slot, rank) so the expected
    trips of a pair can be computed without the runner."""
    key = f"{round(from_lat * 1000)}{round(from_lon * 1000)}"
    geometry = {"points": "zz" * 400, "precision": 6}
    itineraries = []
    for rank in range(ITINERARIES_PER_SLOT):
        board = when + timedelta(minutes=5 + rank * 20)
        change = board + timedelta(minutes=99)
        arrive = change + timedelta(minutes=59)
        a, b, c = (f"{key}{rank}{n}" for n in "abc")
        legs = [
            {
                "mode": "WALK",
                "startTime": _iso(board - timedelta(minutes=5)),
                "endTime": _iso(board),
                "duration": 300,
                "from": {"name": "START", "lat": from_lat, "lon": from_lon},
                "to": _place(a, from_lat, from_lon),
                "legGeometry": geometry,
            },
            {
                "mode": "RAIL",
                "startTime": _iso(board),
                "endTime": _iso(change),
                "duration": 99 * 60,
                "from": _place(a, from_lat, from_lon),
                "to": _place(b, from_lat + 0.5, from_lon + 0.5),
                "routeShortName": f"ZZ {rank}99",
                "agencyName": "ZZ Rail",
                "tripId": f"zz_t{key}{rank}1",
                "legGeometry": geometry,
            },
            {
                "mode": "RAIL",
                "startTime": _iso(change + timedelta(minutes=9)),
                "endTime": _iso(arrive),
                "duration": 50 * 60,
                "from": _place(b, from_lat + 0.5, from_lon + 0.5),
                "to": _place(c, from_lat + 0.9, from_lon + 0.9),
                "routeShortName": f"ZZ {rank}98",
                "agencyName": "ZZ Rail",
                "tripId": f"zz_t{key}{rank}2",
                "legGeometry": geometry,
            },
            {
                "mode": "WALK",
                "startTime": _iso(arrive),
                "endTime": _iso(arrive + timedelta(minutes=4)),
                "duration": 240,
                "from": _place(c, from_lat + 0.9, from_lon + 0.9),
                "to": {"name": "END", "lat": from_lat + 0.9, "lon": from_lon + 0.9},
                "legGeometry": geometry,
            },
        ]
        itineraries.append(
            {
                "duration": int((arrive - board).total_seconds()) + 540,
                "transfers": 1,
                "startTime": legs[0]["startTime"],
                "endTime": legs[-1]["endTime"],
                "legs": legs,
            }
        )
    return {"itineraries": itineraries}


class _FakeMotis:
    """Stands in for `motis_client`: waits like an engine call, then answers
    through the real MOTIS normalisation. Counts calls per thread."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_plan(self, **kw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from app.journey import motis_client

        self.calls += 1
        await asyncio.sleep(SLOT_LATENCY_S)
        raw = _motis_raw(kw["from_lat"], kw["from_lon"], kw["when"])
        return raw, motis_client._itineraries_to_trips(raw)


# ───────────────────────────── the measurement ─────────────────────────────


class _Probe:
    """Event-loop lag ticker, health prober and SQL-thread counter."""

    def __init__(self) -> None:
        self.max_lag_s = 0.0
        self.health_latencies: list[float] = []
        self.sql_on_loop: list[str] = []
        self.sql_off_loop = 0
        self.loop_thread = threading.get_ident()
        self.done = asyncio.Event()
        self.run_s = 0.0
        self.probe_error: Exception | None = None

    def on_sql(self, _conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        if threading.get_ident() == self.loop_thread:
            self.sql_on_loop.append(statement.split("\n", 1)[0][:80])
        else:
            self.sql_off_loop += 1

    async def tick(self) -> None:
        step = 0.005
        while not self.done.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(step)
            self.max_lag_s = max(self.max_lag_s, time.perf_counter() - t0 - step)

    def probe_health(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        """Ask for /healthz/version from ANOTHER thread, as Docker's curl does
        from outside: the request is handed to the web loop and the clock runs
        until the answer is back, so the time the loop takes to get to it
        counts. (A client coroutine on the same loop would start its clock
        only once the loop is free again and would miss the stall.)

        Any failure here (a status other than 200, a timeout, an exception)
        is kept and re-raised by the test once the thread is joined."""
        try:
            while not self.done.is_set():
                t0 = time.perf_counter()
                future = asyncio.run_coroutine_threadsafe(client.get("/healthz/version"), loop)
                r = future.result(timeout=60)
                self.health_latencies.append(time.perf_counter() - t0)
                if r.status_code != 200:
                    raise AssertionError(f"/healthz/version answered {r.status_code}")
                time.sleep(0.02)
        except Exception as exc:  # handed to the test thread, see _run_and_measure
            self.probe_error = exc


def _create_run() -> uuid.UUID:
    from app.db import SessionLocal
    from app.network_coverage import runner

    with SessionLocal() as db:
        run = runner.create_run(
            db,
            actor_user_id=None,
            session_id=SID,
            depart_at=datetime(2099, 9, 9, 0, 0, tzinfo=UTC),
            direction="both",
        )
        db.commit()
        return run.id


@pytest.fixture
def busy_run(fresh_db: str, monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, _FakeMotis]:
    from app.journey import planner_dispatch
    from app.network_coverage import runner

    fake = _FakeMotis()
    monkeypatch.setattr(planner_dispatch, "planner_for_engine", lambda _engine: fake)
    cfg = runner.CoverageConfig(
        pair_parallelism=PAIR_PARALLELISM, within_pair_parallelism=WITHIN_PAIR_PARALLELISM
    )
    monkeypatch.setattr(runner, "_load_coverage_config", lambda _db: cfg)
    return _create_run(), fake


async def _run_and_measure(run_id: uuid.UUID) -> _Probe:
    from app.db import engine
    from app.main import app
    from app.network_coverage import runner

    probe = _Probe()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zz.invalid") as client:
        # Warm-up outside the measurement: the app's first request builds its
        # middleware stack (~0.1 s, once per process, not the run's doing).
        assert (await client.get("/healthz/version")).status_code == 200
        event.listen(engine, "before_cursor_execute", probe.on_sql)
        try:
            ticker = asyncio.create_task(probe.tick())
            prober = threading.Thread(
                target=probe.probe_health, args=(client, asyncio.get_running_loop())
            )
            prober.start()
            await asyncio.sleep(0.05)  # both measuring before the run starts
            t0 = time.perf_counter()
            try:
                await runner.execute_run(run_id)
            finally:
                probe.run_s = time.perf_counter() - t0
                probe.done.set()
                await ticker
                await asyncio.to_thread(prober.join)
        finally:
            event.remove(engine, "before_cursor_execute", probe.on_sql)
    if probe.probe_error is not None:
        raise AssertionError("the health probe thread failed") from probe.probe_error
    return probe


def _expected_pair(origin_lat: float, origin_lon: float, slots: list[datetime]) -> dict[str, Any]:
    """The pair's result as the runner computed it before #392, from the fake
    engine alone: every slot's itineraries are distinct, all board inside the
    day window, so the pair keeps them all; the best is the shortest."""
    from app.journey import motis_client

    trips = [
        t
        for s in slots
        for t in motis_client._itineraries_to_trips(_motis_raw(origin_lat, origin_lon, s))
    ]
    best = min(trips, key=lambda t: t["duration_seconds"])
    return {
        "num_itineraries": len(trips),
        "best_duration_seconds": best["duration_seconds"],
        "best_num_transfers": best["num_transfers"],
    }


def _assert_results_unchanged(run_id: uuid.UUID, fake: _FakeMotis) -> None:
    from app.db import SessionLocal
    from app.models import (
        JourneyTrip,
        NetworkCoverageHub,
        NetworkCoverageResult,
        NetworkCoverageRun,
    )
    from app.network_coverage import runner

    pairs = HUB_COUNT * (HUB_COUNT - 1)
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert (run.total_pairs, run.completed_pairs, run.ok_pairs) == (pairs, pairs, pairs)
        assert (run.no_route_pairs, run.error_pairs) == (0, 0)
        assert run.summary["elapsed_seconds"] > 0
        cfg = runner.CoverageConfig()
        window = runner._resolve_run_window(
            window_start_local=None,
            window_end_local=None,
            window_timezone=None,
            reference_date_value=run.reference_date,
            cfg=cfg,
        )
        slots = runner._slot_boundaries(window, cfg.slot_count)[:-1]
        hubs = {h.id: h for h in db.execute(select(NetworkCoverageHub)).scalars()}
        rows = list(
            db.execute(
                select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id)
            ).scalars()
        )
        assert len(rows) == pairs
        assert len({(r.origin_hub_id, r.dest_hub_id) for r in rows}) == pairs
        for row in rows:
            origin = hubs[row.origin_hub_id]
            expected = _expected_pair(origin.lat, origin.lon, slots)
            assert row.status == "ok"
            assert row.num_itineraries == expected["num_itineraries"]
            assert row.best_duration_seconds == expected["best_duration_seconds"]
            assert row.best_num_transfers == expected["best_num_transfers"]
            assert row.journey_search_id is not None
            assert row.error_message is None
        trips = db.execute(select(JourneyTrip)).scalars().all()
        assert len(trips) == pairs * cfg.slot_count * ITINERARIES_PER_SLOT
    assert fake.calls == pairs * cfg.slot_count


async def test_health_answers_while_a_coverage_run_is_busy(
    busy_run: tuple[uuid.UUID, _FakeMotis],
) -> None:
    run_id, fake = busy_run
    probe = await _run_and_measure(run_id)

    worst_health = max(probe.health_latencies)
    print(
        f"\n#392 harness: {len(probe.health_latencies)} health probes, "
        f"worst {worst_health * 1000:.0f} ms; worst loop lag {probe.max_lag_s * 1000:.0f} ms; "
        f"SQL on loop {len(probe.sql_on_loop)}, off loop {probe.sql_off_loop}; "
        f"run {probe.run_s:.1f} s"
    )
    # Deterministic: no database statement runs on the event-loop thread.
    assert probe.sql_on_loop == []
    assert probe.sql_off_loop > 0
    # Timing, with the scaled bound.
    assert len(probe.health_latencies) > 20
    assert worst_health < HEALTH_LATENCY_BOUND_S
    assert probe.max_lag_s < LOOP_LAG_BOUND_S
    _assert_results_unchanged(run_id, fake)


# ───────────────────────── cancel semantics, unchanged ─────────────────────────


def _counts(run_id: uuid.UUID) -> tuple[int, dict[str, int]]:
    """(completed_pairs on the run row, result rows by status)."""
    from app.db import SessionLocal
    from app.models import NetworkCoverageResult, NetworkCoverageRun

    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        by_status: dict[str, int] = {}
        for status in db.execute(
            select(NetworkCoverageResult.status).where(NetworkCoverageResult.run_id == run_id)
        ).scalars():
            by_status[status] = by_status.get(status, 0) + 1
        return run.completed_pairs, by_status


async def _wait_for_pairs(run_id: uuid.UUID, at_least: int) -> None:
    for _ in range(600):
        completed, _rows = await asyncio.to_thread(_counts, run_id)
        if completed >= at_least:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the run wrote no pairs")


async def test_a_stop_click_mid_run_keeps_the_partial_cells(
    busy_run: tuple[uuid.UUID, _FakeMotis],
) -> None:
    from app.db import SessionLocal
    from app.models import NetworkCoverageRun
    from app.network_coverage import runner

    run_id, fake = busy_run
    task = asyncio.create_task(runner.execute_run(run_id))
    await _wait_for_pairs(run_id, PAIR_PARALLELISM)
    assert runner.request_cancel(run_id) is True
    await task
    calls_at_end = fake.calls

    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert run.summary["cancelled_by_operator"] is True
        rows = _counts(run_id)[1]
        assert 0 < run.completed_pairs == sum(rows.values()) < run.total_pairs
        assert run.ok_pairs == rows.get("ok", 0) == run.completed_pairs
    # Pairs in flight at the click finish; nothing starts after the run ended.
    await asyncio.sleep(SLOT_LATENCY_S * 3)
    assert fake.calls == calls_at_end
    assert run_id not in runner._CANCEL_EVENTS


async def test_a_status_written_by_sql_stops_the_pairs(
    busy_run: tuple[uuid.UUID, _FakeMotis],
) -> None:
    """PR-187's cross-process cancel: an UPDATE of the run's status is seen by
    the (now off-loop) DB cancel check, and the remaining pairs are skipped."""
    from app.db import SessionLocal
    from app.network_coverage import runner

    run_id, _fake = busy_run
    task = asyncio.create_task(runner.execute_run(run_id))
    await _wait_for_pairs(run_id, PAIR_PARALLELISM)

    def _cancel_by_sql() -> None:
        with SessionLocal() as db:
            db.execute(
                text("UPDATE network_coverage_runs SET status = 'cancelled' WHERE id = :r"),
                {"r": run_id},
            )
            db.commit()

    await asyncio.to_thread(_cancel_by_sql)
    await task
    completed, rows = _counts(run_id)
    pairs = HUB_COUNT * (HUB_COUNT - 1)
    assert 0 < sum(rows.values()) < pairs
    assert completed == sum(rows.values())
