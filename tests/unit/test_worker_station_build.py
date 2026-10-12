"""The worker's side of the station build before each MOTIS graph build (MSMM decision 6).

The wait itself is replaced (tests/unit/test_station_builds.py tests it): here,
the graph is always built after it, the job shows each step, the Sessions page
shows the status, and the worker container gets the module's two settings.
Values are invented: build numbers beginning with 99.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app import station_builds as sb
from app import worker
from app.api.admin import sessions as sessions_api

BUILD = 9901
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
TEMPLATE = ROOT / "app" / "templates" / "admin" / "sessions.html"


# ── the status the admin sees ────────────────────────────────────────────


def _job(log: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        session_id="zz-motis",
        status="running",
        log=log,
        created_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
        started_at=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        finished_at=None,
        graph_path=None,
        max_memory=False,
        cancel_requested_at=None,
    )


@pytest.mark.parametrize("word", sorted(sb.STATUS_TEXTS))
def test_the_rebuild_list_carries_the_status(word: str) -> None:
    out = sessions_api._job_to_response(_job("queued\n" + sb.log_line(word)))  # type: ignore[arg-type]
    assert out.station_build == word
    assert out.station_build_text == sb.STATUS_TEXTS[word]


def test_a_job_that_did_not_wait_has_no_status() -> None:
    out = sessions_api._job_to_response(_job("otp build ...\n"))  # type: ignore[arg-type]
    assert out.station_build is None
    assert out.station_build_text is None


def test_the_sessions_page_shows_the_status() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "function _renderStationBuildPill(job)" in html
    assert "escHTML(job.station_build_text)" in html
    # Both the current-build card and the history items show it.
    assert html.count("${_renderStationBuildPill(job)}") == 2


# ── the worker ───────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lines: list[str] = []


def _worker_run(
    monkeypatch: pytest.MonkeyPatch, result: sb.WaitResult, engine: str = "motis"
) -> tuple[tuple[str, bool, str], _Recorder]:
    rec = _Recorder()

    def wait(**kw: Any) -> sb.WaitResult:
        rec.events.append("wait")
        if result.build_id is not None and result.outcome != sb.UP_TO_DATE:
            kw["on_waiting"](result.build_id)
        return result

    def build(**_kw: Any) -> tuple[str, bool, str]:
        rec.events.append("motis")
        return "built", True, "/data/graphs/motis/zz/current"

    def otp(**_kw: Any) -> tuple[str, bool, str]:
        rec.events.append("otp")
        return "built", True, "/data/graphs/zz/current"

    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "run_build_motis", build)
    monkeypatch.setattr(worker, "run_build", otp)
    monkeypatch.setattr(worker, "_note_station_build", lambda _jid, line: rec.lines.append(line))
    out = worker._run_engine_build(
        engine, job_id=uuid.uuid4(), session_id="zz", max_memory=False, container="c"
    )
    return out, rec


@pytest.mark.parametrize(
    "result",
    [
        sb.WaitResult(sb.DONE, None, BUILD),
        sb.WaitResult(sb.UP_TO_DATE, None, BUILD),
        sb.WaitResult(sb.UNAVAILABLE, sb.FAILED, BUILD),
        sb.WaitResult(sb.UNAVAILABLE, sb.NO_INPUTS),
        sb.WaitResult(sb.UNAVAILABLE, sb.BUILD_INTERVAL),
        sb.WaitResult(sb.UNAVAILABLE, sb.FORBIDDEN),
        sb.WaitResult(sb.UNAVAILABLE, sb.UNKNOWN_BUILD, BUILD),
        sb.WaitResult(sb.UNAVAILABLE, sb.TIMEOUT, BUILD),
        sb.WaitResult(sb.UNAVAILABLE, sb.UNREACHABLE),
        sb.WaitResult(sb.UNAVAILABLE, "status_404"),
        sb.WaitResult(sb.OFF),
    ],
    ids=lambda r: f"{r.outcome}-{r.reason}",
)
def test_the_graph_is_always_built_after_the_wait(
    monkeypatch: pytest.MonkeyPatch, result: sb.WaitResult
) -> None:
    out, rec = _worker_run(monkeypatch, result)
    assert rec.events == ["wait", "motis"]
    assert out == ("built", True, "/data/graphs/motis/zz/current")
    if result.outcome == sb.OFF:
        assert rec.lines == []
    else:
        assert sb.status_from_log("".join(rec.lines)) == (
            result.outcome,
            sb.STATUS_TEXTS[result.outcome],
        )


def test_the_job_shows_the_wait_while_it_lasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rec = _worker_run(monkeypatch, sb.WaitResult(sb.DONE, None, BUILD))
    assert rec.lines == [
        sb.log_line(sb.WAITING, build_id=BUILD),
        sb.log_line(sb.DONE, build_id=BUILD),
    ]


def test_a_cancel_during_the_wait_builds_no_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    out, rec = _worker_run(monkeypatch, sb.WaitResult(sb.CANCELLED, None, BUILD))
    assert rec.events == ["wait"]
    assert out[1] is False
    assert out[2] == ""
    assert "cancelled while waiting for the station module build" in out[0]


def test_an_otp_build_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rec = _worker_run(monkeypatch, sb.WaitResult(sb.DONE, None, BUILD), engine="otp")
    assert rec.events == ["otp"]
    assert rec.lines == []


def test_the_worker_wait_asks_for_the_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def wait(**kw: Any) -> sb.WaitResult:
        seen["stop"] = kw["should_stop"]()
        return sb.WaitResult(sb.OFF)

    answers = iter([True])
    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: next(answers))
    worker._wait_for_station_build(uuid.uuid4())
    assert seen["stop"] is True


def test_a_failed_cancel_check_keeps_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def wait(**kw: Any) -> sb.WaitResult:
        seen["stop"] = kw["should_stop"]()
        return sb.WaitResult(sb.OFF)

    def broken(_jid: uuid.UUID) -> bool:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "_cancel_requested", broken)
    worker._wait_for_station_build(uuid.uuid4())
    assert seen["stop"] is False


class _NoteDb:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.statements: list[Any] = []
        self.committed = False

    def __enter__(self) -> _NoteDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def execute(self, statement: Any) -> None:
        if self.fail:
            raise RuntimeError("stand-in")
        self.statements.append(statement)

    def commit(self) -> None:
        self.committed = True


def test_a_step_is_appended_to_the_job_log_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _NoteDb()
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    worker._note_station_build(uuid.uuid4(), sb.log_line(sb.WAITING, build_id=BUILD))
    assert db.committed
    (statement,) = db.statements
    sql = str(statement)
    assert sql.startswith("UPDATE rebuild_jobs SET log=")
    assert "coalesce(rebuild_jobs.log" in sql


def test_a_failed_log_write_never_stops_the_build(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoteDb(fail=True))
    worker._note_station_build(uuid.uuid4(), sb.log_line(sb.WAITING, build_id=BUILD))
    assert "station build line not recorded" in caplog.text
    assert "RuntimeError" in caplog.text


# ── the worker container ─────────────────────────────────────────────────


def test_the_worker_gets_the_two_settings_as_web_does() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    web = compose["services"]["web"]["environment"]
    worker_env = compose["services"]["worker"]["environment"]
    for name in ("STATION_MODULE_URL", "STATION_MODULE_TOKEN"):
        assert worker_env[name] == web[name] == f"${{{name}:-}}"
    # The module's runbook checks this count (`grep -c`): two lines for web,
    # two for worker.
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    assert sum("STATION_MODULE" in line for line in lines) == 4
