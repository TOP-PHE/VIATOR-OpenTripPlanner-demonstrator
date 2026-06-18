"""Unit tests for PR #36 — cross-session (fanout) coverage runs.

The runner's `create_run()` does upfront validation BEFORE touching the
DB, so we can exercise the mode + session_id pairing rules with no real
database. The async fanout-pair execution + result merging is covered
via mocked `otp_client.fetch_plan` so the trip-signature merge logic is
exercised without standing up Postgres.

Out of scope (covered in integration tests once those land):
  - End-to-end POST /runs with mode=fanout via TestClient
  - Live serve container interaction
  - Alembic migration up/down
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.journey import otp_client
from app.network_coverage import runner

# ─────────────────────── create_run validation ───────────────────────


def test_create_run_rejects_invalid_mode():
    """The mode field is a typed enum — anything outside the allowed
    values gets a ValueError BEFORE we touch the DB, so the operator
    gets a clear 400 rather than a malformed row."""
    db = MagicMock()
    with pytest.raises(ValueError, match="mode must be one of"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id="nap-fr-rail",
            depart_at=_naive_depart(),
            mode="all_the_things",
        )


def test_create_run_single_session_requires_session_id():
    """The legacy path needs to know which OTP container to query.
    Allowing single_session with no session_id would later result in a
    `run.session_id IS NULL` row that the runner aborts at execute time —
    fail fast at create instead so the operator sees a 400."""
    db = MagicMock()
    with pytest.raises(ValueError, match=r"single_session.*requires a non-empty session_id"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id=None,
            depart_at=_naive_depart(),
            mode=runner.MODE_SINGLE_SESSION,
        )
    with pytest.raises(ValueError, match=r"single_session.*requires a non-empty session_id"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id="",
            depart_at=_naive_depart(),
            mode=runner.MODE_SINGLE_SESSION,
        )


def test_create_run_fanout_rejects_session_id():
    """Fanout mode is "every session" — passing a session_id is almost
    certainly an operator mistake (e.g. they toggled the radio but forgot
    the dropdown was still populated). Rejecting it makes the intent
    explicit and prevents confusing behaviour."""
    db = MagicMock()
    with pytest.raises(ValueError, match=r"fanout.*must not specify a session_id"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id="nap-fr-rail",
            depart_at=_naive_depart(),
            mode=runner.MODE_FANOUT,
        )


def test_create_run_rejects_invalid_direction():
    """direction is also enum-shaped — validate at the same gate so the
    error message is symmetric with the mode rejection."""
    db = MagicMock()
    with pytest.raises(ValueError, match="direction"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id="nap-fr-rail",
            depart_at=_naive_depart(),
            direction="sideways",
        )


# ─────────────────────── mode + label invariants ───────────────────────


def test_mode_constants_unchanged():
    """If these constants change, the alembic CHECK constraint and the
    pydantic regex in app/api/admin/network_coverage.py have to be
    updated in lockstep. Pinning them with a test prevents one of those
    three places from drifting."""
    assert runner.MODE_SINGLE_SESSION == "single_session"
    assert runner.MODE_FANOUT == "fanout"
    assert runner.VALID_MODES == ("single_session", "fanout")


def test_fanout_session_label_is_stable_string():
    """The matrix sidebar groups rows by session_label — using a known
    constant for fanout runs lets the JS render a distinct icon without
    fuzzy string matching."""
    assert runner.FANOUT_SESSION_LABEL == "fanout"


# ─────────────────────── fanout-pair merge logic ───────────────────────
#
# These tests mock the network + DB calls so we exercise the merge logic
# in `_execute_pair_fanout` directly. The merge has three observable
# effects we care about:
#   1. trips with the same signature collapse into one entry; the
#      shortest-duration one wins the "best" slot
#   2. session_ids on the result row enumerates every session that
#      returned at least one itinerary
#   3. status follows ok > error/timeout > no_route priority


def test_fanout_merges_same_signature_across_sessions(monkeypatch):
    """Two sessions return the same trip → one merged result, both
    session ids recorded. This is the architectural promise of fanout."""
    # Arrange — pretend session A and session B both return trip X
    # (same signature). C returns nothing.
    captured: dict[str, object] = {}

    async def fake_fetch_plan(*, session_id, **_kwargs):
        if session_id == "session-c":
            return {}, []
        return {}, [
            {
                "duration_seconds": 7200 if session_id == "session-a" else 7000,
                "num_transfers": 1,
                "legs": [{"feed_id": "SNCF" if session_id == "session-a" else "DB"}],
            }
        ]

    monkeypatch.setattr(otp_client, "fetch_plan", fake_fetch_plan)
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal(captured))

    # Trip signature stub: ignore the legs, return a fixed signature
    # so both fake trips merge into one entry.
    import app.journey.signature as sig_module

    monkeypatch.setattr(sig_module, "trip_signature", lambda *_a, **_k: "fixed-sig")

    # Stub the recorder so we don't try to write to a real journey_searches row.
    _stub_recorder(monkeypatch)

    import asyncio

    asyncio.run(
        runner._execute_pair_fanout(
            run_id=_uuid(),
            session_ids=["session-a", "session-b", "session-c"],
            engine_by_session={},  # all sessions default to OTP via planner_dispatch
            origin=_make_hub("paris-gdl"),
            dest=_make_hub("milano-centrale"),
            depart_at=_naive_depart(),
        )
    )

    # Assert — exactly one NetworkCoverageResult row was added, status
    # is ok, session_ids enumerates the two sessions that found trips,
    # and the BEST trip (shortest = 7000s from session-b) drives the
    # best_* columns.
    added_rows = captured["added_results"]
    assert isinstance(added_rows, list)
    assert len(added_rows) == 1
    row = added_rows[0]
    assert row.status == "ok"
    assert row.num_itineraries == 1, "same signature must merge to one trip"
    assert row.best_duration_seconds == 7000, "shortest-duration trip wins"
    assert sorted(row.session_ids or []) == ["session-a", "session-b"]
    # session-c found nothing, so it's NOT in session_ids
    assert "session-c" not in (row.session_ids or [])


def test_fanout_status_is_no_route_when_no_session_returns_trips(monkeypatch):
    """All sessions cleanly return 0 itineraries → status='no_route'
    (legitimate, no service on this date), session_ids is None."""
    captured: dict[str, object] = {}

    async def fake_fetch_plan(**_kwargs):
        return {}, []

    monkeypatch.setattr(otp_client, "fetch_plan", fake_fetch_plan)
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal(captured))
    _stub_recorder(monkeypatch)

    import asyncio

    asyncio.run(
        runner._execute_pair_fanout(
            run_id=_uuid(),
            session_ids=["session-a", "session-b"],
            engine_by_session={},
            origin=_make_hub("paris-gdl"),
            dest=_make_hub("bern"),
            depart_at=_naive_depart(),
        )
    )

    added_rows = captured["added_results"]
    assert isinstance(added_rows, list)
    assert len(added_rows) == 1
    row = added_rows[0]
    assert row.status == "no_route"
    assert row.session_ids is None
    assert row.num_itineraries == 0


def test_fanout_status_is_ok_when_any_session_returns_trips(monkeypatch):
    """Mixed bag — one session times out, another succeeds.
    Status should follow ok > error/timeout > no_route — i.e. ok wins."""
    captured: dict[str, object] = {}

    async def fake_fetch_plan(*, session_id, **_kwargs):
        if session_id == "session-error":
            raise TimeoutError("simulated OTP timeout")
        return {}, [
            {
                "duration_seconds": 4800,
                "num_transfers": 0,
                "legs": [{"feed_id": "SBB"}],
            }
        ]

    monkeypatch.setattr(otp_client, "fetch_plan", fake_fetch_plan)
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal(captured))

    import app.journey.signature as sig_module

    monkeypatch.setattr(sig_module, "trip_signature", lambda *_a, **_k: "sig-x")
    _stub_recorder(monkeypatch)

    import asyncio

    asyncio.run(
        runner._execute_pair_fanout(
            run_id=_uuid(),
            session_ids=["session-error", "session-ok"],
            engine_by_session={},
            origin=_make_hub("zurich-hb"),
            dest=_make_hub("bern"),
            depart_at=_naive_depart(),
        )
    )

    added_rows = captured["added_results"]
    assert isinstance(added_rows, list)
    assert len(added_rows) == 1
    row = added_rows[0]
    assert row.status == "ok"
    assert row.session_ids == ["session-ok"]


# ─────────────────────── extracted-helper tests ───────────────────────
#
# These tests target the four helpers extracted from `_execute_pair_fanout`
# (PR #36 follow-up to drop Sonar S3776 cognitive complexity). Each is
# independently testable now, so each gets its own focused test.


def test_derive_fanout_status_priority():
    """Status priority is ok > error/timeout > no_route. The whole point
    of fanout is "ok wins as soon as one session returned trips", so
    any_ok must trump any_error_or_timeout."""
    assert runner._derive_fanout_status(any_ok=True, any_error_or_timeout=False) == "ok"
    assert runner._derive_fanout_status(any_ok=True, any_error_or_timeout=True) == "ok"
    assert runner._derive_fanout_status(any_ok=False, any_error_or_timeout=True) == "error"
    assert runner._derive_fanout_status(any_ok=False, any_error_or_timeout=False) == "no_route"


def test_merge_one_trip_into_signatures_first_trip_creates_slot():
    """A signature seen for the first time creates a new slot with the
    trip as the 'best' and the session in session_ids."""
    by_sig: dict[str, dict[str, object]] = {}
    ops: list[str] = []
    trip = {
        "duration_seconds": 6000,
        "num_transfers": 0,
        "legs": [{"feed_id": "SNCF"}],
    }
    runner._merge_one_trip_into_signatures(
        sid="session-a",
        trip=trip,
        sig="sig-1",
        by_signature=by_sig,
        operators_union=ops,
    )
    assert "sig-1" in by_sig
    assert by_sig["sig-1"]["session_ids"] == ["session-a"]
    assert by_sig["sig-1"]["best"] is trip
    assert ops == ["SNCF"]


def test_merge_one_trip_into_signatures_shorter_wins_best():
    """A second trip with same signature but shorter duration replaces
    'best'; the session id list grows but no duplicates."""
    by_sig: dict[str, dict[str, object]] = {}
    ops: list[str] = []
    long_trip = {"duration_seconds": 8000, "num_transfers": 1, "legs": [{"feed_id": "DB"}]}
    short_trip = {"duration_seconds": 5000, "num_transfers": 0, "legs": [{"feed_id": "SBB"}]}

    runner._merge_one_trip_into_signatures(
        sid="session-a", trip=long_trip, sig="sig-x", by_signature=by_sig, operators_union=ops
    )
    runner._merge_one_trip_into_signatures(
        sid="session-b", trip=short_trip, sig="sig-x", by_signature=by_sig, operators_union=ops
    )
    # Same-signature trips collapse; shortest wins.
    assert by_sig["sig-x"]["best"] is short_trip
    assert sorted(by_sig["sig-x"]["session_ids"]) == ["session-a", "session-b"]
    # Operators union is order-preserving and dedup'd.
    assert ops == ["DB", "SBB"]


def test_merge_one_trip_into_signatures_no_duplicate_session_id():
    """Same session contributing two trips for the same signature must
    not show up twice in session_ids."""
    by_sig: dict[str, dict[str, object]] = {}
    ops: list[str] = []
    trip1 = {"duration_seconds": 7000, "num_transfers": 0, "legs": []}
    trip2 = {"duration_seconds": 7500, "num_transfers": 0, "legs": []}

    runner._merge_one_trip_into_signatures(
        sid="session-a", trip=trip1, sig="s", by_signature=by_sig, operators_union=ops
    )
    runner._merge_one_trip_into_signatures(
        sid="session-a", trip=trip2, sig="s", by_signature=by_sig, operators_union=ops
    )
    assert by_sig["s"]["session_ids"] == ["session-a"]


def test_merge_fanout_results_no_trips_returns_no_route(monkeypatch):
    """All sessions return empty trip lists → status='no_route', empty
    by_signature, empty sessions_with_trips, empty operators_union."""
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal({}))
    per_session: list[runner._FanoutSub] = [
        ("session-a", "no_route", {}, [], 100),
        ("session-b", "no_route", {}, [], 110),
    ]
    status, by_sig, with_trips, ops = runner._merge_fanout_results(per_session)
    assert status == "no_route"
    assert by_sig == {}
    assert with_trips == []
    assert ops == []


def test_merge_fanout_results_error_only_returns_error(monkeypatch):
    """All sessions timed out / errored → status='error'."""
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal({}))
    per_session: list[runner._FanoutSub] = [
        ("session-a", "timeout", {}, [], 5000),
        ("session-b", "error", {}, [], 200),
    ]
    status, _by_sig, with_trips, _ops = runner._merge_fanout_results(per_session)
    assert status == "error"
    assert with_trips == []


# ─────────────────────── API endpoint tests ───────────────────────
#
# These exercise the `POST /api/admin/network-coverage/runs` route
# function directly (bypassing FastAPI's DI), validating each branch of
# the (mode, session_id) gate added in PR #36. The route delegates to
# `runner.create_run` after validation, so we mock the runner and the DB
# get/execute calls — the route's job is to translate (mode, session_id)
# into the right 400/404/409/201 outcome before calling the runner.


def _make_run_create_body(**overrides):
    """A minimal RunCreate body suitable for the POST /runs endpoint."""
    from app.api.admin.network_coverage import RunCreate

    body = {
        "session_id": "nap-fr-rail",
        "depart_at": _naive_depart(),
        "direction": "both",
        "mode": "single_session",
    }
    body.update(overrides)
    return RunCreate(**body)


def _fake_actor():
    a = MagicMock()
    a.id = _uuid()
    return a


def _fake_run_row(**overrides):
    """A NetworkCoverageRun stand-in that satisfies `_run_to_summary`."""
    from datetime import UTC, datetime

    r = MagicMock()
    r.id = _uuid()
    r.session_id = overrides.get("session_id", "nap-fr-rail")
    r.session_label = overrides.get("session_label", "FR rail")
    r.depart_at = datetime(2026, 5, 18, 8, 0, 0, tzinfo=UTC)
    r.started_at = datetime(2026, 5, 18, 8, 0, 0, tzinfo=UTC)
    r.finished_at = None
    r.status = overrides.get("status", "pending")
    r.direction = overrides.get("direction", "both")
    r.mode = overrides.get("mode", "single_session")
    r.total_pairs = 650
    r.completed_pairs = 0
    r.ok_pairs = 0
    r.no_route_pairs = 0
    r.error_pairs = 0
    return r


def test_api_post_runs_rejects_invalid_direction():
    """The route checks direction BEFORE any DB lookups, so a bad value
    fast-fails with 400 and no session_id resolution."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(direction="sideways"),
            bg=BackgroundTasks(),
            db=MagicMock(),
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 400
    assert "direction" in str(exc.value.detail).lower()


