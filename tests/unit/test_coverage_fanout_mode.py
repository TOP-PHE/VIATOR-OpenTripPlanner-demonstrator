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

    monkeypatch.setattr(runner.otp_client, "fetch_plan", fake_fetch_plan)
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

    monkeypatch.setattr(runner.otp_client, "fetch_plan", fake_fetch_plan)
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal(captured))
    _stub_recorder(monkeypatch)

    import asyncio

    asyncio.run(
        runner._execute_pair_fanout(
            run_id=_uuid(),
            session_ids=["session-a", "session-b"],
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

    monkeypatch.setattr(runner.otp_client, "fetch_plan", fake_fetch_plan)
    monkeypatch.setattr(runner, "SessionLocal", _FakeSessionLocal(captured))

    import app.journey.signature as sig_module

    monkeypatch.setattr(sig_module, "trip_signature", lambda *_a, **_k: "sig-x")
    _stub_recorder(monkeypatch)

    import asyncio

    asyncio.run(
        runner._execute_pair_fanout(
            run_id=_uuid(),
            session_ids=["session-error", "session-ok"],
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
