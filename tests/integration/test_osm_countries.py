"""Integration: OSM geographic-scope config + suggestion endpoint.

Covers `osm_countries` validation on `PATCH /api/sessions/{sid}` and the
`GET /api/sessions/{sid}/osm-countries` suggestion (declared provider
countries combined with stop-detected, plus the per-country counts the UI shows).

Fresh-DB harness mirrors test_provider_upload. Skips when Postgres is down.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

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
        pytest.skip("DATABASE_URL is not Postgres; skipping osm-countries test")
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


@pytest.fixture
def inbox(_isolated_inbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.settings import settings as live

    monkeypatch.setattr(live, "inbox_dir", _isolated_inbox)
    return _isolated_inbox


def _make_session(client: TestClient, admin: dict[str, str], sid: str) -> None:
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": sid, "name": "XB", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text


def _write_gtfs(inbox: Path, sid: str, stop_ids: list[str]) -> None:
    """Stage a GTFS zip with the given stop_ids at the session's gtfs slot.

    UIC prefixes drive detection (87=FR, 85=CH, 70=GB); coords are dummy."""
    gtfs_dir = inbox / sid / "gtfs"
    gtfs_dir.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{s},Stop {s},48.0,2.0\n" for s in stop_ids)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n" + rows)
    (gtfs_dir / "feed.zip").write_bytes(buf.getvalue())


# ──────────────────────── validation ────────────────────────


def test_patch_osm_countries_normalises(client: TestClient, admin: dict[str, str]) -> None:
    _make_session(client, admin, "s1")
    r = client.patch(
        "/api/sessions/s1",
        headers=admin,
        json={"config": {"osm_countries": ["fr", "CH", "FR"]}},
    )
    assert r.status_code == 200, r.text

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, "s1")
        assert s is not None
        assert s.config["osm_countries"] == ["CH", "FR"]


def test_patch_osm_countries_rejects_unknown(client: TestClient, admin: dict[str, str]) -> None:
    _make_session(client, admin, "s2")
    r = client.patch(
        "/api/sessions/s2",
        headers=admin,
        json={"config": {"osm_countries": ["FR", "XX"]}},
    )
    assert r.status_code == 400
    assert "not a recognised country" in r.text


# ──────────────────────── suggestion endpoint ────────────────────────


def test_suggest_combines_declared_and_detected(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    sid = "xb"
    _make_session(client, admin, sid)

    # A provider declaring FR + a staged GTFS with FRx6, CHx5, GBx2 stops.
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, sid)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {
                        "id": "SNCF-XB",
                        "country_iso": "FR",
                        "timetable": {"format": "gtfs", "source": "upload"},
                    }
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()

    _write_gtfs(
        inbox,
        sid,
        [f"876860{i}" for i in range(6)]  # FR x6 (prefix 87)
        + [f"850112{i}" for i in range(5)]  # CH x5 (prefix 85)
        + ["7000001", "7000002"],  # GB x2 (prefix 70, below threshold)
    )

    r = client.get(f"/api/sessions/{sid}/osm-countries", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["threshold"] == 5
    rows = {c["iso"]: c for c in body["countries"]}

    assert rows["FR"]["stops"] == 6
    assert rows["FR"]["declared"] is True
    assert rows["FR"]["suggested"] is True

    assert rows["CH"]["stops"] == 5
    assert rows["CH"]["declared"] is False
    assert rows["CH"]["suggested"] is True  # ≥ threshold via stops

    assert rows["GB"]["stops"] == 2
    assert rows["GB"]["suggested"] is False  # below threshold, not declared

    # Full v1 list is returned (32 countries) so the UI can render the checklist.
    assert len(body["countries"]) == 32