def test_api_post_runs_single_session_missing_session_id_400():
    """mode=single_session with no session_id → 400 from the API
    layer (before the runner sees it)."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(session_id=None),
            bg=BackgroundTasks(),
            db=MagicMock(),
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 400
    assert "session_id" in str(exc.value.detail).lower()


def test_api_post_runs_single_session_unknown_session_404():
    """db.get(SessionRow, ...) returns None → 404."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(session_id="missing-session"),
            bg=BackgroundTasks(),
            db=db,
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 404


def test_api_post_runs_single_session_not_serving_400():
    """Session exists but its state isn't 'serving' → 400 with explanation."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    session_row = MagicMock()
    session_row.state = "building"
    db.get.return_value = session_row
    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(),
            bg=BackgroundTasks(),
            db=db,
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 400
    assert "serving" in str(exc.value.detail).lower()


def test_api_post_runs_fanout_with_session_id_400():
    """mode=fanout MUST NOT carry a session_id."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(mode="fanout", session_id="nap-fr-rail"),
            bg=BackgroundTasks(),
            db=MagicMock(),
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 400
    assert "fanout" in str(exc.value.detail).lower()


def test_api_post_runs_fanout_no_eligible_session_409(monkeypatch):
    """mode=fanout with zero serving+include_in_fanout sessions → 409.
    The route runs the SELECT for an eligible session at create time so
    the UI gets a meaningful "you have no fanout sessions" failure
    instead of a 'failed' run row in the sidebar."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    # Simulate "no eligible session" — db.execute(...).scalars().first() == None
    eligible_chain = MagicMock()
    eligible_chain.scalars.return_value.first.return_value = None
    db.execute.return_value = eligible_chain
    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_run_create_body(mode="fanout", session_id=None),
            bg=BackgroundTasks(),
            db=db,
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 409


def test_api_post_runs_single_session_happy_path_returns_summary(monkeypatch):
    """All gates pass → runner.create_run is called and the response
    summary echoes the run row's mode and session_id."""
    from fastapi import BackgroundTasks

    from app.api.admin import network_coverage as api
    from app.models.sessions import SessionState

    db = MagicMock()
    session_row = MagicMock()
    session_row.state = SessionState.SERVING.value
    db.get.return_value = session_row

    fake_run = _fake_run_row(mode="single_session")
    monkeypatch.setattr(api.runner, "create_run", lambda *_a, **_k: fake_run)
    monkeypatch.setattr(api.runner, "execute_run", lambda *_a, **_k: None)

    summary = api.create_run(
        body=_make_run_create_body(),
        bg=BackgroundTasks(),
        db=db,
        actor=_fake_actor(),
    )
    assert summary.mode == "single_session"
    assert summary.session_id == "nap-fr-rail"


