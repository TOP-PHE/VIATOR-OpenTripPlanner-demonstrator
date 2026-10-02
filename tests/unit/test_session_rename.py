"""Renaming a session from the Sessions page: display name only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.admin import sessions as api
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


class _FakeDb:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.committed = False

    def get(self, _model: object, sid: str) -> Any:
        return self.row if sid == self.row.id else None

    def commit(self) -> None:
        self.committed = True


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        id="eu19-transit-motis",
        name="EU19 Transit (MOTIS)",
        category="NAP",
        state="serving",
        engine="motis",
        config={},
        include_in_fanout=True,
        created_at=datetime(2026, 6, 29, tzinfo=UTC),
        archived_at=None,
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch, row: Any, name: str
) -> tuple[Any, list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    db = _FakeDb(row)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    out = api.patch_session(
        row.id,
        SessionPatch(name=name),
        request,  # type: ignore[arg-type]
        SimpleNamespace(headers={}),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=None),  # type: ignore[arg-type]
    )
    assert db.committed
    return out, recorded


def test_rename_strips_saves_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    out, recorded = _patch(monkeypatch, row, "  EU Transit (MOTIS) - 22 pays  ")
    assert out.name == row.name == "EU Transit (MOTIS) - 22 pays"
    (event,) = recorded
    assert event["action"] == "session.updated"
    assert event["metadata"]["changes"] == {
        "name": {"from": "EU19 Transit (MOTIS)", "to": "EU Transit (MOTIS) - 22 pays"}
    }


def test_same_name_writes_no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    _, recorded = _patch(monkeypatch, row, " EU19 Transit (MOTIS) ")
    assert recorded == []


def test_a_blank_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    with pytest.raises(HTTPException) as exc:
        _patch(monkeypatch, row, "   ")
    assert exc.value.status_code == 400
    assert row.name == "EU19 Transit (MOTIS)"
