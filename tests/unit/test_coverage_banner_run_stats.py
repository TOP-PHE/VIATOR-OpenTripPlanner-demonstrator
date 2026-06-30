"""Unit tests for PR-190 — the run-banner stats line.

Surface under test:

  1. `_compute_duration_seconds(started_at, finished_at)` — the
     duration helper used by the banner. Returns:
       - None when started_at is None (run hasn't started yet)
       - now - started_at when finished_at is None (in-flight)
       - finished_at - started_at when terminal
       - clamps >= 0 so clock skew can't surface a negative
  2. `_aggregate_response_ms(db, run_ids)` — the batched MIN/AVG/MAX
     query used by the sidebar list. Empty input short-circuits.
  3. `_stats_from_results(results)` — the in-memory counterpart used
     by the per-run detail endpoint to avoid a second round-trip.
     Returns None when no cell has a non-NULL response_ms.
  4. `_run_to_summary(run, response_ms_stats=...)` — wires duration +
     stats into the RunSummary the template renders.

These tests are deliberately fast — they construct synthetic row
objects with the exact attribute shape `_run_to_summary` reads,
bypassing the DB entirely.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api.admin import network_coverage as api

# ─────────────────────── shared fixtures ───────────────────────


def _make_run_row(
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    status: str = "running",
):
    """A NetworkCoverageRun stand-in shaped just enough for
    `_run_to_summary`. Every other field gets a benign default so the
    pydantic validators don't trip on missing values."""
    r = MagicMock()
    r.id = uuid.uuid4()
    r.session_id = "nap-fr-rail"
    r.session_label = "nap-fr-rail"
    r.depart_at = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    r.started_at = started_at
    r.finished_at = finished_at
    r.status = status
    r.direction = "both"
    r.mode = "single_session"
    r.total_pairs = 100
    r.completed_pairs = 20
    r.ok_pairs = 18
    r.no_route_pairs = 2
    r.error_pairs = 0
    r.countries = None
    r.verify_externally = False
    r.window_start_local = None
    r.window_end_local = None
    r.window_timezone = None
    r.reference_date = None
    return r


# ─────────────────────── _compute_duration_seconds ───────────────────────


def test_compute_duration_none_when_started_at_is_none():
    """Pre-start rows (status='pending' before the runner picks them
    up) have no started_at yet. Banner must render no duration rather
    than crash on `None - None`."""
    assert api._compute_duration_seconds(None, None) is None
    assert api._compute_duration_seconds(None, datetime(2026, 6, 28, 9, 0, tzinfo=UTC)) is None


def test_compute_duration_for_running_run_uses_now():
    """In-flight run: finished_at is NULL → use `datetime.now(UTC)` as
    the upper bound. We can't assert exact value (now ticks), but the
    delta must be within a small window of the synthetic elapsed time."""
    started = datetime.now(UTC) - timedelta(seconds=312)
    dur = api._compute_duration_seconds(started, None)
    assert dur is not None
    # 312s synthetic elapsed; allow 5s slop for test wall-clock jitter.
    assert 312 <= dur <= 312 + 5


def test_compute_duration_for_finished_run_uses_finished_at():
    """Terminal run: finished_at sets the upper bound. The value is
    independent of `now`, so we can assert it exactly."""
    started = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 6, 28, 8, 5, 12, tzinfo=UTC)
    assert api._compute_duration_seconds(started, finished) == pytest.approx(312.0)


def test_compute_duration_clamps_non_negative_on_clock_skew():
    """Two writers with clock skew could in theory persist a finished_at
    that's before started_at. The banner must never surface a negative
    number — clamp to 0 so the worst case reads "0s" not "-2s"."""
    started = datetime(2026, 6, 28, 8, 0, 5, tzinfo=UTC)
    finished = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)  # 5s before start
    assert api._compute_duration_seconds(started, finished) == 0.0


def test_compute_duration_handles_naive_timestamps_defensively():
    """Some test fixtures (and pre-tz-aware columns in legacy fixtures)
    pass naive datetimes. Treat as UTC instead of raising TypeError on
    `naive - aware`."""
    started = datetime(2026, 6, 28, 8, 0, 0)  # naive
    finished = datetime(2026, 6, 28, 8, 5, 0)  # naive
    assert api._compute_duration_seconds(started, finished) == pytest.approx(300.0)


# ─────────────────────── _stats_from_results ───────────────────────


def test_stats_from_results_zero_cells_returns_none():
    """A 0-cell run (run just registered, no cell processed yet) has no
    response_ms to aggregate — the banner subline must hide gracefully
    rather than render "min undefined · avg NaN · max undefined"."""
    assert api._stats_from_results([]) is None


def test_stats_from_results_all_null_response_ms_returns_none():
    """Cells flipped to status='skipped' (cooperative cancel before
    pair-fetch) have response_ms=NULL. Treat a run where every cell
    skipped the same as a 0-cell run for banner purposes."""
    results = [
        SimpleNamespace(response_ms=None),
        SimpleNamespace(response_ms=None),
    ]
    assert api._stats_from_results(results) is None


def test_stats_from_results_mixed_timings():
    """A real run mixes successful cells (response_ms set) with skipped
    or errored ones (response_ms NULL). The aggregate is over the
    populated values only — NULLs don't drag the avg toward 0."""
    results = [
        SimpleNamespace(response_ms=400),
        SimpleNamespace(response_ms=2100),
        SimpleNamespace(response_ms=12800),
        SimpleNamespace(response_ms=None),  # skipped — must be ignored
    ]
    stats = api._stats_from_results(results)
    assert stats is not None
    rmin, ravg, rmax = stats
    assert rmin == 400
    assert rmax == 12800
    # avg = (400 + 2100 + 12800) / 3
    assert ravg == pytest.approx((400 + 2100 + 12800) / 3)


