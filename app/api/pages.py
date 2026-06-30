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
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config_service
from ..db import get_db
from ..models import Session as SessionRow
from ..models import User
from ..security import (
    CurrentUser,
    _decode_to_user,
    _extract_jwt,
)
from ..templating import templates  # shared Jinja env — version global lives here

router = APIRouter(tags=["pages"])


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
        request,
        "_base.html",
        {"current_user": _maybe_user(request)},
        status_code=403,
    )


# ────────────────────────── public pages ──────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is not None:
        # Already logged in — bounce to the most useful page for this role.
        # Phase-2 note: do NOT bounce non-admins to "/" — that redirects back
        # to /login in Phase-2 mode, creating a loop. /journey is universal.
        dest = "/admin/users" if user.role == "platform_admin" else "/journey"
        return RedirectResponse(dest, status_code=303)
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"current_user": None},
    )


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth/register.html",
        {"current_user": None},
    )


@router.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_page(request: Request, token: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "auth/confirm.html",
        {"current_user": None, "token": token},
    )


@router.get("/reset", response_class=HTMLResponse)
def reset_request_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "auth/reset_request.html",
        {"current_user": None},
    )


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_confirm_page(request: Request, token: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "auth/reset_confirm.html",
        {"current_user": None, "token": token},
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
        request,
        "admin/users.html",
        {"current_user": user, "users": users},
    )


@router.get("/admin/config", response_class=HTMLResponse)
def admin_config_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/config")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    return templates.TemplateResponse(
        request,
        "admin/config.html",
        {"current_user": user},
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
    # v0.1.32 — pass the canonical OSM scope presets through to the
    # template so the dropdown auto-renders from the Python config
    # instead of hardcoding the option rows. Eliminates the v0.1.30
    # footgun where adding "rail-focused" worked at the API/runner
    # layer but the UI dropdown didn't expose it (form was hand-written
    # with three options and missed the new one).
    from .. import osm_filter

    return templates.TemplateResponse(
        request,
        "admin/sessions.html",
        {
            "current_user": user,
            "sessions": sessions,
            "osm_scope_presets": osm_filter.OSM_SCOPE_PRESETS,
        },
    )


@router.get("/admin/reports", response_class=HTMLResponse)
def admin_reports_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/reports")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    return templates.TemplateResponse(request, "admin/reports.html", {"current_user": user})


@router.get("/admin/network-coverage", response_class=HTMLResponse)
def admin_network_coverage_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """v0.1.27 — Network coverage matrix page. Lists serving sessions
    so the operator can pick which to run against.

    PR-3 — also passes the COVERAGE_DEFAULT_TIMEZONE choices + the
    three day-window defaults into the template so the Advanced section
    of the run-create form pre-fills (and a `<select>` lists the same
    zones the API accepts)."""
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/network-coverage")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    serving = (
        db.execute(select(SessionRow).where(SessionRow.state == "serving").order_by(SessionRow.id))
        .scalars()
        .all()
    )
    # PR-3 — Advanced section defaults. Read live so an operator edit
    # to /admin/config is reflected on next page load without a redeploy.
    cov_cfg = config_service.get_all(db)
    from ..config_schema import CONFIG_SCHEMA  # local import keeps top-of-file tidy

    tz_spec = CONFIG_SCHEMA.get("COVERAGE_DEFAULT_TIMEZONE", {})
    return templates.TemplateResponse(
        request,
        "admin/network_coverage.html",
        {
            "current_user": user,
            "serving_sessions": serving,
            # PR-3 — pass-through for the Advanced section in the run-
            # create form. tz_choices populates the timezone <select>.
            "coverage_window_start_default": cov_cfg.get("COVERAGE_DEFAULT_WINDOW_START", "00:00"),
            "coverage_window_end_default": cov_cfg.get("COVERAGE_DEFAULT_WINDOW_END", "24:00"),
            "coverage_timezone_default": cov_cfg.get("COVERAGE_DEFAULT_TIMEZONE", "UTC"),
            "coverage_timezone_choices": tz_spec.get("choices", ["UTC"]),
        },
    )


@router.get("/admin/master/stations", response_class=HTMLResponse)
def admin_master_stations_page(request: Request) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/master/stations")
    if user.role not in ("platform_admin", "content_manager"):
        return _forbidden_html(request, "Content-manager or platform-admin access required.")
    return templates.TemplateResponse(
        request,
        "admin/master_stations.html",
        {"current_user": user},
    )


@router.get("/journey", response_class=HTMLResponse)
def journey_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/journey")
    # The "Compare with Swiss OJP reference" checkbox is only rendered
    # when the feature is both enabled AND has a token configured —
    # mirroring the dormant-until-configured rule in config_schema.py.
    cfg = config_service.get_all(db)
    ojp_comparison_enabled = bool(cfg.get("OJP_COMPARISON_ENABLED")) and bool(
        cfg.get("OJP_API_TOKEN")
    )
    return templates.TemplateResponse(
        request,
        "journey.html",
        {
            "current_user": user,
            "ojp_comparison_enabled": ojp_comparison_enabled,
        },
    )


@router.get("/admin/nap-catalogues", response_class=HTMLResponse)
def admin_nap_catalogues_page(request: Request) -> Response:
    """NAP catalogue CRUD (v0.1.12). Platform-admin only — catalogues are
    shared infrastructure consumed by the Import-from-NAP picker."""
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/admin/nap-catalogues")
    if user.role != "platform_admin":
        return _forbidden_html(request, "Platform admin access required.")
    return templates.TemplateResponse(
        request,
        "admin/nap_catalogues.html",
        {"current_user": user},
    )


@router.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request) -> Response:
    """Per-user API-credential library (v0.1.10).

    Available to any logged-in user — end_users won't have sessions to
    attach credentials to, but a workflow where they prep their keys
    before being promoted to content_manager is reasonable. The actual
    list is fetched client-side via /api/credentials and is always
    scoped to the calling user.
    """
    user = _maybe_user(request)
    if user is None:
        return _redirect_to_login("/credentials")
    return templates.TemplateResponse(request, "credentials.html", {"current_user": user})
