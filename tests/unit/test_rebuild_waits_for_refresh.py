"""A rebuild waits for its session's refresh, and a session can ask for every
rebuild in max-memory mode (production, 30 Oct).

A refresh stores its files one at a time and queues a rebuild with the first
(`ingestion.queue_rebuild`). With a 60 s debounce the worker started that
plain job while the refresh was still storing files; the next file queued a
second job, which the operator's max-memory click then upgraded: two builds,
the first on half the new files and without max-memory, cancelled by hand.

Now the worker starts no rebuild of a session whose refresh runs
(`refresh_progress.running_for`, `worker._next_pending_job`), and a session
whose config sets `build_max_memory` gets max-memory for every rebuild, the
ones a refresh queues included (`ingestion.wants_max_memory`). The SQL
condition itself is checked on PostgreSQL in
tests/integration/test_rebuild_waits_for_refresh.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app import ingestion, refresh_progress, worker
from app.api.admin import sessions as api
from app.models import RebuildJob

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
SID = "eu19-transit-motis"


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# ── the session's setting ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"build_max_memory": True}, True),
        ({"build_max_memory": False}, False),
        ({"build_max_memory": "true"}, False),
        ({"build_max_memory": 1}, False),
        ({}, False),
        (None, False),
        ("build_max_memory", False),
    ],
)
def test_only_a_literal_true_asks_for_max_memory(config: object, expected: bool) -> None:
    assert ingestion.wants_max_memory(config) is expected


class _Query:
    def __init__(self, pending: Any) -> None:
        self.pending = pending

    def filter(self, *_a: object) -> _Query:
        return self

    def first(self) -> Any:
        return self.pending


class _IngestDb:
    def __init__(self, config: dict[str, Any] | None, pending: Any = None) -> None:
        self.session = None if config is None else SimpleNamespace(config=config)
        self.pending = pending
        self.added: list[Any] = []
        self.commits = 0
        self.gets: list[object] = []

    def get(self, _model: object, key: object) -> Any:
        self.gets.append(key)
        return self.session

    def query(self, _model: object) -> _Query:
        return _Query(self.pending)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1


def test_a_refresh_of_a_max_memory_session_queues_a_max_memory_job() -> None:
    db = _IngestDb({"build_max_memory": True})
    ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")  # type: ignore[arg-type]
    (job,) = db.added
    assert job.max_memory is True
    assert "new GTFS uploaded (max-memory: the session's setting)" in job.log
    assert db.commits == 1


def test_a_refresh_of_another_session_queues_a_plain_job() -> None:
    db = _IngestDb({})
    ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")  # type: ignore[arg-type]
    (job,) = db.added
    assert job.max_memory is False
    assert "max-memory" not in job.log


def test_a_plain_pending_job_becomes_max_memory_when_the_session_asks() -> None:
    pending = SimpleNamespace(max_memory=False, log="queued — new GTFS uploaded\n")
    db = _IngestDb({"build_max_memory": True}, pending=pending)
    ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")  # type: ignore[arg-type]
    assert db.added == []
    assert pending.max_memory is True
    assert "upgraded to max-memory at" in pending.log
    assert pending.log.endswith("(max-memory: the session's setting)\n")
    assert db.commits == 1


def test_a_pending_job_is_left_alone_otherwise() -> None:
    pending = SimpleNamespace(max_memory=False, log="queued\n")
    db = _IngestDb({}, pending=pending)
    ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")  # type: ignore[arg-type]
    assert (pending.max_memory, pending.log, db.added, db.commits) == (False, "queued\n", [], 0)


def test_a_job_without_a_session_reads_no_session() -> None:
    db = _IngestDb({"build_max_memory": True})
    ingestion.queue_rebuild(db, session_id=None, kind="GTFS")  # type: ignore[arg-type]
    assert db.gets == []
    assert db.added[0].max_memory is False


def test_a_missing_session_asks_for_nothing() -> None:
    db = _IngestDb(None)
    ingestion.queue_rebuild(db, session_id=SID, kind="GTFS")  # type: ignore[arg-type]
    assert db.added[0].max_memory is False


# ── the manual Rebuild ─────────────────────────────────────────────────


class _ApiDb(_IngestDb):
    def flush(self) -> None:
        return None

    def refresh(self, _obj: object) -> None:
        return None


def _enqueue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: dict[str, Any], *, checkbox: bool
) -> tuple[Any, list[dict[str, Any]]]:
    for sub, name in (("gtfs", "zz.zip"), ("osm", "zz.pbf")):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / sub / name).write_bytes(b"zz")
    monkeypatch.setattr(api.ingestion, "session_inbox", lambda _sid: tmp_path)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: recorded.append(kw))
    monkeypatch.setattr(api, "_job_to_response", lambda job: job)
    db = _ApiDb(config)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    job = api.enqueue_rebuild(
        SID,
        request,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=None, username="ops@example.org"),  # type: ignore[arg-type]
        max_memory=checkbox,
    )
    return job, recorded


def test_a_click_without_the_checkbox_follows_the_sessions_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, recorded = _enqueue(monkeypatch, tmp_path, {"build_max_memory": True}, checkbox=False)
    assert job.max_memory is True
    assert "manual trigger (max-memory: the session's setting)" in job.log
    assert recorded[0]["metadata"]["max_memory"] is True


def test_a_click_with_the_checkbox_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    job, _ = _enqueue(monkeypatch, tmp_path, {}, checkbox=True)
    assert job.max_memory is True
    assert job.log.rstrip().endswith("manual trigger (max-memory)")


def test_a_plain_click_on_a_plain_session_stays_plain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job, _ = _enqueue(monkeypatch, tmp_path, {}, checkbox=False)
    assert job.max_memory is False
    assert "max-memory" not in job.log


# ── the worker ─────────────────────────────────────────────────────────


class _Chain:
    def __init__(self, db: _WorkerDb) -> None:
        self.db = db

    def filter(self, *conditions: Any) -> _Chain:
        self.db.filters.extend(conditions)
        return self

    def order_by(self, *_a: object) -> _Chain:
        return self

    def first(self) -> Any:
        return self.db.job


class _WorkerDb:
    def __init__(self, job: Any, session: Any = None, claim: Any = None) -> None:
        self.job = job
        self.session = session
        self.claim = claim
        self.filters: list[Any] = []
        self.statements: list[Any] = []

    def __enter__(self) -> _WorkerDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def query(self, _model: object) -> _Chain:
        return _Chain(self)

    def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: self.claim)

    def get(self, model: object, _key: object) -> Any:
        return self.job if model is RebuildJob else self.session

    def commit(self) -> None:
        return None


def _pending(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "session_id": SID,
        "status": "pending",
        "log": "queued\n",
        "created_at": datetime.now(UTC) - timedelta(minutes=5),
        "max_memory": False,
        "cancel_requested_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_next_job_excludes_sessions_whose_refresh_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 60)
    job = _pending()
    db = _WorkerDb(job)
    assert worker._next_pending_job(db) is job
    held_back = [c for c in db.filters if "source_refresh_progress" in _sql(c)]
    assert len(held_back) == 1
    sql = _sql(held_back[0])
    assert sql.startswith("NOT (EXISTS")
    assert "source_refresh_progress.state = " in sql
    assert "source_refresh_progress.heartbeat_at >= now()" in sql


def test_a_held_back_job_gets_one_line_in_its_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 60)
    db = _WorkerDb(None)
    assert worker._next_pending_job(db) is None
    (note,) = db.statements
    sql = _sql(note)
    assert sql.startswith("UPDATE rebuild_jobs SET log=")
    assert "EXISTS (SELECT" in sql
    assert "NOT LIKE" in sql
    params = note.compile(dialect=postgresql.dialect()).params
    assert worker.WAITING_FOR_REFRESH in " ".join(str(v) for v in params.values())


def test_the_debounce_still_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 600)
    db = _WorkerDb(_pending())
    assert worker._next_pending_job(db) is None


def _tick(monkeypatch: pytest.MonkeyPatch, job: Any, session: Any) -> tuple[_WorkerDb, list]:
    builds: list[dict[str, Any]] = []

    def build(**kw: Any) -> tuple[str, bool, str]:
        builds.append(kw)
        job.cancel_requested_at = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        return "motis import killed", False, ""

    db = _WorkerDb(job, session=session, claim=job.id)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)
    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: False)
    monkeypatch.setattr(worker, "run_build_motis", build)
    worker.tick()
    return db, builds


def _claim(db: _WorkerDb) -> Any:
    return next(s for s in db.statements if "SET status=" in _sql(s))


def test_a_session_set_to_max_memory_builds_a_plain_job_in_max_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(engine="motis", state="serving", config={"build_max_memory": True})
    db, builds = _tick(monkeypatch, _pending(max_memory=False), session)
    assert builds[0]["max_memory"] is True
    claim = _claim(db)
    assert claim.compile(dialect=postgresql.dialect()).params["max_memory"] is True


def test_a_plain_session_builds_a_plain_job_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SimpleNamespace(engine="motis", state="serving", config={})
    _db, builds = _tick(monkeypatch, _pending(max_memory=False), session)
    assert builds[0]["max_memory"] is False


def test_the_claim_also_refuses_a_session_whose_refresh_began(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(engine="motis", state="serving", config={})
    db, _ = _tick(monkeypatch, _pending(), session)
    sql = _sql(_claim(db))
    assert "NOT (EXISTS (SELECT" in sql
    assert "source_refresh_progress" in sql


def test_running_for_names_every_scope_and_the_lease() -> None:
    sql = _sql(refresh_progress.running_for(RebuildJob.session_id))
    assert "source_refresh_progress.scope" not in sql
    assert "source_refresh_progress.session_id = rebuild_jobs.session_id" in sql
    assert "source_refresh_progress.heartbeat_at >= now() - make_interval(" in sql


# ── saving the setting ─────────────────────────────────────────────────


class _PatchDb:
    def __init__(self, config: dict[str, Any]) -> None:
        self.row = SimpleNamespace(id=SID, name="eu19", config=config)

    def get(self, _model: object, _key: object, **_kw: object) -> Any:
        return self.row


@pytest.mark.parametrize("value", ["yes", 1, None, {"on": True}])
def test_saving_anything_but_true_or_false_is_refused(value: object) -> None:
    body = api.SessionPatch(config={"build_max_memory": value})
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    response = SimpleNamespace(headers={})
    db = _PatchDb({})
    actor = SimpleNamespace(id=None)
    with pytest.raises(HTTPException) as exc:
        api.patch_session(SID, body, request, response, db, actor)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert "build_max_memory must be true or false" in str(exc.value.detail)


# ── the page ───────────────────────────────────────────────────────────


def test_the_rebuild_section_offers_the_setting() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-role="maxmem-always"' in html
    assert "s.config.get('build_max_memory') is sameas true" in html
    assert "cfg.build_max_memory = true" in html
    assert "delete cfg.build_max_memory" in html
    assert "method: 'PATCH'" in html
    # While the setting is on, the one-off box reads checked and is locked.
    assert "once.disabled = true" in html
