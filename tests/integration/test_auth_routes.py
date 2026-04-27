"""End-to-end flow tests for the auth API.

Skips cleanly if Postgres is unreachable (same pattern as test_migrations.py).
"""

from __future__ import annotations

import os
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip(f"DATABASE_URL is not Postgres ({url!r}); skipping auth-routes test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); skipping auth-routes test")
    return url


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_or_skip()
    # Set a known bootstrap token before app modules load it.
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "test-bootstrap-token")
    # Force settings to re-read by reimporting? settings is module-level. We
    # work around by patching the loaded settings object directly:
    from app.settings import settings as live_settings

    monkeypatch.setattr(live_settings, "bootstrap_token", "test-bootstrap-token")
    monkeypatch.setattr(live_settings, "public_base_url", "http://test")

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


# ────────────────────────── bootstrap ──────────────────────────


def test_bootstrap_creates_first_platform_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": "test-bootstrap-token",
            "email": "patrick@trackonpath.com",
            "name": "Patrick",
            "password": "a-strong-bootstrap-password",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "platform_admin"
    assert "jwt" in body and "id" in body
    # Cookie also set.
    assert "viator_jwt" in r.cookies


def test_bootstrap_rejects_when_admin_already_exists(client: TestClient) -> None:
    # First bootstrap succeeds.
    client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": "test-bootstrap-token",
            "email": "first@trackonpath.com",
            "name": "First",
            "password": "a-strong-bootstrap-password",
        },
    ).raise_for_status()
    # Second is closed.
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": "test-bootstrap-token",
            "email": "second@trackonpath.com",
            "name": "Second",
            "password": "another-strong-passphrase",
        },
    )
    assert r.status_code == 403
    assert "already exists" in r.json()["detail"]


def test_bootstrap_with_wrong_token_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": "WRONG-TOKEN",
            "email": "x@y.z",
            "name": "X",
            "password": "a-strong-bootstrap-password",
        },
    )
    assert r.status_code == 403


# ────────────────────────── login / me / logout ──────────────────────────


def _bootstrap(client: TestClient) -> tuple[str, str]:
    """Return (jwt, email) for the bootstrapped admin."""
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
    return r.json()["jwt"], "admin@viator.test"


def test_login_success_sets_cookie(client: TestClient) -> None:
    _bootstrap(client)
    # Logout to drop the bootstrap cookie.
    client.post("/api/auth/logout")
    client.cookies.clear()

    r = client.post(
        "/api/auth/login",
        json={"email": "admin@viator.test", "password": "a-strong-admin-password"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "platform_admin"
    assert "viator_jwt" in r.cookies


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    _bootstrap(client)
    client.cookies.clear()
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@viator.test", "password": "wrong-passphrase-xx"},
    )
    assert r.status_code == 401


def test_me_with_valid_jwt(client: TestClient) -> None:
    jwt, email = _bootstrap(client)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200
    assert r.json()["email"] == email
    assert r.json()["role"] == "platform_admin"


def test_me_unauthorized_without_jwt(client: TestClient) -> None:
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_logout_clears_cookie(client: TestClient) -> None:
    _bootstrap(client)
    r = client.post("/api/auth/logout")
    assert r.status_code == 204
    # The Set-Cookie header from the response should clear viator_jwt.
    set_cookie = r.headers.get("set-cookie", "")
    assert "viator_jwt=" in set_cookie


# ────────────────────────── register / confirm flow ──────────────────────────


def test_register_request_returns_204_and_creates_token(client: TestClient) -> None:
    r = client.post(
        "/api/auth/register-request",
        json={"email": "newbie@example.org", "name": "Newbie"},
    )
    assert r.status_code == 204

    from app.db import SessionLocal
    from app.models import VerificationToken

    with SessionLocal() as db:
        rows = db.execute(select(VerificationToken)).scalars().all()
        emails = [r.email for r in rows]
    assert "newbie@example.org" in emails


def test_register_request_for_existing_user_is_silent(client: TestClient) -> None:
    """Email enumeration prevention: same 204 whether the email is known or not."""
    _bootstrap(client)
    r = client.post(
        "/api/auth/register-request",
        json={"email": "admin@viator.test", "name": "Admin"},
    )
    assert r.status_code == 204
    # Verify no verification token was created for the existing user.
    from app.db import SessionLocal
    from app.models import VerificationToken

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(VerificationToken).where(VerificationToken.email == "admin@viator.test")
            )
            .scalars()
            .all()
        )
    assert rows == []


def test_full_register_confirm_login_flow(client: TestClient) -> None:
    """Mint a verification token directly, then drive register-confirm + login."""
    from datetime import datetime, timedelta

    from app.auth import tokens
    from app.db import SessionLocal
    from app.models import VerificationToken

    raw, hashed = tokens.make_verification_token()
    with SessionLocal() as db:
        db.add(
            VerificationToken(
                token_hash=hashed,
                email="freshuser@example.org",
                name="Fresh User",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()

    # check-token first (what the UI does to populate the form).
    r = client.get(f"/api/auth/check-token?t={raw}")
    assert r.status_code == 200
    assert r.json()["email"] == "freshuser@example.org"

    # Confirm with a password.
    r = client.post(
        "/api/auth/register-confirm",
        json={"token": raw, "password": "a-real-passphrase-12+"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "end_user"

    # Re-using the token now fails (single-use).
    r = client.post(
        "/api/auth/register-confirm",
        json={"token": raw, "password": "another-passphrase-12+"},
    )
    assert r.status_code == 400

    # Login as the new user.
    client.cookies.clear()
    r = client.post(
        "/api/auth/login",
        json={"email": "freshuser@example.org", "password": "a-real-passphrase-12+"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "end_user"


def test_check_token_invalid_returns_404(client: TestClient) -> None:
    r = client.get("/api/auth/check-token?t=not-a-real-token-anywhere-just-bytes")
    assert r.status_code == 404


# ────────────────────────── role gating ──────────────────────────


def test_admin_config_requires_platform_admin_jwt(client: TestClient) -> None:
    """JWT-only — basic auth is no longer accepted on /api/admin/config (step 3 swap)."""
    # No JWT.
    r = client.get("/api/admin/config")
    assert r.status_code == 401


def test_admin_config_accepts_platform_admin_jwt(client: TestClient) -> None:
    jwt, _ = _bootstrap(client)
    client.cookies.clear()
    r = client.get("/api/admin/config", headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200, r.text
