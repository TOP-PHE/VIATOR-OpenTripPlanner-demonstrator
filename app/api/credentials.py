"""User-credential CRUD API (v0.1.10).

Each user owns their own credential library. The four endpoints:

    GET    /api/credentials              list mine
    POST   /api/credentials              create one
    PATCH  /api/credentials/{id}         rename / update secret / change note
    DELETE /api/credentials/{id}         drop one

Authorization model:

    - Any logged-in user can manage their OWN credentials. End users have no
      sessions to attach them to (today), but they can have credentials
      ready for when they're promoted to content_manager.
    - Listing returns ONLY the caller's credentials. Cross-user listing is
      not exposed; platform admins who need to investigate a credential
      can use the audit log + direct DB inspection.
    - The decrypted plaintext is NEVER returned in any response. Updating
      a secret requires re-typing it; we don't surface "show current value".

Sensitive-field handling: GET responses replace the secret with a fixed
"********" sentinel so the UI knows "set, but unknown" without exposing
the value. Same pattern as `app/config_service.py` for SMTP_PASS etc.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from .. import credentials as crypto_module
from ..db import get_db
from ..models import UserCredential
from ..security import CurrentUser, client_ip, require_logged_in
from ..settings import settings

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


# ────────────────────────────── pydantic models ──────────────────────────────


class CredentialResponse(BaseModel):
    """Safe-to-return shape. Never includes the plaintext secret."""

    id: str
    name: str
    auth_type: str
    param_name: str | None
    note: str | None
    created_at: str
    last_used_at: str | None

    @classmethod
    def from_orm_credential(cls, c: UserCredential) -> CredentialResponse:
        return cls(
            id=str(c.id),
            name=c.name,
            auth_type=c.auth_type,
            param_name=c.param_name,
            note=c.note,
            created_at=_iso(c.created_at) or "",
            last_used_at=_iso(c.last_used_at),
        )


class CredentialCreate(BaseModel):
    """POST body. `secret` is the plaintext credential value.

    For `bearer`: the token alone (no "Bearer " prefix — we add it).
    For `basic`:  "user:pass" (we b64-encode at send time).
    For `query`:  the value (param_name is the URL key).
    For `header`: the value (param_name is the header name).
    """

    name: str = Field(min_length=1, max_length=80)
    auth_type: str
    param_name: str | None = None
    secret: str = Field(min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=280)


class CredentialPatch(BaseModel):
    """PATCH body. All fields optional. Send `secret` to rotate, omit to keep.

    `auth_type` and `param_name` can be changed together (operator decides
    they want to switch from query-key to header-key for the same
    underlying token, for example).
    """

    name: str | None = Field(default=None, min_length=1, max_length=80)
    auth_type: str | None = None
    param_name: str | None = None
    secret: str | None = Field(default=None, min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=280)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ────────────────────────────────── routes ───────────────────────────────────


@router.get("", response_model=list[CredentialResponse])
def list_my_credentials(
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_logged_in)],
) -> list[CredentialResponse]:
    """List the caller's credentials, ordered by name."""
    rows = (
        db.execute(
            select(UserCredential)
            .where(UserCredential.user_id == actor.id)
            .order_by(UserCredential.name)
        )
        .scalars()
        .all()
    )
    return [CredentialResponse.from_orm_credential(c) for c in rows]


