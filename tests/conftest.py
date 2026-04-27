"""Shared pytest fixtures.

For the CI scaffolding step, this is intentionally minimal — fixtures grow
as the auth, sessions, and ingestion modules land. Each later step adds the
fixtures it needs (db engine, FastAPI test client, sample feed factories, …).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets a fresh per-test inbox dir, never collides with /data/inbox."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("INBOX_DIR", str(inbox))
    return inbox


@pytest.fixture(autouse=True)
def _ci_safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default env vars so settings.Settings() is constructible in tests
    without a real .env file present.
    """
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL", "sqlite:///:memory:"))
    monkeypatch.setenv("ADMIN_USER", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin")