def test_api_post_runs_fanout_happy_path_returns_summary(monkeypatch):
    """mode=fanout, eligible session exists → runner.create_run gets the
    fanout mode and the response carries mode='fanout'."""
    from fastapi import BackgroundTasks

    from app.api.admin import network_coverage as api

    db = MagicMock()
    eligible_chain = MagicMock()
    eligible_chain.scalars.return_value.first.return_value = MagicMock(id="nap-eu-corridors")
    db.execute.return_value = eligible_chain

    fake_run = _fake_run_row(session_id=None, session_label="fanout", mode="fanout")
    monkeypatch.setattr(api.runner, "create_run", lambda *_a, **_k: fake_run)
    monkeypatch.setattr(api.runner, "execute_run", lambda *_a, **_k: None)

    summary = api.create_run(
        body=_make_run_create_body(mode="fanout", session_id=None),
        bg=BackgroundTasks(),
        db=db,
        actor=_fake_actor(),
    )
    assert summary.mode == "fanout"
    assert summary.session_id is None


def test_api_run_to_summary_back_compat_with_missing_mode():
    """Legacy test-fixture rows without a `mode` attribute must
    serialise as 'single_session' (the alembic server_default). Guards
    against the getattr(run, 'mode', ...) safety net being dropped."""
    from app.api.admin.network_coverage import _run_to_summary

    legacy_run = _fake_run_row()
    # Strip the attribute the way an unmigrated fixture would.
    del legacy_run.mode

    summary = _run_to_summary(legacy_run)
    assert summary.mode == "single_session"


