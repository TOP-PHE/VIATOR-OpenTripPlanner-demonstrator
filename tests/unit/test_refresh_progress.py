"""The refresh progress writer without a database (issue #359).

`app/refresh_progress.py`: how often the bytes of a download are written
(throttled: never one write per chunk), what a task's outcome counts as, what
the record may hold (labels and counts, never a URL or a reason text), and
the download hook of `app/feed_fetch.py` that feeds it. The database side
(claim, lease, reads across requests) is in
tests/integration/test_refresh_progress.py.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import feed_fetch, refresh_progress
from app import station_extract_upload as up

LABEL = "provider[ZZRAIL].timetable(gtfs)"


class FakeClock:
    def __init__(self) -> None:
        self.now = 99.0

    def __call__(self) -> float:
        return self.now


def _run(clock: FakeClock, total: int = 3) -> tuple[refresh_progress.RefreshRun, list[dict]]:
    """A run whose writes are kept in a list instead of the database."""
    writes: list[dict[str, Any]] = []
    run = refresh_progress.RefreshRun(
        "zz-session",
        refresh_progress.PROVIDERS,
        uuid.uuid4(),
        refresh_progress._initial_detail(total, None),
        clock=clock,
    )
    run._execute = lambda _statement, **_k: writes.append(  # type: ignore[method-assign]
        {**run.detail, "current": [dict(c) for c in run.detail["current"]]}
    )
    return run, writes


# ── bytes: throttled ─────────────────────────────────────────────────────


def test_bytes_are_written_every_five_percent_at_most() -> None:
    clock = FakeClock()
    run, writes = _run(clock)
    expected = 99 * 1000 * 1000
    with run.task(LABEL):
        for received in range(0, expected + 1, 99_000):  # 1001 chunks, no time passing
            run.bytes_received(received, expected)
    # The task's own write, then one per 5 % step: 20 steps, never per chunk.
    bytes_writes = writes[1:]
    assert len(bytes_writes) == 20
    steps = [w["current"][0]["bytes"] * 100 // expected for w in bytes_writes]
    assert steps == list(range(5, 101, 5))
    last = bytes_writes[-1]["current"][0]
    assert (last["key"], last["bytes"], last["expected"]) == (LABEL, expected, expected)
    assert last["step"] == feed_fetch.STEP_DOWNLOADING


def test_bytes_of_an_unknown_size_are_written_every_two_seconds() -> None:
    clock = FakeClock()
    run, writes = _run(clock)
    with run.task(LABEL):
        for i in range(1, 100):  # 99 chunks, a tenth of a second apart
            clock.now += 0.1
            run.bytes_received(i * 99, None)
    bytes_writes = writes[1:]
    # 9.9 s of chunks: a write at 2.0, 4.0, 6.0 and 8.0 seconds.
    assert len(bytes_writes) == 4
    assert all(w["current"][0]["expected"] is None for w in bytes_writes)


def test_a_slow_download_of_a_known_size_still_shows_bytes_every_two_seconds() -> None:
    clock = FakeClock()
    run, writes = _run(clock)
    with run.task(LABEL):
        for i in range(1, 11):
            clock.now += 2.0
            run.bytes_received(i, 99_000_000)  # far below one 5 % step
    assert len(writes) == 1 + 10


def test_bytes_outside_a_task_are_ignored() -> None:
    run, writes = _run(FakeClock())
    run.bytes_received(99, 999)
    assert writes == []


# ── outcomes and the end ─────────────────────────────────────────────────


def test_outcomes_are_counted_and_failed_labels_kept() -> None:
    run, writes = _run(FakeClock(), total=4)
    run.done("provider[ZZ1].timetable(gtfs)", {"status": "fetched"})
    run.done("provider[ZZ2].timetable(gtfs)", {"status": "unchanged"})
    run.done("provider[ZZ3].timetable(gtfs)", {"status": "skipped", "reason": "HTTP 404"})
    assert writes[-1]["done"] == 3
    assert (writes[-1]["fetched"], writes[-1]["unchanged"], writes[-1]["failed"]) == (1, 1, 1)
    assert writes[-1]["failed_keys"] == ["provider[ZZ3].timetable(gtfs)"]
    run.finish(refresh_progress.FAILED, error="RuntimeError")
    assert run.detail["not_reached"] == 1
    assert run.detail["error"] == "runtimeerror"
    assert run.detail["current"] == []


def test_failed_labels_are_capped() -> None:
    run, _writes = _run(FakeClock(), total=99)
    for i in range(99):
        run.done(f"provider[ZZ{i}].timetable(gtfs)", {"status": "skipped"})
    assert len(run.detail["failed_keys"]) == refresh_progress.FAILED_KEYS_MAX
    assert run.detail["failed"] == 99


def test_a_label_that_is_not_a_label_is_never_stored() -> None:
    run, writes = _run(FakeClock())
    with run.task("https://zz.invalid/feed.zip?apikey=ZZSECRET99"):
        pass
    run.done("<img src=x>", {"status": "skipped"})
    assert writes[0]["current"][0]["key"] == "task"
    assert run.detail["failed_keys"] == ["task"]


@pytest.mark.parametrize(
    ("raised", "state"),
    [
        (None, refresh_progress.FINISHED),
        (RuntimeError, refresh_progress.FAILED),
        (asyncio.CancelledError, refresh_progress.INTERRUPTED),
    ],
)
def test_the_end_state_follows_how_the_body_ended(
    monkeypatch: pytest.MonkeyPatch, raised: type[BaseException] | None, state: str
) -> None:
    run, _writes = _run(FakeClock())
    ended: list[str] = []
    monkeypatch.setattr(run, "finish", lambda s, **_k: ended.append(s))

    async def body() -> None:
        async with run:
            if raised is not None:
                raise raised

    refresh = body()
    if raised is None:
        asyncio.run(refresh)
    else:
        with pytest.raises(raised):
            asyncio.run(refresh)
    assert ended == [state]


def test_the_heartbeat_beats_while_the_body_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _writes = _run(FakeClock())
    beats: list[int] = []
    monkeypatch.setattr(refresh_progress, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(run, "beat", lambda: beats.append(1))
    monkeypatch.setattr(run, "finish", lambda *_a, **_k: None)

    async def body() -> None:
        async with run:
            await asyncio.sleep(0.2)
        count = len(beats)
        await asyncio.sleep(0.05)
        assert len(beats) == count  # stopped with the body

    asyncio.run(body())
    assert len(beats) >= 3


def test_a_failed_write_never_stops_the_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise OSError("stand-in")

    run = refresh_progress.RefreshRun(
        "zz",
        refresh_progress.OSM,
        uuid.uuid4(),
        refresh_progress._initial_detail(1, None),
        factory=broken,
    )
    with run.task("osm_pbf"):
        run.bytes_received(99, 999)
    run.done("osm_pbf", {"status": "fetched"})
    run.finish(refresh_progress.FINISHED)


def test_the_refusal_names_the_running_refresh() -> None:
    started = datetime(2026, 10, 15, 9, 59, tzinfo=UTC)
    detail = refresh_progress.RefreshRunning(refresh_progress.OSM, started).detail()
    assert detail["error"] == "refresh_already_running"
    assert detail["scope"] == "osm"
    assert detail["started_at"] == "2026-10-15T09:59:00+00:00"
    assert "An OSM refresh" in detail["message"]
    assert "2026-10-15T09:59:00+00:00" in detail["message"]


# ── one provider alone ───────────────────────────────────────────────────


def test_a_provider_alone_is_named_in_its_refusals() -> None:
    started = datetime(2026, 10, 15, 9, 59, tzinfo=UTC)
    same = refresh_progress.RefreshRunning("ZZRAIL", started).detail()
    assert (same["error"], same["scope"], same["provider_id"]) == (
        "refresh_already_running",
        "provider",
        "ZZRAIL",
    )
    assert same["message"].startswith("A refresh of provider ZZRAIL is already running")
    assert "2026-10-15T09:59:00+00:00" in same["message"]
    blocks_whole = refresh_progress.RefreshRunning(
        "ZZRAIL", started, refresh_progress.PROVIDERS
    ).detail()
    assert (blocks_whole["error"], blocks_whole["provider_id"]) == (
        "provider_refresh_running",
        "ZZRAIL",
    )
    assert "Refresh providers downloads that provider too" in blocks_whole["message"]
    whole_blocks = refresh_progress.RefreshRunning(
        refresh_progress.PROVIDERS, None, "ZZRAIL"
    ).detail()
    assert (whole_blocks["error"], whole_blocks["scope"], whole_blocks["provider_id"]) == (
        "refresh_already_running",
        "providers",
        None,
    )
    assert whole_blocks["started_at"] is None
    assert "started at" not in whole_blocks["message"]


@pytest.mark.parametrize("pid", ["ZZ", "ZZRAIL", "ZZ-99_RAIL", "Z" * 16])
def test_a_feed_id_is_its_own_scope(pid: str) -> None:
    assert refresh_progress.provider_scope(pid) == pid
    assert refresh_progress.is_provider_scope(pid) is True
    assert refresh_progress.refresh_scope(pid) == pid


@pytest.mark.parametrize(
    "value", ["providers", "osm", "Z", "Z" * 17, "zzrail", "ZZ RAIL", "ZZ\u202eX", "ZZ\n", None]
)
def test_anything_else_is_never_a_provider_scope(value: Any) -> None:
    assert refresh_progress.is_provider_scope(value) is False
    with pytest.raises(ValueError, match="not a feed id"):
        refresh_progress.provider_scope(value)


def test_the_feed_id_rule_is_the_configs() -> None:
    from app import ingestion

    assert refresh_progress._FEED_ID.pattern == ingestion._FEED_ID_RE.pattern.strip("^$")
    assert refresh_progress.refresh_scope(None) == refresh_progress.PROVIDERS


def test_a_claim_of_an_unknown_scope_is_refused_before_the_database() -> None:
    def no_database() -> Any:
        raise AssertionError("the database was opened")

    with pytest.raises(ValueError, match="not a feed id"):
        refresh_progress.claim("zz", "zzjunk", 1, factory=no_database)


@pytest.mark.parametrize(
    ("scope", "follow_up", "extract"),
    [
        (refresh_progress.PROVIDERS, True, {"state": "off"}),
        ("ZZRAIL", True, {"state": "off"}),
        ("ZZRAIL", False, None),
        (refresh_progress.OSM, None, None),
    ],
)
def test_the_end_writes_the_follow_up_only_when_a_pass_follows(
    monkeypatch: pytest.MonkeyPatch, scope: str, follow_up: bool | None, extract: Any
) -> None:
    from app import station_module

    monkeypatch.setattr(station_module, "enabled", lambda: False)
    run = refresh_progress.RefreshRun(
        "zz", scope, uuid.uuid4(), refresh_progress._initial_detail(1, None)
    )
    if follow_up is not None:
        run.follow_up = follow_up
    written: list[Any] = []
    run._execute = lambda statement, **_k: written.append(  # type: ignore[method-assign]
        statement.compile().params
    )
    run.finish(refresh_progress.FINISHED)
    [params] = written
    assert params.get("extract") == extract


def test_the_follow_up_is_fenced_by_its_refresh_scope() -> None:
    whole = refresh_progress.ExtractFollowUp("zz", uuid.uuid4())
    card = refresh_progress.ExtractFollowUp("zz", uuid.uuid4(), scope="ZZRAIL")
    assert (whole.scope, card.scope) == (refresh_progress.PROVIDERS, "ZZRAIL")


def test_the_public_record_names_the_provider_alone() -> None:
    started = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    def row(scope: str) -> Any:
        return SimpleNamespace(
            scope=scope,
            state="finished",
            started_at=started,
            finished_at=started,
            heartbeat_at=started,
            detail={},
            extract={},
        )

    card = refresh_progress._public(row("ZZRAIL"), started)
    assert (card["scope"], card["provider_id"]) == ("provider", "ZZRAIL")
    whole = refresh_progress._public(row("providers"), started)
    assert (whole["scope"], whole["provider_id"]) == ("providers", None)


# ── what a reader gets ───────────────────────────────────────────────────


def test_the_public_record_holds_only_known_shapes() -> None:
    started = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    row = SimpleNamespace(
        scope="providers",
        state="running",
        started_at=started,
        finished_at=None,
        heartbeat_at=started,
        detail={
            "total": 9,
            "done": -1,
            "fetched": True,
            "unchanged": "9",
            "failed": 2,
            "phase": "downloading <b>",
            "current": [{"key": "https://zz.invalid/?apikey=ZZSECRET99", "bytes": 99}],
            "failed_keys": [LABEL, "reason: HTTP 404 https://zz.invalid"],
            "error": "runtimeerror",
            "unexpected": "https://zz.invalid",
        },
        extract={
            "state": "running",
            "total": 9,
            "sent": 1,
            "reasons": {"budget": 2, "free text <x>": 1},
            "current": "ZZRAIL",
            "started_at": "2026-10-15T09:00:00+00:00",
        },
    )
    public = refresh_progress._public(row, started + timedelta(seconds=99))  # type: ignore[arg-type]
    assert public["elapsed_seconds"] == 99
    assert (public["total"], public["done"], public["fetched"], public["unchanged"]) == (9, 0, 0, 0)
    assert public["failed"] == 2
    assert public["phase"] == "other"
    assert public["current"] == [
        {
            "key": "task",
            "bytes": 99,
            "expected": None,
            "step": None,
            "scanned": None,
            "scan_total": None,
            "elapsed_seconds": None,
        }
    ]
    assert public["failed_keys"] == [LABEL, "task"]
    assert "unexpected" not in public
    assert public["extract"]["reasons"] == {"budget": 2, "other": 1}
    assert public["extract"]["current"] == "ZZRAIL"
    assert public["extract"]["handled"] == 0
    assert "ZZSECRET99" not in repr(public)
    assert "zz.invalid" not in repr(public)


def test_the_extract_follow_up_counts_states_and_reason_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow = refresh_progress.ExtractFollowUp("zz", uuid.uuid4())
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(follow, "_write", lambda: written.append(dict(follow.state)))
    follow.outcome(up.FeedOutcome("ZZ0", up.SENT))  # before started: ignored
    assert written == []
    follow.started(3)
    follow.outcome(up.FeedOutcome("ZZ0", up.SKIPPED, up.BUDGET))
    follow.outcome(up.FeedOutcome("ZZ1", up.REFUSED, "Row Text <x>"))
    follow.outcome(up.FeedOutcome("ZZ2", "strange", None))
    follow.outcome(up.FeedOutcome("ZZ3", up.SENT, "zz_word"))  # no reason kept for a success
    follow.finished("weird")
    final = written[-1]
    assert final["state"] == "error"
    assert (final["handled"], final["skipped"], final["refused"]) == (4, 2, 1)
    assert final["reasons"] == {"budget": 1, "other": 1}


# ── the download hook of app/feed_fetch.py ───────────────────────────────


def _client(response: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: response))


def _stream(tmp_path: Path, response: httpx.Response) -> list[tuple[int, int | None]]:
    seen: list[tuple[int, int | None]] = []

    async def go() -> None:
        token = feed_fetch.download_progress.set(lambda r, e: seen.append((r, e)))
        try:
            async with _client(response) as client:
                await feed_fetch._download(
                    client,
                    "https://zz.invalid/f",
                    {},
                    tmp_path / "f",
                    state_url="https://zz.invalid/f",
                )
        finally:
            feed_fetch.download_progress.reset(token)

    asyncio.run(go())
    return seen


def test_the_download_reports_bytes_and_the_announced_size(tmp_path: Path) -> None:
    body = b"9" * (2 * 1024 * 1024 + 99)
    seen = _stream(tmp_path, httpx.Response(200, content=body))
    assert seen == [(1024 * 1024, len(body)), (2 * 1024 * 1024, len(body)), (len(body), len(body))]


def test_a_content_encoded_body_has_no_expected_size(tmp_path: Path) -> None:
    import gzip

    async def body() -> AsyncIterator[bytes]:
        # Streamed as a server sends it: a gzip body is read raw and unpacked
        # by feed_fetch itself (a bytes body would be read and decoded at once).
        yield gzip.compress(b"9" * 999)

    response = httpx.Response(200, content=body(), headers={"Content-Encoding": "gzip"})
    assert _stream(tmp_path, response) == [(999, None)]


def test_a_broken_hook_never_stops_a_download(tmp_path: Path) -> None:
    def broken(_r: int, _e: int | None) -> None:
        raise RuntimeError("stand-in")

    async def go() -> int:
        token = feed_fetch.download_progress.set(broken)
        try:
            async with _client(httpx.Response(200, content=b"9" * 99)) as client:
                download = await feed_fetch._download(
                    client,
                    "https://zz.invalid/f",
                    {},
                    tmp_path / "f",
                    state_url="https://zz.invalid/f",
                )
                return download.size
        finally:
            feed_fetch.download_progress.reset(token)

    assert asyncio.run(go()) == 99


def test_without_a_running_task_nothing_is_reported(tmp_path: Path) -> None:
    assert feed_fetch.download_progress.get() is None

    async def go() -> None:
        async with _client(httpx.Response(200, content=b"9" * 99)) as client:
            await feed_fetch._download(
                client, "https://zz.invalid/f", {}, tmp_path / "f", state_url="https://zz.invalid/f"
            )

    asyncio.run(go())


# ── repair round 1: lost lease, finishing, writer thread, follow-up beat ──


def test_a_refresh_that_lost_its_lease_starts_no_more_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.api.admin import sessions as sessions_api

    run, _writes = _run(FakeClock(), total=3)
    started: list[str] = []

    async def one(_client: Any, _db: Any, _sid: str, _staging: Path, task: Any) -> dict:
        started.append(task.label)
        run._lost = True  # another refresh took the row over meanwhile
        return {"status": "fetched", "key": task.label}

    monkeypatch.setattr(sessions_api, "_refresh_one_task", one)
    tasks = [
        sessions_api._RefreshTask(f"provider[ZZ{i}].timetable(gtfs)", "GTFS", "u", None, None)
        for i in range(3)
    ]
    derived = {
        "sources": {
            "providers": [
                {"id": "ZZXB", "timetable": {"format": "gtfs", "source": "cross_border_filter"}}
            ]
        }
    }
    fetched, _unchanged, _skipped = asyncio.run(
        sessions_api._gather_refresh_outcomes(None, "zz", tmp_path, derived, tasks, run)  # type: ignore[arg-type]
    )
    assert started == ["provider[ZZ0].timetable(gtfs)"]
    assert len(fetched) == 1


def test_a_beat_after_the_end_is_not_a_lost_lease(caplog: pytest.LogCaptureFixture) -> None:
    run = refresh_progress.RefreshRun(
        "zz", refresh_progress.OSM, uuid.uuid4(), refresh_progress._initial_detail(1, None)
    )
    executed: list[Any] = []
    run._execute = lambda statement, **_k: executed.append(statement)  # type: ignore[method-assign]
    run._finishing = True
    run.beat()
    assert executed == []

    class NoRow:
        rowcount = 0

        def __enter__(self) -> NoRow:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def execute(self, _statement: Any) -> NoRow:
            return self

        def commit(self) -> None:
            return None

    quiet = refresh_progress.RefreshRun(
        "zz",
        refresh_progress.OSM,
        uuid.uuid4(),
        refresh_progress._initial_detail(1, None),
        factory=NoRow,
    )
    quiet._finishing = True
    with caplog.at_level("WARNING", logger="app.refresh_progress"):
        quiet._execute("stand-in", beat=True)
    assert not quiet.lost
    assert "lease_lost" not in caplog.text
    quiet._execute("stand-in")  # a progress write that finds no row: lost
    assert quiet.lost


def test_inside_the_context_writes_leave_the_event_loop_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    run = refresh_progress.RefreshRun(
        "zz", refresh_progress.PROVIDERS, uuid.uuid4(), refresh_progress._initial_detail(2, None)
    )
    loop_thread = threading.get_ident()
    seen: list[tuple[bool, Any]] = []

    def execute(statement: Any, **_k: Any) -> None:
        values = statement.compile().params
        seen.append((threading.get_ident() == loop_thread, values.get("state")))

    monkeypatch.setattr(run, "_execute", execute)
    monkeypatch.setattr(refresh_progress, "_extract_on_finish", lambda _state: {})

    async def body() -> None:
        async with run:
            run.done("provider[ZZ1].timetable(gtfs)", {"status": "fetched"})
            run.done("provider[ZZ2].timetable(gtfs)", {"status": "unchanged"})

    asyncio.run(body())
    # Two progress writes, then the end, none on the loop's thread.
    assert [state for _on_loop, state in seen] == [None, None, "finished"]
    assert not any(on_loop for on_loop, _state in seen)


def test_the_follow_up_beats_while_its_pass_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    monkeypatch.setattr(refresh_progress, "EXTRACT_HEARTBEAT_SECONDS", 0.01)
    follow = refresh_progress.ExtractFollowUp("zz", uuid.uuid4())
    beats: list[str] = []
    monkeypatch.setattr(follow, "_write", lambda: beats.append(follow.state["state"]))
    follow.started(1)
    _time.sleep(0.2)
    follow.finished("finished")
    count = len(beats)
    _time.sleep(0.05)
    assert count >= 5
    assert len(beats) == count  # the beat stopped with the pass
    assert beats[-1] == "finished"


@pytest.mark.parametrize(
    ("state", "minutes", "shown"),
    [
        ("queued", 34, "queued"),
        ("queued", 36, "interrupted"),
        ("running", 4, "running"),
        ("running", 6, "interrupted"),
        ("finished", 999, "finished"),
        ("queued", None, "interrupted"),
    ],
)
def test_a_silent_follow_up_reads_interrupted(state: str, minutes: int | None, shown: str) -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    extract: dict[str, Any] = {"state": state}
    if minutes is not None:
        extract["updated_at"] = (now - timedelta(minutes=minutes)).isoformat()
    assert refresh_progress._public_extract(extract, now)["state"] == shown  # type: ignore[index]


def test_a_resumed_download_reports_the_full_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_feed_fetch_resume import BODY, Server

    monkeypatch.setattr(feed_fetch, "RESUME_DELAYS", (0.0,) * 8)
    server = Server(cuts=[2 * 1024 * 1024 + 99])
    seen: list[tuple[int, int | None]] = []

    async def go() -> None:
        token = feed_fetch.download_progress.set(lambda r, e: seen.append((r, e)))
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
                await feed_fetch._download(
                    client,
                    "https://zz.invalid/f",
                    {},
                    tmp_path / "f",
                    state_url="https://zz.invalid/f",
                )
        finally:
            feed_fetch.download_progress.reset(token)

    asyncio.run(go())
    assert any(r.headers.get("range") for r in server.requests)  # it was resumed
    assert {e for _r, e in seen} == {len(BODY)}
    assert seen[-1] == (len(BODY), len(BODY))
    received = [r for r, _e in seen]
    assert received == sorted(received)  # never back to 0 on a resume


# ── steps after the download (production 15 Oct: "100 %" for 20 minutes) ──

_NETEX_XML = (
    b'<?xml version="1.0"?><PublicationDelivery xmlns="http://www.netex.org.uk/netex" '
    b'version="1.0"><ParticipantRef>ZZ</ParticipantRef><dataObjects>'
    + b'<ServiceJourney id="zz99"/>' * 99
    + b"</dataObjects></PublicationDelivery>"
)


def _netex_zip(members: int = 2) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(members):
            z.writestr(f"zz{i}.xml", _NETEX_XML)
    return buf.getvalue()


def test_a_task_goes_through_its_steps_one_write_each() -> None:
    run, writes = _run(FakeClock())
    with run.task(LABEL):
        assert writes[-1]["current"][0]["step"] == feed_fetch.STEP_DOWNLOADING
        assert writes[-1]["current"][0]["started_at"]
        for step in (feed_fetch.STEP_VERIFYING, feed_fetch.STEP_CHECKING_STRUCTURE):
            run.step(step)
            assert writes[-1]["current"][0]["step"] == step
        run.step(feed_fetch.STEP_CHECKING_STRUCTURE)  # the same step again: no write
        run.step(feed_fetch.STEP_STORING)
    assert [w["current"][0]["step"] for w in writes] == [
        feed_fetch.STEP_DOWNLOADING,
        feed_fetch.STEP_VERIFYING,
        feed_fetch.STEP_CHECKING_STRUCTURE,
        feed_fetch.STEP_STORING,
    ]
    # No byte yet (a NAP lookup may come first); the field stays beside the later steps.
    assert writes[0]["current"][0]["bytes"] is None
    assert writes[-1]["current"][0]["bytes"] is None
    run.done(LABEL, {"status": "fetched"})
    assert writes[-1]["current"] == []


def test_an_unknown_step_or_a_step_outside_a_task_writes_nothing() -> None:
    run, writes = _run(FakeClock())
    run.step(feed_fetch.STEP_VERIFYING)
    assert writes == []
    with run.task(LABEL):
        run.step("zz_not_a_step")
    assert len(writes) == 1
    assert writes[0]["current"][0]["step"] == feed_fetch.STEP_DOWNLOADING


def test_the_structure_check_progress_is_throttled_like_the_bytes() -> None:
    clock = FakeClock()
    run, writes = _run(clock)
    total = 99 * 1000 * 1000
    with run.task(LABEL):
        run.step(feed_fetch.STEP_CHECKING_STRUCTURE)
        first = len(writes)
        for scanned in range(0, total + 1, 99_000):  # 1001 reads, no time passing
            run.step(feed_fetch.STEP_CHECKING_STRUCTURE, scanned, total)
    scans = writes[first:]
    assert len(scans) == 20  # one per 5 % step, never one per read
    assert [w["current"][0]["scanned"] * 100 // total for w in scans] == list(range(5, 101, 5))
    assert scans[-1]["current"][0]["scan_total"] == total
    # Without a known total: every two seconds.
    run2, writes2 = _run(clock)
    with run2.task(LABEL):
        run2.step(feed_fetch.STEP_CHECKING_STRUCTURE)
        first = len(writes2)
        for i in range(1, 100):
            clock.now += 0.1
            run2.step(feed_fetch.STEP_CHECKING_STRUCTURE, i * 99, None)
    assert len(writes2) - first == 4


def test_the_downloads_bytes_throttle_does_not_starve_the_next_step() -> None:
    """The structure check starts from 0 % with a fresh throttle: a download
    that ended at 100 % does not hide its first 5 % step."""
    run, writes = _run(FakeClock())
    with run.task(LABEL):
        run.bytes_received(99, 99)
        run.step(feed_fetch.STEP_CHECKING_STRUCTURE)
        before = len(writes)
        run.step(feed_fetch.STEP_CHECKING_STRUCTURE, 5, 99)
    assert len(writes) == before + 1


def test_the_public_record_shows_the_step_and_how_long_the_task_runs() -> None:
    started = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    item = {
        "key": LABEL,
        "bytes": 99,
        "expected": 99,
        "step": feed_fetch.STEP_CHECKING_STRUCTURE,
        "scanned": 9,
        "scan_total": 99,
        "started_at": "2026-10-15T09:00:00+00:00",
    }
    public = refresh_progress._public_current([item], started + timedelta(minutes=18))
    assert public == [
        {
            "key": LABEL,
            "bytes": 99,
            "expected": 99,
            "step": "checking_structure",
            "scanned": 9,
            "scan_total": 99,
            "elapsed_seconds": 18 * 60,
        }
    ]
    odd = {**item, "step": "<b>", "scanned": -9, "started_at": "not a time"}
    shown = refresh_progress._public_current([odd], started)[0]
    assert (shown["step"], shown["scanned"], shown["elapsed_seconds"]) == (None, 0, None)


def test_fetch_reports_verifying_then_the_structure_check_from_a_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from app import netex_structure

    monkeypatch.setattr(netex_structure, "_CHUNK", 999)
    body = _netex_zip()
    calls: list[tuple[str, int | None, int | None, bool]] = []
    loop_thread: list[int] = []

    def report(step: str, done: int | None, total: int | None) -> None:
        calls.append((step, done, total, threading.get_ident() == loop_thread[0]))

    async def go() -> feed_fetch.FetchResult:
        loop_thread.append(threading.get_ident())
        token = feed_fetch.task_step.set(report)
        try:
            async with _client(httpx.Response(200, content=body)) as client:
                return await feed_fetch.fetch_validated(
                    client,
                    fetch_url="https://zz.invalid/f",
                    state_url="https://zz.invalid/f",
                    extra_headers={},
                    kind="NeTEx-EPIP",
                    staging=tmp_path,
                    base_name="zz-netex",
                    suffix=".zip",
                    previous={},
                    have_current=False,
                )
        finally:
            feed_fetch.task_step.reset(token)

    result = asyncio.run(go())
    assert result.status == "fetched"
    total = 2 * len(_NETEX_XML)  # the uncompressed sizes of the zip directory
    assert calls[0][:3] == (feed_fetch.STEP_VERIFYING, None, None)
    assert calls[1][:3] == (feed_fetch.STEP_CHECKING_STRUCTURE, None, None)
    scans = calls[2:]
    assert scans[0][:3] == (feed_fetch.STEP_CHECKING_STRUCTURE, 0, total)
    assert scans[-1][:3] == (feed_fetch.STEP_CHECKING_STRUCTURE, total, total)
    assert len(scans) > 3  # reported per read: more than the start and the end
    done = [d for _s, d, _t, _l in scans]
    assert done == sorted(done)
    # The scan runs in a worker thread, never on the event loop.
    assert not any(on_loop for *_rest, on_loop in scans)


def test_an_unchanged_feed_reports_no_step_after_its_download(tmp_path: Path) -> None:
    calls: list[str] = []
    body = _netex_zip()
    import hashlib

    async def go() -> feed_fetch.FetchResult:
        token = feed_fetch.task_step.set(lambda step, _d, _t: calls.append(step))
        try:
            async with _client(httpx.Response(200, content=body)) as client:
                return await feed_fetch.fetch_validated(
                    client,
                    fetch_url="https://zz.invalid/f",
                    state_url="https://zz.invalid/f",
                    extra_headers={},
                    kind="NeTEx-EPIP",
                    staging=tmp_path,
                    base_name="zz-netex",
                    suffix=".zip",
                    previous={"sha256": hashlib.sha256(body).hexdigest()},
                    have_current=True,
                )
        finally:
            feed_fetch.task_step.reset(token)

    assert asyncio.run(go()).status == "unchanged"
    assert calls == []


def test_a_broken_step_hook_never_stops_the_refresh(caplog: pytest.LogCaptureFixture) -> None:
    def broken(_step: str, _done: int | None, _total: int | None) -> None:
        raise RuntimeError("stand-in")

    token = feed_fetch.task_step.set(broken)
    try:
        with caplog.at_level("WARNING", logger="app.feed_fetch"):
            feed_fetch.report_step(feed_fetch.STEP_STORING)
    finally:
        feed_fetch.task_step.reset(token)
    assert "task step not reported: RuntimeError" in caplog.text
    feed_fetch.report_step(feed_fetch.STEP_STORING)  # no running task: nothing


def test_the_task_context_routes_steps_to_the_run_and_restores_them() -> None:
    run, writes = _run(FakeClock())
    assert feed_fetch.task_step.get() is None
    with run.task(LABEL):
        feed_fetch.report_step(feed_fetch.STEP_STORING)
    assert writes[-1]["current"][0]["step"] == feed_fetch.STEP_STORING
    assert feed_fetch.task_step.get() is None
    assert feed_fetch.download_progress.get() is None


def test_storing_is_reported_and_the_copy_leaves_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from app import feed_resolvers
    from app.api.admin import sessions as sessions_api

    monkeypatch.setattr(sessions_api.settings, "inbox_dir", tmp_path)
    monkeypatch.setattr(feed_resolvers, "_validate_safe_http_url", lambda url: url)
    where: dict[str, bool] = {}
    loop_thread: list[int] = []
    steps: list[str] = []

    def place(*_args: Any, **_kwargs: Any) -> bool:
        where["place_on_loop"] = threading.get_ident() == loop_thread[0]
        where["storing_reported_first"] = steps[-1:] == [feed_fetch.STEP_STORING]
        return True

    def queue_rebuild(db: Any, **_kwargs: Any) -> None:
        where["queue_on_loop"] = threading.get_ident() == loop_thread[0]
        where["db"] = db == "zz-db"

    monkeypatch.setattr(sessions_api.ingestion, "place", place)
    monkeypatch.setattr(sessions_api.ingestion, "queue_rebuild", queue_rebuild)
    staging = tmp_path / "zz" / "_staging"
    staging.mkdir(parents=True)
    task = sessions_api._RefreshTask(LABEL, "NeTEx-EPIP", "https://zz.invalid/f.zip", None, None)

    async def go() -> dict[str, Any]:
        loop_thread.append(threading.get_ident())
        token = feed_fetch.task_step.set(lambda step, _d, _t: steps.append(step))
        try:
            async with _client(httpx.Response(200, content=_netex_zip())) as client:
                return await sessions_api._download_task(
                    client,
                    "zz-db",  # type: ignore[arg-type]
                    "zz",
                    staging,
                    task,
                    task.url,
                )
        finally:
            feed_fetch.task_step.reset(token)

    outcome = asyncio.run(go())
    assert outcome["status"] == "fetched"
    assert list(dict.fromkeys(steps)) == [
        feed_fetch.STEP_VERIFYING,
        feed_fetch.STEP_CHECKING_STRUCTURE,
        feed_fetch.STEP_STORING,
    ]
    assert where == {
        "place_on_loop": False,
        "storing_reported_first": True,
        "queue_on_loop": True,
        "db": True,
    }


def test_no_rebuild_is_queued_when_the_kind_needs_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.admin import sessions as sessions_api

    queued: list[str] = []
    monkeypatch.setattr(sessions_api.ingestion, "place", lambda *_a, **_k: False)
    monkeypatch.setattr(
        sessions_api.ingestion, "queue_rebuild", lambda *_a, **k: queued.append(k["kind"])
    )
    rebuild = asyncio.run(
        sessions_api._dispatch_off_loop(None, tmp_path / "f", "SNCF-MCT", "zz", None)  # type: ignore[arg-type]
    )
    assert rebuild is False
    assert queued == []


# ── repair round 1 ───────────────────────────────────────────────────────


def test_a_finishing_run_takes_no_step() -> None:
    """A copy job that outlives its cancelled request reports nothing once
    the refresh is finishing: no write, so no spurious lease_lost."""
    run, writes = _run(FakeClock())
    with run.task(LABEL):
        run._finishing = True
        run.step(feed_fetch.STEP_STORING)
    assert len(writes) == 1
    assert run.detail["current"][0]["step"] == feed_fetch.STEP_DOWNLOADING


@pytest.mark.parametrize(
    ("state", "shown"),
    [
        (refresh_progress.RUNNING, 1),
        (refresh_progress.INTERRUPTED, 0),
        (refresh_progress.FAILED, 0),
    ],
)
def test_only_a_running_record_has_a_current_task(state: str, shown: int) -> None:
    """An interrupted record keeps the task it died in; shown as "Now: …
    for 3 d" it would grow forever."""
    started = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    row = SimpleNamespace(
        scope="providers",
        state=state,
        started_at=started,
        finished_at=None if state == refresh_progress.RUNNING else started,
        heartbeat_at=started,
        detail={
            "total": 1,
            "current": [{"key": LABEL, "step": "storing", "started_at": started.isoformat()}],
        },
        extract={},
    )
    public = refresh_progress._public(row, started + timedelta(days=3))  # type: ignore[arg-type]
    assert len(public["current"]) == shown


