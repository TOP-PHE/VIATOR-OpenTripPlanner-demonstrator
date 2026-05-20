from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from . import concurrency, config_service, detect, ingestion
from .api import credentials as credentials_routes
from .api import journey as journey_routes
from .api import pages as page_routes
from .api import reports as reports_routes
from .api.admin import config as admin_config
from .api.admin import nap_catalogues as admin_nap_catalogues
from .api.admin import network_coverage as admin_network_coverage
from .api.admin import replay as admin_replay
from .api.admin import sessions as admin_sessions
from .api.admin import users as admin_users
from .api.auth import routes as auth_routes
from .api.master import aliases as master_aliases
from .api.master import stations as master_stations
from .db import SessionLocal
from .logging_config import setup_logging
from .metrics import setup_metrics
from .middleware.request_id import RequestIdMiddleware
from .models import RebuildJob, Upload
from .rate_limit import limiter
from .security import authed, authed_or_none
from .settings import settings
from .templating import templates  # shared Jinja env — version global lives here

# Configure structured JSON logging before any module-level log calls fire.
# Idempotent — safe even if uvicorn re-applies its default LOGGING_CONFIG.
setup_logging()

log = structlog.get_logger(__name__)

# Audit-2026-05 #19 — OpenTelemetry tracing. Wires the SDK + OTLP exporter
# + auto-instrumentation for httpx and stdlib-logging. Must run BEFORE
# the FastAPI app is constructed AND before the SQLAlchemy engine is
# touched, because the instrumentations wrap those objects. Idempotent +
# no-op if OTEL_EXPORTER_OTLP_ENDPOINT isn't set (e.g. in tests).
from .tracing import (  # noqa: E402  - imported after setup_logging on purpose
    instrument_fastapi_app,
    instrument_sqlalchemy_engine,
    setup_tracing,
)

setup_tracing(service_name=os.environ.get("OTEL_SERVICE_NAME", "viator-web"))


app = FastAPI(title="VIATOR — feed ingestion")

# Wrap FastAPI's request lifecycle: every incoming HTTP request becomes
# the root span of a new trace (or continues an existing one if a
# traceparent header is present from an upstream proxy).
instrument_fastapi_app(app)

# Wrap the SQLAlchemy engine so every DB statement emits a span as a
# child of the current request span. The engine is created at module-
# import time in app.db; importing it here is safe because db.py has
# no side effects beyond constructing the engine object.
from .db import engine  # noqa: E402  - imported after tracing setup on purpose

instrument_sqlalchemy_engine(engine)


# ────────────────────────── rate-limit wiring ──────────────────────────
# Routes opt in with @limiter.limit(...). Excess hits → 429 + Retry-After.
app.state.limiter = limiter


def _rate_limit_handler(request: Request, exc: Exception) -> Response:
    # slowapi wraps detail in `exc.detail` (a string like "5 per 1 hour")
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": f"Rate limit exceeded: {getattr(exc, 'detail', str(exc))}"},
        headers={"Retry-After": "60"},
    )


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)
# Request-id middleware runs outermost (added last = called first per Starlette
# semantics) so every log line — including from rate-limit rejections — carries
# the request_id contextvar.
app.add_middleware(RequestIdMiddleware)

# Audit-2026-05 #14 — Prometheus instrumentation. Registers HTTP-metrics
# middleware + the /metrics endpoint + our custom DB-derived collector.
# Must come after middleware registration so the instrumentator's own
# middleware sits *inside* the request-id binding — every metric labelled
# request gets the same request_id contextvar that logs see, supporting
# log-to-metric correlation in Grafana.
setup_metrics(app)


# Brand assets (TrackOnPath logo, UIC logo, VIATOR icons) live in ./branding
# at the repo root and are copied into the container by the Dockerfile.
# Mounted at /static/branding so the UI can <img src="/static/branding/trackonpath-logo.png">.
_branding_dir = Path("branding")
if _branding_dir.is_dir():
    app.mount("/static/branding", StaticFiles(directory=_branding_dir), name="branding")

