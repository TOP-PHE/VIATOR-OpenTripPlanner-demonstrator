"""Integration: journeys stored as canonical JSON (#303 PR 4a, design D1, §5.5).

On a real PostgreSQL:

- the migration `20261104_1200_canonical_storage` deletes every coverage run (and its results)
  and every recorded journey search (and its executions and trips), keeps hubs, sessions and
  users, and swaps the columns (`journey_trips.legs` → `journey`,
  `network_coverage_results.external_itineraries` → `external_journeys`);
- a journey written by the recorder comes back equal from `journey_trips.journey`, through the
  coverage scorer's read (`runner._fetch_viator_journeys_for_search`) and the modal's read
  (`_fetch_trips_by_search`, the canonical JSON with its summary columns, #303 PR 4b);
- ÖBB's journeys stored on a result row come back as canonical journeys.

Invented values only (ZZ hubs, numbers with 99). Fresh-DB harness like the other integration
tests; skips when Postgres is down.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app.journey.canonical import CanonicalJourney
from tests.unit import canonical_fixtures as fx

_USER = "99999999-9999-4999-8999-999999999973"
SID = "zz-store-99"
DEPART = datetime(2099, 9, 9, 9, 0, tzinfo=UTC)
_BEFORE = "20261027_1200_verify_progress"


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping canonical storage test")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


def _fresh_db(url: str, revision: str) -> Config:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    engine.dispose()
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, revision)
    from app import config_service

    config_service.invalidate_cache()
    return cfg


def _seed_user_and_session(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO users (id, email, name, password_hash, role) "
            "VALUES (:u, 'zz-store@zz.invalid', 'ZZ', 'x', 'platform_admin')"
        ),
        {"u": _USER},
    )
    conn.execute(
        text(
            "INSERT INTO sessions (id, name, category, state, engine, created_by) "
            "VALUES (:s, 'ZZ store', 'NAP', 'serving', 'motis', :u)"
        ),
        {"s": SID, "u": _USER},
    )


def _journey() -> CanonicalJourney:
    return fx.journey_from_legs(
        [
            {
                "mode": "RAIL",
                "from_stop_id": "zz_a",
                "from_lat": 45.99,
                "from_lon": 9.99,
                "to_stop_id": "zz_b",
                "to_lat": 46.99,
                "to_lon": 9.99,
                "departure": "2099-09-09T09:09:00+00:00",
                "arrival": "2099-09-09T10:39:00+00:00",
                "route_short_name": "ZZ 99",
            }
        ],
        provider="motis",
    )


def test_the_migration_deletes_runs_and_searches_and_swaps_the_columns() -> None:
    url = _postgres_or_skip()
    cfg = _fresh_db(url, _BEFORE)
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_user_and_session(conn)
        search_id = conn.execute(
            text(
                "INSERT INTO journey_searches (endpoint, origin_lat, origin_lon, dest_lat, "
                "dest_lon, requested_time_kind, requested_time, modes, status) VALUES "
                "('plan', 45.99, 9.99, 46.99, 9.99, 'depart_at', :t, 'TRANSIT,WALK', 'ok') "
                "RETURNING id"
            ),
            {"t": DEPART},
        ).scalar_one()
        execution_id = conn.execute(
            text(
                "INSERT INTO journey_search_executions (search_id, session_id, status) "
                "VALUES (:s, :sid, 'ok') RETURNING id"
            ),
            {"s": search_id, "sid": SID},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO journey_trips (execution_id, trip_signature, rank_in_response, "
                "duration_seconds, num_transfers, departure_at, arrival_at, modes, legs) VALUES "
                "(:e, '9999999999999999', 0, 5400, 0, :t, :t, 'RAIL', '[]')"
            ),
            {"e": execution_id, "t": DEPART},
        )
    from app.db import SessionLocal
    from app.network_coverage import runner

    with SessionLocal() as db:
        run = runner.create_run(
            db, actor_user_id=None, session_id=SID, depart_at=DEPART, direction="both"
        )
        db.commit()
        run_id = run.id
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO network_coverage_results (run_id, origin_hub_id, dest_hub_id, "
                "status, journey_search_id, external_itineraries) VALUES "
                "(:r, 'zz-a', 'zz-b', 'ok', :s, '[]')"
            ),
            {"r": run_id, "s": search_id},
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        counts = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM network_coverage_runs), "
                "(SELECT count(*) FROM network_coverage_results), "
                "(SELECT count(*) FROM journey_searches), "
                "(SELECT count(*) FROM journey_search_executions), "
                "(SELECT count(*) FROM journey_trips)"
            )
        ).one()
        assert tuple(counts) == (0, 0, 0, 0, 0)
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
    columns = inspect(engine)
    trip_columns = {c["name"]: c for c in columns.get_columns("journey_trips")}
    result_columns = {c["name"] for c in columns.get_columns("network_coverage_results")}
    assert "legs" not in trip_columns
    assert trip_columns["journey"]["nullable"] is False
    assert "external_itineraries" not in result_columns
    assert "external_journeys" in result_columns
    engine.dispose()


def test_a_recorded_journey_comes_back_equal_through_both_reads() -> None:
    url = _postgres_or_skip()
    _fresh_db(url, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_user_and_session(conn)
    engine.dispose()

    from app.api.admin.network_coverage import _fetch_trips_by_search
    from app.db import SessionLocal
    from app.journey import recorder
    from app.journey.canonical import CanonicalJourney
    from app.journey.canonical_adapters.trip_dict import to_trip_dict
    from app.network_coverage import runner

    journey = _journey()
    with SessionLocal() as db:
        search = recorder.begin_search(
            db,
            user_id=None,
            ip=None,
            endpoint="plan",
            origin_lat=45.99,
            origin_lon=9.99,
            origin_label=None,
            dest_lat=46.99,
            dest_lon=9.99,
            dest_label=None,
            requested_time_kind="depart_at",
            requested_time=DEPART,
            modes="TRANSIT,WALK",
        )
        recorder.record_execution(
            db,
            search_id=search.id,
            session_id=SID,
            graph_snapshot_id=None,
            status="ok",
            response_ms=99,
            raw_response=None,
            error_message=None,
            journeys=[journey],
        )
        db.commit()
        search_id = search.id

    with SessionLocal() as db:
        (back,) = runner._fetch_viator_journeys_for_search(db, search_id, DEPART)
        trips = _fetch_trips_by_search(db, [search_id], DEPART)
    assert back == journey
    (trip,) = trips[str(search_id)]
    expected = to_trip_dict(journey)
    assert CanonicalJourney.model_validate(trip["journey"]) == journey
    assert (trip["duration_seconds"], trip["num_transfers"], trip["modes"]) == (
        expected["duration_seconds"],
        expected["num_transfers"],
        expected["modes"],
    )


def test_oebb_journeys_stored_on_a_result_row_come_back_as_journeys() -> None:
    url = _postgres_or_skip()
    _fresh_db(url, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_user_and_session(conn)
    engine.dispose()

    from app.db import SessionLocal
    from app.journey.canonical import CanonicalJourney
    from app.models import NetworkCoverageResult
    from app.network_coverage import oebb_journeys, runner

    oebb = fx.oebb_journeys(
        [
            {
                "mode": "RAIL",
                "from_uic": "UIC:8103000",
                "to_uic": "OEBB:904050",
                "dep": "2099-09-09T11:09",
                "arr": "2099-09-09T12:39",
                "route": "ZZ 99",
            }
        ]
    )
    with SessionLocal() as db:
        run = runner.create_run(
            db, actor_user_id=None, session_id=SID, depart_at=DEPART, direction="single"
        )
        db.flush()
        row = NetworkCoverageResult(
            run_id=run.id, origin_hub_id="zz-a", dest_hub_id="zz-b", status="ok"
        )
        runner._persist_alignment_on_row(row, [], oebb)
        db.add(row)
        db.commit()
        row_id = row.id

    with SessionLocal() as db:
        stored = db.get(NetworkCoverageResult, row_id)
        assert stored is not None
        assert stored.external_alignment_tier == "one_sided_oebb"
        stored_journeys = stored.external_journeys
    assert stored_journeys is not None
    (back,) = [CanonicalJourney.model_validate(item) for item in stored_journeys]
    assert back == oebb[0]
    (leg,) = oebb_journeys.scoring_legs(back)
    assert (leg["from_stop_id"], leg["to_stop_id"]) == ("UIC:8103000", "OEBB:904050")
    # 11:09 Vienna (CEST) is 09:09 UTC.
    assert leg["departure"] == "2099-09-09T09:09:00+00:00"
    # A uuid primary key came back: the row was really written and read.
    assert isinstance(row_id, uuid.UUID)
