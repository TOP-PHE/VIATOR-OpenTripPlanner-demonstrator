"""Route-aliases CRUD."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ... import audit
from ...db import get_db
from ...models import RouteAlias
from ...security import CurrentUser, client_ip, require_content_manager

router = APIRouter(prefix="/api/master/route-aliases", tags=["master", "aliases"])


class AliasBody(BaseModel):
    canonical_name: str
    alias: str
    applies_from: date | None = None
    applies_until: date | None = None
    scope_country: str | None = None
    scope_carrier: str | None = None
    notes: str | None = None


class AliasResponse(BaseModel):
    id: str
    canonical_name: str
    alias: str
    applies_from: str | None
    applies_until: str | None
    scope_country: str | None
    scope_carrier: str | None
    notes: str | None


def _to_response(r: RouteAlias) -> AliasResponse:
    return AliasResponse(
        id=str(r.id),
        canonical_name=r.canonical_name,
        alias=r.alias,
        applies_from=r.applies_from.isoformat() if r.applies_from else None,
        applies_until=r.applies_until.isoformat() if r.applies_until else None,
        scope_country=r.scope_country,
        scope_carrier=r.scope_carrier,
        notes=r.notes,
    )


@router.get("", response_model=list[AliasResponse])
def list_aliases(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> list[AliasResponse]:
    rows = db.execute(select(RouteAlias).order_by(RouteAlias.canonical_name)).scalars().all()
    return [_to_response(r) for r in rows]


@router.post("", response_model=AliasResponse, status_code=201)
def create_alias(
    body: AliasBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> AliasResponse:
    row = RouteAlias(**body.model_dump(), created_by=actor.id)
    db.add(row)
    db.flush()
    audit.record(
        db,
        action="route_alias.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="route_alias",
        target_id=str(row.id),
        metadata=body.model_dump(mode="json"),
    )
    db.commit()
    return _to_response(row)


@router.delete("/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alias(
    alias_id: uuid.UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> Response:
    row = db.get(RouteAlias, alias_id)
    if row is None:
        raise HTTPException(404, "Not found")
    db.delete(row)
    audit.record(
        db,
        action="route_alias.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="route_alias",
        target_id=str(alias_id),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