# Routers
app.include_router(auth_routes.router)
app.include_router(admin_config.router)
app.include_router(admin_users.router)
app.include_router(admin_sessions.router)
app.include_router(admin_nap_catalogues.router)
app.include_router(admin_network_coverage.router)
app.include_router(admin_replay.router)
app.include_router(master_stations.router)
app.include_router(master_aliases.router)
app.include_router(reports_routes.router)
app.include_router(journey_routes.router)
app.include_router(credentials_routes.router)
app.include_router(page_routes.router)


_scheduler: object | None = None


@app.on_event("startup")
def _startup() -> None:
    """Run once per worker. Schema is owned by Alembic and applied by the
    container entrypoint *before* uvicorn — we just bootstrap runtime state.
    """
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    # Initialise concurrency gates from the live platform_config. If the DB is
    # unreachable at boot (rare; the entrypoint just ran a migration on it),
    # we fall back to schema defaults so the app still starts.
    try:
        with SessionLocal() as db:
            concurrency.semaphores.reload_from_config(config_service.get_all(db))
            log.info("concurrency.gates_initialised", source="platform_config")
    except Exception:
        log.exception("concurrency.gates_init_failed", fallback="schema_defaults")

    # Audit-2026-05 #30 — regenerate the docker/generated/ stubs from current
    # DB state on every boot. The orchestrator already runs on every session
    # state change; calling it here closes the drift window between "operator
    # deployed v0.1.x" and "first session edit happens" — the stubs always
    # reflect the live DB from the moment the container is reachable. The
    # files themselves are no longer tracked in git (replaced by the
    # bin/viator-bootstrap-stubs.sh first-install script that creates the
    # parse-time-required empty stubs).
    try:
        from . import sessions_orchestrator

        with SessionLocal() as db:
            sessions_orchestrator.regenerate(db)
            log.info("sessions_orchestrator.regenerated_at_boot")
    except Exception:
        log.exception("sessions_orchestrator.boot_regenerate_failed", fatal=False)

    # v0.1.29.3 — mark any in-flight network-coverage runs as failed.
    # FastAPI BackgroundTasks are in-process; a container restart kills
    # them with no DB state cleanup, leaving runs in 'running' status
    # forever. By the time we get here, no `execute_run` from a prior
    # container instance is alive, so anything still 'running' or
    # 'pending' is by definition orphaned. Done in its own try/except
    # so a malformed run row can't keep VIATOR from booting.
    try:
        from .network_coverage import runner as _coverage_runner

        with SessionLocal() as db:
            n = _coverage_runner.mark_orphaned_runs_as_failed(db)
            if n:
                db.commit()
                log.warning("network_coverage.orphans_marked_failed", count=n)
    except Exception:
        log.exception("network_coverage.orphan_cleanup_failed", fatal=False)

    # Optional in-process schedulers — disable in tests via VIATOR_DISABLE_CRONS env var.
    import os as _os

    if _os.environ.get("VIATOR_DISABLE_CRONS"):
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

        from . import retention
        from .master import trainline

        global _scheduler
        sched = AsyncIOScheduler(timezone="UTC")
        # Daily retention prune at 03:00 UTC.
        sched.add_job(retention.prune_once, "cron", hour=3, minute=0, id="retention")

        # Trainline refresh — daily check, but only acts when the configured
        # MASTER_STATIONS_REFRESH_DAYS interval has elapsed (handled in trainline.refresh()).
        async def _master_stations_refresh() -> None:
            with SessionLocal() as db:
                try:
                    await trainline.refresh(db)
                except Exception:
                    log.exception(
                        "scheduler.trainline_refresh_failed", job="master_stations_refresh"
                    )

        sched.add_job(
            _master_stations_refresh, "cron", hour=4, minute=0, id="master_stations_refresh"
        )
        sched.start()
        _scheduler = sched
        log.info("scheduler.started", jobs=["retention", "master_stations_refresh"])
    except Exception:
        log.exception("scheduler.startup_failed", crons_enabled=False)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: Annotated[str | None, Depends(authed_or_none)],
) -> Response:
    """Root page.

    Two modes (driven by `.env` `ADMIN_USER`):

    - **Phase-2 deployment** (`ADMIN_USER=` empty): `authed_or_none`
      returns None, we redirect to `/login`. The Phase-1 upload UI is
      unreachable here — avoids the browser's native basic-auth prompt
      on a bare-hostname visit, which is otherwise confusing UX.
    - **Phase-1 deployment** (`ADMIN_USER` set): basic-auth required;
      `authed_or_none` returns the username and we render the legacy
      upload dashboard.
    """
    if user is None:
        return RedirectResponse("/login", status_code=303)

    with SessionLocal() as db:
        uploads = db.query(Upload).order_by(Upload.created_at.desc()).limit(20).all()
        jobs = db.query(RebuildJob).order_by(RebuildJob.created_at.desc()).limit(10).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "uploads": uploads,
            "jobs": jobs,
            "kinds": sorted(detect.KNOWN_KINDS),
        },
    )


