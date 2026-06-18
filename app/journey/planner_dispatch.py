"""Pick the journey-planner backend for a given session.

Both `otp_client` and `motis_client` expose a `fetch_plan(...)` coroutine
with the same signature and return shape — the dispatcher just returns
the right module so callers can write:

    planner = get_planner(session)
    raw, trips = await planner.fetch_plan(session_id=session.id, ...)

The single source of truth for which backend a session uses is the
`sessions.engine` column (added in alembic
`20260618_0900_session_engine`). Default is `'otp'` — every pre-existing
session backfills to that, so production behaviour is identical for
every existing session until an operator explicitly creates a MOTIS one.

Unknown engine values raise `ValueError` rather than silently falling
back to OTP — a typo'd engine should be loud, not subtly wrong.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from ..models import Session as SessionRow
from . import motis_client, otp_client


class _FetchPlan(Protocol):
    """The fetch_plan signature `otp_client` and `motis_client` both expose.

    Declared on the dispatcher so mypy knows the precise return type at
    every callsite — without this, the dispatched call decays to `Any`
    (ModuleType.fetch_plan is untyped) and breaks `--strict` callers
    that return the unpacked `trips` with a concrete annotation.
    """

    async def __call__(
        self,
        *,
        session_id: str,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        when: datetime,
        timeout_ms: int,
        num_itineraries: int = ...,
        search_window_seconds: int = ...,
        from_stop_id: str | None = ...,
        to_stop_id: str | None = ...,
        session_timezone: str | None = ...,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...


class _Planner(Protocol):
    """Either `otp_client` or `motis_client`, narrowed to what we use."""

    fetch_plan: _FetchPlan


def get_planner(session: SessionRow) -> _Planner:
    """Return the journey-planner module to use for this session."""
    # `getattr` with default keeps the dispatcher usable from old test
    # fixtures that build `SessionRow` instances without an engine attribute
    # (e.g. tests written before P1 landed). At runtime the DB column is
    # NOT NULL so the attribute is always present on real sessions.
    return planner_for_engine(getattr(session, "engine", None) or "otp")


def planner_for_engine(engine: str) -> _Planner:
    """Return the planner module for a raw engine string.

    Used by code paths that snapshot the engine once (e.g. the coverage
    runner reads `{sid: engine}` upfront and passes it down per pair to
    avoid a DB round-trip per fetch_plan call).
    """
    if engine == "otp":
        return cast(_Planner, otp_client)
    if engine == "motis":
        return cast(_Planner, motis_client)
    raise ValueError(f"Unknown session engine: {engine!r} (expected 'otp' or 'motis')")
