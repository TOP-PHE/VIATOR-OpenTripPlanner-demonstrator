"""Shared pytest fixtures + import-time env defaults.

The default-env block at the top runs at conftest **import**, which is before
any test module imports `app.settings`. That order matters: `Settings()` is
constructed at module import time inside `app.settings`, so monkeypatching
env vars in fixtures would be too late.
"""

from __future__ import annotations

# ── Import-time env defaults (must run before any `from app.* import`) ──────
import os

# Default to a Postgres URL even when Postgres isn't running locally — the
# engine is constructed lazily, so unit tests don't connect. Integration tests
# explicitly check connectivity and SKIP if the DB is unreachable.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://viator_ci:viator_ci@localhost:5432/viator_ci",
)
os.environ.setdefault("ADMIN_USER", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
# In tests we never want APScheduler / cron threads ticking in the background.
os.environ.setdefault("VIATOR_DISABLE_CRONS", "1")
# ────────────────────────────────────────────────────────────────────────────

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh per-test inbox dir, never collides with /data/inbox."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("INBOX_DIR", str(inbox))
    return inbox


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Clear slowapi's per-process counters so rate-limited routes don't trip
    across tests (e.g. bootstrap is 3/hour but multiple tests bootstrap)."""
    from app.rate_limit import limiter

    limiter.reset()
