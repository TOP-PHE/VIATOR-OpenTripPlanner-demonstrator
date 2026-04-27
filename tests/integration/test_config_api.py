"""End-to-end test of the platform-config API.

Step 3+: `require_platform_admin` is JWT-based, so each test bootstraps a
platform admin and uses the resulting JWT.

Round-trips:
- GET masks SMTP_PASS when set.
- PATCH validates and persists.
- PATCH with the masked sentinel on a sensitive field is a no-op.
- PATCH with unknown / out-of-bounds keys returns 400 with per-field errors.
- An audit row is written for each change.
- Hot-swap: PATCHing a concurrency limit updates the live semaphore.

Skips cleanly if Postgres is unreachable.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip(f"DATABASE_URL is not Postgres ({url!r}); skipping config-api test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); skipping config-api test")
    return url


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_or_skip()
    from app.settings import settings as live_settings

    monkeypatch.setattr(live_settings, "bootstrap_token", "test-bootstrap-token")

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
def client(fresh_db: str):  # noqa: ARG001
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    """Bootstrap a platform admin, return Authorization header dict."""
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
    client.cookies.clear()  # use the bearer header consistently
    return {"Authorization": f"Bearer {jwt}"}


def test_get_returns_defaults_with_smtp_pass_empty(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    r = client.get("/api/admin/config", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["MAX_CONCURRENT_JOURNEYS"] == 20
    assert body["REGISTRATION_OPEN"] is True
    assert body["SMTP_HOST"] == ""
    # Empty sensitive field is NOT masked — only non-empty values are.
    assert body["SMTP_PASS"] == ""


def test_patch_persists_and_masks_secrets_on_subsequent_get(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    payload = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": 2525,
        "SMTP_USER": "viator",
        "SMTP_PASS": "supersecret",
        "MAX_CONCURRENT_JOURNEYS": 50,
    }
    r = client.patch("/api/admin/config", headers=admin_headers, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["SMTP_HOST"] == "smtp.example.com"
    assert body["SMTP_PORT"] == 2525
    assert body["SMTP_PASS"] == "********"
    assert body["MAX_CONCURRENT_JOURNEYS"] == 50

    r2 = client.get("/api/admin/config", headers=admin_headers)
    assert r2.json()["SMTP_PASS"] == "********"


def test_patch_with_masked_sentinel_does_not_overwrite(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.patch(
        "/api/admin/config", headers=admin_headers, json={"SMTP_PASS": "real-pwd"}
    ).raise_for_status()
    # PATCH with the mask returned by GET — should be a no-op.
    r = client.patch(
        "/api/admin/config", headers=admin_headers, json={"SMTP_PASS": "********"}
    )
    assert r.status_code == 200

    from app.db import SessionLocal
    from app.models import PlatformConfig

    with SessionLocal() as db:
        row = db.execute(
            select(PlatformConfig).where(PlatformConfig.key == "SMTP_PASS")
        ).scalar_one()
        assert row.value == "real-pwd"


def test_patch_with_unknown_key_400(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    r = client.patch(
        "/api/admin/config", headers=admin_headers, json={"NOT_A_KEY": "x"}
    )
    assert r.status_code == 400
    assert "NOT_A_KEY" in r.json()["detail"]["errors"]


def test_patch_with_out_of_bounds_int_400(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    r = client.patch(
        "/api/admin/config", headers=admin_headers, json={"MAX_CONCURRENT_JOURNEYS": 9999}
    )
    assert r.status_code == 400
    assert "MAX_CONCURRENT_JOURNEYS" in r.json()["detail"]["errors"]


def test_patch_writes_audit_event(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.patch(
        "/api/admin/config",
        headers=admin_headers,
        json={"SMTP_FROM": "alerts@viator.local"},
    ).raise_for_status()

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as db:
        events = (
            db.execute(
                select(AuditEvent)
                .where(AuditEvent.action == "config.update")
                .where(AuditEvent.target_id == "SMTP_FROM")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        meta = events[0].metadata_ or {}
        assert meta["key"] == "SMTP_FROM"
        assert meta["to"] == "alerts@viator.local"


def test_patch_concurrency_setting_hot_swaps_semaphore(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    from app import concurrency

    client.patch(
        "/api/admin/config",
        headers=admin_headers,
        json={"MAX_CONCURRENT_JOURNEYS": 77},
    ).raise_for_status()
    assert concurrency.semaphores.journey.limit == 77


def test_get_requires_jwt(client: TestClient) -> None:
    r = client.get("/api/admin/config")
    assert r.status_code == 401