@router.post("", response_model=CredentialResponse, status_code=status.HTTP_201_CREATED)
def create_credential(
    payload: CredentialCreate,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_logged_in)],
) -> CredentialResponse:
    """Create a credential owned by the calling user."""
    try:
        auth_type = crypto_module.validate_auth_type(payload.auth_type)
        if auth_type == "none":
            raise HTTPException(400, "auth_type='none' has no secret to store")
        param_name = crypto_module.validate_param_name(auth_type, payload.param_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Name uniqueness per user (also enforced by DB UNIQUE; check first
    # to give a clean 409 instead of a generic IntegrityError).
    existing = db.execute(
        select(UserCredential).where(
            UserCredential.user_id == actor.id,
            UserCredential.name == payload.name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"You already have a credential named {payload.name!r}. "
            f"Pick a different name or PATCH the existing one.",
        )

    ciphertext, nonce = crypto_module.encrypt(payload.secret, settings.jwt_secret)

    cred = UserCredential(
        user_id=actor.id,
        name=payload.name,
        auth_type=auth_type,
        param_name=param_name,
        ciphertext=ciphertext,
        nonce=nonce,
        note=payload.note,
    )
    db.add(cred)
    db.flush()  # populate cred.id

    audit.record(
        db,
        action="credential.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="user_credential",
        target_id=str(cred.id),
        # NEVER log the secret. Just metadata.
        metadata={
            "name": cred.name,
            "auth_type": cred.auth_type,
            "param_name": cred.param_name,
        },
    )
    db.commit()
    return CredentialResponse.from_orm_credential(cred)


@router.patch("/{cred_id}", response_model=CredentialResponse)
def patch_credential(
    cred_id: uuid.UUID,
    payload: CredentialPatch,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_logged_in)],
) -> CredentialResponse:
    """Rename, change auth scheme, rotate secret, or update note.

    Owner-only — even platform admins can't PATCH another user's credential
    (they can DELETE-and-recreate via the audit log if absolutely needed).
    """
    cred = db.get(UserCredential, cred_id)
    if cred is None or cred.user_id != actor.id:
        # Same 404 for "doesn't exist" and "not yours" — don't leak existence.
        raise HTTPException(404, "Credential not found")

    changes: dict[str, dict[str, object]] = {}

    if payload.name is not None and payload.name != cred.name:
        # Re-check uniqueness on rename.
        clash = db.execute(
            select(UserCredential).where(
                UserCredential.user_id == actor.id,
                UserCredential.name == payload.name,
                UserCredential.id != cred.id,
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, f"You already have a credential named {payload.name!r}")
        changes["name"] = {"from": cred.name, "to": payload.name}
        cred.name = payload.name

    if payload.auth_type is not None:
        try:
            new_type = crypto_module.validate_auth_type(payload.auth_type)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if new_type == "none":
            raise HTTPException(400, "Use DELETE to remove a credential, not auth_type='none'")
        if new_type != cred.auth_type:
            changes["auth_type"] = {"from": cred.auth_type, "to": new_type}
            cred.auth_type = new_type

    # param_name re-validation: re-check against (possibly new) auth_type.
    if payload.param_name is not None or payload.auth_type is not None:
        try:
            cred.param_name = crypto_module.validate_param_name(
                cred.auth_type,  # type: ignore[arg-type]
                payload.param_name if payload.param_name is not None else cred.param_name,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    if payload.secret is not None:
        ciphertext, nonce = crypto_module.encrypt(payload.secret, settings.jwt_secret)
        cred.ciphertext = ciphertext
        cred.nonce = nonce
        # Don't put the secret in `changes`; just record that it was rotated.
        changes["secret"] = {"rotated": True}

    if payload.note is not None and payload.note != cred.note:
        changes["note"] = {"from": cred.note, "to": payload.note}
        cred.note = payload.note

    if changes:
        audit.record(
            db,
            action="credential.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="user_credential",
            target_id=str(cred.id),
            metadata={"changes": changes},
        )
    db.commit()
    return CredentialResponse.from_orm_credential(cred)


# `response_model=None` is needed even for `-> None` returns, otherwise
# FastAPI infers a model from the annotation and refuses status-204 (which
# explicitly forbids a response body).
@router.delete("/{cred_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_credential(
    cred_id: uuid.UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_logged_in)],
) -> None:
    """Delete a credential.

    Note: this does NOT scan session configs for references. If the
    credential is attached to one or more provider URLs, those refreshes
    will start failing with "credential not found" until the operator
    detaches them or picks a different credential. That's intentional —
    surfacing the dangling reference is more useful than silently
    leaving a session unable to authenticate.
    """
    cred = db.get(UserCredential, cred_id)
    if cred is None or cred.user_id != actor.id:
        raise HTTPException(404, "Credential not found")

    audit.record(
        db,
        action="credential.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="user_credential",
        target_id=str(cred.id),
        metadata={"name": cred.name, "auth_type": cred.auth_type},
    )

    db.delete(cred)
    db.commit()
