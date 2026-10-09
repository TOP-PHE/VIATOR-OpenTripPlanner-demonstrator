"""Cancelling a rebuild (Sessions page → History card → ✕ Cancel).

A pending job is cancelled by the web app; a running one is flagged and the
worker, which owns the build container, kills it. Before this, the only way to
stop a build was `docker kill` on the host, and a worker restart left the
import container running with nobody to read its result (2026-10-02).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app import worker
from app.api.admin import sessions as api

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
SID = "eu19-transit-motis"


# ── API ────────────────────────────────────────────────────────────────


class _FakeDb:
    def __init__(self, job: Any) -> None:
        self.job = job
        self.committed = False

    def get(self, _model: object, job_id: uuid.UUID) -> Any:
        return self.job if job_id == self.job.id else None

    def commit(self) -> None:
        self.committed = True

    def refresh(self, _obj: object) -> None:
        return None


def _job(status: str, **kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "session_id": SID,
        "status": status,
        "log": "queued\n",
        "created_at": datetime(2026, 10, 2, 20, 53, tzinfo=UTC),
        "started_at": None,
        "finished_at": None,
        "graph_path": None,
        "max_memory": True,
        "cancel_requested_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _cancel(
    monkeypatch: pytest.MonkeyPatch, job: Any, sid: str = SID
) -> tuple[Any, list[dict[str, Any]], _FakeDb]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    db = _FakeDb(job)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    out = api.cancel_rebuild(
        sid,
        job.id,
        request,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=None, username="ops@example.org"),  # type: ignore[arg-type]
    )
    return out, recorded, db


def test_a_pending_job_is_cancelled_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("pending")
    out, recorded, db = _cancel(monkeypatch, job)
    assert out.status == "cancelled"
    assert job.finished_at is not None
    assert job.cancel_requested_at is None
    assert "cancelled at" in job.log
    assert "ops@example.org" in job.log
    assert db.committed
    assert recorded[0]["action"] == "session.rebuild.cancelled"
    assert recorded[0]["metadata"] == {"job_id": str(job.id), "status": "cancelled"}


def test_a_running_job_is_flagged_for_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("running", started_at=datetime(2026, 10, 2, 20, 54, tzinfo=UTC))
    out, _, _ = _cancel(monkeypatch, job)
    assert out.status == "running"
    assert out.cancel_requested is True
    first = job.cancel_requested_at
    assert first is not None
    # A second click keeps the first request and does not repeat the log line.
    _cancel(monkeypatch, job)
    assert job.cancel_requested_at == first
    assert job.log.count("cancel requested") == 1


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_a_finished_job_answers_409(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    job = _job(status)
    with pytest.raises(HTTPException) as exc:
        _cancel(monkeypatch, job)
    assert exc.value.status_code == 409


def test_a_job_of_another_session_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("pending")
    with pytest.raises(HTTPException) as exc:
        _cancel(monkeypatch, job, sid="nap-fr-rail")
    assert exc.value.status_code == 404


# ── worker ─────────────────────────────────────────────────────────────


def test_final_status() -> None:
    assert worker._final_status(success=False, cancel_requested=True) == "cancelled"
    assert worker._final_status(success=False, cancel_requested=False) == "failed"
    # Finished before the kill landed: the graph is promoted, so it is done.
    assert worker._final_status(success=True, cancel_requested=True) == "done"


def test_build_containers_are_named_after_their_job() -> None:
    job_id = uuid.UUID("a9c1a75b-5e83-4d03-a7a4-1dce2b4208f0")
    name = worker._build_container_name(job_id)
    assert name == "viator-build-a9c1a75b-5e83-4d03-a7a4-1dce2b4208f0"
    assert worker._name_args(name) == ["--name", name]
    assert worker._name_args(None) == []


def test_watcher_kills_the_container_once_cancel_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([False, True, True])
    killed: list[str] = []
    done = threading.Event()

    def kill(name: str) -> None:
        killed.append(name)
        if len(killed) == 2:  # config, then import: both under the same name
            done.set()

    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: next(answers))
    monkeypatch.setattr(worker, "_kill_container", kill)
    worker._watch_for_cancel(uuid.uuid4(), "viator-build-x", done)
    assert killed == ["viator-build-x", "viator-build-x"]


def test_watcher_survives_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    done = threading.Event()

    def flaky(_jid: uuid.UUID) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db gone")
        done.set()
        return False

    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", flaky)
    worker._watch_for_cancel(uuid.uuid4(), "viator-build-x", done)
    assert calls["n"] == 2


def test_startup_kills_build_containers_left_by_a_previous_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> CompletedProcess[str]:
        calls.append(cmd)
        out = "c1\nc2\n" if cmd[1] == "ps" else ""
        return CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)
    worker._kill_orphaned_build_containers()
    assert calls[0][1:] == ["ps", "-q", "--filter", "name=^viator-build-"]
    assert [c[1:] for c in calls[1:]] == [["kill", "c1"], ["kill", "c2"]]


def test_startup_does_nothing_without_build_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> CompletedProcess[str]:
        calls.append(cmd)
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)
    worker._kill_orphaned_build_containers()
    assert len(calls) == 1


# ── page ───────────────────────────────────────────────────────────────


def test_history_cards_offer_cancel_for_pending_and_running_jobs() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "function _renderCancelControl(job)" in html
    assert "job.status !== 'pending' && job.status !== 'running'" in html
    assert 'data-action="cancel-rebuild"' in html
    assert "/rebuilds/${encodeURIComponent(jobId)}/cancel" in html
    # Both the current-build card and the history items render the control.
    assert html.count("${_renderCancelControl(job)}") == 2


# ── tick() ─────────────────────────────────────────────────────────────


class _Chain:
    def __init__(self, job: Any) -> None:
        self.job = job

    def filter(self, *_a: object) -> _Chain:
        return self

    def order_by(self, *_a: object) -> _Chain:
        return self

    def first(self) -> Any:
        return self.job


class _TickDb:
    def __init__(self, job: Any, claim: uuid.UUID | None) -> None:
        self.job = job
        self.claim = claim
        self.session = SimpleNamespace(engine="motis", state="serving")

    def __enter__(self) -> _TickDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def query(self, _model: object) -> _Chain:
        return _Chain(self.job)

    def execute(self, _stmt: object) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.claim)

    def get(self, model: object, _key: object) -> Any:
        return self.job if model is worker.RebuildJob else self.session

    def commit(self) -> None:
        return None


def _tick(
    monkeypatch: pytest.MonkeyPatch, job: Any, claim: uuid.UUID | None
) -> list[dict[str, Any]]:
    builds: list[dict[str, Any]] = []

    def build(**kw: Any) -> tuple[str, bool, str]:
        builds.append(kw)
        job.cancel_requested_at = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
        return "motis import killed", False, ""

    db = _TickDb(job, claim)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)
    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: False)
    monkeypatch.setattr(worker, "run_build_motis", build)
    worker.tick()
    return builds


def test_tick_skips_a_job_cancelled_between_select_and_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job("pending")
    assert _tick(monkeypatch, job, claim=None) == []


def test_tick_records_a_killed_build_as_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("pending")
    builds = _tick(monkeypatch, job, claim=job.id)
    assert builds == [
        {
            "session_id": SID,
            "max_memory": True,
            "container_name": f"viator-build-{job.id}",
        }
    ]
    assert job.status == "cancelled"
    assert "[viator] cancelled by an operator" in job.log
