"""Integration: a rebuild waits for its session's refresh; a session can ask
for every rebuild in max-memory mode (production, 30 Oct).

On a real PostgreSQL (`refresh_progress.running_for`,
`worker._next_pending_job`, the session config key `build_max_memory`):

- a pending job of a session whose refresh runs is not picked, and gets one
  line in its log saying why, however many ticks pass; once the refresh has
  ended it is picked;
- a refresh past its lease (its process died) holds nothing back;
- a refresh of one session never holds back another session's job;
- `build_max_memory` saves as true or false only, and a refresh's rebuild of
  such a session is queued in max-memory mode.

Invented values only (ZZ sessions). Fresh-DB harness like the other
integration tests; skips when Postgres is down.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command

_BOOTSTRAP = "test-bootstrap-token"
SID = "zz-wait"
OTHER = "zz-other"


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping rebuild-wait test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_or_skip()
    from app.settings import settings as live

    monkeypatch.setattr(live, "bootstrap_token", _BOOTSTRAP)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    from app import config_service

    config_service.invalidate_cache()
    return url


@pytest.fixture
def client(fresh_db: str) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": _BOOTSTRAP,
            "email": "admin@viator.example",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    )
    r.raise_for_status()
    jwt = r.json()["jwt"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {jwt}"}


@pytest.fixture
def no_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import worker

    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)


def _make_session(client: TestClient, admin: dict[str, str], sid: str) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": sid, "name": f"ZZ {sid}", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text


def _queue_job(sid: str, minutes_ago: int) -> Any:
    from app.db import SessionLocal
    from app.models import RebuildJob

    with SessionLocal() as db:
        job = RebuildJob(
            session_id=sid,
            status="pending",
            log="queued — new GTFS uploaded\n",
            created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        )
        db.add(job)
        db.commit()
        return job.id


def _next() -> Any:
    """The id of the job the worker would start now, or None."""
    from app import worker
    from app.db import SessionLocal

    with SessionLocal() as db:
        job = worker._next_pending_job(db)
        return job.id if job is not None else None


def _log(job_id: Any) -> str:
    from app.db import SessionLocal
    from app.models import RebuildJob

    with SessionLocal() as db:
        job = db.get(RebuildJob, job_id)
        assert job is not None
        return job.log or ""


def _refresh_row(sid: str, sql: str) -> None:
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.execute(text(sql), {"sid": sid})
        db.commit()


@pytest.mark.usefixtures("fresh_db", "no_debounce")
def test_a_running_refresh_holds_back_its_sessions_job_until_it_ends(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress, worker

    _make_session(client, admin, SID)
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 3)
    job_id = _queue_job(SID, minutes_ago=5)

    assert _next() is None
    assert _next() is None
    log = _log(job_id)
    assert log.startswith("queued — new GTFS uploaded\n")
    assert log.count(worker.WAITING_FOR_REFRESH) == 1

    _refresh_row(
        SID,
        "UPDATE source_refresh_progress SET state = 'finished', finished_at = now() "
        "WHERE session_id = :sid",
    )
    assert _next() == job_id
    assert _log(job_id).count(worker.WAITING_FOR_REFRESH) == 1


@pytest.mark.usefixtures("fresh_db", "no_debounce")
def test_an_osm_refresh_or_one_providers_refresh_holds_it_back_too(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress

    _make_session(client, admin, SID)
    refresh_progress.claim(SID, refresh_progress.OSM, 1)
    job_id = _queue_job(SID, minutes_ago=5)
    assert _next() is None
    _refresh_row(
        SID, "UPDATE source_refresh_progress SET state = 'finished' WHERE session_id = :sid"
    )
    assert _next() == job_id
    refresh_progress.claim(SID, "ZZRAIL", 1)
    assert _next() is None


@pytest.mark.usefixtures("fresh_db", "no_debounce")
def test_a_refresh_past_its_lease_holds_nothing_back(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress, worker

    _make_session(client, admin, SID)
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 3)
    _refresh_row(
        SID,
        "UPDATE source_refresh_progress SET heartbeat_at = now() - interval '10 minutes' "
        "WHERE session_id = :sid",
    )
    job_id = _queue_job(SID, minutes_ago=5)
    assert _next() == job_id
    assert worker.WAITING_FOR_REFRESH not in _log(job_id)


@pytest.mark.usefixtures("fresh_db", "no_debounce")
def test_one_sessions_refresh_never_holds_back_another_sessions_job(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import refresh_progress, worker

    _make_session(client, admin, SID)
    _make_session(client, admin, OTHER)
    refresh_progress.claim(SID, refresh_progress.PROVIDERS, 3)
    held = _queue_job(SID, minutes_ago=10)  # the oldest, but its session refreshes
    free = _queue_job(OTHER, minutes_ago=5)
    assert _next() == free
    assert worker.WAITING_FOR_REFRESH in _log(held)
    assert worker.WAITING_FOR_REFRESH not in _log(free)


@pytest.mark.usefixtures("fresh_db")
def test_the_setting_saves_true_or_false_and_a_refresh_queues_max_memory(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app import ingestion
    from app.db import SessionLocal
    from app.models import RebuildJob

    _make_session(client, admin, SID)
    r = client.patch(
        f"/api/sessions/{SID}", headers=admin, json={"config": {"build_max_memory": "yes"}}
    )
    assert r.status_code == 400, r.text
    r = client.patch(
        f"/api/sessions/{SID}", headers=admin, json={"config": {"build_max_memory": True}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["build_max_memory"] is True

    with SessionLocal() as db:
        ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")
        ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")
        jobs = db.query(RebuildJob).filter(RebuildJob.session_id == SID).all()
    assert len(jobs) == 1
    assert jobs[0].max_memory is True
    assert "(max-memory: the session's setting)" in (jobs[0].log or "")
