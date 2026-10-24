"""Admin platform-config endpoints. See spec §9.8 and §12.4."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ... import audit, config_service
from ...auth import email as email_sender
from ...db import get_db
from ...security import CurrentUser, client_ip, require_platform_admin

router = APIRouter(prefix="/api/admin/config", tags=["admin", "config"])


class SmtpTestBody(BaseModel):
    to: EmailStr


@router.get("", summary="Read full platform configuration (sensitive fields masked)")
def get_config(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    return config_service.as_response(db)


@router.patch("", summary="Update one or more configuration keys")
def patch_config(
    payload: dict[str, Any],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    try:
        new_state = config_service.apply_patch(
            db,
            payload,
            actor_user_id=user.id,
            actor_ip=client_ip(request),
        )
    except config_service.ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": exc.errors},
        ) from exc

    db.commit()
    return new_state


_SMTP_FAILURE_MESSAGES: tuple[tuple[type[Exception], str], ...] = (
    (
        email_sender.SmtpAuthError,
        "SMTP server refused the login — check SMTP_USER / SMTP_PASS.",
    ),
    (
        email_sender.SmtpConnectionError,
        "Could not reach the SMTP server — check SMTP_HOST, SMTP_PORT and SMTP_SECURE.",
    ),
)
_SMTP_FAILURE_DEFAULT = (
    "SMTP server rejected the message. The audit log (smtp.test.failed) has the server's answer."
)


def _smtp_failure_message(exc: Exception) -> str:
    return next(
        (msg for cls, msg in _SMTP_FAILURE_MESSAGES if isinstance(exc, cls)),
        _SMTP_FAILURE_DEFAULT,
    )


@router.post(
    "/smtp/test",
    summary="Send a test email using the current SMTP configuration",
)
async def smtp_test(
    body: SmtpTestBody,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> dict[str, Any]:
    """Try sending a small test email to `body.to`. Returns `{ok, error?}`.

    Always 200; the body indicates success/failure so the admin UI can show a
    nice toast either way. Both outcomes are audit-logged.
    """
    ip = client_ip(request)
    try:
        await email_sender.send_test_email(to_email=str(body.to))
    except email_sender.SmtpNotConfiguredError as exc:
        await run_in_threadpool(
            _audit_smtp_test,
            db,
            "smtp.test.unconfigured",
            user.id,
            ip,
            {"to": str(body.to), "error": str(exc)},
        )
        return {"ok": False, "error": "SMTP is not configured (SMTP_HOST is empty)."}
    except email_sender.EmailSendError as exc:
        await run_in_threadpool(
            _audit_smtp_test,
            db,
            "smtp.test.failed",
            user.id,
            ip,
            {"to": str(body.to), "error": str(exc)},
        )
        # Fixed text only: the exception message comes from the SMTP server
        # and the transport stack (CodeQL py/stack-trace-exposure). The full
        # detail is in the audit event above and the server log.
        return {"ok": False, "error": _smtp_failure_message(exc)}

    await run_in_threadpool(
        _audit_smtp_test, db, "smtp.test.sent", user.id, ip, {"to": str(body.to)}
    )
    return {"ok": True}


def _audit_smtp_test(
    db: Session,
    action: str,
    user_id: uuid.UUID | None,
    ip: str | None,
    metadata: dict[str, Any],
) -> None:
    """The SMTP test's audit event, committed: blocking database work, so
    `smtp_test` runs it in the thread pool (#397)."""
    audit.record(db, action=action, actor_user_id=user_id, actor_ip=ip, metadata=metadata)
    db.commit()
