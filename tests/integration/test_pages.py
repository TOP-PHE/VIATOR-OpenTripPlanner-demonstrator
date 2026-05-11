"""Page-route smoke tests: HTML rendering, redirect-on-auth-failure, role gating.

These tests assert the *shape* of responses, not the visual rendering. The
actual UX is verified manually (screenshot review, browser smoke).
"""

from __future__ import annotations

import os
from datetime import UTC

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping page tests")
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


def _bootstrap_admin(client: TestClient) -> str:
    """Returns the admin's JWT and leaves the cookie set on `client`."""
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
    return r.json()["jwt"]


# ────────────────────────── public pages ──────────────────────────


def test_login_page_renders(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "VIATOR" in body
    assert 'id="login-form"' in body
    # Footer attribution preserved on every page.
    assert "TrackOnPath SAS" in body
    assert "UIC" in body


def test_register_page_renders(client: TestClient) -> None:
    r = client.get("/register")
    assert r.status_code == 200
    assert 'id="reg-form"' in r.text


def test_confirm_page_renders_with_token_in_js(client: TestClient) -> None:
    r = client.get("/confirm/some-opaque-token-value")
    assert r.status_code == 200
    # The token MUST be JS-escaped, not raw — defends against XSS in the URL.
    assert '"some-opaque-token-value"' in r.text


def test_reset_request_page_renders(client: TestClient) -> None:
    r = client.get("/reset")
    assert r.status_code == 200
    assert 'id="reset-req-form"' in r.text


def test_reset_confirm_page_renders_with_token(client: TestClient) -> None:
    r = client.get("/reset/abc123")
    assert r.status_code == 200
    assert '"abc123"' in r.text


def test_login_page_redirects_when_already_logged_in(client: TestClient) -> None:
    _bootstrap_admin(client)
    r = client.get("/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users"


# ────────────────────────── admin/users ──────────────────────────


def test_admin_users_redirects_when_not_logged_in(client: TestClient) -> None:
    r = client.get("/admin/users")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=/admin/users")


def test_admin_users_renders_for_platform_admin(client: TestClient) -> None:
    _bootstrap_admin(client)
    r = client.get("/admin/users")
    assert r.status_code == 200
    body = r.text
    # The bootstrapped admin is in the table.
    assert "admin@viator.example" in body
    # The "you" marker appears next to the current user.
    assert "(you)" in body
    # The role select has the three valid values.
    for role in ("platform_admin", "content_manager", "end_user"):
        assert f'value="{role}"' in body


def test_admin_users_forbidden_for_end_user(client: TestClient) -> None:
    """A logged-in end_user gets 403, not a redirect — they're authenticated, just unauthorised."""
    # Bootstrap admin so a user table exists; then create an end_user via the register flow.
    _bootstrap_admin(client)
    client.cookies.clear()  # drop the admin cookie

    from datetime import datetime, timedelta

    from app.auth import tokens
    from app.db import SessionLocal
    from app.models import VerificationToken

    raw, hashed = tokens.make_verification_token()
    with SessionLocal() as db:
        db.add(
            VerificationToken(
                token_hash=hashed,
                email="enduser@viator.example",
                name="End User",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
    client.post(
        "/api/auth/register-confirm",
        json={"token": raw, "password": "a-real-passphrase-12+"},
    ).raise_for_status()
    # Now the cookie holds the end_user JWT.

    r = client.get("/admin/users")
    assert r.status_code == 403
