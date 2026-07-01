"""Unit tests for the Delete button on the coverage-runs sidebar.

Surface under test: DELETE /api/admin/network-coverage/runs/{run_id}

  a. 404 when the run id doesn't exist
  b. 409 when the run is 'running' — must be stopped first
  c. 204 + hard-delete (db.delete + db.commit) for any terminal or
     'pending' state

Unlike the Stop endpoint (`test_coverage_stop.py`), there's no cooperative-
cancel signal to verify here — this is a plain DB delete, so the only
contract worth locking down is the status guard and that the right row
gets passed to `db.delete`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.admin import network_coverage as api

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "app" / "templates" / "admin" / "network_coverage.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _fake_actor():
    a = MagicMock()
    a.id = uuid.uuid4()
    return a


def _make_run_row(run_id: uuid.UUID | None = None, *, status: str = "completed"):
    r = MagicMock()
    r.id = run_id or uuid.uuid4()
    r.session_id = "nap-fr-rail"
    r.session_label = "nap-fr-rail"
    r.depart_at = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
    r.started_at = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
    r.finished_at = datetime(2026, 7, 1, 8, 5, 0, tzinfo=UTC)
    r.status = status
    r.direction = "both"
    r.mode = "single_session"
    r.total_pairs = 100
    r.completed_pairs = 100
    r.ok_pairs = 90
    r.no_route_pairs = 8
    r.error_pairs = 2
    r.countries = None
    r.verify_externally = False
    return r


def test_delete_endpoint_404_when_run_unknown():
    """A made-up run id surfaces as a clean 404, not a 500."""
    db = MagicMock()
    db.get.return_value = None

    with pytest.raises(HTTPException) as exc:
        api.delete_run(run_id=uuid.uuid4(), db=db, _=_fake_actor())

    assert exc.value.status_code == 404
    db.delete.assert_not_called()
    db.commit.assert_not_called()


def test_delete_endpoint_409_when_run_running():
    """Deleting an in-flight run would race the background worker's
    terminal-state writes — must be stopped first."""
    run = _make_run_row(status="running")
    db = MagicMock()
    db.get.return_value = run

    with pytest.raises(HTTPException) as exc:
        api.delete_run(run_id=run.id, db=db, _=_fake_actor())

    assert exc.value.status_code == 409
    db.delete.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "pending"])
def test_delete_endpoint_deletes_non_running_run(terminal_status):
    """Any non-'running' state is deletable — the whole point is clearing
    out finished test/experimental runs cluttering the sidebar."""
    run = _make_run_row(status=terminal_status)
    db = MagicMock()
    db.get.return_value = run

    result = api.delete_run(run_id=run.id, db=db, _=_fake_actor())

    assert result is None
    db.delete.assert_called_once_with(run)
    db.commit.assert_called_once()


# ─────────────────────── sidebar JS trip-wires ───────────────────────
#
# The Delete button is inline JS in network_coverage.html (same pattern
# as PR-1's Stop button, which has no dedicated JS test either — but the
# row-render conditional here is new enough, and easy enough to typo,
# that a trip-wire is worth it).


def test_delete_button_only_renders_for_non_running_status(template_text: str):
    """The button must be gated the same way the API gates it (409 on
    'running') — otherwise an operator sees a Delete button that always
    fails on in-flight runs."""
    assert "r.status !== 'running'" in template_text
    assert 'data-action="delete-run"' in template_text


def test_delete_run_function_is_defined(template_text: str):
    function_re = r"function\s+deleteRun\s*\("
    assert re.search(function_re, template_text), (
        'deleteRun is wired via data-action="delete-run" but never defined — '
        "the browser will throw a ReferenceError on click."
    )


def test_delete_run_calls_the_delete_endpoint(template_text: str):
    assert "method: 'DELETE'" in template_text
    assert "/api/admin/network-coverage/runs/${encodeURIComponent(runId)}`, {" in template_text


def test_delete_run_confirms_before_sending_request(template_text: str):
    """Irreversible action — must be gated behind a confirm() dialog,
    same convention as stopRun."""
    delete_fn_match = re.search(
        r"async function deleteRun\(.*?\n\}", template_text, flags=re.DOTALL
    )
    assert delete_fn_match, "deleteRun function body not found"
    assert "confirm(" in delete_fn_match.group(0)


def test_delete_run_resets_main_panel_when_current_run_is_deleted(template_text: str):
    """Deleting the currently-loaded run must not leave stale matrix/
    summary content on screen with no run backing it."""
    delete_fn_match = re.search(
        r"async function deleteRun\(.*?\n\}", template_text, flags=re.DOTALL
    )
    assert delete_fn_match
    body = delete_fn_match.group(0)
    assert "runId === CURRENT_RUN_ID" in body
    assert "CURRENT_RUN_ID = null" in body
