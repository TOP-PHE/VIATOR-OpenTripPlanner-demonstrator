"""Admin storage: disk usage of the data volumes and clean-up of what is not in use.

See `app/storage.py` for the rules. Both endpoints are plain `def` so FastAPI
runs them in its thread pool: the scan walks the volumes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ... import audit, storage
from ...db import get_db
from ...models import RebuildJob
from ...models import Session as SessionRow
from ...security import CurrentUser, client_ip, require_platform_admin
from ...settings import settings

router = APIRouter(prefix="/api/admin/storage", tags=["admin", "storage"])


class DeleteBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


def _scan(db: DbSession) -> storage.Report:
    session_ids = set(db.scalars(select(SessionRow.id)))
    busy = {
        sid
        for sid in db.scalars(select(RebuildJob.session_id).where(RebuildJob.status == "running"))
        if sid
    }
    return storage.scan(settings.inbox_dir, settings.graph_dir, session_ids, busy)


@router.get("", summary="Disk usage per session and clean-up candidates")
def get_storage(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    return _scan(db).as_dict()


@router.post("/delete", summary="Delete selected clean-up candidates")
def delete_candidates(
    body: DeleteBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Re-scans first and deletes only ids that are still candidates, so a
    rebuild started or a `current` moved since the page loaded is respected."""
    deleted, skipped = storage.delete(body.ids, _scan(db), settings.inbox_dir, settings.graph_dir)
    freed = sum(d["size_bytes"] for d in deleted)
    if deleted:
        audit.record(
            db,
            action="storage.cleanup",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="storage",
            metadata={"deleted": deleted, "skipped": skipped, "freed_bytes": freed},
        )
        db.commit()
    return {
        "deleted": deleted,
        "skipped": skipped,
        "freed_bytes": freed,
        "report": _scan(db).as_dict(),
    }