@app.post("/upload")
async def upload(
    user: Annotated[str, Depends(authed)],
    declared_standard: Annotated[str, Form()],
    version_label: Annotated[str, Form()] = "",
    file: UploadFile = File(...),
) -> RedirectResponse:
    if declared_standard not in detect.KNOWN_KINDS:
        raise HTTPException(400, f"Unknown standard: {declared_standard}")

    # Gate via the upload semaphore. Excess hits → 503 + audit row.
    try:
        async with concurrency.semaphores.upload.acquire_or_fail():
            return await _do_upload(declared_standard, version_label, file)
    except concurrency.ConcurrencyExceeded as exc:
        # Caller is expected to retry per Retry-After header.
        from . import audit  # local import to keep startup graph small

        with SessionLocal() as db:
            audit.record(
                db,
                action="concurrency.rejected.upload",
                metadata={"limit": exc.limit, "user": user},
            )
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc


async def _do_upload(
    declared_standard: str,
    version_label: str,
    file: UploadFile,
) -> RedirectResponse:
    # Persist to a per-upload folder so concurrent uploads don't collide.
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    staging = settings.inbox_dir / "_staging" / f"{stamp}-{secrets.token_hex(4)}"
    staging.mkdir(parents=True, exist_ok=True)
    stored_path = staging / Path(file.filename or "upload.bin").name

    sha = hashlib.sha256()
    size = 0
    max_bytes = settings.max_upload_mb * 1024 * 1024
    with stored_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                stored_path.unlink(missing_ok=True)
                raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB")
            sha.update(chunk)
            out.write(chunk)

    try:
        detected_kind = detect.detect(stored_path)
    except ValueError as exc:
        raise HTTPException(400, f"Detection failed: {exc}") from exc

    if detected_kind != declared_standard:
        raise HTTPException(
            400,
            f"Declared {declared_standard!r} but file looks like {detected_kind!r} — refusing.",
        )

    with SessionLocal() as db:
        triggered = ingestion.dispatch(stored_path, detected_kind, db)
        record = Upload(
            # FIXME(step-3/-7): user_id and session_id wired up once auth + sessions land.
            user_id=None,
            session_id=None,
            filename=stored_path.name,
            declared_kind=declared_standard,
            detected_kind=detected_kind,
            sha256=sha.hexdigest(),
            size_bytes=size,
            stored_path=str(stored_path),
            version_label=version_label,
            triggered_rebuild=triggered,
        )
        db.add(record)
        db.commit()

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz/version")
def healthz_version() -> dict[str, str]:
    """Reports which build of VIATOR is actually running.

    Use this to verify the deployed container without opening the UI:
        curl -s https://your-host/healthz/version
        → {"version": "v0.1.8"}

    The value is whatever was baked into the image at build time via the
    `VIATOR_VERSION` Docker ARG (set by the GHA workflow from the git tag),
    overridable at runtime by the `VIATOR_VERSION` env var in compose.
    Defaults to "dev" on local builds that didn't pass the build-arg.
    """
    return {"version": settings.viator_version}
