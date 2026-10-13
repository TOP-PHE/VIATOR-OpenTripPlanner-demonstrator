"""Where the stop extracts are sent: before every graph build, OTP and MOTIS,
and before the MOTIS station build wait; after every refresh; what the
Sessions page shows (MSMM 22.3, D-R15 A; VIATOR PR 5).

The pass itself is replaced (tests/unit/test_station_extract_upload.py tests
it). Values are invented: `ZZ` ids, build numbers beginning with 99.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks

from app import station_builds as sb
from app import station_extract_upload as up
from app import worker
from app.api.admin import sessions as sessions_api
from app.settings import settings

BUILD = 9901
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "app" / "templates" / "admin" / "sessions.html"


def _result(*outcomes: up.FeedOutcome, cancelled: bool = False, off: bool = False) -> up.PassResult:
    return up.PassResult(outcomes=list(outcomes), cancelled=cancelled, off=off)


class _Rec:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lines: list[str] = []
        self.kw: dict[str, Any] = {}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    sent: up.PassResult | Exception,
    waited: sb.WaitResult | None = None,
) -> tuple[tuple[str, bool, str], _Rec]:
    rec = _Rec()

    def send(sid: str, **kw: Any) -> up.PassResult:
        rec.events.append("send")
        rec.kw = {"sid": sid, **kw}
        if isinstance(sent, Exception):
            raise sent
        return sent

    def wait(**_kw: Any) -> sb.WaitResult:
        rec.events.append("wait")
        return waited or sb.WaitResult(sb.DONE, None, BUILD)

    def motis(**_kw: Any) -> tuple[str, bool, str]:
        rec.events.append("motis")
        return "built", True, "g"

    def otp(**_kw: Any) -> tuple[str, bool, str]:
        rec.events.append("otp")
        return "built", True, "g"

    monkeypatch.setattr(worker.station_extract_upload, "send_session", send)
    monkeypatch.setattr(worker.station_builds, "wait_before_graph", wait)
    monkeypatch.setattr(worker, "run_build_motis", motis)
    monkeypatch.setattr(worker, "run_build", otp)
    monkeypatch.setattr(worker, "_note_station_build", lambda _jid, line: rec.lines.append(line))
    out = worker._run_engine_build(
        engine, job_id=uuid.uuid4(), session_id="zz", max_memory=False, container="c"
    )
    return out, rec


def test_motis_sends_before_the_station_build_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    out, rec = _run(monkeypatch, "motis", _result(up.FeedOutcome("ZZ0", up.SENT)))
    assert rec.events == ["send", "wait", "motis"]
    assert out == ("built", True, "g")
    assert rec.kw["sid"] == "zz"
    assert rec.kw["budget"] == up.BUILD_BUDGET_SECONDS
    assert rec.lines[0] == "[viator] station-extracts: sent=1\n"


def test_otp_sessions_send_too(monkeypatch: pytest.MonkeyPatch) -> None:
    out, rec = _run(monkeypatch, "otp", _result(up.FeedOutcome("ZZ0", up.HELD)))
    assert rec.events == ["send", "otp"]
    assert out == ("built", True, "g")
    assert rec.lines == ["[viator] station-extracts: held=1\n"]


@pytest.mark.parametrize("engine", ["otp", "motis"])
@pytest.mark.parametrize(
    "sent",
    [
        _result(up.FeedOutcome("ZZ0", up.UNAVAILABLE, "status_404")),
        _result(up.FeedOutcome("ZZ0", up.REFUSED, "row")),
        _result(up.FeedOutcome("ZZ0", up.SKIPPED, up.BUDGET)),
        _result(off=True),
        RuntimeError("stand-in"),
    ],
    ids=["unavailable", "refused", "budget", "off", "raises"],
)
def test_the_graph_is_always_built(
    monkeypatch: pytest.MonkeyPatch, engine: str, sent: up.PassResult | Exception
) -> None:
    out, rec = _run(monkeypatch, engine, sent)
    assert rec.events[-1] == engine
    assert out == ("built", True, "g")
    if isinstance(sent, up.PassResult) and sent.off:
        assert not any(line.startswith(up.LOG_MARKER) for line in rec.lines)


def test_a_failed_pass_is_one_warning_without_its_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _run(monkeypatch, "motis", RuntimeError("stand-in secret"))
    assert "station extracts not sent" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "stand-in secret" not in caplog.text


@pytest.mark.parametrize("engine", ["otp", "motis"])
def test_a_cancel_while_sending_builds_no_graph(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    out, rec = _run(monkeypatch, engine, _result(cancelled=True))
    assert rec.events == ["send"]
    assert out == (worker._CANCELLED_WHILE_SENDING, False, "")
    assert "no build container was started" in out[0]


def test_a_job_without_a_session_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        worker.station_extract_upload, "send_session", lambda *a, **k: called.append("x")
    )
    monkeypatch.setattr(worker, "run_build", lambda **_kw: ("b", True, "g"))
    worker._run_engine_build(
        "otp", job_id=uuid.uuid4(), session_id=None, max_memory=False, container="c"
    )
    assert called == []


def test_the_worker_pass_asks_for_the_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def send(_sid: str, **kw: Any) -> up.PassResult:
        seen["stop"] = kw["should_stop"]()
        return _result(off=True)

    monkeypatch.setattr(worker.station_extract_upload, "send_session", send)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: True)
    worker._send_station_extracts(uuid.uuid4(), "zz")
    assert seen["stop"] is True

    def broken(_jid: uuid.UUID) -> bool:
        raise RuntimeError("stand-in")

    monkeypatch.setattr(worker, "_cancel_requested", broken)
    worker._send_station_extracts(uuid.uuid4(), "zz")
    assert seen["stop"] is False


# ── after a refresh ──────────────────────────────────────────────────────


_ACTOR = SimpleNamespace(id=uuid.uuid4(), username="ops@example.invalid", role="content_manager")
_REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
_CONFIG = {
    "sources": {
        "providers": [
            {"id": "ZZRAIL", "timetable": {"format": "gtfs", "url": "https://zz.invalid/zz.zip"}}
        ]
    }
}


def _fake_db() -> SimpleNamespace:
    row = SimpleNamespace(id="zz-session", config=dict(_CONFIG), state="populated")
    return SimpleNamespace(get=lambda _model, _sid: row, commit=lambda: None)


def _args(endpoint: Any, **given: Any) -> dict[str, Any]:
    """The endpoint's arguments by name; a task list when it takes one (the
    extractor's warm-up of pull request 4 adds it)."""
    args: dict[str, Any] = {**given, "request": _REQUEST, "db": _fake_db(), "actor": _ACTOR}
    if "background" in inspect.signature(endpoint).parameters:
        args["background"] = BackgroundTasks()
    return args


