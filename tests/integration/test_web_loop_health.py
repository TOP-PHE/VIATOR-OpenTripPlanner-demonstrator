"""Integration: web's heavy async routes make no database call on the event
loop, so /healthz stays fast while they work (issue #397).

Every `async def` handler shares web's one event loop with every other
request, `/healthz` and `/healthz/version` (the image HEALTHCHECK, 10 s
timeout) included. A synchronous query made by such a handler stops that
loop for as long as the query takes: #392 found the coverage runner doing it
(health checks of 9-17 s, an unrequested restart). #396 moved the runner's
queries off the loop; #397 does the same for the other async routes (journey
fanout and plan, login, geocode, replay, hub resolve, verify-external, ...).

This harness, in the style of tests/integration/test_coverage_web_health.py,
drives those routes on a real PostgreSQL with a **slow database**: every SQL
statement sleeps SLOW_SQL_S in the thread that runs it. A statement on the
loop thread therefore stalls the loop, as a slow production query would.
While the routes run it measures:

- the SQL statements run on the event-loop thread during the requests (must
  be none: the deterministic check);
- the latency of `GET /healthz`, asked from another thread as Docker's curl
  asks from outside, and the loop lag seen by a ticker (timing check).

Engines, the station module and ÖBB are faked; every route still answers
200 with its usual body. Invented values only (ZZ ids, numbers with 99).
Fresh-DB harness like the other integration tests; skips when Postgres is down.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from alembic import command

_USER = uuid.UUID("99999999-9999-4999-8999-999999999397")
_EMAIL = "zz-loop@viator.example"
_PASSWORD = "zz-loop-password-99"
SIDS = ("zz-loop-a-99", "zz-loop-b-99")
HUBS = ("zz-loop-hub-1", "zz-loop-hub-2")
TRIPS_PER_SESSION = 6
ENGINE_LATENCY_S = 0.05
# Every SQL statement sleeps this long in the thread that runs it. Measured on
# PostgreSQL 16 with this file's sizes, worst /healthz latency: 5.1 s before
# #397 (324 statements on the loop: fanout 164, replay 102, plan 48, login 4,
# verify-external 4, geocode 1, hub resolve 1), 4-8 ms after (30 ms with
# coverage tracing and four CPU hogs on four cores). The deterministic check
# is the SQL count on the loop thread; the timing bound leaves shared CI
# runners room and still fails before #397 (even with no slowdown at all,
# the login's bcrypt held the loop 0.3 s).
SLOW_SQL_S = 0.03
HEALTH_LATENCY_BOUND_S = 1.0
LOOP_LAG_BOUND_S = 1.0


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping web loop-health test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


def _seed(url: str) -> None:
    from app.auth import passwords
    from app.db import SessionLocal
    from app.models import (
        GraphSnapshot,
        NetworkCoverageHub,
        NetworkCoverageResult,
    )
    from app.models import Session as SessionRow
    from app.network_coverage import runner

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash, role) "
                "VALUES (:u, :e, 'ZZ', :p, 'platform_admin')"
            ),
            {"u": _USER, "e": _EMAIL, "p": passwords.hash_password(_PASSWORD)},
        )
        conn.execute(text("DELETE FROM network_coverage_hubs"))
    engine.dispose()
    with SessionLocal() as db:
        for sid in SIDS:
            db.add(
                SessionRow(
                    id=sid,
                    name=f"ZZ {sid}",
                    category="NAP",
                    state="serving",
                    engine="motis",
                    include_in_fanout=True,
                    created_by=_USER,
                    config={"sources": {"providers": [{"id": "ZZ-99"}]}},
                )
            )
        db.flush()
        for sid in SIDS:
            db.add(
                GraphSnapshot(
                    session_id=sid,
                    built_at=datetime(2099, 9, 9, tzinfo=UTC),
                    graph_path=f"/zz/{sid}",
                    source_uploads=[],
                    feed_signature="9" * 64,
                    timetable_main_version="zz-99",
                    service_period_start=date(2099, 1, 1),
                    service_period_end=date(2099, 12, 31),
                    is_current=True,
                )
            )
        for i, hub in enumerate(HUBS):
            db.add(
                NetworkCoverageHub(
                    id=hub,
                    name=f"ZZ Loop Hub {i}",
                    short=f"ZZ{i}",
                    country="ZZ",
                    lat=45.0 + i * 0.099,
                    lon=9.0 + i * 0.099,
                    is_active=True,
                    sort_order=i,
                )
            )
        db.commit()
        run = runner.create_run(
            db,
            actor_user_id=None,
            session_id=SIDS[0],
            depart_at=datetime(2099, 9, 9, 9, 0, tzinfo=UTC),
            direction="both",
        )
        db.flush()
        db.add(
            NetworkCoverageResult(
                run_id=run.id,
                origin_hub_id=HUBS[0],
                dest_hub_id=HUBS[1],
                status="no_route",
            )
        )
        db.commit()


@pytest.fixture
def fresh_db() -> str:
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    engine.dispose()
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    from app import config_service

    config_service.invalidate_cache()
    _seed(url)
    return url


# ─────────────────────────────── the fakes ───────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _place(stop: str, lat: float, lon: float) -> dict[str, Any]:
    return {"name": f"ZZ Stop {stop}", "stopId": f"zz_{stop}", "lat": lat, "lon": lon}


def _trips(session_id: str, when: datetime) -> list[dict[str, Any]]:
    """TRIPS_PER_SESSION itineraries of two rail legs, distinct per session,
    as a MOTIS answer through the real MOTIS normalisation: the fanout
    records them and signs every leg (the signature's SELECTs)."""
    from app.journey import motis_client

    base = when if when.tzinfo else when.replace(tzinfo=UTC)
    shift = SIDS.index(session_id) * 0.099
    itineraries = []
    for rank in range(TRIPS_PER_SESSION):
        board = base + timedelta(minutes=9 + rank * 19)
        change = board + timedelta(minutes=59)
        arrive = change + timedelta(minutes=49)
        a, b, c = (f"{session_id}-{rank}{n}" for n in "abc")
        legs = [
            {
                "mode": "RAIL",
                "startTime": _iso(board),
                "endTime": _iso(change),
                "duration": 59 * 60,
                "from": _place(a, 45.099 + shift, 9.099),
                "to": _place(b, 45.499 + shift, 9.499),
                "routeShortName": f"ZZ {rank}99",
                "agencyName": "ZZ Rail",
                "tripId": f"zz_{a}",
            },
            {
                "mode": "RAIL",
                "startTime": _iso(change + timedelta(minutes=9)),
                "endTime": _iso(arrive),
                "duration": 40 * 60,
                "from": _place(b, 45.499 + shift, 9.499),
                "to": _place(c, 45.999 + shift, 9.999),
                "routeShortName": f"ZZ {rank}98",
                "agencyName": "ZZ Rail",
                "tripId": f"zz_{b}",
            },
        ]
        itineraries.append(
            {
                "duration": int((arrive - board).total_seconds()),
                "transfers": 1,
                "startTime": legs[0]["startTime"],
                "endTime": legs[-1]["endTime"],
                "legs": legs,
            }
        )
    return motis_client._itineraries_to_trips({"itineraries": itineraries})


class _FakeMotis:
    calls = 0

    async def fetch_plan(self, **kw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.calls += 1
        await asyncio.sleep(ENGINE_LATENCY_S)
        return {"itineraries": []}, _trips(kw["session_id"], kw["when"])


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> _FakeMotis:
    from app import station_module
    from app.api import geocode
    from app.api.admin import network_coverage as nc
    from app.journey import planner_dispatch
    from app.network_coverage import external_verify

    fake = _FakeMotis()
    monkeypatch.setattr(planner_dispatch, "get_planner", lambda _session: fake)

    async def _geocode(_session_id: str, _q: str, _client_gone: Any = None) -> list[Any]:
        await asyncio.sleep(ENGINE_LATENCY_S)
        return [
            {
                "type": "STOP",
                "name": "ZZ Loop Stop",
                "lat": 45.099,
                "lon": 9.099,
                "id": "zz_99",
                "areas": [],
            }
        ]

    monkeypatch.setattr(geocode, "_fetch_motis_geocode", _geocode)

    async def _near(lat: float, lon: float, _user_id: uuid.UUID, **_kw: Any) -> Any:
        await asyncio.sleep(ENGINE_LATENCY_S)
        row = {
            "name": "ZZ Loop Station",
            "uic": "9999999",
            "country_iso": "ZZ",
            "distance_m": 99,
            "viator_id": None,
            "latitude": lat,
            "longitude": lon,
            "parent_uic": None,
        }
        return station_module.Outcome([row], "ok")

    monkeypatch.setattr(nc, "_module_refusal", lambda: None)
    monkeypatch.setattr(station_module, "near_outcome", _near)

    async def _verify(**_kw: Any) -> Any:
        await asyncio.sleep(ENGINE_LATENCY_S)
        return external_verify.VerifyResult(
            ok=False, num_connections=0, source="oebb_hafas", error=None
        )

    monkeypatch.setattr(external_verify, "verify_via_oebb_hafas", _verify)
    return fake


# ───────────────────────────── the measurement ─────────────────────────────


class _Probe:
    """Slow-database listener, loop-lag ticker and health prober."""

    def __init__(self) -> None:
        self.loop_thread = threading.get_ident()
        self.sql_on_loop: list[str] = []
        self.sql_off_loop = 0
        self.max_lag_s = 0.0
        self.health_latencies: list[float] = []
        self.done = threading.Event()
        self.probe_error: Exception | None = None

    def on_sql(self, _conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        if threading.get_ident() == self.loop_thread:
            self.sql_on_loop.append(statement.split("\n", 1)[0][:80])
        else:
            self.sql_off_loop += 1
        time.sleep(SLOW_SQL_S)  # the slow database, in whichever thread asked

    async def tick(self) -> None:
        step = 0.005
        while not self.done.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(step)
            self.max_lag_s = max(self.max_lag_s, time.perf_counter() - t0 - step)

    def probe_health(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        """/healthz asked from ANOTHER thread, as Docker's curl does: the
        clock runs while the loop gets round to it (see the #396 harness)."""
        try:
            while not self.done.is_set():
                t0 = time.perf_counter()
                future = asyncio.run_coroutine_threadsafe(client.get("/healthz"), loop)
                r = future.result(timeout=60)
                self.health_latencies.append(time.perf_counter() - t0)
                if r.status_code != 200:
                    raise AssertionError(f"/healthz answered {r.status_code}")
                time.sleep(0.02)
        except Exception as exc:  # handed to the test thread
            self.probe_error = exc


async def _drive_routes(client: httpx.AsyncClient) -> dict[str, httpx.Response]:
    """The heavy async routes, one after the other, as a person would."""
    from app.auth import tokens
    from app.settings import settings

    answers: dict[str, httpx.Response] = {}
    answers["login"] = await client.post(
        "/api/auth/login", json={"email": _EMAIL, "password": _PASSWORD}
    )
    client.cookies.clear()
    client.cookies.set(settings.jwt_cookie_name, tokens.issue_jwt(_USER, _EMAIL, "platform_admin"))
    journey = {
        "from": {"lat": 45.099, "lon": 9.099, "label": "ZZ A", "uic": "9999991"},
        "to": {"lat": 45.999, "lon": 9.999, "label": "ZZ B", "uic": "9999992"},
        "depart_at": "2099-09-09T09:09:00Z",
    }
    answers["fanout"] = await client.post("/api/journey/fanout", json=journey)
    answers["plan"] = await client.post(
        "/api/journey/plan", json={**journey, "session_id": SIDS[0]}
    )
    answers["geocode"] = await client.get("/api/geocode", params={"q": "zz loop"})
    answers["hubs_resolve"] = await client.post(
        "/api/admin/network-coverage/hubs/resolve", json={"skip": []}
    )
    run_id, origin, dest = await asyncio.to_thread(_cell)
    answers["verify_external"] = await client.get(
        f"/api/admin/network-coverage/runs/{run_id}/cells/{origin}/{dest}/verify-external"
    )
    snapshot_id = await asyncio.to_thread(_snapshot_id)
    answers["replay"] = await client.post(
        "/api/admin/replay",
        json={
            "filter": {"session_id": SIDS[0], "since": "2000-01-01T00:00:00Z"},
            "against_graph_snapshot_id": str(snapshot_id),
        },
    )
    return answers


def _cell() -> tuple[uuid.UUID, str, str]:
    from app.db import SessionLocal
    from app.models import NetworkCoverageResult

    with SessionLocal() as db:
        row = db.query(NetworkCoverageResult).one()
        return row.run_id, row.origin_hub_id, row.dest_hub_id


def _snapshot_id() -> uuid.UUID:
    from app.db import SessionLocal
    from app.models import GraphSnapshot

    with SessionLocal() as db:
        snap = db.query(GraphSnapshot).filter(GraphSnapshot.session_id == SIDS[0]).one()
        return snap.id


async def _measure() -> tuple[_Probe, dict[str, httpx.Response]]:
    from app.main import app

    probe = _Probe()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zz.invalid") as client:
        # Warm-up outside the measurement (the app builds its middleware stack
        # on its first request).
        assert (await client.get("/healthz")).status_code == 200
        event.listen(Engine, "before_cursor_execute", probe.on_sql)
        try:
            ticker = asyncio.create_task(probe.tick())
            prober = threading.Thread(
                target=probe.probe_health, args=(client, asyncio.get_running_loop())
            )
            prober.start()
            await asyncio.sleep(0.05)
            try:
                answers = await _drive_routes(client)
            finally:
                probe.done.set()
                await ticker
                await asyncio.to_thread(prober.join)
        finally:
            event.remove(Engine, "before_cursor_execute", probe.on_sql)
    if probe.probe_error is not None:
        raise AssertionError("the health probe thread failed") from probe.probe_error
    return probe, answers


async def test_health_stays_fast_while_the_heavy_async_routes_work(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fakes(monkeypatch)
    probe, answers = await _measure()

    statuses = {name: r.status_code for name, r in answers.items()}
    assert statuses == dict.fromkeys(answers, 200), {
        name: r.text[:300] for name, r in answers.items() if r.status_code != 200
    }
    worst = max(probe.health_latencies)
    print(
        f"\n#397 harness: {len(probe.health_latencies)} health probes, worst "
        f"{worst * 1000:.0f} ms; worst loop lag {probe.max_lag_s * 1000:.0f} ms; "
        f"SQL on loop {len(probe.sql_on_loop)}, off loop {probe.sql_off_loop}"
    )
    # Deterministic: no database statement ran on the event-loop thread.
    assert probe.sql_on_loop == []
    assert probe.sql_off_loop > 0
    # Timing, with the scaled bound.
    assert len(probe.health_latencies) > 10
    assert worst < HEALTH_LATENCY_BOUND_S
    assert probe.max_lag_s < LOOP_LAG_BOUND_S

    # The routes did their work, with their usual answers.
    assert answers["login"].json()["role"] == "platform_admin"
    fanout = answers["fanout"].json()
    assert fanout["status"] == "ok"
    assert len(fanout["trips"]) == TRIPS_PER_SESSION * len(SIDS)
    assert {e["session_id"] for e in fanout["executions"]} == set(SIDS)
    assert all(e["num_itineraries"] == TRIPS_PER_SESSION for e in fanout["executions"])
    plan = answers["plan"].json()
    assert (plan["status"], len(plan["trips"])) == ("ok", TRIPS_PER_SESSION)
    assert answers["geocode"].json()[0]["name"] == "ZZ Loop Stop"
    proposals = answers["hubs_resolve"].json()["proposals"]
    assert [p["state"] for p in proposals] == ["proposed", "proposed"]
    assert answers["verify_external"].json()["ok"] is False
    replay = answers["replay"].json()
    # The fanout and the plan both recorded an execution on SIDS[0].
    assert replay["queued"] == 2
    assert replay["outcomes"]["different_result"] == 2
    assert fake.calls == len(SIDS) + 1 + 2
    await asyncio.to_thread(_assert_recorded, fanout["search_id"], plan["search_id"])


def _assert_recorded(fanout_id: str, plan_id: str) -> None:
    """The searches are stored as before: finished, with their executions and
    every trip of each execution."""
    from app.db import SessionLocal
    from app.models import JourneySearch, JourneySearchExecution, JourneyTrip

    with SessionLocal() as db:
        fanout = db.get(JourneySearch, uuid.UUID(fanout_id))
        plan = db.get(JourneySearch, uuid.UUID(plan_id))
        assert fanout is not None and plan is not None
        assert (fanout.endpoint, fanout.status) == ("fanout", "ok")
        assert fanout.total_trips_unique == TRIPS_PER_SESSION * len(SIDS)
        assert (plan.endpoint, plan.status, plan.total_trips_unique) == (
            "plan",
            "ok",
            TRIPS_PER_SESSION,
        )
        executions = (
            db.query(JourneySearchExecution)
            .filter(JourneySearchExecution.search_id == fanout.id)
            .all()
        )
        assert len(executions) == len(SIDS)
        assert all(e.graph_snapshot_id is not None for e in executions)
        trips = (
            db.query(JourneyTrip)
            .filter(JourneyTrip.execution_id.in_([e.id for e in executions]))
            .count()
        )
        assert trips == TRIPS_PER_SESSION * len(SIDS)
