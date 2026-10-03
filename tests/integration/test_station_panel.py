"""Integration: the station panel against a real Postgres.

Mirrors the fresh-DB harness of the other integration modules (there is no
shared integration conftest in this repo). Skips when Postgres is unreachable.

Every station file used here is synthetic: PLC prefix `ZZ`, invented names.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app.master import station_files as sf
from tests.station_fixtures import csv_bytes

# Not a real secret — the bootstrap token used only by these fixtures.
_BOOTSTRAP = "test-bootstrap-token"

TELREF = csv_bytes(
    sf.FILE_SHAPES[sf.ERA_TELREF],
    {"plc": "ZZ00001", "uopid": "ZZ00001", "name": "Exampleville Central", "iso2": "ZZ"},
)


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping station panel tests")
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
def inbox(_isolated_inbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's inbox singleton at the per-test isolated inbox."""
    from app.settings import settings as live

    monkeypatch.setattr(live, "inbox_dir", _isolated_inbox)
    return _isolated_inbox


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


def _seed_user(role: str) -> tuple[uuid.UUID, str]:
    """Insert a user of `role` directly; returns (id, email)."""
    from app.auth.passwords import hash_password
    from app.db import SessionLocal
    from app.models import User

    email = f"{role.replace('_', '-')}@viator.example"
    with SessionLocal() as db:
        user = User(
            email=email,
            name=role,
            password_hash=hash_password("an-irrelevant-passphrase"),
            role=role,
        )
        db.add(user)
        db.commit()
        return user.id, email


def _headers_for(role: str) -> dict[str, str]:
    from app.auth import tokens

    user_id, email = _seed_user(role)
    return {"Authorization": f"Bearer {tokens.issue_jwt(user_id, email, role)}"}


@pytest.fixture
def content_manager(client: TestClient) -> dict[str, str]:
    return _headers_for("content_manager")


@pytest.fixture
def end_user(client: TestClient) -> dict[str, str]:
    return _headers_for("end_user")


def _count(model: type) -> int:
    from app.db import SessionLocal

    with SessionLocal() as db:
        return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def _upload(
    client: TestClient,
    headers: dict[str, str],
    key: str,
    data: bytes,
    filename: str,
    **form: str,
):
    return client.post(
        f"/api/admin/stations/sources/{key}/versions",
        headers=headers,
        data=form,
        files={"file": (filename, data, "text/csv")},
    )


# ───────────────────────── unit 2: the upload route ─────────────────────────


def test_upload_writes_a_version_and_stores_the_file(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.db import SessionLocal
    from app.models import AuditEvent, StationSourceVersion

    r = _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv", as_of="2026-09-14")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["version"]["source_key"] == "ERA_TELREF"
    assert body["version"]["as_of"] == "2026-09-14"
    assert body["version"]["status"] == "uploaded"

    with SessionLocal() as db:
        (version,) = db.execute(select(StationSourceVersion)).scalars().all()
        stored = Path(version.stored_path)
        actions = set(db.execute(select(AuditEvent.action)).scalars())
    assert stored.parent == inbox / "_stations" / "ERA_TELREF"
    assert stored.read_bytes() == TELREF
    assert version.sha256 == body["version"]["sha256"]
    assert version.uploaded_by is not None
    assert "station_source.version.uploaded" in actions


def test_re_uploading_the_same_file_is_a_no_op(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.models import StationSourceVersion

    first = _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv")
    again = _upload(client, admin, "ERA_TELREF", TELREF, "another_name.csv")
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["version"]["id"] == first.json()["version"]["id"]
    assert _count(StationSourceVersion) == 1
    files = [p for p in (inbox / "_stations" / "ERA_TELREF").rglob("*") if p.is_file()]
    assert len(files) == 1


def test_upload_refuses_a_file_of_another_shape(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.models import StationSourceVersion

    links = csv_bytes(sf.FILE_SHAPES[sf.LINKS])
    r = _upload(client, admin, "ERA_TELREF", links, "station_links_crd_2026-09.csv")
    assert r.status_code == 400
    assert "missing columns" in r.json()["detail"]
    assert _count(StationSourceVersion) == 0


def test_upload_refuses_an_unknown_or_disabled_source(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.db import SessionLocal
    from app.models import StationSource

    assert _upload(client, admin, "NOPE", TELREF, "x.csv").status_code == 404
    with SessionLocal() as db:
        source = db.execute(
            select(StationSource).where(StationSource.key == "ERA_TELREF")
        ).scalar_one()
        source.enabled = False
        db.commit()
    assert _upload(client, admin, "ERA_TELREF", TELREF, "x.csv").status_code == 409


def test_upload_is_for_platform_admins_only(
    client: TestClient,
    admin: dict[str, str],
    content_manager: dict[str, str],
    end_user: dict[str, str],
    inbox: Path,
) -> None:
    from app.models import StationSourceVersion

    assert _upload(client, {}, "ERA_TELREF", TELREF, "x.csv").status_code == 401
    assert _upload(client, end_user, "ERA_TELREF", TELREF, "x.csv").status_code == 403
    assert _upload(client, content_manager, "ERA_TELREF", TELREF, "x.csv").status_code == 403
    assert _count(StationSourceVersion) == 0


def test_session_upload_of_a_station_csv_is_400_not_500(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    """Both pre-existing upload routes refuse a station file; this one used to
    do it with an unhandled ValueError out of `detect`."""
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": "st-test", "name": "Station test", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/sessions/st-test/uploads",
        headers=admin,
        data={"declared_standard": "SNCF-Stations"},
        files={"file": ("station_master_crd_2026-09.csv", csv_bytes(sf.FILE_SHAPES[sf.MASTER]))},
    )
    assert r.status_code == 400
    assert "Detection failed" in r.json()["detail"]
