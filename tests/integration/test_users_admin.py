"""User-management admin API: list + role/active patches + self-protection."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping users-admin test")
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
def client(fresh_db: str):  # noqa: ARG001
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> tuple[str, str]:
    """Bootstrap a platform admin. Returns (jwt, user_id)."""
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
    body = r.json()
    client.cookies.clear()
    return body["jwt"], body["id"]


def _seed_user(email: str, name: str, role: str = "end_user") -> str:
    """Insert a user directly into the DB; returns the new user_id (str)."""
    from app.auth.passwords import hash_password
    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        u = User(
            email=email,
            name=name,
            password_hash=hash_password("an-irrelevant-passphrase"),
            role=role,
        )
        db.add(u)
        db.commit()
        return str(u.id)


def _confirmed_via_self_register(client: TestClient, email: str, name: str) -> str:
    """Use the register flow to create a user; returns user_id."""
    from app.auth import tokens
    from app.db import SessionLocal
    from app.models import VerificationToken

    raw, hashed = tokens.make_verification_token()
    with SessionLocal() as db:
        db.add(
            VerificationToken(
                token_hash=hashed,
                email=email,
                name=name,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        db.commit()

    r = client.post(
        "/api/auth/register-confirm",
        json={"token": raw, "password": "a-real-passphrase-12+"},
    )
    r.raise_for_status()
    return r.json()["id"]


def test_list_users(client: TestClient, admin: tuple[str, str]) -> None:
    jwt, _admin_id = admin
    _seed_user("alice@example.org", "Alice")
    _seed_user("bob@example.org", "Bob")

    r = client.get("/api/users", headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200
    emails = [u["email"] for u in r.json()]
    assert {"admin@viator.test", "alice@example.org", "bob@example.org"}.issubset(set(emails))


def test_promote_user_to_content_manager(
    client: TestClient, admin: tuple[str, str]
) -> None:
    jwt, _ = admin
    target_id = _seed_user("alice@example.org", "Alice")

    r = client.patch(
        f"/api/users/{target_id}",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"role": "content_manager"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "content_manager"


def test_cannot_demote_self(client: TestClient, admin: tuple[str, str]) -> None:
    jwt, admin_id = admin
    r = client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"role": "end_user"},
    )
    assert r.status_code == 400
    assert "demote yourself" in r.json()["detail"]


def test_cannot_deactivate_self(client: TestClient, admin: tuple[str, str]) -> None:
    jwt, admin_id = admin
    r = client.patch(
        f"/api/users/{admin_id}",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"is_active": False},
    )
    assert r.status_code == 400
    assert "deactivate yourself" in r.json()["detail"]


def test_invalid_role_400(client: TestClient, admin: tuple[str, str]) -> None:
    jwt, _ = admin
    target = _seed_user("x@y.z", "X")
    r = client.patch(
        f"/api/users/{target}",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"role": "super-admin"},
    )
    assert r.status_code == 400


def test_non_admin_cannot_list_users(client: TestClient, admin: tuple[str, str]) -> None:
    """A registered end_user must NOT be able to enumerate users."""
    _ = admin  # bootstrap exists
    _confirmed_via_self_register(client, "regular@example.org", "Regular")
    # We're now logged in as the end_user via cookie set by register-confirm.
    r = client.get("/api/users")
    assert r.status_code == 403


def test_list_users_unauthenticated_401(client: TestClient) -> None:
    r = client.get("/api/users")
    assert r.status_code == 401
