"""Where the stop extracts are sent: before every graph build, OTP and MOTIS,
and before the MOTIS station build wait; after every refresh; what the
Sessions page shows (MSMM 22.3, D-R15 A; VIATOR PR 5).

The pass itself is replaced (tests/unit/test_station_extract_upload.py tests
it). Values are invented: `ZZ` ids, build numbers beginning with 99.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
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
    # No extraction starts before a graph build: only extracts already made.
    assert rec.kw["cache_only"] is True
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
    return SimpleNamespace(
        get=lambda _model, _sid: row, commit=lambda: None, refresh=lambda *_a, **_k: None
    )


def _args(endpoint: Any, **given: Any) -> dict[str, Any]:
    """The endpoint's arguments by name; a task list when it takes one (the
    extractor's warm-up of pull request 4 adds it)."""
    args: dict[str, Any] = {**given, "request": _REQUEST, "db": _fake_db(), "actor": _ACTOR}
    if "background" in inspect.signature(endpoint).parameters:
        args["background"] = BackgroundTasks()
    return args


@pytest.fixture
def no_progress_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """`refresh_sources` claims a progress record in the database (issue
    #359, app/refresh_progress.py); these tests have no database."""
    from app import refresh_progress

    def claim(sid: str, scope: str, total: int, **_k: Any) -> refresh_progress.RefreshRun:
        detail = refresh_progress._initial_detail(total, None)
        return refresh_progress.RefreshRun(sid, scope, uuid.uuid4(), detail)

    monkeypatch.setattr(refresh_progress, "claim", claim)
    monkeypatch.setattr(refresh_progress.RefreshRun, "_execute", lambda _self, _stmt, **_k: None)


def _record_queue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sessions_api.station_extract_upload,
        "after_refresh",
        lambda *a, **k: queued.append((*a, *k)),
    )
    return queued


@pytest.mark.usefixtures("no_progress_record")
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
    # The pass reports to this refresh's progress record (its run_id, issue #359).
    assert queued == [("zz-session", "run_id")]


@pytest.mark.usefixtures("no_progress_record")
async def test_a_provider_refresh_queues_its_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refreshed(*_a: Any, **_k: Any) -> sessions_api.RefreshSourcesResponse:
        return sessions_api.RefreshSourcesResponse(fetched=[], skipped=[{"key": "t"}])

    monkeypatch.setattr(sessions_api, "_refresh_provider_urls", refreshed)
    queued = _record_queue(monkeypatch)
    await sessions_api.refresh_provider(
        **_args(sessions_api.refresh_provider, sid="zz-session", pid="ZZRAIL")
    )
    # Its pass reports to this provider's own progress record (its run_id).
    assert queued == [("zz-session", "ZZRAIL", "run_id")]


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


def _page_functions(html: str) -> str:
    """The page's own code of the Station module column, cut out of the
    template: the escaping helpers, the time text and the cell itself."""
    parts = [
        re.search(r"^function escAttr\(s\) \{.*?\}$", html, re.M),
        re.search(r"^function escHTML\(s\) \{.*?^\}", html, re.S | re.M),
        re.search(r"^function _relativeTimeShort\(isoUtc\) \{.*?^\}", html, re.S | re.M),
        re.search(r"^const _STATION_EXTRACT_FLAVOURS = \{.*?^\};", html, re.S | re.M),
        re.search(r"^function feedStationExtractHTML\(x\) \{.*?^\}", html, re.S | re.M),
    ]
    assert all(parts)
    return "\n".join(part.group(0) for part in parts if part)


def _cells(outcomes: list[dict[str, Any] | None]) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = (
        _page_functions(TEMPLATE.read_text(encoding="utf-8"))
        + f"\nconsole.log(JSON.stringify({json.dumps(outcomes)}.map(feedStationExtractHTML)));"
    )
    done = subprocess.run(  # node, with the page's own code
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr
    cells: list[str] = json.loads(done.stdout)
    return cells


def test_the_reason_is_shown_in_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason word beside the state, and the reason's sentence under them,
    escaped; a refusal keeps its word in the text only; no reason, no line."""
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZ0", up.SKIPPED, "input_too_large"))
    up.record_outcome("zz", up.FeedOutcome("ZZ1", up.SKIPPED, up.NOT_CACHED))
    up.record_outcome("zz", up.FeedOutcome("ZZ2", up.REFUSED, "duplicate_stop"))
    up.record_outcome("zz", up.FeedOutcome("ZZ3", up.SENT))
    loaded = [up.load_outcome("zz", f"ZZ{n}") for n in range(4)]
    hostile = {"state": "skipped", "text": "skipped", "reason": "<b>zz</b>", "detail": "<i>zz"}
    big, waiting, refused, sent, odd, empty = _cells([*loaded, hostile, None])
    assert '<code class="feed-extract-reason">input_too_large</code>' in big
    assert (
        '<div class="feed-extract-detail">the archive passes a reading bound: size, '
        "record length, nesting or counts</div>" in big
    )
    assert '<code class="feed-extract-reason">not_cached</code>' in waiting
    assert "waiting for its extract; sent at the next pass" in waiting
    assert ">refused duplicate_stop</span>" in refused
    assert "feed-extract-reason" not in refused
    assert "feed-extract-reason" not in sent
    assert "feed-extract-detail" not in sent
    assert "&lt;b&gt;zz&lt;/b&gt;" in odd
    assert "&lt;i&gt;zz" in odd
    assert "<b>" not in odd
    assert "<i>" not in odd
    assert "title=" not in big  # in clear, not in a tooltip
    assert empty == '<span class="hint">—</span>'


def test_every_reason_text_is_short_and_plain() -> None:
    """Fixed sentences: no path, no address, no markup; an unknown reason
    word, or a refusal word of the module, has none."""
    for word, text in up.REASON_TEXTS.items():
        assert re.fullmatch(r"[a-z0-9_]{1,40}", word)
        assert 10 <= len(text) <= 100, word
        assert not re.search(r"[<>/\\]|https?:|inbox|token=", text), word


def test_the_detail_comes_from_the_reason_word_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZ0", up.UNAVAILABLE, "status_500"))
    up.record_outcome("zz", up.FeedOutcome("ZZ1", up.SKIPPED, up.BUDGET))
    up.record_outcome("zz", up.FeedOutcome("ZZ2", up.SENT))
    first, second, third = (up.load_outcome("zz", f"ZZ{n}") for n in range(3))
    assert first is not None
    assert first["detail"] is None
    assert second is not None
    assert second["detail"] == "the pass ran out of time before it; sent at the next pass"
    assert third is not None
    assert third["detail"] is None


def test_an_upload_into_the_slot_forgets_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    up.record_outcome("zz", up.FeedOutcome("ZZRAIL", up.SENT))
    up.record_outcome("zz", up.FeedOutcome("ZZOTHER", up.SENT))
    sessions_api._forget_fetch_state_for_slot("zz", "GTFS", "zzrail.zip")
    assert up.load_outcome("zz", "ZZRAIL") is None
    assert up.load_outcome("zz", "ZZOTHER") is not None
    sessions_api._forget_fetch_state_for_slot("zz", "OSM-PBF", None)
    assert up.load_outcome("zz", "ZZOTHER") is not None


def test_a_saved_config_forgets_the_removed_providers() -> None:
    source = inspect.getsource(sessions_api.patch_session)
    assert (
        "station_extract_upload.forget_outcomes_except(sid, _provider_ids(s.config or {}))"
        in source
    )
    assert sessions_api._provider_ids(_CONFIG) == {"ZZRAIL"}
    assert sessions_api._provider_ids({"sources": "zz"}) is None
