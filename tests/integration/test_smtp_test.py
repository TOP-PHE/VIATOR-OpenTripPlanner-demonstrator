"""End-to-end test of POST /api/admin/config/smtp/test.

Real SMTP isn't reachable in CI, so we patch `aiosmtplib.send` to:
  - return success → endpoint returns {ok: true}
  - raise SMTPException → endpoint returns {ok: false, error: ...}
The "SMTP not configured" path is exercised without any patching (no SMTP_HOST set).

Each path is also expected to write the matching `audit_events` row.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from alembic import command


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping SMTP-test integration")
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

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
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


def _set_smtp(client: TestClient, headers: dict[str, str]) -> None:
    """Configure SMTP via the admin API so the test endpoint has something to use."""
    client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "SMTP_HOST": "smtp.example.org",
            "SMTP_PORT": 587,
            "SMTP_SECURE": "starttls",
            "SMTP_USER": "viator",
            "SMTP_PASS": "supersecret",
            "SMTP_FROM": "no-reply@viator.example",
        },
    ).raise_for_status()


def test_unconfigured_returns_ok_false(client: TestClient, admin_headers: dict[str, str]) -> None:
    """No SMTP_HOST set → graceful {ok: false, error}, NOT a 500."""
    r = client.post(
        "/api/admin/config/smtp/test",
        headers=admin_headers,
        json={"to": "ops@viator.example"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "SMTP" in body["error"]

    # Audit row was written.
    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as db:
        events = (
            db.execute(select(AuditEvent).where(AuditEvent.action == "smtp.test.unconfigured"))
            .scalars()
            .all()
        )
    assert len(events) == 1


def test_send_success_returns_ok_true(client: TestClient, admin_headers: dict[str, str]) -> None:
    _set_smtp(client, admin_headers)

    with patch("app.auth.email.aiosmtplib.send", new=AsyncMock(return_value=None)):
        r = client.post(
            "/api/admin/config/smtp/test",
            headers=admin_headers,
            json={"to": "ops@viator.example"},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True}

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as db:
        events = (
            db.execute(select(AuditEvent).where(AuditEvent.action == "smtp.test.sent"))
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert (events[0].metadata_ or {}).get("to") == "ops@viator.example"


def test_send_failure_returns_ok_false_with_error(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    _set_smtp(client, admin_headers)

    boom = aiosmtplib.SMTPException("server said no")
    with patch("app.auth.email.aiosmtplib.send", new=AsyncMock(side_effect=boom)):
        r = client.post(
            "/api/admin/config/smtp/test",
            headers=admin_headers,
            json={"to": "ops@viator.example"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "server said no" in body["error"]

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as db:
        events = (
            db.execute(select(AuditEvent).where(AuditEvent.action == "smtp.test.failed"))
            .scalars()
            .all()
        )
    assert len(events) == 1


def test_smtp_test_requires_platform_admin(client: TestClient) -> None:
    r = client.post(
        "/api/admin/config/smtp/test",
        json={"to": "ops@viator.example"},
    )
    assert r.status_code == 401  # no JWT
