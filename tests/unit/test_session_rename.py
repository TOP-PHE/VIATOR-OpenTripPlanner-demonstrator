"""Renaming a session from the Sessions page: display name only."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.admin.sessions import SessionPatch

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"


def test_patch_name_has_the_create_bounds() -> None:
    assert SessionPatch(name="EU Transit (MOTIS) - 22 pays").name == "EU Transit (MOTIS) - 22 pays"
    assert SessionPatch().name is None
    for bad in ("", "x" * 201):
        with pytest.raises(ValidationError):
            SessionPatch(name=bad)


def test_sessions_page_offers_rename_of_the_name_only() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-action="rename"' in html
    assert 'data-role="session-name"' in html
    assert "JSON.stringify({name: name.trim()})" in html