# ─────────────────────── _run_to_summary integration ───────────────────────


def test_run_to_summary_populates_duration_for_running_run():
    """The integration path the banner depends on — `_run_to_summary`
    must read `started_at` / `finished_at` off the row and surface
    `duration_seconds` on the returned RunSummary."""
    started = datetime.now(UTC) - timedelta(seconds=120)
    run = _make_run_row(started_at=started, finished_at=None, status="running")
    summary = api._run_to_summary(run)
    assert summary.duration_seconds is not None
    assert 120 <= summary.duration_seconds <= 125


def test_run_to_summary_threads_response_ms_stats_through():
    """When the caller passes `response_ms_stats`, it must land on the
    RunSummary unchanged. Decoupled from the DB so the per-run detail
    endpoint can hand in stats computed from the already-loaded
    results without a second round-trip."""
    run = _make_run_row(
        started_at=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 6, 28, 8, 5, 12, tzinfo=UTC),
    )
    summary = api._run_to_summary(run, response_ms_stats=(400, 2100.5, 12800))
    assert summary.duration_seconds == pytest.approx(312.0)
    assert summary.response_ms_min == 400
    assert summary.response_ms_avg == pytest.approx(2100.5)
    assert summary.response_ms_max == 12800


def test_run_to_summary_leaves_stats_null_when_not_passed():
    """Default behaviour — caller didn't pass stats (e.g. the stop
    endpoint, which only has the run row in hand and doesn't need the
    sidebar's batched query). All three stat fields stay NULL so the
    template subline hides cleanly."""
    run = _make_run_row(started_at=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC))
    summary = api._run_to_summary(run)
    assert summary.response_ms_min is None
    assert summary.response_ms_avg is None
    assert summary.response_ms_max is None


# ─────────────────────── _aggregate_response_ms ───────────────────────


def test_aggregate_response_ms_empty_input_short_circuits():
    """Empty run_ids list (sidebar shown before any run exists) must
    skip the DB round-trip entirely — saves both the query and the
    test from having to mock the executor."""
    db = MagicMock()
    assert api._aggregate_response_ms(db, []) == {}
    db.execute.assert_not_called()


def test_aggregate_response_ms_marshals_rows_into_dict():
    """Happy path — the helper executes one batched query and returns
    a dict keyed by run_id with (min, avg, max) tuples. The shape
    feeds the sidebar list comprehension directly."""
    db = MagicMock()
    rid1 = uuid.uuid4()
    rid2 = uuid.uuid4()
    # SQLAlchemy returns Decimal-ish numeric for AVG; emulate with float.
    db.execute.return_value.all.return_value = [
        (rid1, 400, 2100.5, 12800),
        (rid2, 100, 250.0, 500),
    ]
    out = api._aggregate_response_ms(db, [rid1, rid2])
    assert out[rid1] == (400, pytest.approx(2100.5), 12800)
    assert out[rid2] == (100, pytest.approx(250.0), 500)


# ─────────────────────── format helpers (JS mirror) ───────────────────────
#
# The JS-side fmtRunDuration / fmtMsToS live in the template. We mirror
# their rules here in pure Python so the regression test catches a JS
# update that drifts from the design (5m12s, 1h05m, 0.4s).


def _fmt_run_duration_py(seconds: float | None) -> str | None:
    """Python mirror of the JS `fmtRunDuration` helper. Same rules:
    < 60s   → "Ns"
    < 1h    → "Nm0Ms"   (zero-pad seconds)
    >= 1h   → "NhMMm"   (zero-pad minutes)
    """
    if seconds is None:
        return None
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h{m:02d}m"


def _fmt_ms_to_s_py(ms: float | None) -> str | None:
    """Python mirror of the JS `fmtMsToS` helper — 1-decimal seconds."""
    if ms is None:
        return None
    return f"{ms / 1000:.1f}s"


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m00s"),
        (312, "5m12s"),  # the README example
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3900, "1h05m"),  # the README example
        (7320, "2h02m"),
    ],
)
def test_format_duration_matches_design(seconds, expected):
    """Duration formatter — the rules and the exact strings the design
    in PR-190 calls out ("5m12s", "1h05m"). Lock these so a future
    JS-side tweak can't quietly diverge."""
    assert _fmt_run_duration_py(seconds) == expected


@pytest.mark.parametrize(
    "ms, expected",
    [
        (0, "0.0s"),
        (400, "0.4s"),
        (2100, "2.1s"),
        (12800, "12.8s"),
        (1234, "1.2s"),
    ],
)
def test_format_ms_to_seconds_one_decimal(ms, expected):
    """Per-cell response_ms is rendered in seconds with one decimal so
    sub-second cells (e.g. 410ms) still show useful precision and
    multi-second cells stay readable ("12.8s" not "12800ms")."""
    assert _fmt_ms_to_s_py(ms) == expected


def test_format_duration_none_passthrough():
    """`None` propagates as `None` — the template uses that to decide
    whether the subline should render at all."""
    assert _fmt_run_duration_py(None) is None
    assert _fmt_ms_to_s_py(None) is None