def _record_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sessions_api.station_extract_upload, "after_refresh", lambda *a: queued.append(a)
    )
    return queued


@pytest.mark.parametrize("bucket", ["fetched", "unchanged", "skipped"])
async def test_every_session_refresh_queues_the_pass(
    monkeypatch: pytest.MonkeyPatch, bucket: str
) -> None:
    outcome: dict[str, list[Any]] = {"fetched": [], "unchanged": [], "skipped": []}
    outcome[bucket] = [{"key": "provider[ZZRAIL].timetable(gtfs)"}]

    async def gathered(*_a: Any, **_k: Any) -> tuple[list[Any], ...]:
        return outcome["fetched"], outcome["unchanged"], outcome["skipped"]

    async def no_cascade(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(sessions_api, "_gather_refresh_outcomes", gathered)
    monkeypatch.setattr(sessions_api, "_cascade_derived_refresh", no_cascade)
    monkeypatch.setattr(sessions_api, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(sessions_api.audit, "record", lambda *_a, **_k: None)
    queued = _record_queue(monkeypatch)
    await sessions_api.refresh_sources(**_args(sessions_api.refresh_sources, sid="zz-session"))
    # Whatever the refresh found: a first deploy or a new extractor version
    # sends what the module lacks even when every file is unchanged.
    assert queued == [("zz-session",)]


async def test_a_provider_refresh_queues_its_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refreshed(*_a: Any, **_k: Any) -> sessions_api.RefreshSourcesResponse:
        return sessions_api.RefreshSourcesResponse(fetched=[], skipped=[{"key": "t"}])

    monkeypatch.setattr(sessions_api, "_refresh_provider_urls", refreshed)
    queued = _record_queue(monkeypatch)
    await sessions_api.refresh_provider(
        **_args(sessions_api.refresh_provider, sid="zz-session", pid="ZZRAIL")
    )
    assert queued == [("zz-session", "ZZRAIL")]


# ── what the Sessions page shows ─────────────────────────────────────────


def test_the_provider_status_carries_the_last_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZ0", up.REFUSED, "duplicate_stop"))
    status = sessions_api.ProviderStatus(feed_id="ZZ0", state="ok")
    status.station_extract = up.load_outcome("zz", "ZZ0")
    dumped = status.model_dump()
    assert dumped["station_extract"]["text"] == "refused duplicate_stop"


def test_the_status_route_reads_the_outcome() -> None:
    source = inspect.getsource(sessions_api.get_providers_status)
    assert 'status.station_extract = station_extract_upload.load_outcome(sid, p["id"])' in source


def test_the_sessions_page_shows_the_outcome() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ">Station module</th>" in html
    assert "${feedStationExtractHTML(s.station_extract)}" in html
    assert "escHTML(x.text)" in html
    assert "escAttr(x.reason)" in html
