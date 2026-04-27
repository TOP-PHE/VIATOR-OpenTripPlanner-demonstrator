"""HTML page router — login, register, confirm, password-reset, admin/users.

Page routes are deliberately separate from the JSON API (`app/api/...`):
they render Jinja templates and handle browser flows (redirects, cookies)
rather than returning JSON.

For protected pages we use **redirect-on-auth-failure** semantics instead of
the JSON API's 401: an unauthenticated browser hitting `/admin/users` lands on
`/login` instead of seeing a 401 modal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Session as SessionRow
from ..models import User
from ..security import (
    CurrentUser,
    _decode_to_user,
    _extract_jwt,
)


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


# ────────────────────────── helpers ──────────────────────────


def _maybe_user(request: Request) -> CurrentUser | None:
    """Return the JWT-authenticated user, or None — never raises."""
    return _decode_to_user(_extract_jwt(request))


def _redirect_to_login(next_path: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/login?next={next_path}",
        status_code=303,
    )


def _forbidden_html(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        "_base.html",
        {"request": request, "current_user": _maybe_user(request)},
        status_code=403,
    )


# ────────────────────────── public pages ──────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is not None:
        # Already logged in — bounce to the most useful place.
        dest = "/admin/users" if user.role == "platform_admin" else "/"
        return RedirectResponse(dest, status_code=303)
    return templates.TemplateResponse(
        "auth/login.html",
        {"request": request, "current_user": None},
    )


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "auth/register.html",
        {"request": request, "current_user": None},
    )


@router.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_page(request: Request, token: str) -> HTMLResponse:
    return templates.TemplateResponse(
        "auth/confirm.html",
        {"request": request, "current_user": None, "token": token},
    )


@router.get("/reset", response_class=HTMLResponse)
def reset_request_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "auth/reset_request.html",
        {"request": request, "current_user": None},
    )


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_confirm_page(request: Request, token: str) -> HTMLResponse:
    return templates.TemplateResponse(
        "auth/reset_confirm.html",
        {"request": request, "current_user": None, "token": token},
    )


# ────────────────────────── admin pages ──────────────────────────


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/users")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")

    users = db.execute(select(User).order_by(User.created_at)).scalars().all()
    return templates.TemplateResponse(
        "admin/users.html",
        {"request": request, "current_user": user, "users": users},
    )


@router.get("/admin/config", response_class=HTMLResponse)
def admin_config_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/config")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    return templates.TemplateResponse(
        "admin/config.html",
        {"request": request, "current_user": user},
    )


@router.get("/admin/sessions", response_class=HTMLResponse)
def admin_sessions_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/sessions")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    sessions = db.execute(select(SessionRow).order_by(SessionRow.created_at)).scalars().all()
    return templates.TemplateResponse(
        "admin/sessions.html",
        {"request": request, "current_user": user, "sessions": sessions},
    )


@router.get("/admin/reports", response_class=HTMLResponse)
def admin_reports_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/reports")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    return templates.TemplateResponse(
        "admin/reports.html", {"request": request, "current_user": user}
    )


@router.get("/admin/master/stations", response_class=HTMLResponse)
def admin_master_stations_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/master/stations")
    if user.role not in ("platform_admin", "content_manager"):
        return _forbidden_html(request, "Content-manager or platform-admin access required.")
    return templates.TemplateResponse(
        "admin/master_stations.html",
        {"request": request, "current_user": user},
    )


@router.get("/journey", response_class=HTMLResponse)
def journey_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/journey")
    return templates.TemplateResponse(
        "journey.html", {"request": request, "current_user": user}
    )
