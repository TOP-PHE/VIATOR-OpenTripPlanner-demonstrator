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

from types import ModuleType

from ..models import Session as SessionRow
from . import motis_client, otp_client


def get_planner(session: SessionRow) -> ModuleType:
    """Return the journey-planner module to use for this session."""
    # `getattr` with default keeps the dispatcher usable from old test
    # fixtures that build `SessionRow` instances without an engine attribute
    # (e.g. tests written before P1 landed). At runtime the DB column is
    # NOT NULL so the attribute is always present on real sessions.
    return planner_for_engine(getattr(session, "engine", None) or "otp")


def planner_for_engine(engine: str) -> ModuleType:
    """Return the planner module for a raw engine string.

    Used by code paths that snapshot the engine once (e.g. the coverage
    runner reads `{sid: engine}` upfront and passes it down per pair to
    avoid a DB round-trip per fetch_plan call).
    """
    if engine == "otp":
        return otp_client
    if engine == "motis":
        return motis_client
    raise ValueError(f"Unknown session engine: {engine!r} (expected 'otp' or 'motis')")
