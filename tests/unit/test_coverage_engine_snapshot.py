"""P1 MOTIS — coverage runner's engine-snapshot helpers.

`_resolve_session_engine` and `_snapshot_fanout_sessions` were extracted
out of `execute_run` to keep its cognitive complexity below Sonar's
S3776 threshold once the engine branches landed. Each is independently
unit-testable — they take a DB session and read sessions.engine, that's it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.network_coverage import runner


def test_resolve_session_engine_returns_otp_for_none_id():
    """Defensive: a coverage run whose session_id is None (which should
    never happen for single_session mode but the column is nullable in
    fanout mode) must NOT crash the runner — fall back to OTP."""
    db = MagicMock()
    assert runner._resolve_session_engine(db, None) == "otp"


def test_resolve_session_engine_returns_otp_when_session_was_deleted():
    """If the session was deleted between coverage-run create and execute,
    `db.get` returns None — the legacy default keeps the run alive rather
    than failing at engine resolution."""
    db = MagicMock()
    db.get.return_value = None
    assert runner._resolve_session_engine(db, "nap-fr-rail") == "otp"


def test_resolve_session_engine_reads_engine_attr():
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id="nap-de-rail", engine="motis")
    assert runner._resolve_session_engine(db, "nap-de-rail") == "motis"


def test_resolve_session_engine_defaults_otp_when_engine_attr_missing():
    """Old test fixture / legacy ORM row without the engine attribute
    falls back to 'otp' instead of raising AttributeError."""
    db = MagicMock()
    # SimpleNamespace lets us simulate a row that doesn't have the column
    db.get.return_value = SimpleNamespace(id="x")  # no engine attr
    assert runner._resolve_session_engine(db, "x") == "otp"


def test_resolve_session_engine_defaults_otp_when_engine_is_none():
    """A pre-migration row where engine is NULL in the DB → 'otp'."""
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id="x", engine=None)
    assert runner._resolve_session_engine(db, "x") == "otp"


def test_snapshot_fanout_sessions_returns_ids_and_engine_map():
    """The fanout snapshot must enumerate every serving + include_in_fanout
    session AND record each one's engine, so the per-pair runner doesn't
    need to round-trip the DB per fetch_plan call."""
    fake_rows = [
        SimpleNamespace(id="session-a", engine="otp"),
        SimpleNamespace(id="session-b", engine="motis"),
        SimpleNamespace(id="session-c", engine="otp"),
    ]

    db = MagicMock()
    # SQLAlchemy chain: db.execute(...).scalars().all() → fake_rows
    db.execute.return_value.scalars.return_value.all.return_value = fake_rows

    ids, engines = runner._snapshot_fanout_sessions(db)

    assert ids == ["session-a", "session-b", "session-c"]
    assert engines == {
        "session-a": "otp",
        "session-b": "motis",
        "session-c": "otp",
    }


def test_snapshot_fanout_sessions_handles_missing_engine_attr():
    """Same back-compat as the single-session resolver: a row without
    the engine attribute (legacy fixture) maps to 'otp', not a crash."""
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = [
        SimpleNamespace(id="legacy-session"),  # no engine attr
    ]

    ids, engines = runner._snapshot_fanout_sessions(db)
    assert ids == ["legacy-session"]
    assert engines == {"legacy-session": "otp"}


def test_snapshot_fanout_sessions_empty_when_no_serving_sessions():
    """No serving + fanout-enabled sessions → empty snapshot. The caller
    (`execute_run`) uses this to short-circuit fanout runs cleanly."""
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []

    ids, engines = runner._snapshot_fanout_sessions(db)
    assert ids == []
    assert engines == {}
