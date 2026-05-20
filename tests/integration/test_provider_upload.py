"""Integration: attaching an upload to a provider (v0.1.37, Phase 1).

Proves the new `provider_id` form field on `POST /{sid}/uploads`:
  - lands the file at the provider's own inbox slot (`<feed_id>.zip`),
  - records the link on the `Upload` row + in the response,
  - matches the file's detected format against the provider's declared one,
  - rejects an upload for a provider that isn't configured.

Mirrors the fresh-DB harness used by test_phase2_surface (no shared
integration conftest in this repo). Skips when Postgres is unreachable.
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


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping provider-upload test")
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
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the app's inbox to a known, writable tmp dir for this test.

    settings is constructed at import, so we patch the live singleton's
    attribute (read at request time by both the endpoint and ingestion)
    rather than relying on the INBOX_DIR env var being picked up.
    """
    from app.settings import settings as live

    root = tmp_path / "inbox"
    root.mkdir()
    monkeypatch.setattr(live, "inbox_dir", root)
    return root


def _gtfs_zip_bytes() -> bytes:
    """Minimal zip that `app.detect.detect` recognises as GTFS."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nA,A,https://a,Europe/Paris\n",
        )
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n8768600,T,48.8,2.3\n")
        z.writestr("routes.txt", "route_id,agency_id,route_short_name,route_type\nR,A,R,2\n")
        z.writestr("trips.txt", "route_id,service_id,trip_id\nR,WD,T\n")
        z.writestr(
            "stop_times.txt",
            "trip_id,stop_sequence,stop_id,arrival_time,departure_time\nT,1,8768600,08:00,08:00\n",
        )
    return buf.getvalue()


def _make_session_with_provider(client: TestClient, admin: dict[str, str], sid: str) -> None:
    """Create a session, then set its provider config directly in the DB so
    the test doesn't depend on create/PATCH config normalisation."""
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": sid, "name": "XB test", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text

    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models import Session as SessionRow

    with SessionLocal() as db:
        s = db.get(SessionRow, sid)
        assert s is not None
        s.config = {
            "sources": {
                "providers": [
                    {"id": "SNCF-XB", "timetable": {"format": "gtfs", "source": "upload"}}
                ]
            }
        }
        flag_modified(s, "config")
        db.commit()


def test_upload_attached_to_provider_lands_at_feed_slot(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    sid = "xb-test"
    _make_session_with_provider(client, admin, sid)

    r = client.post(
        f"/api/sessions/{sid}/uploads",
        headers=admin,
        data={"declared_standard": "GTFS", "provider_id": "SNCF-XB"},
        files={"file": ("feed.zip", _gtfs_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["provider_feed_id"] == "SNCF-XB"

    # Landed at the provider's own slot (lowercased feed id), not gtfs.zip.
    assert (inbox / sid / "gtfs" / "sncf-xb.zip").is_file()
    assert not (inbox / sid / "gtfs" / "gtfs.zip").exists()

    # The Upload row carries the provider link.
    from app.db import SessionLocal
    from app.models import Upload

    with SessionLocal() as db:
        rows = db.query(Upload).filter(Upload.session_id == sid).all()
        assert len(rows) == 1
        assert rows[0].provider_feed_id == "SNCF-XB"
        assert rows[0].stored_path.endswith("sncf-xb.zip")


def test_upload_for_unknown_provider_rejected(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    sid = "xb-test"
    _make_session_with_provider(client, admin, sid)

    r = client.post(
        f"/api/sessions/{sid}/uploads",
        headers=admin,
        data={"declared_standard": "GTFS", "provider_id": "NOPE"},
        files={"file": ("feed.zip", _gtfs_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 400
    assert "not configured" in r.text


def test_upload_without_provider_id_is_unchanged_legacy_path(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    sid = "xb-test"
    _make_session_with_provider(client, admin, sid)

    # No provider_id → legacy generic slot, no provider link.
    r = client.post(
        f"/api/sessions/{sid}/uploads",
        headers=admin,
        data={"declared_standard": "GTFS"},
        files={"file": ("feed.zip", _gtfs_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["provider_feed_id"] is None
    assert (inbox / sid / "gtfs" / "gtfs.zip").is_file()
