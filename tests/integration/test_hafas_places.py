"""Integration: ÖBB's places of the coverage runs, added to ÖBB's stop list once a run has ended
(the station module's design 22.20, decision 70 way a; `app/network_coverage/hafas_places.py`).

Real PostgreSQL 16 at `head` (the fixture of test_coverage_web_health.py). The runs and their
cells are written as the coverage check leaves them: ÖBB's answers through the real HAFAS
adapter, in the JSON of `external_journeys`. Checked: a run is added once it has ended, and only
once; a later answer adds its calls and gives its name; a UIC once seen and the operators are
kept; a run deleted keeps its places in the list; the totals count without naming. Invented
values only (`ZZ`, numbers with 99).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from tests.integration.test_coverage_web_health import fresh_db  # noqa: F401  (pytest fixture)
from tests.unit.test_hafas_places import NORD, OST, SUD, stored

AT = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
LATER = AT + timedelta(days=1)
EARLIER = AT - timedelta(days=1)


def _run(*, status: str = "completed", verify: bool = True, started: datetime = AT) -> uuid.UUID:
    from app.db import SessionLocal
    from app.models import NetworkCoverageRun

    with SessionLocal() as db:
        run = NetworkCoverageRun(
            session_label="zz-cov-99",
            depart_at=AT,
            status=status,
            verify_externally=verify,
            started_at=started,
        )
        db.add(run)
        db.commit()
        return run.id


def _cells(run_id: uuid.UUID, answers: list[list[Any]], verified_at: datetime | None) -> None:
    from app.db import SessionLocal
    from app.models import NetworkCoverageResult

    with SessionLocal() as db:
        for n, journeys in enumerate(answers):
            db.add(
                NetworkCoverageResult(
                    run_id=run_id,
                    origin_hub_id=f"zz-a{n}",
                    dest_hub_id=f"zz-b{n}",
                    status="ok",
                    external_journeys=journeys,
                    external_verified_at=verified_at,
                )
            )
        db.commit()


def _collect() -> Any:
    from app.db import SessionLocal
    from app.network_coverage import hafas_places

    return hafas_places.collect_pending(SessionLocal)


def _places() -> dict[str, Any]:
    from app.db import SessionLocal
    from app.models import HafasPlace

    with SessionLocal() as db:
        return {row.ext_id: row for row in db.execute(select(HafasPlace)).scalars()}


@pytest.fixture
def db_ready(fresh_db: str) -> str:  # noqa: F811
    return fresh_db


def test_an_ended_run_is_added_once_with_its_calls_and_places(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.models import HafasPlaceRun

    run_id = _run()
    rail = stored([(0, 1, 0)], [NORD, SUD])
    _cells(run_id, [rail, rail, None, []], AT)

    first = _collect()
    again = _collect()

    assert (first.runs, first.calls, first.listed, first.failed) == (1, 4, 2, 0)
    assert (again.runs, again.calls, again.listed) == (0, 0, 2)
    places = _places()
    nord, sud = places["9900001"], places["9900002"]
    assert (nord.name, nord.uic, nord.times_seen) == ("ZZ Nord", "9900001", 2)
    assert (sud.uic, sud.times_seen) == ("9911112", 2)
    assert (nord.first_seen_at, nord.last_seen_at) == (AT, AT)
    assert nord.operators == ["ZZ Bahn"]
    with SessionLocal() as db:
        added = db.get(HafasPlaceRun, run_id)
        assert added is not None
        assert (added.calls, added.places) == (4, 2)


def test_a_run_still_running_or_without_the_oebb_check_is_not_added(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.models import NetworkCoverageRun

    running = _run(status="running")
    unchecked = _run(verify=False)
    for run_id in (running, unchecked):
        _cells(run_id, [stored([(0, 1, 0)], [NORD, SUD])], AT)

    assert _collect().runs == 0
    assert _places() == {}
    with SessionLocal() as db:
        run = db.get(NetworkCoverageRun, running)
        assert run is not None
        run.status = "cancelled"
        db.commit()
    assert _collect().runs == 1
    assert set(_places()) == {"9900001", "9900002"}


def test_a_later_answer_adds_its_calls_gives_its_name_and_keeps_a_uic(db_ready: str) -> None:
    renamed = {**NORD, "name": "ZZ Nord Hbf"}
    renamed.pop("gidL")
    older = {**NORD, "name": "ZZ Nord alt"}
    first, second, late_old = _run(), _run(started=LATER), _run(started=LATER + timedelta(hours=1))
    _cells(first, [stored([(0, 1, 0)], [NORD, SUD])], AT)
    _cells(second, [stored([(0, 1, 1)], [renamed, OST])], LATER)
    # A run added last whose answer is older than the latest: its calls count, its name does not.
    _cells(late_old, [stored([(0, 1, 0)], [older, SUD])], EARLIER)

    assert _collect().runs == 3

    nord = _places()["9900001"]
    assert (nord.name, nord.uic, nord.times_seen) == ("ZZ Nord Hbf", "9900001", 3)
    assert (nord.first_seen_at, nord.last_seen_at) == (EARLIER, LATER)
    assert nord.operators == ["ZZ Bahn", "ZZ Fern"]


def test_the_operators_of_a_place_stay_bounded_in_the_database(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.network_coverage import hafas_places
    from app.network_coverage.hafas_places import SeenPlace

    for batch in range(2):
        place = SeenPlace("9900001", "ZZ Nord", 49.99, 9.99, AT, AT, calls=1)
        place.operators = {f"ZZ {batch}{n:02d}" for n in range(15)}
        with SessionLocal() as db:
            hafas_places.upsert_places(db, [place])
            db.commit()

    nord = _places()["9900001"]
    assert len(nord.operators) == hafas_places.OPERATORS_MAX
    assert nord.operators == sorted(nord.operators)
    assert nord.times_seen == 2


def test_a_deleted_run_keeps_its_places_in_the_list(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.models import HafasPlaceRun, NetworkCoverageRun

    run_id = _run()
    _cells(run_id, [stored([(0, 1, 0)], [NORD, SUD])], AT)
    _collect()

    with SessionLocal() as db:
        db.execute(delete(NetworkCoverageRun).where(NetworkCoverageRun.id == run_id))
        db.commit()
        assert db.get(HafasPlaceRun, run_id) is None

    assert set(_places()) == {"9900001", "9900002"}


def test_a_run_added_by_another_pass_is_not_counted_twice(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.network_coverage import hafas_places

    run_id = _run()
    _cells(run_id, [stored([(0, 1, 0)], [NORD, SUD])], AT)

    with SessionLocal() as db:
        assert hafas_places.collect_run(db, run_id) is not None
    with SessionLocal() as db:
        assert hafas_places.collect_run(db, run_id) is None
    assert _places()["9900001"].times_seen == 1


def test_the_totals_count_places_calls_countries_and_uic_codes(db_ready: str) -> None:
    from app.db import SessionLocal
    from app.network_coverage import hafas_places

    run_id = _run()
    _cells(run_id, [stored([(0, 1, 0), (0, 2, 0)], [NORD, SUD, OST])], AT)
    _collect()

    with SessionLocal() as db:
        totals = hafas_places.totals(db)

    assert (totals.places, totals.calls, totals.runs) == (3, 4, 1)
    assert totals.with_uic == 2
    assert sum(totals.by_country.values()) + totals.unknown_country == 3