def test_the_task_time_is_measured_on_the_web_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """started_at is written with the web process's clock: the reader
    measures with the same clock, never PostgreSQL's `now`."""
    run, _writes = _run(FakeClock())
    with run.task(LABEL):
        pass
    started = datetime.fromisoformat(run.detail["current"][0]["started_at"])
    row = SimpleNamespace(
        scope="providers",
        state=refresh_progress.RUNNING,
        started_at=started,
        finished_at=None,
        heartbeat_at=started,
        detail=run.detail,
        extract={},
    )
    db_clock_far_ahead = started + timedelta(hours=9)
    public = refresh_progress._public(row, db_clock_far_ahead)  # type: ignore[arg-type]
    assert public["current"][0]["elapsed_seconds"] < 60


def _slow_place(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[str]]:
    import threading

    from app import ingestion

    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    real = ingestion.place

    def place(path: Path, *args: Any, **kwargs: Any) -> bool:
        entered.set()
        assert release.wait(10)
        calls.append("placed" if path.exists() else "source gone mid-copy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ingestion, "place", place)
    return entered, release, calls


def test_a_request_cancelled_during_the_copy_still_ends_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client went away while the file was copied into the slot: the
    copy is not cut, its source is removed only after it, the caller's file
    bookkeeping runs, and the rebuild is queued with a session of its own."""
    import threading

    from app import ingestion
    from app.api.admin import sessions as sessions_api

    monkeypatch.setattr(ingestion.settings, "inbox_dir", tmp_path / "inbox")
    entered, release, calls = _slow_place(monkeypatch)
    queued = threading.Event()
    queued_with: list[tuple[str, str]] = []

    def own_session(sid: str, kind: str) -> None:
        queued_with.append((sid, kind))
        queued.set()

    monkeypatch.setattr(sessions_api, "_queue_rebuild_own_session", own_session)
    monkeypatch.setattr(
        ingestion, "queue_rebuild", lambda *_a, **_k: pytest.fail("not on the request's session")
    )
    source = tmp_path / "zz-staged.zip"
    source.write_bytes(b"zz new")
    after: list[str] = []

    async def go() -> None:
        copy = asyncio.ensure_future(
            sessions_api._dispatch_off_loop(
                "zz-closed-db",  # type: ignore[arg-type]
                source,
                "GTFS",
                "zz",
                "zz.zip",
                after=lambda: after.append("state saved"),
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        copy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await copy
        assert source.exists()  # the cancelled request did not pull it away
        release.set()
        for _ in range(500):
            if queued.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(go())
    assert calls == ["placed"]
    assert after == ["state saved"]
    assert queued_with == [("zz", "GTFS")]
    assert not source.exists()
    assert (tmp_path / "inbox" / "zz" / "gtfs" / "zz.zip").read_bytes() == b"zz new"


def test_a_failed_copy_removes_its_source_and_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import ingestion
    from app.api.admin import sessions as sessions_api

    def broken(*_a: Any, **_k: Any) -> bool:
        raise OSError("stand-in disk full")

    monkeypatch.setattr(ingestion, "place", broken)
    queued: list[str] = []
    monkeypatch.setattr(ingestion, "queue_rebuild", lambda *_a, **k: queued.append(k["kind"]))
    source = tmp_path / "zz.zip"
    source.write_bytes(b"zz")
    after: list[str] = []

    async def go() -> None:
        await sessions_api._dispatch_off_loop(
            None,  # type: ignore[arg-type]
            source,
            "GTFS",
            "zz",
            None,
            after=lambda: after.append("x"),
        )

    failing = go()
    with pytest.raises(OSError, match="stand-in"):
        asyncio.run(failing)
    assert not source.exists()
    assert (queued, after) == ([], [])


@pytest.mark.parametrize(
    ("config", "ids"),
    [
        (
            {
                "sources": {
                    "providers": [{"id": "ZZRAIL"}, {"id": "ZZBUS"}, {"x": 1}, "ZZ", {"id": 9}]
                }
            },
            {"ZZRAIL", "ZZBUS"},
        ),
        ({"sources": {"providers": "ZZRAIL"}}, set()),
        ({"sources": []}, set()),
        (None, set()),
    ],
)
def test_the_shown_card_records_follow_the_saved_providers(config: Any, ids: set[str]) -> None:
    assert refresh_progress.provider_ids(config) == ids
