"""Unit tests for the PR-E auto-external-verify sweep (feat/coverage-
auto-verify-external).

Four layers under test:

  1. `RunCreate.verify_externally` Pydantic — default False, accepts True.
  2. `runner.create_run(..., verify_externally=...)` — propagates the
     flag onto the NetworkCoverageRun row.
  3. `runner._run_external_verify_sweep(db, run, rows)` — calls
     `external_verify.verify_via_oebb_hafas` with the right kwargs,
     persists the VerifyResult fields onto each row, handles
     soft-deleted hubs and per-cell exceptions, returns the rollup
     counters.
  4. Phase-3 dispatch — execute_run skips the sweep when
     run.verify_externally is False, runs it when True.

Out of scope:
  - The alembic migration itself — covered by the standard migration
    smoke run on CI.
  - The matrix-view JS / cell-modal pre-render — exercised by browser
    smoke testing post-deploy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.network_coverage import external_verify, runner

# ─────────────────────── RunCreate.verify_externally validator ───────────────────────


def _make_body(**overrides):
    """A minimal RunCreate kwargs dict, overrideable per-test."""
    from app.api.admin.network_coverage import RunCreate

    base = {
        "session_id": "nap-fr-rail",
        "depart_at": datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        "direction": "both",
        "mode": "single_session",
    }
    base.update(overrides)
    return RunCreate(**base)


def test_runcreate_verify_externally_default_is_false():
    """Omitting the field — every legacy submit shape — must default to
    False so existing clients are unaffected by PR-E."""
    assert _make_body().verify_externally is False


def test_runcreate_verify_externally_accepts_true():
    """Operator ticks the run-form checkbox → body carries True →
    runner persists it on the row."""
    assert _make_body(verify_externally=True).verify_externally is True


# ─────────────────────── runner.create_run propagation ───────────────────────


def _stub_db_for_create_run():
    """A MagicMock db that satisfies the `_load_active_hubs` query path
    create_run hits. Returns a single fake hub so the
    'no active hubs' guard doesn't trip."""
    fake_hub = MagicMock()
    fake_hub.id = "paris-gdl"
    fake_hub.name = "Paris Gare de Lyon"
    fake_hub.country = "FR"
    fake_hub.lat = 48.84
    fake_hub.lon = 2.37
    scalars = MagicMock()
    scalars.all.return_value = [fake_hub]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db = MagicMock()
    db.execute.return_value = exec_result
    return db


def test_create_run_default_verify_externally_false_on_row():
    """Default call (no verify_externally kwarg) → the persisted run row
    carries verify_externally=False. Catches a regression where the
    kwarg gets renamed and silently defaults something else."""
    db = _stub_db_for_create_run()
    run = runner.create_run(
        db,
        actor_user_id=uuid.uuid4(),
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        direction="single",
    )
    assert run.verify_externally is False


def test_create_run_propagates_verify_externally_true():
    """Explicit verify_externally=True → row.verify_externally=True.
    Pinned so the kwarg doesn't get accidentally dropped in a future
    signature change."""
    db = _stub_db_for_create_run()
    run = runner.create_run(
        db,
        actor_user_id=uuid.uuid4(),
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        direction="single",
        verify_externally=True,
    )
    assert run.verify_externally is True


# ─────────────────────── _run_external_verify_sweep ───────────────────────


def _make_row(origin="bxl-mid", dest="gva-c", status="no_route"):
    """A NetworkCoverageResult stand-in with the slug fields the sweep
    reads + writable external_* attributes."""
    r = MagicMock()
    r.origin_hub_id = origin
    r.dest_hub_id = dest
    r.status = status
    r.external_ok = None
    r.external_num_connections = None
    r.external_best_duration_seconds = None
    r.external_best_transfers = None
    r.external_source = None
    r.external_error = None
    r.external_verified_at = None
    # PR-196a — alignment-heatmap fields. The sweep writes these even
    # when the VIATOR side is empty (one-sided tiers), so the stub has
    # to leave them as plain `None` rather than the auto-MagicMock
    # default that would confuse equality assertions in tests.
    r.external_itineraries = None
    r.external_alignment_score = None
    r.external_alignment_tier = None
    # PR-196a — the sweep fetches VIATOR trips by journey_search_id;
    # None bypasses the fetch (returns []), matching every legacy test
    # that pre-dated the alignment hook.
    r.journey_search_id = None
    return r


def _make_hub(slug, lat, lon):
    """A NetworkCoverageHub stand-in with coords."""
    h = MagicMock()
    h.id = slug
    h.lat = lat
    h.lon = lon
    return h


