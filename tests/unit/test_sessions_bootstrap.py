"""Audit-2026-05 #30 — bootstrap script + boot-time orchestrator regenerate.

Covers two related concerns:

1. `bin/viator-bootstrap-stubs.sh` creates the parse-time-required compose +
   nginx stubs idempotently on a fresh clone, before any container starts.
2. `sessions_orchestrator.regenerate()` writes valid stubs even when the DB
   contains zero sessions — the boot-time call's most common case (immediately
   after a fresh install, before any session has been created).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.sessions_orchestrator import regenerate

# ─────────────────────────── orchestrator round-trip ──────────────────────────


def test_regenerate_with_zero_sessions_writes_valid_stubs(tmp_path: Path) -> None:
    """The boot-time call lands here right after a fresh install. Both files
    must be created with content compose / nginx accept as valid even when
    no sessions exist yet."""
    import yaml

    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []

    out = regenerate(db, output_dir=tmp_path)

    compose_text = out["compose"].read_text(encoding="utf-8")
    nginx_text = out["nginx"].read_text(encoding="utf-8")

    # Compose must contain a `services:` mapping (even if empty) so the
    # parent `include:` parses without error.
    assert "services:" in compose_text
    assert "volumes: {}" in compose_text
    assert "no serving sessions" in compose_text

    # Regression guard for the empty-services bug: previously emitted
    # `services:` followed only by a comment, which YAML parses as
    # `services: null` and `docker compose up` then rejects with
    # `services must be a mapping`. The fix emits an explicit empty
    # flow-style mapping (`{}`) so YAML resolves `services` to {} not None.
    parsed = yaml.safe_load(compose_text)
    assert parsed["services"] == {}, (
        f"services must parse as an empty mapping, got {parsed['services']!r}. "
        "If this is None, the empty-services regression is back — see "
        "render_compose() in app/sessions_orchestrator.py."
    )
    assert parsed["volumes"] == {}

    # Nginx file is created (empty content is fine — nginx tolerates an
    # included file with no location blocks).
    assert out["nginx"].exists()
    assert "DO NOT EDIT BY HAND" in nginx_text


def test_regenerate_is_idempotent(tmp_path: Path) -> None:
    """Calling regenerate() twice with the same DB state produces byte-identical
    output. Idempotency matters because the web container's _startup hook
    runs on every restart — a non-deterministic regenerate would flap the
    file mtime + churn nginx if any reload watcher exists."""
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []

    regenerate(db, output_dir=tmp_path)
    first_compose = (tmp_path / "docker-compose.sessions.yml").read_bytes()
    first_nginx = (tmp_path / "nginx-sessions.conf").read_bytes()

    regenerate(db, output_dir=tmp_path)
    second_compose = (tmp_path / "docker-compose.sessions.yml").read_bytes()
    second_nginx = (tmp_path / "nginx-sessions.conf").read_bytes()

    assert first_compose == second_compose
    assert first_nginx == second_nginx


# ─────────────────────────── bootstrap shell script ───────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "bin" / "viator-bootstrap-stubs.sh"


def _make_fake_repo(tmp_path: Path) -> Path:
    """Create a minimal repo skeleton (just `bin/` and an empty
    `docker/generated/`) so the script's `cd ../` from `bin/` lands somewhere
    sensible. Returns the path that plays the role of the repo root."""
    fake_root = tmp_path / "repo"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "docker").mkdir()
    shutil.copy(_BOOTSTRAP_SCRIPT, fake_root / "bin" / "viator-bootstrap-stubs.sh")
    (fake_root / "bin" / "viator-bootstrap-stubs.sh").chmod(stat.S_IRWXU)
    return fake_root


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script — runs on Linux CI")
def test_bootstrap_script_creates_missing_stubs(tmp_path: Path) -> None:
    fake_root = _make_fake_repo(tmp_path)
    gen_dir = fake_root / "docker" / "generated"

    assert not (gen_dir / "docker-compose.sessions.yml").exists()
    assert not (gen_dir / "nginx-sessions.conf").exists()

    result = subprocess.run(
        ["sh", str(fake_root / "bin" / "viator-bootstrap-stubs.sh")],
        capture_output=True,
        text=True,
        check=True,
    )

    compose = (gen_dir / "docker-compose.sessions.yml").read_text()
    nginx = (gen_dir / "nginx-sessions.conf").read_text()

    assert "services: {}" in compose
    assert "DO NOT EDIT" not in nginx  # the stub is intentionally minimal
    assert "Bootstrap stub" in compose
    assert "created" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script — runs on Linux CI")
def test_bootstrap_script_is_idempotent(tmp_path: Path) -> None:
    """Run twice — the second run must not clobber operator-modified content
    or change file mtimes, because compose may be using the file as we run."""
    fake_root = _make_fake_repo(tmp_path)
    gen_dir = fake_root / "docker" / "generated"

    subprocess.run(
        ["sh", str(fake_root / "bin" / "viator-bootstrap-stubs.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    # Operator-style modification — the orchestrator would have overwritten
    # this with a real services block; the script must not undo that.
    custom = "services:\n  otp-foo:\n    image: example\n"
    (gen_dir / "docker-compose.sessions.yml").write_text(custom)

    result = subprocess.run(
        ["sh", str(fake_root / "bin" / "viator-bootstrap-stubs.sh")],
        capture_output=True,
        text=True,
        check=True,
    )

    assert (gen_dir / "docker-compose.sessions.yml").read_text() == custom
    assert "created" not in result.stdout
