"""NAP catalogues CRUD — platform-admin owned, used by Import-from-NAP picker.

Endpoints:

    GET    /api/admin/nap-catalogues          list every catalogue (with credential names)
    POST   /api/admin/nap-catalogues          create
    PATCH  /api/admin/nap-catalogues/{id}     edit
    DELETE /api/admin/nap-catalogues/{id}     drop one

Authorization: platform_admin only. Catalogues are platform-wide
infrastructure — content_managers consume them via the import modal but
don't manage them. (The credential attached to a catalogue is still
user-owned; see `app/credentials.py` for the threat model.)

Related: `app/api/credentials.py` for the per-user credential CRUD.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import audit
from ...db import get_db
from ...models import NapCatalogue, UserCredential
from ...security import CurrentUser, client_ip, require_platform_admin

router = APIRouter(prefix="/api/admin/nap-catalogues", tags=["admin", "nap-catalogues"])


# ──────────────────────────── pydantic models ────────────────────────────


# Subset of the importer's classify_modes() known modes — kept here too
# so the API rejects typos at the schema layer instead of at fetch time.
_VALID_MODES = {"rail", "urban", "bus", "bike"}


class NapCatalogueResponse(BaseModel):
    id: str
    name: str
    url: str
    default_country: str | None
    default_modes: list[str]  # decoded from comma-joined string for UI convenience
    credential_id: str | None
    credential_name: str | None  # joined for display in the picker — saves a round-trip
    note: str | None
    created_at: str

    @classmethod
    def from_orm_catalogue(
        cls, c: NapCatalogue, credential_name: str | None
    ) -> NapCatalogueResponse:
        return cls(
            id=str(c.id),
            name=c.name,
            url=c.url,
            default_country=c.default_country,
            default_modes=_decode_modes(c.default_modes),
            credential_id=str(c.credential_id) if c.credential_id else None,
            credential_name=credential_name,
            note=c.note,
            created_at=c.created_at.isoformat() if c.created_at else "",
        )


class NapCatalogueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: HttpUrl
    default_country: str | None = Field(default=None, max_length=2)
    default_modes: list[str] | None = None
    credential_id: str | None = None
    note: str | None = Field(default=None, max_length=280)


class NapCataloguePatch(BaseModel):
    """All fields optional; omitted fields keep their current value."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: HttpUrl | None = None
    default_country: str | None = Field(default=None, max_length=2)
    default_modes: list[str] | None = None
    credential_id: str | None = None
    note: str | None = Field(default=None, max_length=280)


# ────────────────────────────── helpers ──────────────────────────────


def _encode_modes(modes: list[str] | None) -> str | None:
    """Validate + comma-join. None / [] → None (clear default)."""
    if not modes:
        return None
    cleaned: list[str] = []
    for m in modes:
        v = (m or "").strip().lower()
        if not v:
            continue
        if v not in _VALID_MODES:
            raise ValueError(f"unknown mode {m!r}; valid: {sorted(_VALID_MODES)}")
        if v not in cleaned:
            cleaned.append(v)
    return ",".join(cleaned) if cleaned else None


def _decode_modes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def _resolve_credential_id(db: Session, raw: str | None) -> uuid.UUID | None:
    """Validate that a credential exists. Returns the UUID or None.

    Note: we do NOT enforce ownership — a platform_admin attaching another
    user's credential to a catalogue is allowed (they're configuring shared
    infrastructure). The credential's value is what gets used; if the owner
    deletes it later, the catalogue's credential_id goes NULL via the FK
    SET NULL cascade and the next NAP fetch falls back to anonymous.
    """
    if not raw:
        return None
    try:
        cid = uuid.UUID(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"credential_id={raw!r} is not a valid UUID") from exc
    cred = db.get(UserCredential, cid)
    if cred is None:
        raise HTTPException(404, f"credential_id={raw!r} not found")
    return cid


def _credential_name_for(db: Session, cid: uuid.UUID | None) -> str | None:
    """Look up the credential's friendly name. Returns None if unset / orphaned."""
    if cid is None:
        return None
    cred = db.get(UserCredential, cid)
    return cred.name if cred else None


# ──────────────────────────────── routes ────────────────────────────────


