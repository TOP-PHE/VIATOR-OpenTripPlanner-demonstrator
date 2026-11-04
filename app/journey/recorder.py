"""Search recording — `journey_searches` + executions + trips writers.

Used by the fanout endpoint (step 14), the journey/plan endpoint, the replay and the coverage
runner. A trip row stores the canonical journey (`journey_trips.journey`, #303 PR 4a); its summary
columns and `trip_signature` are written from the journey with today's rules, through the trip
dict bridge, so the fanout merge and the reports keep their behaviour (design D5).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session as DbSession

from ..models import JourneySearch, JourneySearchExecution, JourneyTrip
from .canonical import CanonicalJourney
from .canonical_adapters.trip_dict import to_trip_dict
from .signature import trip_signature


def begin_search(
    db: DbSession,
    *,
    user_id: uuid.UUID | None,
    ip: str | None,
    endpoint: str,
    origin_lat: float,
    origin_lon: float,
    origin_label: str | None,
    dest_lat: float,
    dest_lon: float,
    dest_label: str | None,
    requested_time_kind: str,
    requested_time: datetime,
    modes: str,
    replay_of_search_id: uuid.UUID | None = None,
) -> JourneySearch:
    """Create a journey_searches row in 'pending' state. Returns the row.

    Status is updated to 'ok'/'partial'/etc. via `finish_search()` once the
    fanout completes.
    """
    s = JourneySearch(
        ts=datetime.now(UTC),
        user_id=user_id,
        ip=ip,
        endpoint=endpoint,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_label=origin_label,
        dest_lat=dest_lat,
        dest_lon=dest_lon,
        dest_label=dest_label,
        requested_time_kind=requested_time_kind,
        requested_time=requested_time,
        modes=modes,
        status="ok",
        replay_of_search_id=replay_of_search_id,
    )
    db.add(s)
    db.flush()
    return s


def record_execution(
    db: DbSession,
    *,
    search_id: uuid.UUID,
    session_id: str,
    graph_snapshot_id: uuid.UUID | None,
    status: str,
    response_ms: int,
    raw_response: dict[str, Any] | None,
    error_message: str | None,
    journeys: Sequence[CanonicalJourney],
) -> JourneySearchExecution:
    """Record one (search x session) execution + its journeys, in answer order."""
    exe = JourneySearchExecution(
        search_id=search_id,
        session_id=session_id,
        graph_snapshot_id=graph_snapshot_id,
        status=status,
        num_itineraries=len(journeys),
        response_ms=response_ms,
        raw_response=raw_response,
        error_message=error_message,
    )
    db.add(exe)
    db.flush()

    for rank, journey in enumerate(journeys):
        db.add(
            journey_trip(db, execution_id=exe.id, session_id=session_id, rank=rank, journey=journey)
        )
    return exe


def journey_trip(
    db: DbSession,
    *,
    execution_id: uuid.UUID,
    session_id: str,
    rank: int,
    journey: CanonicalJourney,
) -> JourneyTrip:
    """The `journey_trips` row of one journey: the canonical JSON, and the summary columns the
    reports and the coverage export read without it."""
    trip = to_trip_dict(journey)
    return JourneyTrip(
        execution_id=execution_id,
        trip_signature=trip_signature(db, session_id=session_id, legs=trip["legs"]),
        rank_in_response=rank,
        duration_seconds=int(trip["duration_seconds"]),
        num_transfers=int(trip["num_transfers"]),
        departure_at=trip["departure_at"],
        arrival_at=trip["arrival_at"],
        modes=trip["modes"],
        journey=journey.model_dump(mode="json"),
        fare=None,
    )


def finish_search(
    db: DbSession,
    search: JourneySearch,
    *,
    total_response_ms: int,
    total_trips_unique: int,
    status: str,
) -> None:
    search.total_response_ms = total_response_ms
    search.total_trips_unique = total_trips_unique
    search.status = status
