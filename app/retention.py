"""Three-tier retention cron.

  raw_response (heaviest) → JOURNEY_RAW_RESPONSE_RETENTION_DAYS  (default 30)
  trips        (medium)   → JOURNEY_TRIPS_RETENTION_DAYS         (default 180)
  searches     (summary)  → JOURNEY_SEARCH_RETENTION_DAYS        (default 365)
  audit_events            → AUDIT_RETENTION_DAYS                 (default 365)

Pruning higher tiers (raw, then trips) before the search summaries lets us keep
year-on-year analytics while shedding the bulky storage early.

Worker invocation: APScheduler runs `prune_once()` daily at 03:00 UTC. Manual
trigger from CLI: `python -m app.retention`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.orm import Session as DbSession

from . import config_service
from .db import SessionLocal
from .models import AuditEvent, JourneySearch, JourneySearchExecution, JourneyTrip


log = logging.getLogger(__name__)


def prune_once() -> dict[str, int]:
    """Run one pass of the retention cron. Returns counts of rows pruned per table."""
    counts = {"raw_response": 0, "trips": 0, "executions": 0, "searches": 0, "audit": 0}

    with SessionLocal() as db:
        cfg = config_service.get_all(db)
        now = datetime.now(timezone.utc)

        # 1. raw_response (set NULL on old executions)
        cutoff_raw = now - timedelta(days=int(cfg["JOURNEY_RAW_RESPONSE_RETENTION_DAYS"]))
        result = db.execute(
            update(JourneySearchExecution)
            .where(JourneySearchExecution.raw_response.is_not(None))
            .where(JourneySearchExecution.id.in_(
                db.query(JourneySearchExecution.id)
                .join(JourneySearch, JourneySearch.id == JourneySearchExecution.search_id)
                .filter(JourneySearch.ts < cutoff_raw)
                .scalar_subquery()
            ))
            .values(raw_response=None)
        )
        counts["raw_response"] = result.rowcount or 0

        # 2. trips (delete old)
        cutoff_trips = now - timedelta(days=int(cfg["JOURNEY_TRIPS_RETENTION_DAYS"]))
        result = db.execute(
            delete(JourneyTrip).where(
                JourneyTrip.execution_id.in_(
                    db.query(JourneySearchExecution.id)
                    .join(JourneySearch, JourneySearch.id == JourneySearchExecution.search_id)
                    .filter(JourneySearch.ts < cutoff_trips)
                    .scalar_subquery()
                )
            )
        )
        counts["trips"] = result.rowcount or 0

        # 3. executions (delete old, after their trips are gone)
        result = db.execute(
            delete(JourneySearchExecution).where(
                JourneySearchExecution.search_id.in_(
                    db.query(JourneySearch.id)
                    .filter(JourneySearch.ts < cutoff_trips)
                    .scalar_subquery()
                )
            )
        )
        counts["executions"] = result.rowcount or 0

        # 4. searches summaries
        cutoff_searches = now - timedelta(days=int(cfg["JOURNEY_SEARCH_RETENTION_DAYS"]))
        result = db.execute(delete(JourneySearch).where(JourneySearch.ts < cutoff_searches))
        counts["searches"] = result.rowcount or 0

        # 5. audit
        cutoff_audit = now - timedelta(days=int(cfg["AUDIT_RETENTION_DAYS"]))
        result = db.execute(delete(AuditEvent).where(AuditEvent.ts < cutoff_audit))
        counts["audit"] = result.rowcount or 0

        db.commit()

    log.info("retention cron: %s", counts)
    return counts


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    print(prune_once())


if __name__ == "__main__":  # pragma: no cover
    main()