@router.get("", response_model=list[NapCatalogueResponse])
def list_catalogues(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[NapCatalogueResponse]:
    """List every catalogue, ordered by name."""
    rows = db.execute(select(NapCatalogue).order_by(NapCatalogue.name)).scalars().all()
    return [
        NapCatalogueResponse.from_orm_catalogue(c, _credential_name_for(db, c.credential_id))
        for c in rows
    ]


@router.post("", response_model=NapCatalogueResponse, status_code=status.HTTP_201_CREATED)
def create_catalogue(
    payload: NapCatalogueCreate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> NapCatalogueResponse:
    try:
        modes_str = _encode_modes(payload.default_modes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Name uniqueness — DB also enforces, but we want a clean 409.
    if (
        db.execute(
            select(NapCatalogue).where(NapCatalogue.name == payload.name)
        ).scalar_one_or_none()
        is not None
    ):
        raise HTTPException(409, f"A catalogue named {payload.name!r} already exists")

    cred_id = _resolve_credential_id(db, payload.credential_id)

    cat = NapCatalogue(
        name=payload.name,
        url=str(payload.url),
        default_country=payload.default_country.upper() if payload.default_country else None,
        default_modes=modes_str,
        credential_id=cred_id,
        note=payload.note,
    )
    db.add(cat)
    db.flush()

    audit.record(
        db,
        action="nap_catalogue.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="nap_catalogue",
        target_id=str(cat.id),
        metadata={
            "name": cat.name,
            "url": cat.url,
            "default_country": cat.default_country,
            "default_modes": cat.default_modes,
            "has_credential": cred_id is not None,
        },
    )
    db.commit()
    return NapCatalogueResponse.from_orm_catalogue(cat, _credential_name_for(db, cat.credential_id))


@router.patch("/{cat_id}", response_model=NapCatalogueResponse)
def patch_catalogue(
    cat_id: uuid.UUID,
    payload: NapCataloguePatch,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> NapCatalogueResponse:
    cat = db.get(NapCatalogue, cat_id)
    if cat is None:
        raise HTTPException(404, "Catalogue not found")

    changes: dict[str, dict[str, object]] = {}

    if payload.name is not None and payload.name != cat.name:
        clash = db.execute(
            select(NapCatalogue).where(NapCatalogue.name == payload.name, NapCatalogue.id != cat.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, f"A catalogue named {payload.name!r} already exists")
        changes["name"] = {"from": cat.name, "to": payload.name}
        cat.name = payload.name

    if payload.url is not None and str(payload.url) != cat.url:
        changes["url"] = {"from": cat.url, "to": str(payload.url)}
        cat.url = str(payload.url)

    if payload.default_country is not None:
        new_country = payload.default_country.upper() or None
        if new_country != cat.default_country:
            changes["default_country"] = {"from": cat.default_country, "to": new_country}
            cat.default_country = new_country

    if payload.default_modes is not None:
        try:
            new_modes = _encode_modes(payload.default_modes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if new_modes != cat.default_modes:
            changes["default_modes"] = {"from": cat.default_modes, "to": new_modes}
            cat.default_modes = new_modes

    if payload.credential_id is not None:
        # Empty string = explicit clear ("no credential"). Otherwise resolve.
        new_cid = (
            _resolve_credential_id(db, payload.credential_id) if payload.credential_id else None
        )
        if new_cid != cat.credential_id:
            changes["credential_id"] = {
                "from": str(cat.credential_id) if cat.credential_id else None,
                "to": str(new_cid) if new_cid else None,
            }
            cat.credential_id = new_cid

    if payload.note is not None and payload.note != cat.note:
        changes["note"] = {"from": cat.note, "to": payload.note}
        cat.note = payload.note

    if changes:
        audit.record(
            db,
            action="nap_catalogue.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="nap_catalogue",
            target_id=str(cat.id),
            metadata={"changes": changes},
        )
    db.commit()
    return NapCatalogueResponse.from_orm_catalogue(cat, _credential_name_for(db, cat.credential_id))


@router.delete("/{cat_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_catalogue(
    cat_id: uuid.UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> None:
    cat = db.get(NapCatalogue, cat_id)
    if cat is None:
        raise HTTPException(404, "Catalogue not found")

    audit.record(
        db,
        action="nap_catalogue.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="nap_catalogue",
        target_id=str(cat.id),
        metadata={"name": cat.name, "url": cat.url},
    )
    db.delete(cat)
    db.commit()
