"""Integration: a coverage result row never points at a rolled-back journey
search (issue #397, item 2).

Before #392's durable part, `_record_pair_journey_search` committed the
journey search and the result row in separate steps: a failed commit could
leave a result row whose `journey_search_id` named a search that was never
stored. Since #392 the search (in a savepoint) and the result row are ONE
transaction (`runner._record_then_persist` / `_write_pair_once`), and the
search id is read only once the savepoint is released. These tests pin that
on a real PostgreSQL, through the real write path:

- a commit that fails once is retried, and the pair ends with exactly one
  search, the one its result row points at;
- a commit that keeps failing stores neither the search nor the result row;
- a recorder write refused by the database inside its savepoint leaves the
  result row with no search id (and the recorder's error), and no search.

Invented values only (ZZ hubs, numbers with 99). Fresh-DB harness like the
other integration tests; skips when Postgres is down.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError

from alembic import command

_USER = "99999999-9999-4999-8999-999999999972"
SID = "zz-pair-99"
DEPART = datetime(2099, 9, 9, 9, 0, tzinfo=UTC)


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping coverage pair-write test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def run_id() -> uuid.UUID:
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    from app import config_service

    config_service.invalidate_cache()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash, role) "
                "VALUES (:u, 'zz-pair@zz.invalid', 'ZZ', 'x', 'platform_admin')"
            ),
            {"u": _USER},
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, name, category, state, engine, created_by) "
                "VALUES (:s, 'ZZ pair', 'NAP', 'serving', 'motis', :u)"
            ),
            {"s": SID, "u": _USER},
        )
    engine.dispose()

    from app.db import SessionLocal
    from app.network_coverage import runner

    with SessionLocal() as db:
        run = runner.create_run(
            db, actor_user_id=None, session_id=SID, depart_at=DEPART, direction="both"
        )
        db.commit()
        return run.id


def _hub(name: str, lat: float) -> Any:
    from app.network_coverage.hubs import Hub

    return Hub(id=f"zz-{name}", name=f"ZZ {name}", short=name, region="", lat=lat, lon=9.99)


ORIGIN = _hub("a", 45.99)
DEST = _hub("b", 46.99)


def _trip() -> dict[str, Any]:
    return {
        "departure_at": "2099-09-09T09:09:00Z",
        "arrival_at": "2099-09-09T10:39:00Z",
        "duration_seconds": 5400,
        "num_transfers": 0,
        "modes": "RAIL",
        "legs": [
            {
                "mode": "RAIL",
                "from_stop_id": "zz_a",
                "from_lat": 45.99,
                "from_lon": 9.99,
                "to_stop_id": "zz_b",
                "to_lat": 46.99,
                "to_lon": 9.99,
                "departure": "2099-09-09T09:09:00Z",
                "arrival": "2099-09-09T10:39:00Z",
                "route_short_name": "ZZ 99",
                "feed_id": "ZZ-99",
            }
        ],
    }


def _write(run_id: uuid.UUID) -> bool:
    """One pair write through the real path (`_record_then_persist`)."""
    from app.network_coverage import runner

    return runner._record_then_persist(
        runner._record_pair_journey_search,
        {
            "run_id": run_id,
            "session_id": SID,
            "origin": ORIGIN,
            "dest": DEST,
            "depart_at": DEPART,
            "status": "ok",
            "response_ms": 99,
            "raw": {"zz": 99},
            "trips": [_trip()],
            "error_message": None,
        },
        {
            "run_id": run_id,
            "origin": ORIGIN,
            "dest": DEST,
            "status": "ok",
            "response_ms": 99,
            "num_itineraries": 1,
            "best_duration_seconds": 5400,
            "best_num_transfers": 0,
            "best_operators": "ZZ-99",
        },
    )


def _stored(run_id: uuid.UUID) -> tuple[list[Any], int, int]:
    """(result rows of the run, journey searches, executions)."""
    from app.db import SessionLocal
    from app.models import JourneySearch, JourneySearchExecution, NetworkCoverageResult

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(NetworkCoverageResult).where(NetworkCoverageResult.run_id == run_id)
            ).scalars()
        )
        db.expunge_all()
        searches = db.execute(select(func.count()).select_from(JourneySearch)).scalar_one()
        executions = db.execute(
            select(func.count()).select_from(JourneySearchExecution)
        ).scalar_one()
        return rows, searches, executions


class _FailingCommits:
    """Makes the next `times` commits that carry a result row fail with a
    dropped connection (a transient error the pair write retries). A commit
    of the search alone (were it ever committed apart) goes through: the
    search would then stay behind without its result row."""

    def __init__(self, times: int) -> None:
        self.left = times
        self.failed = 0

    def __call__(self, session: Any) -> None:
        from app.models import NetworkCoverageResult

        pending = [*session.new, *session.identity_map.values()]
        if self.left > 0 and any(isinstance(o, NetworkCoverageResult) for o in pending):
            self.left -= 1
            self.failed += 1
            raise OperationalError("COMMIT", {}, Exception("zz-99 server closed the connection"))


@pytest.fixture
def failing_commits(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.db import SessionLocal
    from app.network_coverage import runner

    monkeypatch.setattr(runner, "_WRITE_RETRY_DELAYS_S", (0.0, 0.0))
    installed: list[_FailingCommits] = []

    def _install(times: int) -> _FailingCommits:
        failing = _FailingCommits(times)
        event.listen(SessionLocal, "before_commit", failing)
        installed.append(failing)
        return failing

    yield _install
    for failing in installed:
        event.remove(SessionLocal, "before_commit", failing)


def test_a_failed_commit_is_retried_and_the_row_points_at_the_one_search(
    run_id: uuid.UUID, failing_commits: Any
) -> None:
    from app.db import SessionLocal
    from app.models import JourneySearch

    failing = failing_commits(1)
    assert _write(run_id) is True
    assert failing.failed == 1

    rows, searches, executions = _stored(run_id)
    assert len(rows) == 1
    assert (searches, executions) == (1, 1)
    with SessionLocal() as db:
        search = db.get(JourneySearch, rows[0].journey_search_id)
        assert search is not None
        assert (search.origin_label, search.dest_label) == ("ZZ a", "ZZ b")


def test_a_commit_that_keeps_failing_stores_neither_search_nor_result(
    run_id: uuid.UUID, failing_commits: Any
) -> None:
    from app.network_coverage import runner

    failing = failing_commits(99)
    assert _write(run_id) is False
    assert failing.failed == len(runner._WRITE_RETRY_DELAYS_S) + 1
    assert _stored(run_id) == ([], 0, 0)


def test_a_refused_recorder_write_leaves_the_row_without_a_search(
    run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database refuses the search inside its savepoint (a check
    constraint): the savepoint is rolled back, the result row is written
    with no search id and the recorder's error, and no search is stored."""
    from app.journey import recorder

    real_begin = recorder.begin_search

    def _refused_endpoint(db: Any, **kw: Any) -> Any:
        return real_begin(db, **{**kw, "endpoint": "zz-not-an-endpoint"})

    monkeypatch.setattr(recorder, "begin_search", _refused_endpoint)
    assert _write(run_id) is True

    rows, searches, executions = _stored(run_id)
    assert len(rows) == 1
    assert rows[0].journey_search_id is None
    assert rows[0].error_message.startswith("[recorder] IntegrityError")
    assert (searches, executions) == (0, 0)
