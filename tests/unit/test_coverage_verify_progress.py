"""The ÖBB check (verify sweep) saves each answer as it comes and records
its progress; the page shows it.

Without a database: a fake session records each statement. Pinned here:

- `verify_progress.SweepWriter` writes the run's progress and the queued
  answers in one transaction, fenced on the run being `running` (and held by
  its executor when the id is known), every few answers or seconds; a write
  that finds the run gone stops it; a failed write keeps its answers for
  the next; every field the sweep fills on a row is saved;
- the sweep hands every cell to the writer, skips the cells already
  answered (a resume), and stops asking ÖBB once the writer has stopped;
- the API's "verifying" status and time-left estimate;
- the page's progress line, pill hint, run-list bar, modal texts and the
  checkbox hint.

The database side (another session sees the answers during the check, a
resume asks only the cells left, a SQL cancel stops the check) is
tests/integration/test_coverage_verify_progress.py.

Invented values only (ZZ hubs, numbers with 99).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import NetworkCoverageResult
from app.network_coverage import external_verify, runner, verify_progress

RUN_ID = uuid.UUID("99999999-9999-4999-8999-999999999901")
EXEC = "zz-host:99:aaaa"
TEMPLATE = (
    Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "network_coverage.html"
)


# ─────────────────────────────── fakes ───────────────────────────────


class _Db:
    """A session that records statements; the fenced UPDATE of the run
    returns the run id while `held`, else nothing (the run is gone)."""

    def __init__(self, log: list[Any], *, held: bool = True, fail: Exception | None = None):
        self.log = log
        self.held = held
        self.fail = fail

    def __enter__(self) -> _Db:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, stmt: Any, params: Any = None) -> Any:
        self.log.append(("execute", stmt, params))
        if self.fail is not None and "network_coverage_results" in str(stmt):
            raise self.fail
        result = MagicMock()
        result.scalar_one_or_none.return_value = RUN_ID if self.held else None
        return result

    def commit(self) -> None:
        self.log.append(("commit",))

    def rollback(self) -> None:
        self.log.append(("rollback",))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _writer(
    log: list[Any], *, held: bool = True, fail: Exception | None = None, **kw: Any
) -> tuple[verify_progress.SweepWriter, _Clock]:
    clock = _Clock()
    w = verify_progress.SweepWriter(
        RUN_ID,
        kw.pop("executor_id", EXEC),
        session_factory=lambda: _Db(log, held=held, fail=fail),
        clock=clock,
        **kw,
    )
    return w, clock


def _row(origin: str = "zz-a", dest: str = "zz-b", **fields: Any) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.origin_hub_id = origin
    row.dest_hub_id = dest
    row.status = fields.pop("status", "ok")
    row.journey_search_id = None
    for name in verify_progress.SAVED_FIELDS:
        setattr(row, name, None)
    for name, value in fields.items():
        setattr(row, name, value)
    return row


def _run_updates(log: list[Any]) -> list[Any]:
    return [e[1] for e in log if e[0] == "execute" and "network_coverage_runs" in str(e[1])]


def _row_writes(log: list[Any]) -> list[list[dict[str, Any]]]:
    return [e[2] for e in log if e[0] == "execute" and "network_coverage_results" in str(e[1])]


# ─────────────────────────────── the writer ───────────────────────────────


def test_start_records_the_total_and_the_cells_already_answered() -> None:
    log: list[Any] = []
    w, _ = _writer(log)

    assert w.start(total=99, already_saved=9) is True

    (stmt,) = _run_updates(log)
    sql = str(stmt)
    params = stmt.compile().params
    assert (params["verify_total"], params["verify_done"], params["verify_done_at_start"]) == (
        99,
        9,
        9,
    )
    assert "verify_started_at=now()" in sql.replace(" ", "")
    assert "verify_progress_at=now()" in sql.replace(" ", "")
    # The fence: running and held by this executor.
    assert params["status_1"] == "running"
    assert params["claimed_by_1"] == EXEC
    assert log[0][1].text.startswith("SET LOCAL lock_timeout")
    assert log[-1] == ("commit",)
    assert w.saved == 9


def test_without_an_executor_id_the_fence_is_the_status_alone() -> None:
    log: list[Any] = []
    w, _ = _writer(log, executor_id=None)
    w.start(total=9, already_saved=0)
    params = _run_updates(log)[0].compile().params
    assert params["status_1"] == "running"
    assert "claimed_by_1" not in params


def test_answers_are_written_every_n_cells_with_the_progress() -> None:
    log: list[Any] = []
    w, _ = _writer(log, flush_every_cells=3, flush_every_s=999)
    w.start(total=9, already_saved=1)
    log.clear()
    rows = [_row(f"zz-{i}", "zz-z", external_ok=True) for i in range(3)]

    w.cell_checked(rows[0])
    w.cell_checked(rows[1])
    assert _row_writes(log) == []  # not due yet
    w.cell_checked(rows[2])

    (written,) = _row_writes(log)
    assert [v["id"] for v in written] == [r.id for r in rows]
    assert all(v["external_ok"] is True for v in written)
    progress = _run_updates(log)[0].compile().params
    assert progress["verify_done"] == 4  # 1 earlier + these 3
    # The run row is updated (the fence) before the answers, in one transaction.
    kinds = [e[0] for e in log]
    assert kinds == ["execute", "execute", "execute", "commit"]
    assert w.saved == 4
    assert w.pending == 0


def test_answers_are_written_once_enough_time_has_passed() -> None:
    log: list[Any] = []
    w, clock = _writer(log, flush_every_cells=999, flush_every_s=5.0)
    w.start(total=9, already_saved=0)
    log.clear()

    w.cell_checked(_row("zz-a"))
    assert _row_writes(log) == []
    clock.now += 5.0
    w.cell_checked(_row("zz-b"))

    assert [len(batch) for batch in _row_writes(log)] == [2]


def test_flush_writes_what_is_left() -> None:
    log: list[Any] = []
    w, _ = _writer(log, flush_every_cells=999, flush_every_s=999)
    w.start(total=2, already_saved=0)
    w.cell_checked(_row("zz-a"))
    assert w.flush() is True
    assert [len(batch) for batch in _row_writes(log)] == [1]
    assert w.saved == 1


def test_a_write_that_finds_the_run_gone_stops_the_writer() -> None:
    log: list[Any] = []
    w, _ = _writer(log, held=False, flush_every_cells=1)

    assert w.start(total=9, already_saved=0) is False
    assert w.stopped is True
    assert ("rollback",) in log
    assert ("commit",) not in log
    log.clear()
    w.cell_checked(_row())
    assert w.flush() is False
    assert log == []  # nothing more is written once stopped


def test_a_failed_write_keeps_its_answers_for_the_next_one() -> None:
    log: list[Any] = []
    w, _ = _writer(log, fail=RuntimeError("zz database away"), flush_every_cells=1)
    w.start(total=9, already_saved=0)  # writes no answer rows: succeeds
    w.cell_checked(_row("zz-a"))  # this write fails

    assert w.stopped is False
    assert w.pending == 1
    assert w.saved == 0

    w._session_factory = lambda: _Db(log)  # the database is back
    w.cell_checked(_row("zz-b"))
    assert [len(batch) for batch in _row_writes(log)][-1] == 2
    assert w.saved == 2


def test_the_saved_fields_are_result_columns() -> None:
    columns = set(NetworkCoverageResult.__table__.columns.keys())
    assert set(verify_progress.SAVED_FIELDS) <= columns


class _RecordingRow:
    """A result row that records which attributes the sweep sets."""

    def __init__(self, origin: str, dest: str) -> None:
        object.__setattr__(self, "set_names", set())
        object.__setattr__(self, "id", uuid.uuid4())
        object.__setattr__(self, "origin_hub_id", origin)
        object.__setattr__(self, "dest_hub_id", dest)
        object.__setattr__(self, "journey_search_id", None)
        object.__setattr__(self, "status", "ok")

    def __setattr__(self, name: str, value: Any) -> None:
        self.set_names.add(name)
        object.__setattr__(self, name, value)


def _hub(slug: str, lat: float, lon: float) -> MagicMock:
    hub = MagicMock()
    hub.id, hub.lat, hub.lon = slug, lat, lon
    return hub


def _db_with_hubs(*slugs: str) -> MagicMock:
    hubs = {s: _hub(s, 45.99 + i, 9.99 + i) for i, s in enumerate(slugs)}
    db = MagicMock()
    db.get.side_effect = lambda _model, key: hubs.get(key)
    return db


def _run(verify: bool = True) -> MagicMock:
    run = MagicMock()
    run.id = RUN_ID
    run.verify_externally = verify
    run.depart_at = datetime(2099, 9, 9, 9, 0, tzinfo=UTC)
    return run


_FAST = runner.CoverageConfig(verify_sleep_ms=0)
_ANSWER = external_verify.VerifyResult(source="fahrplan.oebb.at", ok=True, num_connections=1)


@pytest.mark.asyncio
async def test_every_field_the_sweep_fills_is_saved() -> None:
    """Guard for a new external_* field: the writer must save it too."""
    rows = [_RecordingRow("zz-a", "zz-b"), _RecordingRow("zz-c", "zz-gone")]
    with patch.object(external_verify, "verify_via_oebb_hafas", AsyncMock(return_value=_ANSWER)):
        await runner._run_external_verify_sweep(
            db=_db_with_hubs("zz-a", "zz-b", "zz-c"), run=_run(), rows=rows, cfg=_FAST
        )
    filled = set().union(*(r.set_names for r in rows))
    assert filled <= set(verify_progress.SAVED_FIELDS)
    assert "external_itineraries" in filled  # the answered row went through the scorer


# ─────────────────────────────── the sweep ───────────────────────────────


class _FakeWriter:
    def __init__(self, *, stop_after: int | None = None, stopped_at_start: bool = False) -> None:
        self.started: dict[str, int] | None = None
        self.checked: list[Any] = []
        self.flushes = 0
        self.stopped = False
        self._stop_after = stop_after
        self._stopped_at_start = stopped_at_start

    def start(self, *, total: int, already_saved: int) -> bool:
        self.started = {"total": total, "already_saved": already_saved}
        self.stopped = self._stopped_at_start
        return not self.stopped

    def cell_checked(self, row: Any) -> None:
        self.checked.append(row)
        if self._stop_after is not None and len(self.checked) >= self._stop_after:
            self.stopped = True

    def flush(self) -> bool:
        self.flushes += 1
        return True


@pytest.mark.asyncio
async def test_the_sweep_hands_every_cell_to_the_writer_whatever_its_answer() -> None:
    db = _db_with_hubs("zz-a", "zz-b", "zz-c", "zz-d")
    answered = _row("zz-a", "zz-b")
    exploding = _row("zz-c", "zz-d")
    hub_gone = _row("zz-a", "zz-gone")

    async def _verify(**kw: Any) -> external_verify.VerifyResult:
        if kw["from_lat"] == 47.99:  # zz-c
            raise RuntimeError("zz adapter explosion")
        return _ANSWER

    writer = _FakeWriter()
    with patch.object(external_verify, "verify_via_oebb_hafas", side_effect=_verify):
        await runner._run_external_verify_sweep(
            db=db, run=_run(), rows=[answered, exploding, hub_gone], cfg=_FAST, writer=writer
        )
    assert {id(r) for r in writer.checked} == {id(answered), id(exploding), id(hub_gone)}
    assert exploding.external_error == "sweep_exception"
    assert hub_gone.external_error == "hub_missing"


@pytest.mark.asyncio
async def test_the_sweep_stops_asking_once_the_writer_has_stopped() -> None:
    db = _db_with_hubs(*(f"zz-{i}" for i in range(9)))
    rows = [_row(f"zz-{i}", f"zz-{i + 1}") for i in range(8)]
    fake = AsyncMock(return_value=_ANSWER)
    writer = _FakeWriter(stop_after=2)
    cfg = runner.CoverageConfig(verify_sleep_ms=0, verify_parallelism=1)
    with patch.object(external_verify, "verify_via_oebb_hafas", fake):
        await runner._run_external_verify_sweep(
            db=db, run=_run(), rows=rows, cfg=cfg, writer=writer
        )
    assert fake.await_count == 2
    assert len(writer.checked) == 2


@pytest.mark.asyncio
async def test_a_resumed_check_asks_only_the_cells_without_an_answer() -> None:
    earlier = datetime(2099, 9, 9, 10, 0, tzinfo=UTC)
    done_ok = _row("zz-a", "zz-b", external_verified_at=earlier, external_ok=True)
    done_err = _row("zz-b", "zz-a", external_verified_at=earlier, external_error="zz timeout")
    todo = _row("zz-a", "zz-c")
    skipped = _row("zz-c", "zz-a", status="skipped")
    captured: list[Any] = []

    async def _sweep(*, db: Any, run: Any, rows: Any, cfg: Any, writer: Any) -> dict[str, int]:
        captured.extend(rows)
        for row in rows:
            row.external_verified_at = earlier
            row.external_ok = False
        return {}

    writer = _FakeWriter()
    with patch.object(runner, "_run_external_verify_sweep", side_effect=_sweep):
        counters = await runner._maybe_run_external_verify_sweep(
            db=MagicMock(), run=_run(), rows=[done_ok, done_err, todo, skipped], writer=writer
        )
    assert captured == [todo]
    assert writer.started == {"total": 3, "already_saved": 2}
    assert writer.flushes == 1
    # Counted from the rows: the earlier attempt's answers and this one's.
    assert counters == {"verified": 3, "ok": 1, "no_route": 1, "error": 1}


@pytest.mark.asyncio
async def test_no_cell_is_asked_when_the_writer_finds_the_run_gone_at_the_start() -> None:
    writer = _FakeWriter(stopped_at_start=True)
    sweep = AsyncMock(return_value={})
    with patch.object(runner, "_run_external_verify_sweep", sweep):
        counters = await runner._maybe_run_external_verify_sweep(
            db=MagicMock(), run=_run(), rows=[_row()], writer=writer
        )
    sweep.assert_not_awaited()
    assert counters["verified"] == 0


@pytest.mark.asyncio
async def test_without_a_writer_the_sweep_is_as_before() -> None:
    row = _row("zz-a", "zz-b")
    with patch.object(external_verify, "verify_via_oebb_hafas", AsyncMock(return_value=_ANSWER)):
        counters = await runner._maybe_run_external_verify_sweep(
            db=_db_with_hubs("zz-a", "zz-b"), run=_run(), rows=[row], cfg=_FAST
        )
    assert counters == {"verified": 1, "ok": 1, "no_route": 0, "error": 0}
    assert row.external_ok is True


# ─────────────────────────────── the API ───────────────────────────────


def _api_run(**fields: Any) -> MagicMock:
    run = MagicMock()
    run.status = fields.pop("status", "running")
    run.claimed_by = fields.pop("claimed_by", EXEC)
    run.heartbeat_at = fields.pop("heartbeat_at", datetime.now(UTC))
    run.cancel_requested_at = fields.pop("cancel_requested_at", None)
    for name in (
        "verify_total",
        "verify_done",
        "verify_done_at_start",
        "verify_started_at",
        "verify_progress_at",
    ):
        setattr(run, name, fields.pop(name, None))
    assert not fields
    return run


_START = datetime(2099, 9, 9, 18, 0, tzinfo=UTC)


def _summary_fields(run: MagicMock, *, finished_at: datetime | None) -> None:
    """The fields `_run_to_summary` validates, with benign values (like
    tests/unit/test_coverage_banner_run_stats.py)."""
    run.id = RUN_ID
    run.session_id = run.session_label = "zz-session-99"
    run.direction, run.mode = "both", "single_session"
    run.depart_at = _START
    run.started_at, run.finished_at = _START - timedelta(hours=9), finished_at
    run.total_pairs = run.completed_pairs = run.ok_pairs = 9999
    run.no_route_pairs = run.error_pairs = 0
    run.countries = None
    run.verify_externally = True
    run.window_start_local = run.window_end_local = run.window_timezone = None
    run.reference_date = None
    run.resume_count = 0
    run.summary = None


@pytest.mark.parametrize(
    ("fields", "shown"),
    [
        ({}, "running"),
        ({"verify_started_at": _START}, "verifying"),
        # A Stop during the check does not interrupt it: no "stopping".
        ({"verify_started_at": _START, "cancel_requested_at": _START}, "verifying"),
        ({"cancel_requested_at": _START}, "stopping"),
        # Its worker stopped: "resuming" wins over "verifying".
        ({"verify_started_at": _START, "claimed_by": None}, "resuming"),
        (
            {"verify_started_at": _START, "heartbeat_at": datetime.now(UTC) - timedelta(days=1)},
            "resuming",
        ),
        ({"verify_started_at": _START, "status": "completed"}, "completed"),
    ],
)
def test_display_status_during_the_check(fields: dict[str, Any], shown: str) -> None:
    from app.api.admin.network_coverage import _display_status

    assert _display_status(_api_run(**fields))[0] == shown


@pytest.mark.parametrize(
    ("done", "at_start", "minutes", "left"),
    [
        (120, 0, 2.0, 2.0 * 60 * (9999 - 120) / 120),  # the attempt's pace
        (1120, 1000, 2.0, 2.0 * 60 * (9999 - 1120) / 120),  # earlier answers left out
        (10, 0, 2.0, None),  # too few answers to measure
        (120, 0, 0.5, None),  # too short to measure
        (9999, 0, 2.0, 0.0),  # finished
    ],
)
def test_time_left_at_the_current_attempts_pace(
    done: int, at_start: int, minutes: float, left: float | None
) -> None:
    from app.api.admin.network_coverage import _verify_seconds_left

    run = _api_run(
        verify_total=9999,
        verify_done=done,
        verify_done_at_start=at_start,
        verify_started_at=_START,
        verify_progress_at=_START + timedelta(minutes=minutes),
    )
    assert _verify_seconds_left(run) == (pytest.approx(left) if left is not None else None)


def test_no_estimate_before_the_check_starts() -> None:
    from app.api.admin.network_coverage import _verify_elapsed_seconds, _verify_seconds_left

    run = _api_run()
    assert _verify_seconds_left(run) is None
    assert _verify_elapsed_seconds(run) is None


def test_the_run_summary_carries_the_progress() -> None:
    from app.api.admin.network_coverage import _run_to_summary

    run = _api_run(
        verify_total=9999,
        verify_done=999,
        verify_done_at_start=0,
        verify_started_at=_START,
        verify_progress_at=_START + timedelta(minutes=20),
    )
    _summary_fields(run, finished_at=None)

    summary = _run_to_summary(run)

    assert summary.display_status == "verifying"
    assert (summary.verify_total, summary.verify_done) == (9999, 999)
    assert summary.verify_started_at == _START.isoformat()
    assert summary.verify_elapsed_seconds == 1200.0
    assert summary.verify_seconds_left == pytest.approx(1200.0 * 9000 / 999)


def test_a_run_whose_check_never_started_has_no_progress() -> None:
    from app.api.admin.network_coverage import _run_to_summary

    run = MagicMock()  # the verify_* attributes stay MagicMocks, as in older fixtures
    run.status, run.claimed_by, run.heartbeat_at = "completed", EXEC, None
    _summary_fields(run, finished_at=_START)
    summary = _run_to_summary(run)
    assert summary.verify_total is None
    assert summary.verify_seconds_left is None


# ─────────────────────────────── the page ───────────────────────────────


@pytest.fixture(scope="module")
def page() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    match = re.search(rf"^(?:async )?function {name}\(.*?^}}$", text, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def test_the_checkbox_hint_says_every_cell_and_hours_not_minutes(page: str) -> None:
    hint = re.search(r'id="cov-verify-externally-hint"[^>]*>(.*?)</span>', page, re.S)
    assert hint
    text = " ".join(hint.group(1).split())
    assert "every cell" in text
    en_dash = chr(0x2013)
    assert f"1{en_dash}2&nbsp;h more per 3&nbsp;600 pairs" in text
    assert "progress" in text
    assert f"1{en_dash}5&nbsp;min" not in page  # the old, wrong estimate


def test_the_summary_shows_the_progress_line(page: str) -> None:
    assert "${buildVerifyProgressLine(run)}" in _function(page, "renderRun")
    line = _function(page, "buildVerifyProgressLine")
    assert "if (run.verify_total === null || run.verify_total === undefined) return '';" in line
    assert "cov-progress-bar verify" in line
    assert "${escHTML(text)}" in line
    live = _function(page, "verifyLiveText")
    assert "fmtRunDuration(run.verify_seconds_left)" in live
    assert "time left: measuring" in live


def test_the_pill_explains_verifying(page: str) -> None:
    hints = re.search(r"const RUN_STATUS_HINTS = \{(.*?)\n\};", page, re.S)
    assert hints
    assert "verifying:" in hints.group(1)
    assert "Stop does not interrupt it" in hints.group(1)
    assert '.cov-pill[data-status="verifying"]' in page


def test_the_run_list_bar_follows_the_check(page: str) -> None:
    run_list = _function(page, "reloadRunList")
    assert "const { pct, barCls, verifyMeta } = runListProgress(r);" in run_list
    assert 'class="${barCls}"' in run_list
    assert "${verifyMeta}" in run_list
    progress = _function(page, "runListProgress")
    assert "if (r.display_status === 'verifying') {" in progress
    assert "pct: verifyPct(r)," in progress
    assert "barCls: 'cov-progress-bar verify'," in progress


def test_the_modal_says_when_oebb_was_not_asked_yet(page: str) -> None:
    grid = _function(page, "renderDirectionCompareGrid")
    assert "dir.external_verified_at" in grid
    assert "OEBB_NOT_ASKED_HTML" in grid
    assert "ÖBB not asked yet for this cell" in page
    controls = _function(page, "renderModalControls")
    assert "Not compared with ÖBB yet" in controls
    assert "No alignment data recorded for this cell." in controls
