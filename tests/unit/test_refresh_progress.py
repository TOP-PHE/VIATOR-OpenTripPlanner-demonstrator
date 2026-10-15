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
    assert bytes_writes[-1]["current"][0] == {"key": LABEL, "bytes": expected, "expected": expected}


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

    if raised is None:
        asyncio.run(body())
    else:
        with pytest.raises(raised):
            asyncio.run(body())
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
    assert public["current"] == [{"key": "task", "bytes": 99, "expected": None}]
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
                await feed_fetch._stream_to(client, "https://zz.invalid/f", {}, tmp_path / "f")
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

    response = httpx.Response(
        200, content=gzip.compress(b"9" * 999), headers={"Content-Encoding": "gzip"}
    )
    assert _stream(tmp_path, response) == [(999, None)]


def test_a_broken_hook_never_stops_a_download(tmp_path: Path) -> None:
    def broken(_r: int, _e: int | None) -> None:
        raise RuntimeError("stand-in")

    async def go() -> int:
        token = feed_fetch.download_progress.set(broken)
        try:
            async with _client(httpx.Response(200, content=b"9" * 99)) as client:
                _status, _h, _sha, size = await feed_fetch._stream_to(
                    client, "https://zz.invalid/f", {}, tmp_path / "f"
                )
                return size
        finally:
            feed_fetch.download_progress.reset(token)

    assert asyncio.run(go()) == 99


def test_without_a_running_task_nothing_is_reported(tmp_path: Path) -> None:
    assert feed_fetch.download_progress.get() is None

    async def go() -> None:
        async with _client(httpx.Response(200, content=b"9" * 99)) as client:
            await feed_fetch._stream_to(client, "https://zz.invalid/f", {}, tmp_path / "f")

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
