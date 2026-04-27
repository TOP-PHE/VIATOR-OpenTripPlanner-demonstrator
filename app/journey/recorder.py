"""Search recording — `journey_searches` + executions + trips writers.

Used by the fanout endpoint (step 14) and the journey/plan endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session as DbSession

from ..models import JourneySearch, JourneySearchExecution, JourneyTrip
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
    graph_snapshot_id: uuid.UUID,
    status: str,
    response_ms: int,
    raw_response: dict[str, Any] | None,
    error_message: str | None,
    trips: list[dict[str, Any]],
) -> JourneySearchExecution:
    """Record one (search x session) execution + its trips."""
    exe = JourneySearchExecution(
        search_id=search_id,
        session_id=session_id,
        graph_snapshot_id=graph_snapshot_id,
        status=status,
        num_itineraries=len(trips),
        response_ms=response_ms,
        raw_response=raw_response,
        error_message=error_message,
    )
    db.add(exe)
    db.flush()

    for rank, t in enumerate(trips):
        sig = trip_signature(db, session_id=session_id, legs=t.get("legs", []))
        db.add(
            JourneyTrip(
                execution_id=exe.id,
                trip_signature=sig,
                rank_in_response=rank,
                duration_seconds=int(t["duration_seconds"]),
                num_transfers=int(t.get("num_transfers", 0)),
                departure_at=t["departure_at"],
                arrival_at=t["arrival_at"],
                modes=t.get("modes", ""),
                legs=t.get("legs", []),
                fare=t.get("fare"),
            )
        )
    return exe


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