def _make_db_with_hubs(hubs: dict[str, MagicMock]):
    """A MagicMock db where db.get(NetworkCoverageHub, slug) returns the
    matching hub from `hubs`, or None if missing."""
    db = MagicMock()

    def _get(model, key):
        # The sweep only does db.get(NetworkCoverageHub, slug); other
        # callers are stubbed elsewhere in their own tests.
        return hubs.get(key)

    db.get.side_effect = _get
    return db


def _make_run(verify_externally=True):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.verify_externally = verify_externally
    run.depart_at = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    return run


@pytest.mark.asyncio
async def test_sweep_calls_verify_with_run_depart_at_and_hub_coords():
    """The single most important wiring assertion: the sweep passes the
    run's depart_at and each hub's lat/lon coords into
    verify_via_oebb_hafas — NOT, say, slug strings or now() — so HAFAS
    actually answers the right question."""
    hubs = {
        "bxl-mid": _make_hub("bxl-mid", 50.8358, 4.3361),
        "gva-c": _make_hub("gva-c", 46.2104, 6.1424),
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    rows = [_make_row("bxl-mid", "gva-c", "no_route")]

    fake_verify = AsyncMock(
        return_value=external_verify.VerifyResult(
            source="fahrplan.oebb.at",
            ok=True,
            num_connections=3,
            best_duration_seconds=4 * 3600 + 15 * 60,
            best_transfers=1,
        )
    )
    # PR-2 — pass a CoverageConfig with verify_sleep_ms=0 so the test
    # completes instantly (replaces the old `patch.object(_VERIFY_SLEEP_BETWEEN_MS, 0)`
    # idiom now that those constants live on the CoverageConfig dataclass).
    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with patch.object(external_verify, "verify_via_oebb_hafas", fake_verify):
        counters = await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    fake_verify.assert_awaited_once()
    call_kwargs = fake_verify.await_args.kwargs
    assert call_kwargs["from_lat"] == 50.8358
    assert call_kwargs["from_lon"] == 4.3361
    assert call_kwargs["to_lat"] == 46.2104
    assert call_kwargs["to_lon"] == 6.1424
    assert call_kwargs["depart_at"] == run.depart_at
    assert counters == {"verified": 1, "ok": 1, "no_route": 0, "error": 0}


@pytest.mark.asyncio
async def test_sweep_persists_verify_result_onto_row():
    """All VerifyResult fields land on the row's external_* attributes.
    Pinned so a future refactor that drops a field doesn't silently
    lose data."""
    hubs = {
        "bxl-mid": _make_hub("bxl-mid", 50.8358, 4.3361),
        "gva-c": _make_hub("gva-c", 46.2104, 6.1424),
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    row = _make_row()
    rows = [row]

    fake_verify = AsyncMock(
        return_value=external_verify.VerifyResult(
            source="fahrplan.oebb.at",
            ok=True,
            num_connections=5,
            best_duration_seconds=2 * 3600 + 18 * 60,
            best_transfers=0,
        )
    )
    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with patch.object(external_verify, "verify_via_oebb_hafas", fake_verify):
        await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    assert row.external_source == "fahrplan.oebb.at"
    assert row.external_ok is True
    assert row.external_num_connections == 5
    assert row.external_best_duration_seconds == 2 * 3600 + 18 * 60
    assert row.external_best_transfers == 0
    assert row.external_error is None
    assert row.external_verified_at is not None


@pytest.mark.asyncio
async def test_sweep_counters_split_by_verdict_class():
    """Three cells with three different verdicts → counters split
    correctly: ok / no_route / error all incremented separately so the
    matrix sidebar can render a meaningful rollup."""
    hubs = {
        "a": _make_hub("a", 50.0, 4.0),
        "b": _make_hub("b", 51.0, 5.0),
        "c": _make_hub("c", 52.0, 6.0),
        "d": _make_hub("d", 53.0, 7.0),
        "e": _make_hub("e", 54.0, 8.0),
        "f": _make_hub("f", 55.0, 9.0),
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    rows = [
        _make_row("a", "b", "no_route"),
        _make_row("c", "d", "no_route"),
        _make_row("e", "f", "no_route"),
    ]

    verdicts = [
        external_verify.VerifyResult(source="fahrplan.oebb.at", ok=True, num_connections=2),
        external_verify.VerifyResult(source="fahrplan.oebb.at", ok=False, num_connections=0),
        external_verify.VerifyResult(source="fahrplan.oebb.at", ok=False, error="HTTP 500"),
    ]
    fake_verify = AsyncMock(side_effect=verdicts)
    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with patch.object(external_verify, "verify_via_oebb_hafas", fake_verify):
        counters = await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    assert counters["verified"] == 3
    assert counters["ok"] == 1
    assert counters["no_route"] == 1
    assert counters["error"] == 1


@pytest.mark.asyncio
async def test_sweep_handles_soft_deleted_hub():
    """A hub that was soft-deleted between run and sweep returns None
    from db.get → row gets external_error='hub_missing' and the sweep
    moves on without raising. Matches the click-verify endpoint's 404
    path but persisted so the matrix UI can render it."""
    hubs = {
        "bxl-mid": _make_hub("bxl-mid", 50.8358, 4.3361),
        # gva-c missing → simulates soft-delete
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    row = _make_row("bxl-mid", "gva-c", "no_route")
    rows = [row]

    fake_verify = AsyncMock()  # should NOT be called
    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with patch.object(external_verify, "verify_via_oebb_hafas", fake_verify):
        counters = await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    fake_verify.assert_not_awaited()
    assert row.external_error == "hub_missing"
    assert row.external_source == "fahrplan.oebb.at"
    assert row.external_verified_at is not None
    assert counters == {"verified": 1, "ok": 0, "no_route": 0, "error": 1}


@pytest.mark.asyncio
async def test_sweep_writes_alignment_fields_on_successful_verify():
    """PR-196a — after a successful verify the sweep MUST also persist
    the alignment trio (external_itineraries / score / tier) so the
    heatmap has something to render. Without this assertion a refactor
    that drops the `_persist_alignment_on_row` call would silently leave
    the matrix grey on rows that have a verdict but no tier."""
    hubs = {
        "bxl-mid": _make_hub("bxl-mid", 50.8358, 4.3361),
        "gva-c": _make_hub("gva-c", 46.2104, 6.1424),
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    row = _make_row()
    rows = [row]

    # ÖBB returns one itinerary; the scorer would normally pull VIATOR
    # trips via the search_id JOIN — we stub _fetch_viator_trips_for_search
    # to skip the DB hop, and compute_alignment to pin the (score, tier)
    # output independent of the actual scoring algorithm.
    fake_itinerary = external_verify.VerifyItinerary(
        legs=[
            external_verify.VerifyLeg(
                mode="RAIL",
                from_uic="UIC:8014441",
                to_uic="UIC:8503000",
                dep_utc="2026-06-28T08:00",
                arr_utc="2026-06-28T12:30",
                route_name="ICE 24",
            )
        ],
        departure_at="2026-06-28T08:00",
        arrival_at="2026-06-28T12:30",
        duration_seconds=4 * 3600 + 30 * 60,
        num_transfers=0,
    )
    fake_verify = AsyncMock(
        return_value=external_verify.VerifyResult(
            source="fahrplan.oebb.at",
            ok=True,
            num_connections=1,
            best_duration_seconds=4 * 3600 + 30 * 60,
            best_transfers=0,
            itineraries=[fake_itinerary],
        )
    )
    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with (
        patch.object(external_verify, "verify_via_oebb_hafas", fake_verify),
        patch.object(runner, "_fetch_viator_trips_for_search", return_value=[]),
        patch.object(runner, "compute_alignment", return_value=(0.7, "mostly_agree")),
    ):
        await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    assert row.external_alignment_score == 0.7
    assert row.external_alignment_tier == "mostly_agree"
    # JSONB column was populated with the dumped itinerary list — pin the
    # length, not the exact shape, so a future field addition doesn't
    # break the test.
    assert isinstance(row.external_itineraries, list)
    assert len(row.external_itineraries) == 1


# ─────── _fetch_viator_trips_for_search (apples-to-apples window fix) ───────


def _make_trip_row(departure_at):
    row = MagicMock()
    row.duration_seconds = 1800
    row.num_transfers = 0
    row.departure_at = departure_at
    row.arrival_at = departure_at
    row.modes = "RAIL"
    row.legs = []
    return row


def _db_returning_trips(rows):
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = rows
    return db


def test_fetch_viator_trips_for_search_excludes_trips_before_depart_at():
    """The scope-mismatch bug: a cell's VIATOR trips span the whole
    K-slot day window while ÖBB's verify call is one forward-looking
    search anchored at run.depart_at — trips departing before that
    anchor were never comparable to what ÖBB was actually asked and
    must be excluded from the scorer's input, not merely deprioritised."""
    depart_at = datetime(2026, 7, 13, 6, 0, 0, tzinfo=UTC)
    db = _db_returning_trips(
        [
            _make_trip_row(datetime(2026, 7, 13, 2, 34, 0, tzinfo=UTC)),  # before — dropped
            _make_trip_row(datetime(2026, 7, 13, 6, 0, 0, tzinfo=UTC)),  # exact boundary — kept
            _make_trip_row(datetime(2026, 7, 13, 7, 20, 0, tzinfo=UTC)),  # after — kept
        ]
    )
    out = runner._fetch_viator_trips_for_search(db, uuid.uuid4(), depart_at)
    assert [t["departure_at"] for t in out] == [
        "2026-07-13T06:00:00+00:00",
        "2026-07-13T07:20:00+00:00",
    ]


def test_fetch_viator_trips_for_search_none_when_no_search_id():
    assert runner._fetch_viator_trips_for_search(MagicMock(), None, datetime.now(UTC)) == []


# ─────────────────────── _maybe_run_external_verify_sweep candidate filter ───────────────────────


@pytest.mark.asyncio
async def test_maybe_sweep_verifies_status_ok_cells_not_just_failures():
    """PR-196a regression guard: the candidate filter used to be
    (status in {no_route, timeout, error}) — which left every status='ok'
    cell with NULL external_ok and broke the 'Show only where ÖBB
    disagrees' filter (the whole matrix went white). The new filter
    sweeps every non-skipped row so the heatmap can score ok-cells too.

    This test asserts the new behaviour by passing rows in all the
    relevant statuses and capturing the rows actually dispatched to
    `_run_external_verify_sweep`."""
    captured: list = []

    async def _capture_sweep(*, db, run, rows, cfg):
        captured.extend(rows)
        return {"verified": len(rows), "ok": 0, "no_route": 0, "error": 0}

    rows = [
        _make_row("a", "b", status="ok"),
        _make_row("c", "d", status="no_route"),
        _make_row("e", "f", status="timeout"),
        _make_row("g", "h", status="error"),
        _make_row("i", "j", status="skipped"),  # only status that must NOT sweep
    ]
    db = MagicMock()
    run = _make_run(verify_externally=True)

    with patch.object(runner, "_run_external_verify_sweep", side_effect=_capture_sweep):
        await runner._maybe_run_external_verify_sweep(db=db, run=run, rows=rows)

    captured_statuses = sorted(r.status for r in captured)
    assert captured_statuses == ["error", "no_route", "ok", "timeout"]
    # 'skipped' rows are never swept — there's no VIATOR answer to score
    # against and they exist for legitimate exclusion reasons (e.g.
    # same-hub diagonal, configured exclusion).
    assert all(r.status != "skipped" for r in captured)


@pytest.mark.asyncio
async def test_maybe_sweep_returns_zero_when_run_did_not_opt_in():
    """Opt-out path: a run with verify_externally=False short-circuits
    before any candidate filter — no rows are dispatched even if every
    row has a status that would qualify. Pinned so a refactor that
    moves the verify_externally check inside the filter doesn't
    accidentally start sweeping every legacy run."""
    captured: list = []

    async def _capture_sweep(*, db, run, rows, cfg):
        captured.extend(rows)
        return {"verified": 0, "ok": 0, "no_route": 0, "error": 0}

    rows = [_make_row("a", "b", status="no_route")]
    db = MagicMock()
    run = _make_run(verify_externally=False)

    with patch.object(runner, "_run_external_verify_sweep", side_effect=_capture_sweep):
        counters = await runner._maybe_run_external_verify_sweep(db=db, run=run, rows=rows)

    assert captured == []
    assert counters == {"verified": 0, "ok": 0, "no_route": 0, "error": 0}


@pytest.mark.asyncio
async def test_sweep_continues_when_one_cell_raises():
    """Per-cell exceptions are swallowed + logged + persisted as
    external_error='sweep_exception' so one bad cell doesn't abort the
    rest of the run. The other rows still get their verdicts written."""
    hubs = {
        "a": _make_hub("a", 50.0, 4.0),
        "b": _make_hub("b", 51.0, 5.0),
        "c": _make_hub("c", 52.0, 6.0),
        "d": _make_hub("d", 53.0, 7.0),
    }
    db = _make_db_with_hubs(hubs)
    run = _make_run()
    row_ok = _make_row("a", "b", "no_route")
    row_explode = _make_row("c", "d", "no_route")
    rows = [row_ok, row_explode]

    async def _fake(**kwargs):
        if kwargs["from_lat"] == 52.0:  # the row_explode call
            raise RuntimeError("simulated adapter explosion")
        return external_verify.VerifyResult(source="fahrplan.oebb.at", ok=True, num_connections=1)

    fast_cfg = runner.CoverageConfig(verify_sleep_ms=0)
    with patch.object(external_verify, "verify_via_oebb_hafas", side_effect=_fake):
        counters = await runner._run_external_verify_sweep(db=db, run=run, rows=rows, cfg=fast_cfg)

    # row_ok got its verdict cleanly
    assert row_ok.external_ok is True
    assert row_ok.external_error is None
    # row_explode got the sweep-exception sentinel
    assert row_explode.external_error == "sweep_exception"
    assert row_explode.external_verified_at is not None
    # Counters reflect: 1 ok + 1 error = 2 verified
    assert counters["verified"] == 2
    assert counters["ok"] == 1
    assert counters["error"] == 1
