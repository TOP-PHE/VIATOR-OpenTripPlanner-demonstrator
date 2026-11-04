"""#303 PR 4a — the recorder stores canonical journeys (`journey_trips.journey`).

DB-free: a fake session collects what `record_execution` adds. The round trip through the
JSONB column is `model_dump(mode="json")` then `CanonicalJourney.model_validate`; the summary
columns and `trip_signature` are written from the journey with today's rules (the trip dict
bridge), so the reports and the fanout merge keep their behaviour (design D5).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.journey import recorder
from app.journey.canonical import CanonicalJourney
from app.journey.canonical_adapters import hafas_adapter, motis_adapter, otp_adapter
from app.journey.canonical_adapters.trip_dict import to_trip_dict
from app.journey.signature import trip_signature
from app.models import JourneySearchExecution, JourneyTrip
from tests.unit import canonical_fixtures as fx


class _Db:
    """What `record_execution` touches: add/flush, and the lookups `trip_signature` makes
    (no `stations_xref` row, no route alias)."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self._lookups = MagicMock()
        self._lookups.get.return_value = None
        self._lookups.query.return_value.filter.return_value.first.return_value = None

    def add(self, row: Any) -> None:
        self.added.append(row)

    def flush(self) -> None:
        for row in self.added:
            if isinstance(row, JourneySearchExecution) and row.id is None:
                row.id = uuid.uuid4()

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._lookups.get(*args, **kwargs)

    def query(self, *args: Any, **kwargs: Any) -> Any:
        return self._lookups.query(*args, **kwargs)


def _journeys() -> list[CanonicalJourney]:
    return [
        *motis_adapter.journeys_from_plan(fx.MOTIS_PLAN, session_id="eu19"),
        *otp_adapter.journeys_from_plan(fx.OTP_PLAN, session_id="ch"),
        *hafas_adapter.journeys_from_trip_search(fx.HAFAS_TRIP_SEARCH),
    ]


def _record(journeys: list[CanonicalJourney]) -> tuple[JourneySearchExecution, list[JourneyTrip]]:
    db = _Db()
    exe = recorder.record_execution(
        db,  # type: ignore[arg-type]
        search_id=uuid.uuid4(),
        session_id="eu19",
        graph_snapshot_id=None,
        status="ok",
        response_ms=99,
        raw_response=None,
        error_message=None,
        journeys=journeys,
    )
    trips = [row for row in db.added if isinstance(row, JourneyTrip)]
    return exe, trips


def test_every_journey_is_stored_in_answer_order() -> None:
    journeys = _journeys()
    exe, rows = _record(journeys)
    assert exe.num_itineraries == len(journeys)
    assert [row.rank_in_response for row in rows] == list(range(len(journeys)))
    assert all(row.execution_id == exe.id for row in rows)


@pytest.mark.parametrize("index", range(len(_journeys())))
def test_the_stored_json_is_the_journey(index: int) -> None:
    journey = _journeys()[index]
    _exe, rows = _record([journey])
    (row,) = rows
    # What Postgres hands back: the JSON text parsed again.
    stored = json.loads(json.dumps(row.journey))
    assert stored["schema_version"] == "1"
    assert CanonicalJourney.model_validate(stored) == journey


@pytest.mark.parametrize("index", range(len(_journeys())))
def test_the_summary_columns_are_todays_values(index: int) -> None:
    journey = _journeys()[index]
    _exe, (row,) = _record([journey])
    trip = to_trip_dict(journey)
    assert row.duration_seconds == trip["duration_seconds"]
    assert row.num_transfers == trip["num_transfers"]
    assert row.modes == trip["modes"]
    assert datetime.fromisoformat(str(row.departure_at)) == datetime.fromisoformat(
        trip["departure_at"]
    )
    assert datetime.fromisoformat(str(row.arrival_at)) == datetime.fromisoformat(trip["arrival_at"])
    assert row.fare is None


def test_the_signature_is_computed_on_todays_legs() -> None:
    journey = _journeys()[0]
    _exe, (row,) = _record([journey])
    db = _Db()
    expected = trip_signature(db, session_id="eu19", legs=to_trip_dict(journey)["legs"])  # type: ignore[arg-type]
    assert row.trip_signature == expected


def test_no_journey_records_an_empty_execution() -> None:
    exe, rows = _record([])
    assert exe.num_itineraries == 0
    assert rows == []
