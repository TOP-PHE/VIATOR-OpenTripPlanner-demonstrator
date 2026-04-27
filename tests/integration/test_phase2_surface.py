"""End-to-end smoke that the new Phase-2 surfaces are wired into FastAPI.

These tests verify HTTP routes exist and return the right status code shape
for a platform admin. Heavier behaviour-level coverage lands later as each
feature gains real-world data.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping phase-2 surface test")
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

    monkeypatch.setattr(live, "bootstrap_token", "test-bootstrap-token")

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
            "token": "test-bootstrap-token",
            "email": "admin@viator.test",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    )
    r.raise_for_status()
    jwt = r.json()["jwt"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {jwt}"}


# ────────────────────────── sessions ──────────────────────────


def test_sessions_crud(client: TestClient, admin: dict[str, str]) -> None:
    r = client.get("/api/sessions", headers=admin)
    assert r.status_code == 200 and r.json() == []

    r = client.post(
        "/api/sessions",
        headers=admin,
        json={
            "id": "nap-fr-test",
            "name": "NAP FR test",
            "category": "NAP",
            "config": {},
            "include_in_fanout": False,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "created"

    r = client.patch(
        "/api/sessions/nap-fr-test",
        headers=admin,
        json={"include_in_fanout": True},
    )
    assert r.status_code == 200
    assert r.json()["include_in_fanout"] is True

    r = client.post("/api/sessions/nap-fr-test/archive", headers=admin)
    assert r.status_code == 204


def test_session_id_must_be_slug(client: TestClient, admin: dict[str, str]) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": "Not_A_Slug", "name": "X", "category": "NAP", "config": {}},
    )
    assert r.status_code == 400


# ────────────────────────── master data ──────────────────────────


def test_master_stations_empty(client: TestClient, admin: dict[str, str]) -> None:
    r = client.get("/api/master/stations", headers=admin)
    assert r.status_code == 200
    assert r.json() == []


def test_route_aliases_crud(client: TestClient, admin: dict[str, str]) -> None:
    r = client.post(
        "/api/master/route-aliases",
        headers=admin,
        json={"canonical_name": "TGV INOUI", "alias": "TGV"},
    )
    assert r.status_code == 201, r.text
    alias_id = r.json()["id"]

    r = client.get("/api/master/route-aliases", headers=admin)
    assert any(a["alias"] == "TGV" for a in r.json())

    r = client.delete(f"/api/master/route-aliases/{alias_id}", headers=admin)
    assert r.status_code == 204


# ────────────────────────── reports ──────────────────────────


def test_reports_endpoints_return_empty_initially(
    client: TestClient, admin: dict[str, str]
) -> None:
    for path in (
        "/api/reports/searches",
        "/api/reports/od-pairs",
        "/api/reports/volume-per-user",
        "/api/reports/volume-per-session",
        "/api/reports/trip-source-distribution",
        "/api/reports/unmatched-trips",
    ):
        r = client.get(path, headers=admin)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


# ────────────────────────── journey (fanout error path) ──────────────────────────


def test_fanout_409_when_no_fanout_sessions(client: TestClient, admin: dict[str, str]) -> None:
    r = client.post(
        "/api/journey/fanout",
        headers=admin,
        json={
            "from": {"lat": 48.85, "lon": 2.35},
            "to": {"lat": 45.76, "lon": 4.83},
            "depart_at": "2026-05-01T08:00:00",
            "modes": ["TRANSIT", "WALK"],
        },
    )
    assert r.status_code == 409


# ────────────────────────── pages ──────────────────────────


def test_admin_pages_render_for_admin(client: TestClient) -> None:
    # Bootstrap leaves the cookie set on the client.
    client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": "test-bootstrap-token",
            "email": "admin@viator.test",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    ).raise_for_status()
    for path in (
        "/admin/users",
        "/admin/config",
        "/admin/sessions",
        "/admin/reports",
        "/admin/master/stations",
        "/journey",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code}"
        assert "VIATOR" in r.text


# ────────────────────────── retention ──────────────────────────


def test_retention_runs_against_empty_db(client: TestClient, admin: dict[str, str]) -> None:
    """The retention cron should run cleanly on an empty schema (zero rows pruned)."""
    from app import retention

    counts = retention.prune_once()
    assert all(v == 0 for v in counts.values())


# ────────────────────────── trip signature ──────────────────────────


def test_trip_signature_uses_uic_when_xref_exists(
    client: TestClient, admin: dict[str, str]
) -> None:
    """A leg with a stop_id mapped via stations_xref → master_stations.uic
    should produce a UIC-anchored signature, not a lat/lon one."""
    from app.db import SessionLocal
    from app.journey.signature import trip_signature
    from app.models import MasterStation, StationXref

    # Create a session and a master station + xref entry.
    client.post(
        "/api/sessions",
        headers=admin,
        json={"id": "sigtest", "name": "Sig", "category": "MANUAL", "config": {}},
    ).raise_for_status()

    with SessionLocal() as db:
        db.add(MasterStation(uic="8727100", name="Paris Gare de Lyon", source="manual"))
        db.add(
            StationXref(
                session_id="sigtest", stop_id="StopArea:OCETrain TER-87271007", uic="8727100"
            )
        )
        db.commit()

        sig_with_uic = trip_signature(
            db,
            session_id="sigtest",
            legs=[
                {
                    "mode": "TRAIN",
                    "from_stop_id": "StopArea:OCETrain TER-87271007",
                    "to_stop_id": "StopArea:OCETrain TER-87723197",  # no xref → falls back to lat/lon
                    "departure": "2026-05-01T08:00:00",
                    "arrival": "2026-05-01T10:00:00",
                    "from_lat": 48.85,
                    "from_lon": 2.35,
                    "to_lat": 45.76,
                    "to_lon": 4.83,
                    "route_short_name": "TGV INOUI 6107",
                }
            ],
        )
    assert len(sig_with_uic) == 16