# ─────────────────────── helpers ───────────────────────


def _naive_depart():
    """A timezone-naive datetime for depart_at — runner doesn't enforce
    timezone-aware at create time (the API normalises before calling).
    Validation paths under test don't read this field, so the value
    doesn't matter — just needs to be a datetime."""
    from datetime import datetime

    return datetime(2026, 5, 18, 8, 0, 0)


def _uuid():
    import uuid

    return uuid.uuid4()


def _make_hub(slug: str):
    """A minimal Hub for the runner's signature."""
    from app.network_coverage.hubs import Hub

    return Hub(id=slug, name=slug.replace("-", " "), short=slug[:5], region="", lat=48.0, lon=2.0)


def _stub_recorder(monkeypatch):
    """Stub the journey_searches recorder so the pair executor doesn't try
    to write to a real DB. The recorder is purely for click-cell drilldown
    persistence — orthogonal to the merge logic we're testing here."""
    fake_search = MagicMock()
    fake_search.id = _uuid()

    monkeypatch.setattr(runner.recorder, "begin_search", lambda *_a, **_k: fake_search)
    monkeypatch.setattr(runner.recorder, "record_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.recorder, "finish_search", lambda *_a, **_k: None)


class _FakeSessionLocal:
    """A SessionLocal stand-in that records db.add() calls + provides the
    minimal surface the runner expects (commit, rollback, get, execute,
    context-manager). Captures added NetworkCoverageResult rows for
    assertion."""

    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured
        captured.setdefault("added_results", [])

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def add(self, row) -> None:
        # Only capture rows we actually care about asserting on.
        from app.models import NetworkCoverageResult

        if isinstance(row, NetworkCoverageResult):
            self._captured["added_results"].append(row)  # type: ignore[union-attr]

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def get(self, _model, _key):
        # Return a truthy stand-in so recorder.record_execution gets called.
        m = MagicMock()
        m.id = "any-session"
        return m

    def execute(self, _stmt):
        # The runner's UPDATE-on-counters path — no-op in tests.
        return MagicMock()
