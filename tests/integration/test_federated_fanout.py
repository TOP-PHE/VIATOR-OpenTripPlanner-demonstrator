"""Integration: the federated (cross-session) fallback in /api/journey/fanout.

When no single serving session returns an end-to-end itinerary and the form
sent UIC endpoints, fanout calls the federated planner and surfaces the stitched
itineraries under `federated_trips`. Here the per-session OTP calls and the
planner are mocked (no live OTP); we assert the wiring + the response shape.

Fresh-DB harness mirrors the other integration tests. Skips when Postgres is down.
"""

from __future__ import annotations

import os

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command

_BOOTSTRAP = "test-bootstrap-token"


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping federated-fanout test")
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
def client(fresh_db: str):
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


def _make_serving_session(sid: str) -> None:
    from app.db import SessionLocal
    from app.models import Session as SessionRow
    from app.models.identity import User
    from app.models.sessions import SessionState

    with SessionLocal() as db:
        # `created_by` is NOT NULL → reuse the bootstrapped platform user
        # (the `admin` fixture runs before the test body, so one exists).
        creator = db.query(User).first()
        assert creator is not None, "admin fixture must bootstrap a user first"
        db.add(
            SessionRow(
                id=sid,
                name="XB",
                category="NAP",
                state=SessionState.SERVING.value,
                include_in_fanout=True,
                created_by=creator.id,
                config={"sources": {"providers": [{"id": "SNCF-XB"}]}},
            )
        )
        db.commit()


def test_fanout_surfaces_federated_trips_when_no_single_session_result(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_serving_session("nap-eu-corridors")

    # No single session returns anything (mock OTP → empty), so the fanout
    # falls through to the federated planner, which we mock to return a stitch.
    from app.journey import federated_planner, otp_client

    async def _no_trips(**_kw):
        return ({}, [])

    monkeypatch.setattr(otp_client, "fetch_plan", _no_trips)

    fake_stitch = {
        "departure_at": "2026-05-22T08:00:00Z",
        "arrival_at": "2026-05-22T12:00:00Z",
        "duration_seconds": 14400,
        "num_transfers": 2,
        "modes": "RAIL",
        "legs": [],
        "via_hubs": ["8500010"],
        "stitched_from_sessions": ["nap-eu-corridors", "nap-ch-rail"],
        "federated": True,
    }

    async def _fake_plan_federated(*_a, **_k):
        return [fake_stitch]

    monkeypatch.setattr(federated_planner, "plan_federated", _fake_plan_federated)

    r = client.post(
        "/api/journey/fanout",
        headers=admin,
        json={
            "from": {"lat": 48.84, "lon": 2.37, "label": "Paris", "uic": "8768600"},
            "to": {"lat": 46.80, "lon": 7.15, "label": "Fribourg", "uic": "8504200"},
            "depart_at": "2026-05-22T08:00:00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trips"] == []  # no single-session itinerary
    assert body["federated_trips"], "federated fallback should have produced a stitch"
    stitch = body["federated_trips"][0]
    assert stitch["via_hubs"] == ["8500010"]
    assert stitch["stitched_from_sessions"] == ["nap-eu-corridors", "nap-ch-rail"]


def test_fanout_skips_federated_without_uic(
    client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_serving_session("nap-eu-corridors")
    from app.journey import federated_planner, otp_client

    async def _no_trips(**_kw):
        return ({}, [])

    monkeypatch.setattr(otp_client, "fetch_plan", _no_trips)

    called = {"hit": False}

    async def _should_not_run(*_a, **_k):
        called["hit"] = True
        return []

    monkeypatch.setattr(federated_planner, "plan_federated", _should_not_run)

    # No UIC on the endpoints → the federated planner is never invoked.
    r = client.post(
        "/api/journey/fanout",
        headers=admin,
        json={
            "from": {"lat": 48.84, "lon": 2.37, "label": "Paris"},
            "to": {"lat": 46.80, "lon": 7.15, "label": "Fribourg"},
            "depart_at": "2026-05-22T08:00:00",
        },
    )
    assert r.status_code == 200, r.text
    assert "federated_trips" not in r.json()
    assert called["hit"] is False
