"""Audit-event helper.

Every state-changing call site is expected to write one row via `record(...)`.
Keep the helper *boring* — no business logic. Callers shape the metadata.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def record(
    db: Session,
    *,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    actor_ip: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit row. The caller commits the surrounding transaction.

    `action` is dotted convention: 'config.update', 'login.fail', 'upload.dispatch',
    'concurrency.rejected.journey', etc.
    """
    event = AuditEvent(
        action=action,
        actor_user_id=actor_user_id,
        actor_ip=actor_ip,
        target_kind=target_kind,
        target_id=target_id,
        metadata_=metadata,
    )
    db.add(event)
    return event
