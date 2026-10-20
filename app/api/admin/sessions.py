"""Admin sessions CRUD + per-session uploads / source-refresh / rebuilds /
promote. See spec §9.3 and §4.

Phase-A.5 wiring (this file):

- `POST /<sid>/uploads`         multipart upload → ingestion.dispatch()
- `POST /<sid>/sources/refresh` httpx-download URLs from config.sources
- `GET  /<sid>/sources/progress` the refreshes' progress records (issue #359)
- `POST /<sid>/rebuilds`        enqueue OTP build job (worker picks up)
- `GET  /<sid>/rebuilds`        list this session's rebuild jobs
- `POST /<sid>/promote`         regenerate compose+nginx fragments, signal
                                worker to compose-up + nginx-reload, set
                                state='serving'
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import re
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm.attributes import flag_modified

from ... import (
    audit,
    country_gate,
    detect,
    feed_fetch,
    feed_resolvers,
    inbox_sweep,
    ingestion,
    nap_source_map,
    refresh_progress,
    serve_memory,
    sessions_orchestrator,
    staleness,
    station_builds,
    station_extract,
    station_extract_upload,
    station_module,
)
from ...db import get_db
from ...models import AuditEvent, GraphSnapshot, RebuildJob, Upload
from ...models import Session as SessionRow
from ...models.sessions import SessionCategory, SessionEngine, SessionState
from ...security import (
    CurrentUser,
    client_ip,
    require_content_manager,
    require_platform_admin,
)
from ...settings import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["admin", "sessions"])

# Sentinel file the worker watches for. When present, the worker runs
# `docker compose -p viator up -d` (picks up new otp-<sid> services from the
# regenerated fragment) and `docker exec viator-nginx-1 nginx -s reload`,
# then deletes the file. See app/worker.py.
_RELOAD_TRIGGER = Path("/data/generated/.reload-trigger")

# Mapping of `config.sources` key → detect.KNOWN_KINDS value. Lowercase keys
# are friendlier in JSON; the detect/dispatch layers expect canonical names.
_SOURCE_KEY_TO_KIND: dict[str, str] = {
    "gtfs": "GTFS",
    "osm_pbf": "OSM-PBF",
    "netex_nordic": "NeTEx-Nordic",
    "netex_epip": "NeTEx-EPIP",
    "mct": "SNCF-MCT",
    "stations": "SNCF-Stations",
}


_VALID_CATEGORIES = {c.value for c in SessionCategory}
_VALID_STATES = {s.value for s in SessionState}
_VALID_ENGINES = {e.value for e in SessionEngine}
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


class SessionCreate(BaseModel):
    id: str = Field(min_length=2, max_length=63, description="slug: ^[a-z][a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(description="NAP | MERITS | MANUAL | EXPERIMENTAL")
    config: dict[str, Any] = Field(default_factory=dict)
    include_in_fanout: bool = False
    # P1 MOTIS — planner backend for this session. Default 'otp' keeps
    # legacy create-form payloads (which don't send `engine`) working
    # bit-identical to pre-P1 behaviour.
    engine: str = Field(default="otp", description="otp | motis")


class SessionPatch(BaseModel):
    # Same bounds as SessionCreate.name; surrounding spaces are stripped in
    # patch_session, which then refuses a blank name.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    include_in_fanout: bool | None = None
    state: str | None = None


class SessionResponse(BaseModel):
    id: str
    name: str
    category: str
    state: str
    engine: str
    config: dict[str, Any]
    include_in_fanout: bool
    created_at: str
    archived_at: str | None
    # Soft staleness signal (v0.1.7.1): non-null when the operator has
    # edited URLs since the last refresh. UI displays as a yellow banner
    # and adds a confirm dialog before Rebuild graph. Never blocks rebuild
    # by itself — that's handled by the harder input-presence check.
    staleness_warning: str | None = None

    @classmethod
    def from_orm_session(cls, s: SessionRow) -> SessionResponse:
        return cls(
            id=s.id,
            name=s.name,
            category=s.category,
            state=s.state,
            engine=getattr(s, "engine", "otp") or "otp",
            config=s.config or {},
            include_in_fanout=s.include_in_fanout,
            created_at=s.created_at.isoformat() if s.created_at else "",
            archived_at=s.archived_at.isoformat() if s.archived_at else None,
            staleness_warning=staleness.staleness_warning(s.config or {}),
        )


# ────────────────────────── routes ──────────────────────────


@router.get("", response_model=list[SessionResponse], summary="List sessions")
def list_sessions(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> list[SessionResponse]:
    rows = db.execute(select(SessionRow).order_by(SessionRow.created_at)).scalars().all()
    # Hand the pooled connection back before building the answers (every
    # session's full config): the rows stay loaded, detached, and the page
    # can ask for this list many times at once. Same pattern as the login
    # check of app/api/credentials.py.
    db.close()
    return [SessionResponse.from_orm_session(s) for s in rows]


class MeasuredServeMemory(BaseModel):
    used_bytes: float
    # Seconds since the container started; missing when cAdvisor gave none.
    uptime_seconds: float | None = None


class ServeMemoryResponse(BaseModel):
    # False when Prometheus is not set or did not answer: the page then shows
    # each OTP session's heap and cap only.
    available: bool
    # Keyed by session id: the OTP serve containers cAdvisor sees right now.
    sessions: dict[str, MeasuredServeMemory] = Field(default_factory=dict)


@router.get("/serve-memory", summary="Measured memory of the OTP serve containers")
async def serve_memory_usage(
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> ServeMemoryResponse:
    """What each OTP serve container uses now (issue #384), for the Sessions page.

    Read from Prometheus (cAdvisor's working set, the "Container memory"
    panel), cached a minute for the whole process: the page asks once per load.
    """
    measured = await serve_memory.measured_by_session()
    if measured is None:
        return ServeMemoryResponse(available=False)
    return ServeMemoryResponse(
        available=True,
        sessions={sid: MeasuredServeMemory(**values) for sid, values in measured.items()},
    )


@router.post(
    "",
    status_code=201,
    summary="Create a session",
    responses={
        # Declared at the decorator level so Sonar's S8415 is satisfied
        # for every HTTPException(400) in the body (slug / category /
        # engine / actor validation). Same pattern as patch_session.
        # `response_model=` dropped (S8409): FastAPI infers it from the
        # `-> SessionResponse` return annotation since 0.95+.
        400: {"description": "Slug / category / engine validation failed."},
        409: {
            "description": "The session exists already, or a provider declares a country "
            "neither VIATOR's Trainline list nor the station module knows."
        },
    },
)
def create_session(
    body: SessionCreate,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SessionResponse:
    if not _SLUG.match(body.id):
        raise HTTPException(400, "Session id must be a slug: ^[a-z][a-z0-9-]+$")
    if body.category not in _VALID_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Must be one of {sorted(_VALID_CATEGORIES)}")
    if body.engine not in _VALID_ENGINES:
        raise HTTPException(400, f"Invalid engine. Must be one of {sorted(_VALID_ENGINES)}")
    if db.get(SessionRow, body.id) is not None:
        raise HTTPException(409, f"Session {body.id!r} already exists")

    if actor.id is None:
        raise HTTPException(400, "Sessions can only be created by JWT-authenticated admins")
    # The country gate of a save, on the providers a new session is created
    # with: they become its stored providers, which later saves no longer
    # gate (`_stored_countries`). A new country is accepted with a warning
    # when VIATOR uses the station module (`_new_countries`).
    warning = _new_countries(_gate_blocking(db, _stored_countries(body.config)), _SAVE_ADVICE)
    if warning:
        response.headers["X-Warnings"] = json.dumps([warning])

    s = SessionRow(
        id=body.id,
        name=body.name,
        category=body.category,
        state=SessionState.CREATED.value,
        engine=body.engine,
        config=body.config,
        include_in_fanout=body.include_in_fanout,
        created_by=actor.id,
    )
    db.add(s)
    audit.record(
        db,
        action="session.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=body.id,
        metadata={"category": body.category, "name": body.name, "engine": body.engine},
    )
    db.commit()
    return SessionResponse.from_orm_session(s)


@router.patch(
    "/{sid}",
    # `response_model=` dropped in v0.1.32.21 — the function's
    # `-> SessionResponse` return annotation already conveys the same
    # info, and SonarCloud rule python:S6781 flags the duplication.
    # FastAPI infers the response model from the return annotation
    # since 0.95+ (we're on 0.136+).
    responses={
        # v0.1.32.21 — declare 400 in the OpenAPI spec for SonarCloud
        # rule python:S6788. patch_session() raises HTTPException(400)
        # from several config-field validators (otp_timezone,
        # otp_build_heap, otp_heap, otp_api_timeout). The rule is
        # satisfied at the decorator level — individual raise sites
        # don't need per-line documentation.
        400: {"description": "Config field failed validation (heap / timezone / timeout)."},
    },
)
def patch_session(
    sid: str,
    body: SessionPatch,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> SessionResponse:
    # Row lock until the commit: two saves of one session run one after the
    # other, so each save's quarantine plan (issue #381) sees the config the
    # other one stored.
    s = db.get(SessionRow, sid, with_for_update=True)
    if s is None:
        raise HTTPException(404, "Session not found")

    changes: dict[str, dict[str, Any]] = {}
    new_name = body.name.strip() if body.name is not None else None
    if new_name is not None and not new_name:
        raise HTTPException(400, "Session name cannot be blank")
    if new_name is not None and new_name != s.name:
        changes["name"] = {"from": s.name, "to": new_name}
        s.name = new_name
    if body.config is not None and body.config != s.config:
        # Validate osm_scope if present — fail-fast at save time means the
        # operator gets a clear UI error instead of an opaque build failure
        # when osmium-tool errors out on an unknown scope name.
        if "osm_scope" in body.config:
            from ... import osm_filter

            try:
                body.config["osm_scope"] = osm_filter.validate_scope(body.config["osm_scope"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.21 — validate otp_timezone if present. Same fail-fast rationale:
        # an invalid IANA tz here would cause OTP to refuse the build with
        # "Cannot resolve zone id <bogus>". Catching at save time means the
        # operator sees the error in a toast next to the dropdown instead of
        # 5 minutes into a rebuild log.
        if "otp_timezone" in body.config:
            from ... import otp_timezone as _otp_tz

            try:
                body.config["otp_timezone"] = _otp_tz.validate_timezone(body.config["otp_timezone"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.23 — validate otp_build_heap. Same fail-fast pattern. A bad
        # value (e.g. "12 GB" with a space, or "12gb" with the wrong unit)
        # would silently fall back to the env-var default at the worker —
        # confusing because the operator's deliberate UI choice would be
        # invisibly ignored. Reject up front.
        if "otp_build_heap" in body.config:
            from ... import otp_heap as _otp_heap

            try:
                body.config["otp_build_heap"] = _otp_heap.validate_heap(
                    body.config["otp_build_heap"]
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.32.21 — validate otp_heap (the SERVE-time JVM -Xmx, distinct
        # from otp_build_heap above). Used by the per-session OTP serving
        # container after the build completes. Same fail-fast pattern as
        # otp_build_heap. Surfaced 2026-05-10 when an operator picked a
        # 64g build heap, the build succeeded, but the serving container
        # crash-looped at the hidden 4g default — see audit-2026-05.md.
        if "otp_heap" in body.config:
            from ... import otp_heap as _otp_heap

            try:
                body.config["otp_heap"] = _otp_heap.validate_heap(body.config["otp_heap"])
            except ValueError as exc:
                # 400 is already declared on the route decorator's `responses`
                # parameter (set in v0.1.32.21 alongside this validation
                # block); SonarCloud rule python:S6788 is satisfied at the
                # decorator level, not per-raise.
                raise HTTPException(400, str(exc)) from exc

        # v0.1.24 — validate otp_api_timeout. Operator picks how long OTP
        # is allowed to spend per journey-search request. Bad values
        # (e.g. "30 s" with a space, "30sec", ISO-8601 "PT30S") would
        # silently fall back to default; reject up front for the same
        # reason as the other knobs.
        if "otp_api_timeout" in body.config:
            from ... import otp_api_timeout as _otp_api_timeout

            try:
                body.config["otp_api_timeout"] = _otp_api_timeout.validate_timeout(
                    body.config["otp_api_timeout"]
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # v0.1.40 — validate osm_countries (the GEOGRAPHIC scope: crop the OSM
        # street graph to these served countries, orthogonal to osm_scope's
        # tag filter). Same fail-fast pattern; an unknown ISO code 400s here
        # rather than producing an empty crop polygon at build time. See
        # docs/osm-geographic-scope-design.md.
        if "osm_countries" in body.config:
            from ... import osm_geo as _osm_geo

            try:
                body.config["osm_countries"] = _osm_geo.validate_countries(
                    body.config["osm_countries"]
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        # Validate provider bundles (v0.1.6) on save. We accept the legacy
        # gtfs[]/gtfs="..." shapes too (normalize_providers handles them),
        # but for save-time validation we only error on the v0.1.6-shaped
        # providers list since that's what the v0.1.6 UI emits. Legacy
        # shapes pass through unmodified (they'll be migrated on next
        # PATCH that uses the new UI).
        if isinstance(body.config.get("sources"), dict) and "providers" in body.config["sources"]:
            try:
                providers = ingestion.normalize_providers(body.config)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            _refuse_new_derived_formats(providers, s.config or {})
            # Country-gate: every provider that declares country_iso=X must
            # have at least one master_stations row with country_iso=X, or X
            # must be a country the station module serves (D-R12 B; asked
            # only when the Trainline list lacks one: `_unserved`). A country
            # neither knows is accepted with a warning when VIATOR uses the
            # module (the module serves it only after accepting a feed that
            # VIATOR sends once this save is done; the graph is built and warns,
            # `country_gate.build_warning`); without the module, refused with
            # a clear message and the Trainline-import advice, as before.
            # Only the countries this save adds are gated: a country already
            # among the stored providers passed the gate when it was saved,
            # and must not be refused because the module is down or paused
            # now. Every declared country still feeds the OSM warning below.
            declared_countries = {p["country_iso"] for p in providers if p["country_iso"]}
            missing = _gate_blocking(db, declared_countries - _stored_countries(s.config))
            new_country = _new_countries(missing, _SAVE_ADVICE)
            # All checks passed — write back the canonicalised provider
            # list (drops empty fields, normalises country to upper-case,
            # etc.). Operator never sees the cleanup; the next GET returns
            # the normalised shape.
            body.config["sources"]["providers"] = providers

            # Soft warning when the OSM PBF URL likely doesn't cover one
            # of the declared provider countries (v0.1.7-D). Surfaced via
            # `X-Warnings` response header — UI parses + toasts. Never
            # blocks the save (per agreed design).
            osm_warning = _osm_coverage_warning(
                body.config["sources"].get("osm_pbf"),
                declared_countries,
            )
            warnings = [text for text in (new_country, osm_warning) if text]
            if warnings:
                response.headers["X-Warnings"] = json.dumps(warnings)

        # Track staleness: bump `sources_changed_at` if (and only if) the
        # `sources` subtree actually changed. Edits that only touch
        # osm_scope or other non-sources keys don't bump this — the
        # downloaded data is still fresh w.r.t. URLs.
        if not staleness.sources_subtree_equal(s.config, body.config):
            staleness.mark_sources_changed(body.config)

        changes["config"] = {"from": s.config, "to": body.config}
        s.config = body.config
    if body.include_in_fanout is not None and body.include_in_fanout != s.include_in_fanout:
        changes["include_in_fanout"] = {"from": s.include_in_fanout, "to": body.include_in_fanout}
        s.include_in_fanout = body.include_in_fanout
    if body.state is not None and body.state != s.state:
        if body.state not in _VALID_STATES:
            raise HTTPException(400, f"Invalid state {body.state!r}")
        changes["state"] = {"from": s.state, "to": body.state}
        s.state = body.state

    # Every check passed: plan the quarantine now, rename after the commit.
    plan = _save_quarantine_plan(changes)
    if changes:
        audit.record(
            db,
            action="session.updated",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={"changes": changes},
        )
    db.commit()
    if plan is not None:
        _quarantine_after_save(db, request, actor, sid, plan)
    if "config" in changes:
        # A provider removed from the session shows no station module status.
        station_extract_upload.forget_outcomes_except(sid, _provider_ids(s.config or {}))
    return SessionResponse.from_orm_session(s)


def _refuse_new_derived_formats(providers: list[dict[str, Any]], stored: dict[str, Any]) -> None:
    """400 when a save declares a derived (cross-border) provider in a format
    other than GTFS: the filter writes GTFS only (issue #381). A derived
    provider stored before with that same format is let through, so an
    existing session stays savable; its file is GTFS all the same
    (`ingestion.provider_slot`)."""
    try:
        before = {
            (p["id"], (p.get("timetable") or {}).get("format"))
            for p in ingestion.normalize_providers(stored)
            if (p.get("timetable") or {}).get("source") == ingestion.DERIVED_SOURCE
        }
    except ValueError:
        before = set()
    for p in providers:
        tt = p.get("timetable") or {}
        fmt = tt.get("format")
        if (
            tt.get("source") == ingestion.DERIVED_SOURCE
            and fmt != ingestion.DERIVED_TIMETABLE_FORMAT
            and (p["id"], fmt) not in before
        ):
            raise HTTPException(
                400,
                f"provider {p['id']!r}: a cross-border filter (derived) provider is GTFS "
                f"(the filter reads and writes GTFS only); set its format to gtfs, not {fmt!r}",
            )


def _save_quarantine_plan(changes: dict[str, dict[str, Any]]) -> inbox_sweep.SavePlan | None:
    """The files a config save will quarantine (issue #381), from the two
    configs the save compared (`changes`, never read again from the row);
    None when the config did not change. Touches no file."""
    config_change = changes.get("config")
    if config_change is None:
        return None
    return inbox_sweep.plan_save_quarantine(config_change["from"] or {}, config_change["to"] or {})


def _quarantine_after_save(
    db: DbSession,
    request: Request,
    actor: CurrentUser,
    sid: str,
    plan: inbox_sweep.SavePlan,
) -> None:
    """After a save's commit: rename the planned other-format files (GTFS and
    NeTEx share `<id>.zip`; the one in the other subdir would reach the next
    build, which can start before any refresh) and record them in a
    follow-up audit row, `session.format_switch.quarantined`. A failure to
    record logs a WARNING; the files are right either way."""
    orphaned = inbox_sweep.apply_save_quarantine(settings.inbox_dir / sid, plan)
    if not orphaned:
        return
    try:
        audit.record(
            db,
            action="session.format_switch.quarantined",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={"orphaned": orphaned},
        )
        db.commit()
    except Exception as exc:  # the renames are done; only their audit row is lost
        log.warning("session.quarantine_audit_failed type=%s", type(exc).__name__)
        _rollback_quietly(db)


def _provider_ids(config: dict[str, Any]) -> set[str] | None:
    """The session's provider ids, or None when the config cannot be read."""
    try:
        return {p["id"] for p in ingestion.normalize_providers(config)}
    except ValueError:
        return None


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> Response:
    """Permanently delete a session and all its data.

    This is the irreversible "start from scratch" path — distinct from
    `POST /{sid}/archive` which keeps the inbox/graphs and DB rows
    around so they can be restored. Delete:

      - Wipes session row + all FK-referenced child rows (rebuild_jobs,
        uploads, graph_snapshots, journey_search_executions and their
        trips, mct_overrides, stations_xref).
      - Removes the session's filesystem trees (inbox/<sid>/,
        graphs/<sid>/).
      - Re-runs the sessions orchestrator so the per-session compose
        and nginx fragments drop the deleted session, then touches the
        reload-trigger so the worker tears down the otp-<sid> service
        + reloads nginx.

    NOT done by this endpoint:

      - Audit events tagged with this session: kept (immutable record
        of what once existed). Their `target_id` references a session
        that no longer exists in `sessions`, but `audit_events.target_id`
        is just a string column — no FK enforces it.
      - Master-station rows: never owned by a session, untouched.

    Cascade order matters because FK columns to sessions.id don't have
    `ondelete=CASCADE` (an alembic migration to add cascade everywhere
    would be cleaner long-term — tracked as Phase-3 cleanup).
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    # Snapshot what we're about to delete for the audit row — useful when
    # an operator deletes the wrong session and wants to know what was lost.
    # Explicit dict[str, Any] type so the later `setdefault("filesystem_warnings", [])`
    # below is allowed; without the annotation mypy narrows the value type to
    # the union of the literal initialiser values and rejects appending a list.
    audit_metadata: dict[str, Any] = {
        "name": s.name,
        "category": s.category,
        "state": s.state,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "config_summary": {
            "providers": [
                p.get("id") for p in (s.config or {}).get("sources", {}).get("providers", [])
            ],
            "had_osm_pbf": bool((s.config or {}).get("sources", {}).get("osm_pbf")),
        },
    }

    # ── DB cascade — explicit because no FK has ondelete=CASCADE today ──
    from ...models import (  # local imports keep top-of-file lean
        GraphSnapshot,
        JourneySearchExecution,
        McTOverride,
        StationXref,
    )

    # journey_search_executions has trips that cascade automatically because
    # the trips→executions FK already has ondelete="CASCADE".
    db.query(JourneySearchExecution).filter(JourneySearchExecution.session_id == sid).delete(
        synchronize_session=False
    )
    db.query(McTOverride).filter(McTOverride.session_id == sid).delete(synchronize_session=False)
    db.query(StationXref).filter(StationXref.session_id == sid).delete(synchronize_session=False)
    db.query(GraphSnapshot).filter(GraphSnapshot.session_id == sid).delete(
        synchronize_session=False
    )
    db.query(Upload).filter(Upload.session_id == sid).delete(synchronize_session=False)
    db.query(RebuildJob).filter(RebuildJob.session_id == sid).delete(synchronize_session=False)
    db.delete(s)
    # Force the unit-of-work to flush BEFORE the orchestrator queries
    # SELECT * FROM sessions. Without this, the orchestrator sees the
    # to-be-deleted session as still present (autoflush doesn't always
    # fire reliably across `db.delete()` + a sibling SELECT in the
    # same transaction, and the consequence is a generated
    # nginx-sessions.conf that still references the dead `otp-<sid>`
    # upstream — nginx then refuses to reload with "host not found in
    # upstream" until someone wipes the file manually).
    db.flush()

    # Re-run the orchestrator so the deleted session drops out of the
    # compose + nginx fragments. Done before commit so any DB-side
    # constraint failure rolls back the orchestrator change too.
    sessions_orchestrator.regenerate(db)

    # ── Filesystem cleanup ─────────────────────────────────────────
    # Done before commit so a permission failure surfaces as 500 rather
    # than leaving the DB inconsistent with disk. Worker has rw on both
    # inbox + graphs volumes; web has rw on inbox + ro on graphs (per
    # current docker-compose), so this delete needs the worker — but
    # we run it inline in web for simplicity. If web lacks permission,
    # the cleanup is best-effort: log and continue.
    import shutil  # local — only needed here

    # Trees to remove:
    #   inbox/<sid>/                       — staged GTFS / OSM / etc.
    #   graphs/<sid>/                      — built graph + timestamped history
    #   graphs/.cache/<sid>/               — streetGraph.obj cache (v0.1.7).
    #                                        Outlives a session otherwise.
    for tree in (
        settings.inbox_dir / sid,
        settings.graph_dir / sid,
        settings.graph_dir / ".cache" / sid,
    ):
        if tree.exists():
            try:
                shutil.rmtree(tree)
            except OSError as exc:
                # Filesystem is reclaimable later via worker cleanup or
                # operator SSH; don't block the API delete on it.
                audit_metadata.setdefault("filesystem_warnings", []).append(
                    {"path": str(tree), "error": str(exc)}
                )

    # Touch the reload trigger so the worker tears down the otp-<sid>
    # container (if state was 'serving') and reloads nginx with the
    # new fragments. Same mechanism as `promote`.
    _RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    _RELOAD_TRIGGER.write_text(datetime.now(UTC).isoformat())

    audit.record(
        db,
        action="session.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata=audit_metadata,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{sid}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> Response:
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    s.state = SessionState.ARCHIVED.value
    s.include_in_fanout = False
    s.archived_at = datetime.now(UTC)
    audit.record(
        db,
        action="session.archived",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────── per-session uploads ─────────────────────────


class UploadResponse(BaseModel):
    id: str
    filename: str
    declared_kind: str
    detected_kind: str
    size_bytes: int
    triggered_rebuild: bool
    provider_feed_id: str | None = None


def _queue_extracts(
    background: BackgroundTasks,
    sid: str,
    config: dict[str, Any] | None,
    refreshed: bool,
    only_provider: str | None = None,
) -> None:
    """After a refresh that fetched a timetable or found it unchanged: the
    station module's stop extracts of the files now in the slots, queued
    after the answer is sent for the one warm thread of app/station_extract.py
    (`queue_warm` returns at once: no request thread ever waits for one)."""
    if refreshed:
        background.add_task(
            station_extract.queue_warm,
            sid,
            dict(config or {}),
            only_provider=only_provider,
        )


def _queue_upload_extract(
    background: BackgroundTasks,
    sid: str,
    config: dict[str, Any] | None,
    provider_id: str | None,
    source: str | None,
    sha256: str,
) -> None:
    """After an upload to an `upload` provider: the stop extract of that file,
    keyed by the sha256 of the uploaded bytes, queued after the answer."""
    if provider_id is not None and source == "upload":
        background.add_task(
            station_extract.queue_warm,
            sid,
            dict(config or {}),
            only_provider=provider_id,
            upload_sha256={provider_id: sha256},
        )


@router.post(
    "/{sid}/uploads",
    status_code=201,
    responses={
        400: {
            "description": "Unknown standard, format/standard mismatch, "
            "malformed config, or an unconfigured provider_id"
        },
        404: {"description": "Session not found"},
    },
)
async def upload_to_session(
    sid: str,
    declared_standard: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
    background: BackgroundTasks,
    provider_id: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    """Upload one file into this session's inbox.

    Streams to a staging file, sha256-hashes on the way, runs format
    detection, and dispatches via `app.ingestion.dispatch` which:

    - moves the file into the right per-kind subfolder under
      `inbox/<sid>/<kind>/`,
    - rotates any prior file of the same kind (`.old` suffix),
    - enqueues a `RebuildJob` for the worker if the kind triggers one
      (GTFS, OSM-PBF, NeTEx-Nordic, NeTEx-EPIP).

    A new Upload row is persisted; the session state advances to
    `populated` if it was at `created` or `configured`.

    `provider_id` (v0.1.37, optional): attach this upload to a configured
    provider. The file then lands at that provider's own inbox slot
    (`<feed_id>.zip`) instead of the generic `gtfs.zip`, the `Upload` row
    records the link, and the detected format must match the provider's
    declared timetable format. Omit it for the legacy per-session upload.
    See docs/provider-source-modes-design.md.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if declared_standard not in detect.KNOWN_KINDS:
        raise HTTPException(400, f"Unknown standard: {declared_standard}")
    if actor.id is None:
        raise HTTPException(400, "Uploads require a JWT-authenticated actor")

    # Stream to a per-session staging file while computing sha256.
    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename or "upload.bin")
    staged_path = staging / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{safe_name}"

    sha = hashlib.sha256()
    size = 0
    with staged_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):  # 1 MiB at a time
            sha.update(chunk)
            out.write(chunk)
            size += len(chunk)

    # Verify the format matches what the user said. dispatch() can move it
    # only if detect agrees.
    detected = _detected_kind(staged_path, declared_standard)

    # Optional: attach this upload to a configured provider (v0.1.37). The
    # file then lands at the provider's own slot (`<feed_id>.zip`) and the
    # Upload row records the link, instead of the generic gtfs.zip.
    staged_filename: str | None = None
    provider_feed_id: str | None = None
    upload_source: str | None = None
    if provider_id:
        try:
            providers = ingestion.normalize_providers(s.config)
        except ValueError as exc:
            staged_path.unlink(missing_ok=True)
            raise HTTPException(400, f"session config is malformed: {exc}") from exc
        match = next((p for p in providers if p["id"] == provider_id), None)
        if match is None:
            staged_path.unlink(missing_ok=True)
            raise HTTPException(
                400,
                f"provider {provider_id!r} is not configured in this session "
                "(add and save the provider card first)",
            )
        fmt = match["timetable"]["format"]
        expected_kind = ingestion.TIMETABLE_FORMAT_DETAILS[fmt]["kind"]
        if detected != expected_kind:
            staged_path.unlink(missing_ok=True)
            raise HTTPException(
                400,
                f"file detected as {detected!r}, but provider {provider_id!r} "
                f"expects {expected_kind!r} ({fmt})",
            )
        staged_filename = ingestion.staged_filename_for_format(provider_id, fmt)
        provider_feed_id = provider_id
        upload_source = match["timetable"].get("source")

    # The copy into the slot off the event loop (an upload can be GB); it
    # removes the staging file. The slot no longer holds the last downloaded
    # file — the job forgets the fetch state of every task that writes it, so
    # no refresh can call it "unchanged", even if this request is cancelled.
    triggered = await _dispatch_off_loop(
        db,
        staged_path,
        detected,
        sid,
        staged_filename,
        after=functools.partial(_forget_fetch_state_for_slot, sid, detected, staged_filename),
    )
    # Where did dispatch end up putting it? Reconstruct from the rules. With
    # a provider slot the name is `<feed_id>.zip`; otherwise the legacy name.
    final_path = _reconstruct_dispatch_target(sid, detected, staged_filename or staged_path.name)

    upload = Upload(
        session_id=sid,
        user_id=actor.id,
        filename=safe_name,
        declared_kind=declared_standard,
        detected_kind=detected,
        sha256=sha.hexdigest(),
        size_bytes=size,
        stored_path=str(final_path),
        triggered_rebuild=triggered,
        provider_feed_id=provider_feed_id,
    )
    db.add(upload)

    if s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

    # §12 — uploading a national feed cascades to any cross-border views derived
    # from it (same single-source-of-truth rule as refresh). The scan is cheap and
    # finds nothing when this upload wasn't a provider feed something links to.
    cascaded = await _cascade_derived_refresh(db, sid)

    audit.record(
        db,
        action="session.upload",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="upload",
        target_id=str(s.id),
        metadata={
            "session_id": sid,
            "kind": detected,
            "declared": declared_standard,
            "size_bytes": size,
            "triggered_rebuild": triggered,
            "provider_feed_id": provider_feed_id,
            "cascaded": _cascade_keys(cascaded),
        },
    )
    db.commit()
    db.refresh(upload)
    _queue_upload_extract(background, sid, s.config, provider_feed_id, upload_source, upload.sha256)
    return UploadResponse(
        id=str(upload.id),
        filename=upload.filename,
        declared_kind=upload.declared_kind,
        detected_kind=upload.detected_kind,
        size_bytes=upload.size_bytes,
        triggered_rebuild=upload.triggered_rebuild,
        provider_feed_id=upload.provider_feed_id,
    )


_COUNTRY_TO_OSM_HINTS: dict[str, set[str]] = {
    # Country ISO → substrings the OSM URL might contain to suggest coverage.
    # Geofabrik regional names are the common case; "europe" / "world" act
    # as wildcard catch-alls. Heuristic only — false positives (claims an
    # uncovered country) are tolerable; false negatives (claims a covered
    # country isn't covered, surfacing a confusing warning) are not. So
    # this list errs on the side of including more hint strings.
    "FR": {"france", "europe", "world", "planet"},
    "DE": {"germany", "deutschland", "europe", "world", "planet"},
    "IT": {"italy", "italia", "europe", "world", "planet"},
    "ES": {"spain", "espana", "europe", "world", "planet"},
    "PT": {"portugal", "europe", "world", "planet"},
    "NL": {"netherlands", "nederland", "europe", "world", "planet"},
    "BE": {"belgium", "belgie", "europe", "world", "planet"},
    "LU": {"luxembourg", "europe", "world", "planet"},
    "CH": {"switzerland", "suisse", "schweiz", "europe", "world", "planet"},
    "AT": {"austria", "oesterreich", "europe", "world", "planet"},
    "GB": {"britain", "uk", "england", "scotland", "wales", "europe", "world", "planet"},
    "IE": {"ireland", "europe", "world", "planet"},
    "DK": {"denmark", "europe", "world", "planet", "nordic"},
    "SE": {"sweden", "sverige", "europe", "world", "planet", "nordic"},
    "NO": {"norway", "norge", "europe", "world", "planet", "nordic"},
    "FI": {"finland", "europe", "world", "planet", "nordic"},
    "PL": {"poland", "polska", "europe", "world", "planet"},
    "CZ": {"czech", "europe", "world", "planet"},
    "HU": {"hungary", "europe", "world", "planet"},
    "GR": {"greece", "europe", "world", "planet"},
    # Add more as the demonstrator's reach grows. Catch-all behaviour for
    # countries not in this dict: no warning is emitted (we don't know
    # what hints to look for).
}


def _osm_coverage_warning(osm_url: str | None, declared_countries: set[str]) -> str | None:
    """Return a soft warning string when the OSM URL likely doesn't cover one
    or more declared provider countries. Returns None when:

      - osm_url is empty (operator hasn't set one yet — no false positive)
      - declared_countries is empty (no providers, nothing to check)
      - we don't have heuristic hints for any of the declared countries
        (better to stay silent than emit a guess we can't justify)
      - every declared country has at least one matching hint substring
        in the URL

    The warning is **soft** — it never blocks save (per agreed v0.1.6 design).
    Operators sometimes know things our heuristic doesn't (e.g. they merged
    multiple PBFs offline and uploaded the result manually); a hard block
    would frustrate those legitimate cases.
    """
    if not osm_url or not declared_countries:
        return None
    url_lower = osm_url.lower()
    uncovered: list[str] = []
    for ci in sorted(declared_countries):
        hints = _COUNTRY_TO_OSM_HINTS.get(ci)
        if hints is None:
            continue  # we don't know what to look for; stay silent
        if not any(h in url_lower for h in hints):
            uncovered.append(ci)
    if not uncovered:
        return None
    return (
        f"OSM URL doesn't appear to cover {uncovered}. Coordinate searches "
        "outside the PBF's region will fail with LOCATION_NOT_FOUND. "
        "If the URL is correct (e.g. you merged regions offline), ignore "
        "this; otherwise switch to a wider PBF."
    )


# The country gate (`app/country_gate.py`): the Trainline rows, then the
# station module for the gap.
_countries_without_stations = country_gate.countries_without_stations
_unserved = country_gate.unserved_by_module
# The countries the stored providers of a session already declare: a save
# gates only the countries it adds.
_stored_countries = country_gate.declared_countries

# The country gate's refusal (HTTP 409): the error word the pages and the
# PowerShell scripts read, and the advice of each kind of save.
_GATE_ERROR = country_gate.GATE_ERROR
_SAVE_ADVICE = (
    "Import them from Trainline first (POST /api/master/stations/refresh-trainline), "
    "then retry this save."
)
_IMPORT_ADVICE = "Import them from Trainline first, then retry the bulk-import."


def _gate_409(missing: set[str], advice: str) -> HTTPException:
    """The country gate's refusal for the countries `missing`: given only
    when VIATOR does not use the station module (`_new_countries`)."""
    return HTTPException(
        409,  # Conflict — semantically "preconditions not met"
        detail={
            "error": _GATE_ERROR,
            "missing_countries": sorted(missing),
            "message": (
                f"Cannot save: no master_stations rows for {sorted(missing)}, and "
                f"VIATOR does not use the station module. {advice}"
            ),
        },
    )


def _gate_blocking(db: DbSession, added: set[str]) -> set[str]:
    """The countries of `added` neither VIATOR's Trainline list nor the
    station module knows (`_unserved`), from a caller outside the event
    loop (a synchronous route); the module asked only for the Trainline
    gap, and none of it when `added` is empty."""
    missing = _countries_without_stations(db, added) if added else set()
    if missing:
        missing = _unserved(missing, station_module.countries_blocking())
    return missing


def _new_countries(missing: set[str], advice: str) -> str | None:
    """What a save does with the countries `missing` it adds that neither
    the Trainline list nor the station module knows: with the module used,
    accepted, and the warning to give (`country_gate`: the module serves a
    country only after it accepted a feed VIATOR sends once the provider is
    saved); without it, refused (409), as before. None for none."""
    if not missing:
        return None
    if not station_module.enabled():
        raise _gate_409(missing, advice)
    log.info("session.new_countries countries=%s", ",".join(sorted(missing)))
    return country_gate.new_country_warning(missing)


def _safe_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_ZIP_GLOB = "*.zip"


def _read_gtfs_stops(zip_path: Path) -> list[tuple[str | None, float | None, float | None]]:
    """Read `(stop_id, stop_lat, stop_lon)` from a GTFS zip's stops.txt.

    Best-effort for the osm-countries auto-detect: a missing / garbled
    stops.txt yields `[]`, and unparseable coordinates become None (the
    detector then leans on the UIC prefix). Never raises.
    """
    import csv
    import io
    import zipfile

    out: list[tuple[str | None, float | None, float | None]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open("stops.txt") as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
            for row in reader:
                out.append(
                    (
                        row.get("stop_id"),
                        _safe_float(row.get("stop_lat")),
                        _safe_float(row.get("stop_lon")),
                    )
                )
    except (KeyError, zipfile.BadZipFile, OSError, UnicodeDecodeError):
        return out
    return out


def _detected_kind(staged_path: Path, declared_standard: str) -> str:
    """What the staged file is, or 400 (and the staged copy removed) when it is
    not what the caller declared.

    `detect` raises on a file it cannot classify (an unknown extension, a CSV
    that is neither SNCF stations nor MCT) and `zipfile` on a zip that is not
    one. Both are the caller's mistake: 400, not 500.
    """
    try:
        detected = detect.detect(staged_path)
    except (ValueError, zipfile.BadZipFile) as exc:
        staged_path.unlink(missing_ok=True)
        raise HTTPException(400, f"Detection failed: {exc}") from exc
    if detected != declared_standard:
        staged_path.unlink(missing_ok=True)
        raise HTTPException(
            400,
            f"File looks like {detected!r}, but declared as {declared_standard!r}",
        )
    return detected


def _safe_filename(name: str) -> str:
    """Strip path components and dodgy chars from an uploaded filename."""
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:200] or "upload.bin"


def _reconstruct_dispatch_target(sid: str, kind: str, filename: str) -> Path:
    """Predict where ingestion.dispatch put the file. Mirrors that module's rules."""
    base = settings.inbox_dir / sid
    if kind in ingestion.STAGE_INTO_OTP_INBOX:
        return base / ingestion.STAGE_INTO_OTP_INBOX[kind] / filename
    if kind in ingestion.ARCHIVE_ONLY:
        return base / "archive" / kind / filename
    if kind in ingestion.LOAD_TO_DB:
        ext = Path(filename).suffix
        return base / "runtime" / kind / f"latest{ext}"
    return base / filename  # fallback (shouldn't hit)


# ──────────────────── refresh sources from configured URLs ────────────────────


class RefreshSourcesResponse(BaseModel):
    fetched: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    # Checked upstream and found identical to the file already in the slot
    # (HTTP 304 or sha256 match) — nothing rotated, no rebuild queued.
    unchanged: list[dict[str, Any]] = []
    # PR #33: filenames renamed to `.orphaned` because their provider was
    # removed from the session config but the inbox file lingered. Surfaced
    # to the operator so they can verify the build picks up only the
    # currently-configured providers.
    orphaned: list[str] = []
    # §12 — derived cross-border feeds in *other* sessions that were rebuilt
    # because they link to a provider in this (national) session. Empty unless
    # this refresh actually changed a national feed something derives from.
    cascaded: list[dict[str, Any]] = []


# v0.1.19 — per-provider fetch status, surfaced on the provider cards in the
# admin UI. Read-only view derived from filesystem (inbox file mtime + size)
# plus the latest refresh audit row. No new DB table; the audit log + inbox
# already carry everything we need to disambiguate "never attempted" from
# "fetched OK" from "last attempt failed".
class ProviderStatus(BaseModel):
    feed_id: str
    state: str  # "ok" | "stale" | "pending" | "error"
    fetched_at: datetime | None = None
    size_bytes: int | None = None
    error_hint: str | None = None  # short, UI-friendly explanation when state == "error"
    # v0.1.37 — content source ("url" | "upload"; "server_file" later) and,
    # for upload-source providers, the original filename of the file
    # currently attached to this provider. Lets the card show *which* file
    # backs the provider, not just that one is present.
    source: str | None = None
    upload_filename: str | None = None
    # Feed-status panel (docs/nap-feed-resolvers.md): grouping keys and the
    # per-task fetch state kept by app/feed_fetch.py.
    label: str | None = None
    country_iso: str | None = None
    format: str | None = None
    resolver_type: str | None = None  # "tdg" | "udata" | … for source "nap"
    checked_at: datetime | None = None  # last time a refresh confirmed the file current
    # {"at", "status": "fetched" | "unchanged" | "skipped", "reason"} of the
    # latest refresh of this provider's timetable, including a full skip reason.
    last_attempt: dict[str, Any] | None = None
    # True when the file in the slot passed the format check on its way in
    # (a checked download, or an upload — both run detect). None = unknown:
    # no file, a derived feed, or a file that predates the check. A format
    # check only — it says nothing about the timetable content.
    format_ok: bool | None = None
    # NeTEx only: {"level": "ok"|"warn"|"red", "messages": [...]} from the
    # structure check run at download (app/netex_structure.py). Never blocks.
    structure: dict[str, Any] | None = None
    # The last time this provider's stop extract went to the station module
    # (MSMM 22.3; app/station_extract_upload.py): {"at", "state", "reason",
    # "text", "detail", "summary"}, the text one of "sent", "already held",
    # "refused <reason>", "skipped", "module unavailable", the detail a fixed
    # sentence saying what the reason means (REASON_TEXTS) or None, the summary
    # what the extractor saw of a feed without a rail station (counts and mode
    # words, at most 100 characters) or None. None until a first try.
    station_extract: dict[str, Any] | None = None


# How recently a provider's inbox file must have been refreshed before we
# stop calling it "ok" and start calling it "stale". 24h is a sensible
# default for daily-published GTFS / GTFS-RT-paired feeds. Operator-tunable
# later if anyone asks; today there's no operator nudge to make it
# session-specific so we keep it module-level and obvious.
_PROVIDER_FRESHNESS_HOURS = 24


def _relock_session(db: DbSession, s: SessionRow) -> None:
    """Re-read the session row under a row lock (SELECT … FOR UPDATE), just
    before a refresh writes its state and config: the row was loaded when
    the request began, and during a long download another card's refresh or
    a config save may have changed it. The refresh then changes only its own
    keys on the current config, and the lock holds until its commit."""
    db.refresh(s, with_for_update=True)


def _sweep_orphans(sid: str, s: SessionRow) -> list[str]:
    """PR #33 — sweep orphaned inbox files left behind by providers that
    used to be in `sources.providers` but no longer are. Without this,
    removing a provider via the UI leaves its `<id>.zip` in inbox/gtfs/
    and the OTP entrypoint's `gtfs/*.zip` glob picks it up at build time
    — operator thinks they removed BrittanyFerries, build still fails
    on BrittanyFerries' data. Surfaced 2026-05-11. A file of a format the
    provider no longer uses (`netex/<id>.zip` once it is GTFS) is an
    orphan too: the sweep compares the subdir with the name (issue #381).

    Called right after `_relock_session`: the expected slots come from the
    config stored now, re-read under the row lock, not the one captured when
    the refresh began — a format switch saved (and its new file uploaded)
    during the download keeps its new file, and a stale file the refresh
    downloaded after that switch is quarantined."""
    expected = inbox_sweep.expected_provider_filenames(s.config or {})
    return inbox_sweep.sweep_orphaned_provider_files(settings.inbox_dir / sid, expected)


def _mark_providers_refreshed(s: SessionRow, *, fetched: bool, unchanged: bool) -> None:
    """The session's state and staleness after a provider refresh."""
    if fetched and s.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        s.state = SessionState.POPULATED.value

    # Staleness tracking (v0.1.7.1): mark refresh completed so the next
    # rebuild is no longer flagged stale. Done only when at least one
    # task fetched or confirmed its file current — if every URL failed,
    # the on-disk data is still stale and the operator needs to know.
    if fetched or unchanged:
        if s.config is None:
            s.config = {}
        staleness.mark_refresh_completed(s.config)
        flag_modified(s, "config")


@router.post(
    "/{sid}/sources/refresh",
    responses={
        400: {"description": "No JWT actor, or no sources configured."},
        404: {"description": "Session not found."},
        409: {
            "description": "A refresh of this kind is already running for the session "
            "(`refresh_already_running`, with its start time; issue #359), or a "
            "refresh of one provider alone (`provider_refresh_running`, with its "
            "provider id and start time)."
        },
    },
)
async def refresh_sources(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
    background: BackgroundTasks,
) -> RefreshSourcesResponse:
    """Download every PROVIDER URL in `config.sources` into the session's inbox.

    Includes: each provider's timetable + GTFS-RT (handled by OTP at runtime,
    not pre-fetched here) + MCT + stations CSV. Excludes the session-level
    OSM PBF — that has its own POST /sources/osm/refresh endpoint
    (v0.1.14) so a provider tweak doesn't accidentally invalidate the
    streetGraph cache and add 30 min to the next build.

    On success the file is dispatched via `ingestion.dispatch`, which
    queues a rebuild for kinds that warrant one. Existing files of the
    same kind are rotated (`.old` suffix) by dispatch.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    # Read once: a task's unexpected error rolls the session back, which
    # expires `s`, and reading it again would need a pooled connection
    # outside any task's boundary (`_unexpected_skip`).
    config: dict[str, Any] = s.config or {}
    sources: dict[str, Any] = config.get("sources", {})
    if not sources:
        raise HTTPException(400, "No sources configured. Set config.sources first.")

    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    # v0.1.14: providers-only by default. OSM PBF lives behind its own
    # endpoint so refreshing a GTFS feed doesn't accidentally bust the
    # streetGraph cache. See `_build_refresh_tasks`'s `include_osm` doc.
    work = _build_refresh_tasks(config, include_osm=False)
    # Issue #359: the progress record every page reads; 409 while one runs.
    run = _claim_refresh(sid, refresh_progress.PROVIDERS, len(work) + _derived_count(config))
    async with run:
        fetched, unchanged, skipped = await _gather_refresh_outcomes(
            db, sid, staging, config, work, run
        )
        run.phase(refresh_progress.FINISHING)

        # §12 — single-source-of-truth cascade. When this session's feeds actually
        # changed, re-derive every cross_border_filter provider (in other sessions)
        # that's linked to a provider here. So refreshing the Renfe national feed
        # automatically rebuilds the corridors cross-border view — no human has to
        # remember the second one.
        cascaded = await _cascade_derived_refresh(db, sid) if fetched else []

        # Last, under the row lock until the commit (never held over the cascade).
        _relock_session(db, s)
        orphaned = _sweep_orphans(sid, s)
        _mark_providers_refreshed(s, fetched=bool(fetched), unchanged=bool(unchanged))
        saved_config = s.config  # the commit below expires `s`

        audit.record(
            db,
            action="session.sources.refreshed",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={
                "scope": "providers",  # v0.1.14: distinguishes from osm-only refreshes
                "fetched": [f["key"] for f in fetched],
                "unchanged": [u["key"] for u in unchanged],
                "skipped": [s_["key"] for s_ in skipped],
                "orphaned": orphaned,  # PR #33
                "cascaded": _cascade_keys(cascaded),
            },
        )
        db.commit()
    _queue_extracts(background, sid, saved_config, bool(fetched or unchanged))
    # MSMM 22.3 (D-R15 A): at the end of every refresh, whatever the engine,
    # the stop extracts the station module lacks are sent, by the process's
    # sender thread (the refresh never waits on the module).
    station_extract_upload.after_refresh(sid, run_id=run.run_id)
    return RefreshSourcesResponse(
        fetched=fetched,
        unchanged=unchanged,
        skipped=skipped,
        orphaned=orphaned,
        cascaded=cascaded,
    )


@router.get("/{sid}/sources/progress", responses={404: {"description": "Session not found."}})
def get_refresh_progress(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """The progress of the session's latest provider refresh and OSM refresh
    (issue #359; app/refresh_progress.py): `{"providers": record | None,
    "osm": record | None, "single": [record, ...], "now"}`, `single` the
    refreshes of one provider alone (a provider card's Refresh). Read from
    the database, so it survives a page reload and is the same for every
    tab, browser and web process. The
    same role as the refresh buttons: whoever may start a refresh may watch
    it. Labels and counts only: never a URL or a reason text."""
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    return refresh_progress.read(sid, providers=refresh_progress.provider_ids(s.config))


# ──── OSM-only refresh (v0.1.14) ────


# Number of historical OSM PBFs to keep on rotation. Each refresh shifts
# osm.pbf → osm.pbf.old.1 (and existing .old.1 → .old.2, etc.); anything
# beyond this count is deleted. Reasonable budget on disk for France-wide:
# ~5 GB x 3 = 15 GB. Operators on a tight VPS can manually delete .old.N
# files between refreshes if disk pressure builds.
_OSM_OLD_GENERATIONS_KEPT = 3


def _rotate_osm_pbf(session_inbox: Path) -> list[str]:
    """Shift osm.pbf → osm.pbf.old.1 → .old.2 → .old.N. Returns a list
    of human-readable rotation events for the audit/UI response.

    Idempotent on missing files — if there's no current osm.pbf yet, no-ops.
    Best-effort on individual rename failures (logs and continues so the
    rotation can still proceed; surfacing as a warning is more useful than
    aborting the whole refresh).
    """
    osm_dir = session_inbox / "osm"
    events: list[str] = []
    if not osm_dir.is_dir():
        return events

    # 1. Drop the oldest generation if it would push us over budget.
    oldest = osm_dir / f"osm.pbf.old.{_OSM_OLD_GENERATIONS_KEPT}"
    if oldest.exists():
        try:
            oldest.unlink()
            events.append(f"deleted oldest .old.{_OSM_OLD_GENERATIONS_KEPT}")
        except OSError as exc:
            log.warning("could not delete %s: %s", oldest, exc)

    # 2. Shift .old.<N-1> → .old.<N>, .old.<N-2> → .old.<N-1>, etc.
    for n in range(_OSM_OLD_GENERATIONS_KEPT - 1, 0, -1):
        src = osm_dir / f"osm.pbf.old.{n}"
        dst = osm_dir / f"osm.pbf.old.{n + 1}"
        if src.exists():
            try:
                src.rename(dst)
                events.append(f".old.{n} → .old.{n + 1}")
            except OSError as exc:
                log.warning("could not rotate %s → %s: %s", src, dst, exc)

    # 3. Move the current osm.pbf → osm.pbf.old.1 (if it exists).
    current = osm_dir / "osm.pbf"
    if current.exists():
        try:
            current.rename(osm_dir / "osm.pbf.old.1")
            events.append("osm.pbf → .old.1")
        except OSError as exc:
            log.warning("could not rotate %s → osm.pbf.old.1: %s", current, exc)

    return events


class RefreshOsmResponse(BaseModel):
    fetched: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    rotated: list[str]


@router.post(
    "/{sid}/sources/osm/refresh",
    responses={
        400: {"description": "No JWT actor, or config.sources.osm_pbf is unset."},
        404: {"description": "Session not found."},
        409: {
            "description": "A refresh of this kind is already running for the session "
            "(`refresh_already_running`, with its start time; issue #359)."
        },
    },
)
async def refresh_osm(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RefreshOsmResponse:
    """Re-download the session's OSM PBF only.

    **Side effect that operators MUST know about**: the streetGraph.obj
    cache key is `sha256(osm.pbf):scope`. Geofabrik rolls the PBF nightly,
    so any non-trivial gap between the cached fetch and this one will
    invalidate the cache → the next rebuild includes a 25-min full OSM
    parse + intersect step.

    The UI's "Refresh OSM" button shows a confirm dialog with this
    warning. CLI callers see it documented in this docstring.

    Rotation: before overwriting, the current `osm.pbf` is shifted to
    `osm.pbf.old.1` (existing `.old.1` → `.old.2`, etc., up to
    _OSM_OLD_GENERATIONS_KEPT). This makes a manual rollback ("revert to
    yesterday's OSM") a one-command `mv` away — useful when a fresh
    Geofabrik PBF turns out to have a regression.

    Returns:
        `fetched`  — the OSM-PBF download outcome (one item, or zero on skip)
        `skipped`  — non-empty only if the OSM URL is unset or download failed
        `rotated`  — list of rotation events for the audit trail / UI display
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    sources: dict[str, Any] = (s.config or {}).get("sources", {})
    osm_url = sources.get("osm_pbf")
    if not isinstance(osm_url, str) or not osm_url:
        raise HTTPException(400, "config.sources.osm_pbf is unset; nothing to refresh")

    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)

    # Rotate BEFORE downloading so a failed fetch leaves the previous
    # generation in place but recoverable from .old.1 (operator can mv it
    # back to osm.pbf if they need it).
    # Issue #359: claimed before the rotation, so a refused second click
    # rotates nothing.
    run = _claim_refresh(sid, refresh_progress.OSM, 1)
    async with run:
        rotated = _rotate_osm_pbf(settings.inbox_dir / sid)

        fetched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        # Pass staged_filename="osm.pbf" so dispatch uses the targeted-rotation
        # branch (only rotates the exact file). The legacy "rotate everything
        # not ending in .old" branch would re-rotate our .old.<N> generations,
        # producing garbage like osm.pbf.old.1.old. See app/ingestion.py.
        task = _RefreshTask("osm_pbf", "OSM-PBF", osm_url, "osm.pbf", None)
        async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
            with run.task(task.label):
                outcome = await _refresh_one_task(client, db, sid, staging, task)
            run.done(task.label, outcome)
            if outcome.get("status") == "fetched":
                fetched.append({k: v for k, v in outcome.items() if k != "status"})
            else:
                skipped.append({k: v for k, v in outcome.items() if k != "status"})
        run.phase(refresh_progress.FINISHING)

        if fetched:
            # Under the row lock until the commit: a config saved during the
            # download (a provider card's refresh, the config form) is kept.
            _relock_session(db, s)
            if s.config is None:
                s.config = {}
            staleness.mark_refresh_completed(s.config)
            flag_modified(s, "config")

        audit.record(
            db,
            action="session.osm.refreshed",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={
                "url": osm_url,
                "rotated": rotated,
                "fetched": [f["key"] for f in fetched],
                "skipped": [s_["key"] for s_ in skipped],
                # Flagged so monitoring can alert on osm refreshes (they're rare
                # by intent — every one invalidates the streetGraph cache).
                "invalidates_street_graph_cache": bool(fetched),
            },
        )
        db.commit()
    return RefreshOsmResponse(fetched=fetched, skipped=skipped, rotated=rotated)


def _url_suffix(url: str) -> str:
    """Best-guess file extension from a URL, for the staged filename."""
    tail = url.rsplit("/", 1)[-1].lower()
    for ext in (".pbf", ".zip", ".csv", ".xml", ".gz"):
        if ext in tail:
            return ext
    return ""


# ──── refresh task model — used by both session-wide and per-provider ────


class _RefreshTask(NamedTuple):
    """One download. `label` is what we surface in the API response —
    operators see things like `provider[SNCF].timetable(gtfs)` or
    `provider[SNCF].mct`, not just bare keys. `credential_id` (v0.1.10) is the
    optional UUID of a user_credentials row whose decrypted secret should be
    applied to the HTTP request; None means anonymous fetch. `resolver` is set
    for a `source: "nap"` timetable — `url` is then only its display form and
    the file URL is resolved at refresh time (app/feed_resolvers.py)."""

    label: str
    kind: str
    url: str
    staged_filename: str | None
    credential_id: str | None
    resolver: dict[str, Any] | None = None


def _build_refresh_tasks(
    config: dict[str, Any],
    *,
    only_provider: str | None = None,
    include_osm: bool = False,
) -> list[_RefreshTask]:
    """Flatten a session config into a list of download tasks.

    `only_provider`, when set, filters to that provider's tasks only —
    used by the per-provider refresh endpoint. None means "everything".

    `include_osm` (v0.1.14): controls whether the session-level OSM PBF
    is included. **Default False** — refreshing providers must NOT
    re-fetch OSM, because Geofabrik rolls the PBF nightly, which would
    invalidate the streetGraph.obj cache (sha256(osm.pbf):scope) and
    force a 30-min full rebuild on what should have been a quick
    transit-only swap. The dedicated POST /sources/osm/refresh endpoint
    sets this to True.

    Per-provider refresh (only_provider != None) ignores `include_osm`
    entirely — it never refreshes OSM.

    Two input shapes:
      A. v0.1.6 native — `sources.providers = [{...}]` plus session-level
         `sources.osm_pbf`. Each provider contributes 1-3 tasks (timetable,
         mct, stations_csv) depending on what's set.
      B. v0.1.4 / pre — flat `sources.gtfs/osm_pbf/mct/stations`. Lifted
         into the same task tuples via normalize_providers.
    """
    sources = config.get("sources") or {}
    if not isinstance(sources, dict):
        return []

    tasks: list[_RefreshTask] = []

    # Provider tasks (multi-format timetable + optional mct/stations).
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        # Operator has saved a malformed shape via raw API — skip provider
        # tasks rather than crash refresh. Country-gate / save-time
        # validation is the right place to surface the error; here we
        # just degrade gracefully.
        providers = []

    for p in providers:
        if only_provider is not None and p["id"] != only_provider:
            continue
        pid = p["id"]
        tt = p.get("timetable") or {}
        tt_url = tt.get("url")
        tt_fmt = tt.get("format", "gtfs")
        resolver = tt.get("resolver") if tt.get("source") == "nap" else None
        if tt_url or resolver:
            tasks.append(
                _RefreshTask(
                    f"provider[{pid}].timetable({tt_fmt})",
                    ingestion.TIMETABLE_FORMAT_DETAILS[tt_fmt]["kind"],
                    feed_resolvers.describe(resolver) if resolver else str(tt_url),
                    ingestion.staged_filename_for_format(pid, tt_fmt),
                    p.get("timetable_credential_id"),
                    resolver,
                )
            )
        if p.get("mct_url"):
            tasks.append(
                _RefreshTask(
                    f"provider[{pid}].mct",
                    "SNCF-MCT",
                    p["mct_url"],
                    None,
                    p.get("mct_credential_id"),
                )
            )
        if p.get("stations_csv_url"):
            tasks.append(
                _RefreshTask(
                    f"provider[{pid}].stations_csv",
                    "SNCF-Stations",
                    p["stations_csv_url"],
                    None,
                    p.get("stations_csv_credential_id"),
                )
            )

    # Session-level OSM PBF — opt-in via include_osm (v0.1.14). Per-provider
    # refresh never includes OSM. Geofabrik-class hosts don't require auth,
    # so no credential field today.
    if (
        only_provider is None
        and include_osm
        and isinstance(sources.get("osm_pbf"), str)
        and sources["osm_pbf"]
    ):
        tasks.append(_RefreshTask("osm_pbf", "OSM-PBF", sources["osm_pbf"], None, None))

    return tasks


async def _materialise_derived_provider(
    db: DbSession, target_sid: str, provider: dict[str, Any], staging: Path
) -> dict[str, Any] | None:
    """Run the cross-border filter for one derived provider into `target_sid`'s slot.

    Returns an outcome dict (fetched/skipped), or None when `provider` isn't a
    `cross_border_filter` provider. The CPU/IO filter and the copy into the slot
    run in a worker thread (they touch no DB); the rebuild is queued on the
    event loop (`_dispatch_off_loop`). See docs/provider-source-modes-design.md §12.
    An unexpected exception (the copy, the rebuild queue's database) skips
    this provider alone, like a download task (`_unexpected_skip`); a
    cancellation still ends the refresh.
    """
    tt = provider.get("timetable") or {}
    if tt.get("source") != "cross_border_filter":
        return None
    key = f"provider[{provider['id']}].cross_border_filter"
    try:
        return await _filter_derived_provider(db, target_sid, provider, tt, staging, key)
    except Exception as exc:  # one provider's surprise must not end the batch
        return {"status": "skipped", "key": key, "error": _unexpected_skip(db, key, exc)}


async def _filter_derived_provider(
    db: DbSession,
    target_sid: str,
    provider: dict[str, Any],
    tt: dict[str, Any],
    staging: Path,
    key: str,
) -> dict[str, Any]:
    """`_materialise_derived_provider`'s work, for a derived provider whose
    timetable block is `tt` and whose outcome key is `key`."""
    from app import gtfs_cross_border_filter as xb

    pid = provider["id"]
    df = tt.get("derived_from") or {}
    src_session, src_provider = df.get("session_id"), df.get("provider_id")
    src_subdir, src_name = ingestion.staged_slot_for_format(
        str(src_provider), ingestion.DERIVED_TIMETABLE_FORMAT
    )
    src_slot = settings.inbox_dir / str(src_session) / src_subdir / src_name
    if not src_slot.exists():
        return {"status": "skipped", "key": key, "error": _missing_source_reason(db, df)}
    out_tmp = staging / f"{pid.lower()}-xb.zip"
    try:
        stats = await asyncio.to_thread(
            xb.filter_to_cross_border,
            src_slot,
            out_tmp,
            rail_only=bool(tt.get("rail_only", True)),
            home_country=tt.get("home_country"),
        )
    except Exception as exc:  # a malformed source feed shouldn't 500 the refresh
        # Log only the exception type — session/provider ids + exc are operator-
        # controlled (path/config/feed) so they don't belong raw in a log line
        # (S5145). The full hint goes to the API response.
        log.warning("cross-border filter failed for a derived provider: %s", type(exc).__name__)
        return {"status": "skipped", "key": key, "error": f"filter failed: {exc}"}
    outcome: dict[str, Any] = {
        "status": "fetched",
        "key": key,
        "routes_kept": stats.routes_kept,
        "trips_kept": stats.trips_kept,
        "summary": stats.summary_line(),
    }
    try:
        await _dispatch_off_loop(
            db,
            out_tmp,
            ingestion.TIMETABLE_FORMAT_DETAILS[ingestion.DERIVED_TIMETABLE_FORMAT]["kind"],
            target_sid,
            ingestion.provider_slot(provider)[1],
        )
    except _RebuildNotQueued as exc:
        outcome["reason"] = _rebuild_not_queued(db, key, exc)
    return outcome


def _missing_source_reason(db: DbSession, derived_from: dict[str, Any]) -> str:
    """Why a derived provider has no source feed: the source provider is
    declared in another format than GTFS (the filter reads GTFS only; its
    earlier file was quarantined at the switch, issue #381), or its GTFS
    file is not there yet."""
    src_session = str(derived_from.get("session_id"))
    src_provider = str(derived_from.get("provider_id"))
    row = db.get(SessionRow, src_session)
    try:
        providers = ingestion.normalize_providers((row.config if row else None) or {})
    except ValueError:
        providers = []
    fmt = next(
        (ingestion.provider_timetable_format(p) for p in providers if p["id"] == src_provider),
        ingestion.DERIVED_TIMETABLE_FORMAT,
    )
    if fmt != ingestion.DERIVED_TIMETABLE_FORMAT:
        return (
            f"source provider {src_session}/{src_provider} is not GTFS (format {fmt}); "
            "the cross-border filter reads GTFS only"
        )
    return f"source feed not found: {src_session}/{src_provider} (refresh that session first)"


async def _run_derived_filters(
    db: DbSession, sid: str, config: dict[str, Any], staging: Path
) -> list[dict[str, Any]]:
    """Materialise every `cross_border_filter` provider declared in this session."""
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return []
    outcomes: list[dict[str, Any]] = []
    for p in providers:
        outcome = await _materialise_derived_provider(db, sid, p, staging)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def _is_derived_from(provider: dict[str, Any], source_session_id: str) -> bool:
    """True if `provider` is a cross_border_filter feed linked to source_session_id."""
    tt = provider.get("timetable") or {}
    if tt.get("source") != "cross_border_filter":
        return False
    return (tt.get("derived_from") or {}).get("session_id") == source_session_id


def _cascade_keys(cascaded: list[dict[str, Any]]) -> list[str]:
    """Compact `target_session:key` labels for audit metadata."""
    return [f"{c['target_session']}:{c['key']}" for c in cascaded]


async def _cascade_derived_refresh(db: DbSession, source_session_id: str) -> list[dict[str, Any]]:
    """Re-derive every `cross_border_filter` provider (in any *other* session)
    linked to `source_session_id`.

    The single-source-of-truth cascade: when a national feed refreshes, the
    cross-border views derived from it rebuild automatically — no operator has
    to remember to refresh the second feed. See provider-source-modes-design.md §12.

    The sessions are read once, as plain values, before the first filter:
    an unexpected error in one target (`_unexpected_skip`) rolls the request's
    session back and expires every row it loaded. Such an error, in the read
    or in one target, skips that part only, never the refresh: its audit
    entry and commit come after the cascade.
    """
    try:
        targets = [
            (t.id, t.config or {})
            for t in db.execute(select(SessionRow)).scalars().all()
            if t.id != source_session_id
        ]
    except Exception as exc:  # the cascade is skipped, the refresh goes on
        _unexpected_skip(db, "cascade", exc)
        return []
    outcomes: list[dict[str, Any]] = []
    for target_id, config in targets:
        try:
            outcomes.extend(await _cascade_into(db, source_session_id, target_id, config))
        except Exception as exc:  # this target is skipped, the others go on
            outcomes.append(
                {
                    "status": "skipped",
                    "key": "cascade",
                    "target_session": target_id,
                    "error": _unexpected_skip(db, "cascade", exc),
                }
            )
    return outcomes


async def _cascade_into(
    db: DbSession, source_session_id: str, target_id: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """The cascade's outcomes for one target session (its configuration
    `config`): each of its providers derived from `source_session_id`."""
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return []
    linked = [p for p in providers if _is_derived_from(p, source_session_id)]
    if not linked:
        return []
    staging = settings.inbox_dir / target_id / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    outcomes: list[dict[str, Any]] = []
    for p in linked:
        outcome = await _materialise_derived_provider(db, target_id, p, staging)
        if outcome is not None:
            outcomes.append({**outcome, "target_session": target_id})
    return outcomes


def _derived_count(config: dict[str, Any]) -> int:
    """How many outcomes `_run_derived_filters` gives for this config."""
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return 0
    return sum(
        1 for p in providers if (p.get("timetable") or {}).get("source") == "cross_border_filter"
    )


async def _dispatch_off_loop(
    db: DbSession,
    path: Path,
    kind: str,
    sid: str,
    staged_filename: str | None,
    *,
    after: Callable[[], None] | None = None,
) -> bool:
    """`ingestion.dispatch` without blocking the event loop. One job in a
    worker thread copies `path` into the slot (a national file is GB;
    `ingestion.place`, atomic and locked per slot), then deletes `path`
    (always: the source is this call's to remove, never the caller's while
    the copy may still read it) and, after a placed file, runs `after` (the
    caller's file bookkeeping, e.g. the fetch state). The rebuild is queued
    here, on the thread that owns the request's database session. True when
    a rebuild was queued.

    Cancelled while the copy runs (the request went away), the job is
    shielded and finishes on its own; once it has placed the file, the
    rebuild is queued with a database session of its own, so the slot never
    holds a new file that no build will pick up. Then the cancellation goes
    on; nothing here awaits again."""

    def job() -> bool:
        try:
            rebuild = ingestion.place(path, kind, session_id=sid, staged_filename=staged_filename)
        finally:
            path.unlink(missing_ok=True)
        if after is not None:
            after()
        return rebuild

    copy = asyncio.ensure_future(asyncio.to_thread(job))
    try:
        rebuild = await asyncio.shield(copy)
    except asyncio.CancelledError:
        copy.add_done_callback(functools.partial(_queue_after_cancelled_copy, sid=sid, kind=kind))
        raise
    if rebuild:
        try:
            ingestion.queue_rebuild(db, session_id=sid, kind=kind)
        except Exception as exc:
            raise _RebuildNotQueued(type(exc).__name__) from exc
    return rebuild


class _RebuildNotQueued(Exception):
    """`_dispatch_off_loop` placed the file in its slot (and ran `after`),
    then could not queue its rebuild; the argument is the type name of the
    error. The file is stored: the next refresh finds it unchanged and
    queues nothing, so the caller says a rebuild must be started by hand."""


def _rebuild_not_queued(db: DbSession, label: str, exc: _RebuildNotQueued) -> str:
    """The note of a stored file whose rebuild was not queued, after one
    WARNING line (label and type) and a rollback of the request's session
    (the rebuild queue's statement failed in it)."""
    log.warning(
        "refresh.rebuild_not_queued key=%s type=%s", refresh_progress.task_label(label), exc
    )
    _rollback_quietly(db)
    return f"stored, but no rebuild was queued ({exc}); start a rebuild by hand"


def _queue_after_cancelled_copy(copy: asyncio.Future[bool], *, sid: str, kind: str) -> None:
    """Done callback of a copy whose request was cancelled: queue its rebuild
    off the event loop, with a database session of its own."""
    if copy.cancelled() or copy.exception() is not None or not copy.result():
        return
    asyncio.get_running_loop().run_in_executor(None, _queue_rebuild_own_session, sid, kind)


def _queue_rebuild_own_session(sid: str, kind: str) -> None:
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            ingestion.queue_rebuild(db, session_id=sid, kind=kind)
    except Exception as exc:  # a lost queue entry: the next refresh or build catches up
        log.warning("rebuild not queued after a cancelled copy: %s", type(exc).__name__)


def _refuse_if_running(sid: str, scope: str) -> None:
    """409, as `_claim_refresh` would answer, without claiming."""
    try:
        refresh_progress.check_free(sid, scope)
    except refresh_progress.RefreshRunning as exc:
        raise HTTPException(409, detail=exc.detail()) from exc


def _claim_refresh(sid: str, scope: str, total: int) -> refresh_progress.RefreshRun:
    """The session's progress record for this refresh (issue #359); 409 with
    the running refresh's start time while another one of `scope` runs."""
    try:
        return refresh_progress.claim(sid, scope, total)
    except refresh_progress.RefreshRunning as exc:
        raise HTTPException(409, detail=exc.detail()) from exc


async def _gather_refresh_outcomes(
    db: DbSession,
    sid: str,
    staging: Path,
    config: dict[str, Any],
    work: list[_RefreshTask],
    run: refresh_progress.RefreshRun | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the URL-download tasks then the derived (cross_border_filter)
    providers, partitioning every outcome into (fetched, unchanged, skipped).
    `run`, when given, records each task's start, bytes and outcome.

    Derived providers are materialised after the URL downloads so a same-session
    national+derived pair resolves in one refresh (see
    docs/provider-source-modes-design.md §12)."""
    buckets: dict[str, list[dict[str, Any]]] = {"fetched": [], "unchanged": [], "skipped": []}
    await _run_url_tasks(db, sid, staging, work, run, buckets)
    if run is not None and run.lost:
        return buckets["fetched"], buckets["unchanged"], buckets["skipped"]
    if run is not None and _derived_count(config):
        run.phase(refresh_progress.DERIVED, refresh_progress.DERIVED_LABEL)
    for outcome in await _run_derived_filters(db, sid, config, staging):
        if run is not None:
            run.done(str(outcome.get("key")), outcome)
        _bucket_outcome(buckets, outcome)
    return buckets["fetched"], buckets["unchanged"], buckets["skipped"]


async def _run_url_tasks(
    db: DbSession,
    sid: str,
    staging: Path,
    work: list[_RefreshTask],
    run: refresh_progress.RefreshRun | None,
    buckets: dict[str, list[dict[str, Any]]],
) -> None:
    """Download `work` one task after the other into `buckets`; `run`, when
    given, records each task's start, steps, bytes and outcome. Used by the
    whole session's refresh and by one provider's alone."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
        for task in work:
            if run is not None and run.lost:
                # Another refresh took the record over (this one stalled past
                # its lease): start no more downloads.
                return
            if run is None:
                outcome = await _refresh_one_task(client, db, sid, staging, task)
            else:
                with run.task(task.label):
                    outcome = await _refresh_one_task(client, db, sid, staging, task)
                run.done(task.label, outcome)
            _bucket_outcome(buckets, outcome)


def _bucket_outcome(buckets: dict[str, list[dict[str, Any]]], outcome: dict[str, Any]) -> None:
    """File a task outcome under its status (anything unknown counts as skipped)."""
    status = outcome.get("status")
    bucket = buckets[status] if status in ("fetched", "unchanged") else buckets["skipped"]
    bucket.append({k: v for k, v in outcome.items() if k != "status"})


# v0.1.19 — pure state-derivation helper. Lives alongside `_build_refresh_tasks`
# because both translate the same provider-config shape into something the UI
# cares about: that one tells you what *will* be fetched next, this one tells
# you what *has* been fetched already and how that's going.
#
# Pure: takes only data the caller has already gathered (file metadata + audit
# meta dict) and returns a ProviderStatus. No DB / FS access of its own —
# makes it trivial to unit-test without TestClient or Postgres.
def _derive_provider_status(
    *,
    feed_id: str,
    timetable_format: str,
    inbox_root: Path,
    latest_audit_meta: dict[str, Any] | None,
    now: datetime,
    freshness_hours: int = _PROVIDER_FRESHNESS_HOURS,
    checked_at: datetime | None = None,
) -> ProviderStatus:
    """Decide what to show on a provider card based on inbox + audit state.

    State machine (priority order):
      - file present, mtime within freshness window           → "ok"
      - file present, mtime older than freshness window       → "stale"
      - file missing, last refresh skipped this provider      → "error"
      - file missing, no audit history (or audit didn't touch
        this provider)                                        → "pending"

    The audit metadata we accept is whatever the existing
    `session.sources.refreshed` / `session.provider.refreshed` rows already
    record — a `fetched: [task_key]` and `skipped: [task_key]` list, where
    each task_key looks like `provider[SNCF].timetable(gtfs)` or
    `provider[SNCF].mct`. We match by the `provider[<feed_id>].` prefix so
    timetable / mct / stations_csv tasks all roll up to the same provider.

    `inbox_root` is the per-session inbox dir (`/data/inbox/<sid>`). The file
    we're looking for is at `<inbox_root>/<subdir>/<feed_id_lower>.zip` where
    `<subdir>` is `gtfs/` or `netex/` per the timetable format — same
    convention `dispatch()` uses to stage downloaded files.

    `checked_at` is when a refresh last confirmed the file is still current
    upstream (HTTP 304 / same sha256). An unchanged file keeps its old mtime,
    so freshness is measured from whichever is later. `unchanged` audit keys
    count as a successful attempt.
    """
    fmt_details = ingestion.TIMETABLE_FORMAT_DETAILS.get(timetable_format)
    if fmt_details is None:
        # Unknown format on a saved provider — degrade to pending rather than
        # crash the endpoint. The country-gate / save-time validation is the
        # right place to refuse the bad value; here we just don't pretend to
        # know where its file would live.
        return ProviderStatus(feed_id=feed_id, state="pending")

    file_path = (
        inbox_root
        / fmt_details["subdir"]
        / ingestion.staged_filename_for_format(feed_id, timetable_format)
    )

    fetched_at: datetime | None = None
    size_bytes: int | None = None
    if file_path.is_file():
        st = file_path.stat()
        fetched_at = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        size_bytes = st.st_size

    in_fetched = False
    in_skipped = False
    if latest_audit_meta:
        marker = f"provider[{feed_id}]."
        in_fetched = any(
            isinstance(k, str) and k.startswith(marker)
            for k in [
                *latest_audit_meta.get("fetched", []),
                *latest_audit_meta.get("unchanged", []),
            ]
        )
        in_skipped = any(
            isinstance(k, str) and k.startswith(marker)
            for k in latest_audit_meta.get("skipped", [])
        )

    if fetched_at is not None:
        last_confirmed = max(fetched_at, checked_at) if checked_at else fetched_at
        age_h = (now - last_confirmed).total_seconds() / 3600.0
        state = "ok" if age_h <= freshness_hours else "stale"
        # If the *latest* audit row has this provider only in skipped (no
        # successful task), the file is from an earlier successful run but
        # the most recent attempt failed. Surface that as a partial-error
        # hint without flipping the whole state to "error" — the operator
        # still has usable data, just stale-after-failed-refresh.
        error_hint = None
        if in_skipped and not in_fetched:
            error_hint = "last refresh failed — using previous file"
        return ProviderStatus(
            feed_id=feed_id,
            state=state,
            fetched_at=fetched_at,
            size_bytes=size_bytes,
            error_hint=error_hint,
        )

    # File is missing — was it ever attempted?
    if in_skipped and not in_fetched:
        return ProviderStatus(
            feed_id=feed_id,
            state="error",
            error_hint="last refresh attempt failed — click Refresh to see why",
        )
    return ProviderStatus(feed_id=feed_id, state="pending")


def _fetch_state_dir(sid: str) -> Path:
    """Per-task conditional-GET / sha256 state (app/feed_fetch.py). Kept out
    of gtfs/, netex/ and osm/ — every build globs those."""
    return settings.inbox_dir / sid / "_fetch_state"


def _parse_iso(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _fetch_checked_at(sid: str, label: str) -> datetime | None:
    """When a refresh last confirmed this task's file current, if ever."""
    return _parse_iso(feed_fetch.load_state(_fetch_state_dir(sid), label).get("checked_at"))


def _record_attempt(sid: str, label: str, outcome: dict[str, Any]) -> None:
    """Remember this task's latest outcome (with the full skip reason) for the
    feed-status panel. Best-effort, like every fetch-state write. Stored next
    to the conditional-GET validators; `fetch_validated` ignores the key."""
    state_dir = _fetch_state_dir(sid)
    state = feed_fetch.load_state(state_dir, label)
    state["last_attempt"] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": outcome.get("status"),
        "reason": outcome.get("reason"),
    }
    if outcome.get("resumes"):
        state["last_attempt"]["resumes"] = outcome["resumes"]
    feed_fetch.save_state(state_dir, label, state)


def _current_target_exists(sid: str, kind: str, staged_filename: str | None) -> bool:
    """Does the slot a task would dispatch into already hold *this task's*
    file? Without one, an "unchanged" verdict would leave the provider with
    nothing. LOAD_TO_DB kinds (SNCF-MCT / SNCF-Stations) share one slot per
    kind across every provider, so a file there proves nothing about this
    task — always download them."""
    if kind not in ingestion.STAGE_INTO_OTP_INBOX:
        return False
    name = staged_filename or ingestion.STAGE_INTO_OTP_INBOX_FILENAME[kind]
    return (settings.inbox_dir / sid / ingestion.STAGE_INTO_OTP_INBOX[kind] / name).is_file()


def _labels_writing_slot(kind: str, staged_filename: str | None) -> list[str]:
    """Every refresh-task label whose download lands in the slot `kind` +
    `staged_filename` names — whatever the provider's *current* source is,
    since a later switch back to url/nap would reuse the stale state."""
    if kind == "OSM-PBF":
        return ["osm_pbf"]
    subdir = ingestion.STAGE_INTO_OTP_INBOX.get(kind)
    if subdir is None:
        return []
    name = staged_filename or ingestion.STAGE_INTO_OTP_INBOX_FILENAME[kind]
    pid = Path(name).stem.upper()  # feed ids are upper-case; slot names are lower
    return [
        f"provider[{pid}].timetable({fmt})"
        for fmt, details in ingestion.TIMETABLE_FORMAT_DETAILS.items()
        if details["subdir"] == subdir
    ]


def _forget_fetch_state_for_slot(sid: str, kind: str, staged_filename: str | None) -> None:
    state_dir = _fetch_state_dir(sid)
    for label in _labels_writing_slot(kind, staged_filename):
        feed_fetch.state_path(state_dir, label).unlink(missing_ok=True)
    if staged_filename and kind in ingestion.STAGE_INTO_OTP_INBOX and kind != "OSM-PBF":
        # The station module status said what became of the file now replaced.
        station_extract_upload.forget_outcome(sid, Path(staged_filename).stem.upper())


async def _authorize(
    client: httpx.AsyncClient, db: DbSession, credential_id: str | None, url: str
) -> tuple[str, dict[str, str], Any]:
    """Apply a stored credential (v0.1.10) to `url`, logging in first for a
    login scheme. Returns (fetch_url, extra_headers, credential_row_or_None);
    raises ValueError with an operator-facing reason when the credential is
    gone, undecryptable or its login is refused."""
    if not credential_id:
        return url, {}, None
    # Triple-dot: this file is at app/api/admin/sessions.py; we need
    # `app.credentials` (the crypto module) and `app.models`. Single-dot
    # would resolve to `app.api.credentials` (the router).
    from ... import credentials as crypto_module
    from ...models import UserCredential

    cred = db.get(UserCredential, uuid.UUID(credential_id))
    if cred is None:
        raise ValueError(f"credential {credential_id} not found (was it deleted?)")
    try:
        fetch_url, extra_headers = await crypto_module.authorize(
            client, cred, url, settings.jwt_secret
        )
    except crypto_module.CredentialDecryptError as exc:
        raise ValueError(f"credential {cred.name!r} cannot be decrypted: {exc}") from exc
    except crypto_module.CredentialLoginError as exc:
        raise ValueError(f"credential {cred.name!r}: {exc}") from exc
    return fetch_url, extra_headers, cred


async def _refresh_one_task(
    client: httpx.AsyncClient,
    db: DbSession,
    sid: str,
    staging: Path,
    task: _RefreshTask,
) -> dict[str, Any]:
    """Run one resolve+download+format-check+dispatch task. Returns a dict for
    the response — `status` is `fetched` (new file dispatched, rebuild
    queued), `unchanged` (upstream file identical to the one in the slot —
    nothing rotated, no rebuild) or `skipped` (failed; the slot keeps its
    previous file). Per-task failures never abort the rest of the batch: an
    unexpected exception (a database pool timeout, a bug) skips this task
    alone (`_unexpected_skip`); a cancellation still ends the refresh.
    """
    try:
        outcome = await _resolve_and_download(client, db, sid, staging, task)
    except Exception as exc:  # one task's surprise must not end the batch
        outcome = _skipped(task, task.url, _unexpected_skip(db, task.label, exc))
    _record_attempt(sid, task.label, outcome)
    return outcome


def _unexpected_skip(db: DbSession, label: str, exc: Exception) -> str:
    """The skip reason of a task (or a derived provider) that raised an
    unexpected exception, after one ERROR line and a rollback of the
    request's session. The reason and the log name the exception type only:
    its text can quote a URL with its query string or a token. The rollback
    leaves the session usable by the next task and by the refresh's own end
    (row lock, audit, commit) whatever the failure left it in, such as a
    PostgreSQL transaction aborted by a failed statement; a pool timeout
    leaves nothing to roll back."""
    kind = type(exc).__name__
    log.error(
        "refresh.task_failed_unexpectedly key=%s type=%s",
        refresh_progress.task_label(label),
        kind,
    )
    _rollback_quietly(db)
    return f"unexpected error ({kind}); see the web log"


def _rollback_quietly(db: DbSession) -> None:
    """Roll the request's session back so its next statement can run; a
    failed rollback logs its type (the next statement then says more)."""
    try:
        db.rollback()
    except Exception as exc:  # best effort: the refresh goes on
        log.warning("refresh.rollback_failed type=%s", type(exc).__name__)


async def _resolve_and_download(
    client: httpx.AsyncClient,
    db: DbSession,
    sid: str,
    staging: Path,
    task: _RefreshTask,
) -> dict[str, Any]:
    if task.resolver is None:
        return await _download_task(client, db, sid, staging, task, task.url)

    # The file URL comes from a third-party catalogue — every redirect hop
    # of the lookup and the download is re-checked against the SSRF guard.
    async def _sign(url: str) -> tuple[str, dict[str, str]]:
        try:
            fetch_url, headers, _ = await _authorize(client, db, task.credential_id, url)
        except ValueError as exc:
            raise feed_resolvers.ResolveError(str(exc)) from exc
        return fetch_url, headers

    async with feed_resolvers.redirect_guard(client):
        try:
            url = await feed_resolvers.resolve(
                client, task.resolver, auth=_sign if task.credential_id else None
            )
        except feed_resolvers.ResolveError as exc:
            return _skipped(task, task.url, f"NAP resolver failed: {exc}")
        return await _download_task(client, db, sid, staging, task, url)


def _skipped(task: _RefreshTask, url: str, reason: str) -> dict[str, Any]:
    return {"status": "skipped", "key": task.label, "url": url, "reason": reason}


async def _download_task(
    client: httpx.AsyncClient,
    db: DbSession,
    sid: str,
    staging: Path,
    task: _RefreshTask,
    url: str,
) -> dict[str, Any]:
    """Download + format-check + dispatch `url` for `task` (its resolver, if
    any, has already run — `url` is the file to fetch)."""
    label, kind = task.label, task.kind

    def _skip(reason: str) -> dict[str, Any]:
        return _skipped(task, url, reason)

    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return _skip("not an http(s) URL")

    # Credential none → anonymous fetch; missing or undecryptable (e.g.
    # JWT_SECRET rotated) → this task fails with the reason, others proceed.
    try:
        fetch_url, extra_headers, cred = await _authorize(client, db, task.credential_id, url)
    except ValueError as exc:
        return _skip(str(exc))

    state_dir = _fetch_state_dir(sid)
    base_key = label.split("[", 1)[0]
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    try:
        result = await feed_fetch.fetch_validated(
            client,
            fetch_url=fetch_url,
            state_url=url,
            extra_headers=extra_headers,
            kind=kind,
            staging=staging,
            # uuid: two overlapping refreshes of one session must not share
            # staging files.
            base_name=f"{ts}-{base_key}-{uuid.uuid4().hex[:8]}",
            suffix=_url_suffix(url),
            previous=feed_fetch.load_state(state_dir, label),
            have_current=_current_target_exists(sid, kind, task.staged_filename),
        )
    except feed_fetch.FetchError as exc:
        return _skip(str(exc))

    # Stamp last_used_at on the credential so users can see "this hasn't
    # been used in months — maybe drop it." Best-effort.
    # Committed at once: a later task's unexpected error rolls the session
    # back (`_unexpected_skip`), which would discard a stamp only flushed.
    if cred is not None:
        try:
            cred.last_used_at = datetime.now(UTC)
            db.commit()
        except Exception as exc:
            log.warning("refresh.credential_stamp_failed type=%s", type(exc).__name__)
            # A failed commit leaves the session needing a rollback before
            # the next task, or the refresh's end, can use it.
            _rollback_quietly(db)

    if result.status == "unchanged":
        feed_fetch.save_state(state_dir, label, result.state)
        return {
            "status": "unchanged",
            "key": label,
            "kind": kind,
            "url": url,
            "reason": result.reason,
        }

    assert result.path is not None  # "fetched" always carries the format-checked file
    feed_fetch.report_step(feed_fetch.STEP_STORING)
    # The fetch state is saved by the copy's own job once the file is in
    # the slot, so a request cancelled meanwhile still leaves them paired.
    failed, note = await _store_fetched(
        db,
        task,
        sid,
        result.path,
        functools.partial(feed_fetch.save_state, state_dir, label, result.state),
    )
    if failed is not None:
        return _skip(failed)

    outcome: dict[str, Any] = {
        "status": "fetched",
        "key": label,
        "kind": kind,
        "url": url,
        "size_bytes": result.size_bytes,
    }
    if note is not None:
        outcome["reason"] = note
    if result.resumes:
        # A cut body was resumed on the way (app/feed_fetch.py); shown in the
        # feed status panel's "Last result".
        outcome["resumes"] = result.resumes
    assessment = (result.state.get("structure") or {}).get("assessment") or {}
    if assessment.get("level") in ("warn", "red"):
        outcome["structure"] = assessment
    return outcome


async def _store_fetched(
    db: DbSession, task: _RefreshTask, sid: str, path: Path, after: Callable[[], None]
) -> tuple[str | None, str | None]:
    """Copy a fetched, checked file into the task's slot and queue its
    rebuild (`_dispatch_off_loop`). Returns (skip reason, note): a skip
    reason when the file did not reach the slot (the job removed it), a note
    when it did but its rebuild could not be queued. Both name the error's
    type only (its text can hold a path or a query); the request's session
    is rolled back after either."""
    try:
        await _dispatch_off_loop(db, path, task.kind, sid, task.staged_filename, after=after)
    except _RebuildNotQueued as exc:
        return None, _rebuild_not_queued(db, task.label, exc)
    except Exception as exc:
        kind = type(exc).__name__
        log.warning(
            "refresh.dispatch_failed key=%s type=%s", refresh_progress.task_label(task.label), kind
        )
        _rollback_quietly(db)
        return f"dispatch failed ({kind}); see the web log", None
    return None, None


# ──── bulk import providers from a National Access Point (v0.1.8) ────


class ImportFromNapBody(BaseModel):
    """Filters for bulk-importing providers from a NAP catalogue.

    All filters are optional — omitting them imports every dataset whose
    URL isn't already in the session. Practical use is to filter by
    country + modes (e.g. country=FR, modes=["rail"]) for a focused
    demonstrator session.

    `preview=True` returns the proposed providers WITHOUT persisting,
    so the UI can show a confirmation table before the operator commits.

    v0.1.12: identifies the NAP via the catalogue's UUID instead of a free
    URL. The legacy `nap_url` field is still accepted for back-compat (CLI
    callers hitting the API directly) but the UI sends nap_catalogue_id.
    Exactly one of the two must be set.
    """

    nap_catalogue_id: str | None = Field(
        default=None,
        description="UUID of a row in nap_catalogues. Server resolves URL + "
        "credential at fetch time. Preferred over nap_url since v0.1.12.",
    )
    nap_url: str | None = Field(
        default=None,
        description="Legacy: a NAP endpoint URL, accepted only if it is the default "
        "FR NAP or the URL of a saved catalogue. Use nap_catalogue_id (managed at "
        "/admin/nap-catalogues) so credentials can be attached. Anonymous fetch only.",
    )
    country: str | None = Field(default=None, max_length=2, description="ISO-2 country filter")
    modes: list[str] | None = Field(
        default=None,
        description="Subset of {rail, urban, bus, bike}. None = no mode filter.",
    )
    include_publishers: list[str] | None = Field(
        default=None,
        description="Optional whitelist — substring match on publisher name.",
    )
    exclude_dataset_ids: list[str] | None = Field(
        default=None,
        description="Optional skip list of NAP dataset ids.",
    )
    include_dataset_ids: list[str] | None = Field(
        default=None,
        description="Optional positive list — when set, ONLY datasets whose id "
        "is in the list are kept. Used by the picker UI: preview returns the "
        "full filtered list with dataset_ids; on confirm the operator's "
        "checked subset is sent here so only those get persisted. (v0.1.12)",
    )
    preview: bool = Field(
        default=False,
        description="True = dry-run, return proposed providers without saving. "
        "False = persist them to session.config.sources.providers[].",
    )


class ImportFromNapResponse(BaseModel):
    providers: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    warnings: list[str]
    preview: bool


def _legacy_nap_url(db: DbSession, requested: str) -> str:
    """The stored URL matching the legacy `nap_url` field; 400 otherwise."""
    from ...master import nap_importer
    from ...models import NapCatalogue

    if requested == nap_importer.DEFAULT_FR_NAP_URL:
        return nap_importer.DEFAULT_FR_NAP_URL
    stored = db.execute(
        select(NapCatalogue.url).where(NapCatalogue.url == requested).limit(1)
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            400,
            "nap_url must be the default FR NAP or the URL of a saved NAP catalogue "
            "(/admin/nap-catalogues); prefer nap_catalogue_id.",
        )
    return str(stored)


@router.post(
    "/{sid}/providers/import-from-nap",
    response_model=ImportFromNapResponse,
    summary="Bulk-import providers from a NAP catalogue (preview or commit)",
    responses={
        400: {
            "description": "No NAP named, a malformed id, or a nap_url that is not allow-listed."
        },
        404: {"description": "Session or NAP catalogue not found."},
    },
)
async def import_providers_from_nap(
    sid: str,
    body: ImportFromNapBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> ImportFromNapResponse:
    """Fetch a NAP catalogue, filter, and add new providers in one call.

    Workflow:
      1. Operator opens the Configure section, clicks "Import from NAP"
      2. UI calls this endpoint with `preview=True` and the chosen filters
      3. Endpoint returns a table of (proposed providers + skipped reasons
         + warnings) — UI shows it as a confirmation modal
      4. Operator clicks Confirm → UI calls again with `preview=False`
      5. Endpoint persists the providers AND returns the same shape

    Persisting also bumps `_meta.sources_changed_at` (v0.1.7.1 staleness
    tracking) so the operator gets the "click Refresh sources before
    Rebuild" reminder.

    Country-gate (v0.1.6) runs on each new provider, as on the manual save:
    a country with no master_stations rows that the station module does
    not serve is accepted with a warning (in `warnings`) when VIATOR uses
    the module; without it, the import fails with the same 409 the manual
    save uses (import master_stations first, then re-run the import).
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Bulk-import requires a JWT-authenticated actor")

    from ... import credentials as crypto_module
    from ...master import nap_importer
    from ...models import NapCatalogue, UserCredential

    # v0.1.12: resolve the catalogue → (url, credential). Legacy `nap_url`
    # path stays as an anonymous-only escape hatch for CLI callers; UI
    # always sends nap_catalogue_id.
    nap_url: str
    nap_auth: tuple[str, str, str | None] | None = None  # (auth_type, plaintext, param_name)
    catalogue_name: str | None = None

    if body.nap_catalogue_id:
        try:
            cat_uuid = uuid.UUID(body.nap_catalogue_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                400, f"nap_catalogue_id={body.nap_catalogue_id!r} is not a UUID"
            ) from exc
        cat = db.get(NapCatalogue, cat_uuid)
        if cat is None:
            raise HTTPException(404, f"NAP catalogue {body.nap_catalogue_id!r} not found")
        nap_url = cat.url
        catalogue_name = cat.name
        if cat.credential_id is not None:
            cred = db.get(UserCredential, cat.credential_id)
            if cred is None:
                # SET NULL cascade fired but the catalogue row hasn't been
                # re-saved yet — fall back to anonymous + warn in audit.
                log.warning(
                    "catalogue %s references missing credential %s; "
                    "falling back to anonymous NAP fetch",
                    cat.name,
                    cat.credential_id,
                )
            elif cred.auth_type in crypto_module.AUTH_TYPES_NEEDING_LOGIN:
                raise HTTPException(
                    400,
                    f"NAP credential {cred.name!r} is a login ({cred.auth_type}); "
                    "catalogue imports support static keys and tokens only.",
                )
            else:
                try:
                    plaintext = crypto_module.decrypt(
                        cred.ciphertext, cred.nonce, settings.jwt_secret
                    )
                except crypto_module.CredentialDecryptError as exc:
                    raise HTTPException(
                        500,
                        f"NAP credential {cred.name!r} cannot be decrypted: {exc}. "
                        "Recreate the credential at /credentials.",
                    ) from exc
                nap_auth = (cred.auth_type, plaintext, cred.param_name)
    elif body.nap_url:
        # Legacy field, allow-listed (CodeQL py/full-ssrf): it may only name
        # the default FR NAP or a saved catalogue, and the URL fetched is the
        # stored one, never the request's string. Catalogues are created by
        # platform admins at /admin/nap-catalogues.
        nap_url = _legacy_nap_url(db, body.nap_url)
    else:
        raise HTTPException(
            400,
            "Either nap_catalogue_id (preferred) or nap_url (legacy) must be set.",
        )

    existing_providers = (s.config or {}).get("sources", {}).get("providers") or []

    try:
        result = await nap_importer.import_from_nap(
            existing_providers=existing_providers,
            nap_url=nap_url,
            nap_auth=nap_auth,
            country=body.country.upper() if body.country else None,
            modes=body.modes,
            include_publishers=body.include_publishers,
            exclude_dataset_ids=body.exclude_dataset_ids,
            include_dataset_ids=body.include_dataset_ids,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"NAP fetch failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.preview:
        # Dry-run path: return proposal without touching session config.
        return ImportFromNapResponse(
            providers=result["providers"],
            skipped=result["skipped"],
            warnings=result["warnings"],
            preview=True,
        )

    # Commit path: merge new providers into session config + run all the
    # same validations that the regular PATCH endpoint runs (provider
    # validation, country-gate). Reuses normalize_providers so the shape
    # ends up canonical.
    if result["providers"]:
        # v0.1.12: strip the `_nap_dataset_id` bookkeeping field the importer
        # attaches for the picker UI — it shouldn't leak into session.config.
        # Easier to filter here than to thread "is this preview?" into the
        # importer.
        cleaned = [
            {k: v for k, v in p.items() if not k.startswith("_")} for p in result["providers"]
        ]
        new_config = dict(s.config or {})
        sources = dict(new_config.get("sources") or {})
        merged_providers = list(existing_providers) + cleaned
        sources["providers"] = merged_providers
        new_config["sources"] = sources

        # Validate via the same path the manual save uses.
        try:
            providers_canon = ingestion.normalize_providers(new_config)
        except ValueError as exc:
            raise HTTPException(400, f"Imported providers failed validation: {exc}") from exc

        # Country-gate: only the countries the import adds (see the manual
        # save). A coroutine, so the module is awaited, never blocked on.
        added_countries = {
            p["country_iso"] for p in providers_canon if p["country_iso"]
        } - _stored_countries(s.config)
        missing = _countries_without_stations(db, added_countries) if added_countries else set()
        if missing:
            missing = _unserved(missing, await station_module.countries())
        new_country = _new_countries(missing, _IMPORT_ADVICE)
        if new_country:
            result["warnings"] = [*result["warnings"], new_country]

        # Persist canonicalised providers + bump staleness.
        sources["providers"] = providers_canon
        new_config["sources"] = sources
        staleness.mark_sources_changed(new_config)
        s.config = new_config
        flag_modified(s, "config")

        audit.record(
            db,
            action="session.providers.bulk_imported",
            actor_user_id=actor.id,
            actor_ip=client_ip(request),
            target_kind="session",
            target_id=sid,
            metadata={
                "nap_url": nap_url,
                "nap_catalogue_id": body.nap_catalogue_id,
                "nap_catalogue_name": catalogue_name,
                "nap_authenticated": nap_auth is not None,
                "filters": {
                    "country": body.country,
                    "modes": body.modes,
                    "include_publishers": body.include_publishers,
                    "exclude_dataset_ids": body.exclude_dataset_ids,
                    # v0.1.12 picker: recorded so the audit row shows which
                    # specific datasets the operator hand-picked (vs the
                    # broader filters that were also applied).
                    "include_dataset_ids": body.include_dataset_ids,
                },
                "added_count": len(result["providers"]),
                "added_ids": [p["id"] for p in result["providers"]],
                "skipped_count": len(result["skipped"]),
            },
        )
        db.commit()

    return ImportFromNapResponse(
        providers=result["providers"],
        skipped=result["skipped"],
        warnings=result["warnings"],
        preview=False,
    )


# ──── per-provider refresh endpoint (v0.1.6) ────


_AUDIT_ACTION_PROVIDER_REFRESHED = "session.provider.refreshed"


def _finalise_provider_refresh(
    db: DbSession,
    *,
    request: Request,
    actor: CurrentUser,
    session: SessionRow,
    sid: str,
    pid: str,
    fetched: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    unchanged: list[dict[str, Any]] | None = None,
) -> RefreshSourcesResponse:
    """Shared tail for both per-provider refresh paths: advance state, clear the
    staleness flag (lossy-by-design — see refresh_sources), audit, commit."""
    unchanged = unchanged or []
    _relock_session(db, session)
    if fetched and session.state in (SessionState.CREATED.value, SessionState.CONFIGURED.value):
        session.state = SessionState.POPULATED.value
    if fetched or unchanged:
        if session.config is None:
            session.config = {}
        staleness.mark_refresh_completed(session.config)
        flag_modified(session, "config")

    audit.record(
        db,
        action=_AUDIT_ACTION_PROVIDER_REFRESHED,
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={
            "provider_id": pid,
            "fetched": [f["key"] for f in fetched],
            "unchanged": [u["key"] for u in unchanged],
            "skipped": [s_["key"] for s_ in skipped],
        },
    )
    db.commit()
    return RefreshSourcesResponse(fetched=fetched, unchanged=unchanged, skipped=skipped)


async def _refresh_derived_provider(
    db: DbSession,
    *,
    request: Request,
    actor: CurrentUser,
    session: SessionRow,
    sid: str,
    provider: dict[str, Any],
    staging: Path,
    run: refresh_progress.RefreshRun | None = None,
) -> RefreshSourcesResponse:
    """Per-provider 'Refresh' for a cross_border_filter provider: re-run the
    filter on the linked national feed into this provider's slot (it owns no URL
    to download). `run`: its progress record (one task, the filter)."""
    fetched: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    key = f"provider[{provider['id']}].cross_border_filter"
    if run is not None:
        run.phase(refresh_progress.DERIVED, key)
    outcome = await _materialise_derived_provider(db, sid, provider, staging)
    if outcome is not None:
        if run is not None:
            run.done(key, outcome)
        bucket = fetched if outcome.get("status") == "fetched" else skipped
        bucket.append({k: v for k, v in outcome.items() if k != "status"})
    return _finalise_provider_refresh(
        db,
        request=request,
        actor=actor,
        session=session,
        sid=sid,
        pid=provider["id"],
        fetched=fetched,
        skipped=skipped,
    )


async def _refresh_provider_urls(
    db: DbSession,
    *,
    request: Request,
    actor: CurrentUser,
    session: SessionRow,
    sid: str,
    pid: str,
    staging: Path,
    work: list[_RefreshTask],
    run: refresh_progress.RefreshRun | None = None,
) -> RefreshSourcesResponse:
    """Per-provider 'Refresh' for a url- or nap-source provider: download its
    timetable + optional MCT + stations CSV (`work`) into the slot, each task
    recorded in `run`, its progress record."""
    buckets: dict[str, list[dict[str, Any]]] = {"fetched": [], "unchanged": [], "skipped": []}
    await _run_url_tasks(db, sid, staging, work, run, buckets)
    return _finalise_provider_refresh(
        db,
        request=request,
        actor=actor,
        session=session,
        sid=sid,
        pid=pid,
        fetched=buckets["fetched"],
        skipped=buckets["skipped"],
        unchanged=buckets["unchanged"],
    )


_PROVIDER_NOT_SAVED = (
    "This provider is not in the saved configuration of the session: save the configuration first."
)


@router.post(
    "/{sid}/providers/{pid}/refresh",
    responses={
        400: {"description": "No JWT actor, an invalid config, or no URL to refresh."},
        404: {
            "description": "Session not found, or the provider is not in the saved "
            "configuration (save the configuration first)."
        },
        409: {
            "description": "A refresh of this provider alone, or the whole session's "
            "provider refresh, is running (`refresh_already_running`, with its scope "
            "and start time; issue #359)."
        },
    },
)
async def refresh_provider(
    sid: str,
    pid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
    background: BackgroundTasks,
) -> RefreshSourcesResponse:
    """Refresh one provider. URL-source providers download their files; a
    cross_border_filter (derived) provider re-runs the filter on its linked
    national feed. Doesn't touch the session-level OSM PBF — different, heavier
    concern.

    Use case: operator just added IDFM to a session that already has SNCF
    serving live. They click "Refresh" on IDFM's card and only IDFM is touched.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if actor.id is None:
        raise HTTPException(400, "Refresh requires a JWT-authenticated actor")

    # Confirm the provider exists in this session's config — otherwise we'd
    # silently no-op, which is a worse UX than 404.
    try:
        providers = ingestion.normalize_providers(s.config or {})
    except ValueError as exc:
        raise HTTPException(400, f"Session config is invalid: {exc}") from exc
    provider = next((p for p in providers if p["id"] == pid), None)
    if provider is None:
        # Short, and never the list of every provider id (a NAP-imported
        # session has hundreds): the page refreshes only saved cards.
        raise HTTPException(404, _PROVIDER_NOT_SAVED)

    # Derived providers own no URL — "Refresh this provider" re-runs the filter
    # instead of downloading (without this the URL path 400s "no URLs to refresh").
    if (provider.get("timetable") or {}).get("source") == "cross_border_filter":
        return await _refresh_one_derived(db, request, actor, s, sid, provider)

    scope = refresh_progress.provider_scope(pid)
    work = _build_refresh_tasks(s.config or {}, only_provider=pid)
    if not work:
        _refuse_if_running(sid, scope)  # a running refresh says so first, as before
        raise HTTPException(
            400,
            f"Provider {pid!r} has no URLs to refresh "
            "(no timetable, MCT, or stations CSV configured).",
        )
    # Issue #359: this provider's own progress record; 409 while it, or the
    # whole session's provider refresh (it downloads this provider too), runs.
    run = _claim_refresh(sid, scope, len(work))
    async with run:
        result = await _refresh_provider_urls(
            db,
            request=request,
            actor=actor,
            session=s,
            sid=sid,
            pid=pid,
            staging=_staging_dir(sid),
            work=work,
            run=run,
        )
    _queue_extracts(background, sid, s.config, bool(result.fetched or result.unchanged), pid)
    # MSMM 22.3: this provider's extract, if the module lacks it (sender
    # thread), followed in this refresh's record.
    station_extract_upload.after_refresh(sid, pid, run_id=run.run_id)
    return result


def _staging_dir(sid: str) -> Path:
    staging = settings.inbox_dir / sid / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


async def _refresh_one_derived(
    db: DbSession,
    request: Request,
    actor: CurrentUser,
    session: SessionRow,
    sid: str,
    provider: dict[str, Any],
) -> RefreshSourcesResponse:
    """A derived provider's card refresh, in its own progress record (one
    task, the filter). No station-module pass follows it, so the record has
    no follow-up line."""
    run = _claim_refresh(sid, refresh_progress.provider_scope(provider["id"]), 1)
    run.follow_up = False
    async with run:
        return await _refresh_derived_provider(
            db,
            request=request,
            actor=actor,
            session=session,
            sid=sid,
            provider=provider,
            staging=_staging_dir(sid),
            run=run,
        )


# ───────────────────────── per-provider status (v0.1.19) ─────────────────────


@router.get("/{sid}/providers/status", response_model=dict[str, ProviderStatus])
def get_providers_status(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, ProviderStatus]:
    """Per-provider fetch status — what's in the inbox, when, and whether the
    last refresh attempt succeeded for each provider configured on this
    session. Used by the admin UI to render status pills on each provider
    card so operators can see at a glance which feeds need a refresh.

    Source-of-truth strategy (v0.1.19, no new tables):
      * **Inbox file** at `<inbox>/<subdir>/<feed_id_lower>.zip` — its
        existence + mtime is the canonical "is this fetched, and when".
      * **Latest refresh audit row** (`session.sources.refreshed` or
        `session.provider.refreshed`) — disambiguates "never attempted"
        from "attempted and failed". Audit metadata only stores task
        keys, not full error reasons; the UI hint says "click Refresh to
        see why" and the per-provider refresh endpoint returns the full
        skip reason in its response on demand.

    A future v0.1.20+ may move this to a dedicated `provider_fetch_status`
    table written by ingestion, with sparkline-grade history. The
    filesystem-derived view here is a deliberate "ship the obvious thing
    first" — operators told us they need *some* visibility right now.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    try:
        providers = ingestion.normalize_providers(s.config or {})
    except ValueError:
        # Same defensive degrade as `_build_refresh_tasks` — if config is
        # somehow malformed, return an empty status map rather than 500.
        # The Configure form will surface the validation error on save.
        return {}

    # Pull the most recent refresh audit row (either scope) for this session.
    # We don't need any older history — only the *latest* attempt informs
    # the "did the last refresh fail for this provider" question.
    latest_audit = db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.target_kind == "session",
            AuditEvent.target_id == sid,
            AuditEvent.action.in_(["session.sources.refreshed", _AUDIT_ACTION_PROVIDER_REFRESHED]),
        )
        .order_by(desc(AuditEvent.ts))
        .limit(1)
    ).scalar_one_or_none()
    latest_meta = latest_audit.metadata_ if latest_audit is not None else None

    inbox_root = settings.inbox_dir / sid
    now = datetime.now(UTC)

    # v0.1.37 — latest uploaded filename per provider, so an upload-source
    # card can show *which* file is attached. One query, newest-first;
    # setdefault keeps the first (most recent) per feed id.
    latest_upload_filename: dict[str, str] = {}
    for feed_id_val, filename in db.execute(
        select(Upload.provider_feed_id, Upload.filename)
        .where(Upload.session_id == sid, Upload.provider_feed_id.is_not(None))
        .order_by(desc(Upload.created_at))
    ).all():
        if feed_id_val is not None:
            latest_upload_filename.setdefault(feed_id_val, filename)

    out: dict[str, ProviderStatus] = {}
    for p in providers:
        tt = p.get("timetable") or {}
        fmt = tt.get("format", "gtfs")
        fetch_state = feed_fetch.load_state(
            _fetch_state_dir(sid), f"provider[{p['id']}].timetable({fmt})"
        )
        status = _derive_provider_status(
            feed_id=p["id"],
            # The file's format: GTFS for a derived provider (issue #381).
            timetable_format=ingestion.provider_timetable_format(p),
            inbox_root=inbox_root,
            latest_audit_meta=latest_meta,
            now=now,
            checked_at=_parse_iso(fetch_state.get("checked_at")),
        )
        _decorate_status(status, p, fetch_state, latest_upload_filename.get(p["id"]))
        status.station_extract = station_extract_upload.load_outcome(sid, p["id"])
        out[p["id"]] = status
    return out


def _decorate_status(
    status: ProviderStatus,
    provider: dict[str, Any],
    fetch_state: dict[str, Any],
    upload_filename: str | None,
) -> None:
    """Add the feed-status panel fields to a derived `ProviderStatus`."""
    tt = provider.get("timetable") or {}
    status.source = tt.get("source")
    if status.source == "upload":
        status.upload_filename = upload_filename
    status.label = provider.get("label")
    status.country_iso = provider.get("country_iso")
    status.format = tt.get("format", "gtfs")
    resolver = tt.get("resolver") if status.source == "nap" else None
    status.resolver_type = resolver.get("type") if isinstance(resolver, dict) else None
    status.checked_at = _parse_iso(fetch_state.get("checked_at"))
    attempt = fetch_state.get("last_attempt")
    status.last_attempt = attempt if isinstance(attempt, dict) else None
    status.format_ok = _format_ok(status, fetch_state)
    structure = fetch_state.get("structure")
    if isinstance(structure, dict) and isinstance(structure.get("assessment"), dict):
        status.structure = structure["assessment"]


def _format_ok(status: ProviderStatus, fetch_state: dict[str, Any]) -> bool | None:
    """Did the file now in the slot pass the format check on its way in?

    Uploads always run `detect.detect`; url/nap downloads do since the
    fetch pipeline (their state then carries the file's sha256). A manual
    upload deletes that state, so a stale sha256 never vouches for an
    uploaded file. Anything else (derived feeds, files scp'd in, files from
    before the check existed) is unknown, not OK."""
    if status.fetched_at is None:
        return None
    if status.source == "upload":
        return True if status.upload_filename else None
    if status.source in ("url", "nap", None) and fetch_state.get("sha256"):
        return True
    return None


# ─────────────── automated NAP sources, per country (app/nap_source_map.py) ───────────────


class NapSourcesPlan(BaseModel):
    countries: list[nap_source_map.CountryPlan]
    # Map entries whose provider id this session doesn't have (a map built
    # for eu19 also serves eu11, which carries a subset).
    not_in_session: list[str]


class NapSourcesApplyBody(BaseModel):
    provider_ids: list[str] = Field(min_length=1, max_length=200)


class NapSourcesApplyResponse(BaseModel):
    changed: list[str]
    skipped: list[dict[str, str]]
    # The saved config, so the page can refresh its Configure form — a later
    # "Save config" from a stale form would silently undo the switch.
    config: dict[str, Any]


@router.get(
    "/{sid}/nap-sources",
    responses={
        400: {"description": "Session config is malformed"},
        404: {"description": "Session not found"},
    },
)
def get_nap_sources(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> NapSourcesPlan:
    """Which of this session's providers can move to an automated NAP
    source, grouped by country. Read-only."""
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    try:
        providers = ingestion.normalize_providers(s.config or {})
    except ValueError as exc:
        raise HTTPException(400, f"session config is malformed: {exc}") from exc
    source_map = nap_source_map.load_map()
    in_session = {p["id"] for p in providers}
    return NapSourcesPlan(
        countries=nap_source_map.plan(providers, source_map),
        not_in_session=sorted(set(source_map) - in_session),
    )


@router.post(
    "/{sid}/nap-sources/apply",
    responses={
        400: {"description": "Legacy config shape, or the switched config fails validation"},
        404: {"description": "Session not found"},
    },
)
def apply_nap_sources(
    sid: str,
    body: NapSourcesApplyBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> NapSourcesApplyResponse:
    """Switch the given providers' timetables to their mapped automated
    source. Config only — nothing is downloaded; refresh afterwards. Same
    validation, staleness bump and audit trail as a config save
    (`patch_session`); countries don't change, so no country gate."""
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    config = s.config or {}
    if not isinstance((config.get("sources") or {}).get("providers"), list):
        raise HTTPException(
            400,
            "this session still uses a legacy sources shape — save its Configure form once first",
        )
    new_config, changed, skipped = nap_source_map.apply(
        config, set(body.provider_ids), nap_source_map.load_map()
    )
    if changed:
        _save_nap_switch(db, request, actor, s, new_config, changed, skipped)
    return NapSourcesApplyResponse(changed=changed, skipped=skipped, config=s.config or {})


def _save_nap_switch(
    db: DbSession,
    request: Request,
    actor: CurrentUser,
    s: SessionRow,
    new_config: dict[str, Any],
    changed: list[str],
    skipped: list[dict[str, str]],
) -> None:
    try:
        new_config["sources"]["providers"] = ingestion.normalize_providers(new_config)
    except ValueError as exc:
        raise HTTPException(400, f"switched config fails validation: {exc}") from exc
    if not staleness.sources_subtree_equal(s.config, new_config):
        staleness.mark_sources_changed(new_config)
    previous = {
        p.get("id"): p.get("timetable")
        for p in ((s.config or {}).get("sources") or {}).get("providers") or []
        if isinstance(p, dict) and p.get("id") in changed
    }
    s.config = new_config
    audit.record(
        db,
        action="session.nap_sources.applied",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=s.id,
        # `previous` keeps each replaced timetable, so a switch can be undone by hand.
        metadata={"changed": changed, "skipped": skipped, "previous": previous},
    )
    db.commit()


# ───────────────────────── rebuilds ─────────────────────────


class SnapshotInfo(BaseModel):
    """v0.1.20 — graph_snapshot data joined onto each rebuild job so the admin
    UI can show "what was built, when, what's in it, is it the one currently
    serving" without grovelling through logs.

    Populated by the worker on successful build (see `app/worker.py` —
    `record_snapshot` is called in the success path; older successful
    builds done before v0.1.20 won't have a snapshot row and will surface
    as `snapshot=None` in the response).
    """

    built_at: str
    feed_signature: str  # 64-char sha256 — first 8 chars are shown in the UI
    is_current: bool  # at most one per session has this True
    timetable_main_version: str  # e.g. "2026-W14_2026-W39"
    timetable_update_version: int  # 1, 2, 3... within the same main_version
    service_period_start: str  # ISO date
    service_period_end: str  # ISO date
    source_uploads: list[dict[str, Any]]  # [{filename, sha256, kind, upload_id}]
    main_version_source: str  # "auto" | "manual_override"


class RebuildJobResponse(BaseModel):
    id: str
    session_id: str | None
    status: str
    log: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    graph_path: str | None
    # v0.1.20 — derived / joined fields that make the rebuild table useful.
    duration_seconds: int | None = None  # finished_at - started_at, when both exist
    snapshot: SnapshotInfo | None = None  # joined from graph_snapshots by rebuild_job_id
    cache_hit: bool | None = None  # parsed from log; None when not detectable
    max_memory: bool = False  # v0.1.38 — job ran (or will run) in max-memory mode
    # A running job whose cancel the worker has not acted on yet.
    cancel_requested: bool = False
    # The station module's build a MOTIS graph build waits for (MSMM decision
    # 6): the last step the worker recorded in the log (`waiting`, `done`,
    # `up_to_date`, `unavailable`, `cancelled`) and its fixed sentence; None
    # when the job did not wait (OTP, or the module not configured).
    station_build: str | None = None
    station_build_text: str | None = None


class OsmCountryRow(BaseModel):
    iso: str
    name: str
    stops: int  # stops detected in this country (UIC prefix or coordinate)
    declared: bool  # a provider declared country_iso = this
    suggested: bool  # should be pre-ticked (declared OR ≥ threshold stops)
    selected: bool  # currently saved in session.config.osm_countries


class OsmCountriesResponse(BaseModel):
    threshold: int  # min stops to auto-suggest (the UI shows the rule)
    selected: list[str]  # currently-saved osm_countries
    countries: list[OsmCountryRow]  # full v1 list, ordered for the checklist


@router.get("/{sid}/osm-countries", responses={404: {"description": "Session not found"}})
def suggest_osm_countries(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> OsmCountriesResponse:
    """Geographic-scope suggestion for the session's Configure form.

    Combines two signals: countries the providers **declare** (`country_iso`)
    and countries **detected** from the staged GTFS stops (UIC prefix +
    coordinate). Pre-ticks declared countries plus any with >= threshold
    stops. The full v1 list is
    returned with per-country stop counts so the operator sees *why* each box
    is (un)ticked and can override — e.g. a 2-stop UK is a visible one-click
    add. See docs/osm-geographic-scope-design.md.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    from ... import osm_geo

    cfg = s.config or {}
    try:
        providers = ingestion.normalize_providers(cfg)
    except ValueError:
        providers = []
    declared = {
        p["country_iso"] for p in providers if p.get("country_iso")
    } & osm_geo.VALID_COUNTRIES

    stops: list[tuple[str | None, float | None, float | None]] = []
    gtfs_dir = ingestion.session_inbox(sid) / "gtfs"
    if gtfs_dir.exists():
        for zip_path in sorted(gtfs_dir.glob(_ZIP_GLOB)):
            stops.extend(_read_gtfs_stops(zip_path))
    counts = osm_geo.detect_from_stops(stops)

    suggested = declared | set(osm_geo.suggested_countries(counts))
    selected = set(osm_geo.validate_countries(cfg.get("osm_countries")))
    rows = [
        OsmCountryRow(
            iso=iso,
            name=name,
            stops=counts.get(iso, 0),
            declared=iso in declared,
            suggested=iso in suggested,
            selected=iso in selected,
        )
        for iso, name in osm_geo.COUNTRY_NAMES.items()
    ]
    return OsmCountriesResponse(
        threshold=osm_geo.SUGGEST_MIN_STOPS,
        selected=sorted(selected),
        countries=rows,
    )


@router.post(
    "/{sid}/rebuilds",
    status_code=201,
    responses={
        400: {"description": "No transit feed or no OSM PBF staged yet."},
        404: {"description": "Session not found."},
    },
)
def enqueue_rebuild(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
    max_memory: bool = False,
) -> RebuildJobResponse:
    """Manually enqueue a rebuild for this session. Idempotent — coalesces
    with any existing pending job for the same session.

    `max_memory=true` (UI checkbox) marks the job so the worker stops the
    serving sessions + observability stack, sizes the build heap to host RAM,
    runs the build, then restarts everything — the "worst-case build on a
    single box" path. When coalescing into an existing pending job, the flag
    only ever upgrades (a max-memory request wins over a plain one).

    Refuses (400) when inputs aren't staged on disk. The OTP build itself
    fails ~30 seconds in with "no OSM data available" if you skip the
    Refresh-sources step; this guard saves operators that round-trip and
    points them at the action they actually need.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")

    # Guard: confirm the inputs OTP needs are actually on disk. The session
    # state advances to `populated` when a refresh succeeds OR an upload
    # lands, so checking state alone isn't enough — operators can be at
    # `populated` from a GTFS upload while OSM is still missing. Inspect
    # the filesystem directly.
    sess_inbox = ingestion.session_inbox(sid)
    gtfs_zips = (
        sorted((sess_inbox / "gtfs").glob(_ZIP_GLOB)) if (sess_inbox / "gtfs").exists() else []
    )
    netex_zips = (
        sorted((sess_inbox / "netex").glob(_ZIP_GLOB)) if (sess_inbox / "netex").exists() else []
    )
    osm_pbfs = sorted((sess_inbox / "osm").glob("*.pbf")) if (sess_inbox / "osm").exists() else []
    if not (gtfs_zips or netex_zips):
        raise HTTPException(
            400,
            f"No transit feed staged for session {sid!r}. "
            "Click 'Refresh all sources' (or use the Upload form) before Rebuild graph.",
        )
    if not osm_pbfs:
        raise HTTPException(
            400,
            f"No OSM PBF staged for session {sid!r}. "
            "Click 'Refresh all sources' (or upload one manually) before Rebuild graph.",
        )

    # Reuse the same coalescing logic ingestion uses.
    pending = (
        db.query(RebuildJob)
        .filter(RebuildJob.status == "pending")
        .filter(RebuildJob.session_id == sid)
        .first()
    )
    suffix = " (max-memory)" if max_memory else ""
    if pending is None:
        pending = RebuildJob(
            session_id=sid,
            status="pending",
            max_memory=max_memory,
            log=f"queued at {datetime.now(UTC).isoformat()} — manual trigger{suffix}\n",
        )
        db.add(pending)
        db.flush()
    elif max_memory and not pending.max_memory:
        # Upgrade an already-pending plain rebuild to max-memory.
        pending.max_memory = True
        pending.log = (pending.log or "") + (
            f"upgraded to max-memory at {datetime.now(UTC).isoformat()}\n"
        )

    audit.record(
        db,
        action="session.rebuild.enqueued",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={"job_id": str(pending.id), "max_memory": max_memory},
    )
    db.commit()
    db.refresh(pending)
    return _job_to_response(pending)


@router.get("/{sid}/rebuilds", response_model=list[RebuildJobResponse])
def list_rebuilds(
    sid: str,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    limit: int = 20,
) -> list[RebuildJobResponse]:
    """Recent rebuild jobs for this session, newest first.

    v0.1.20: each row includes joined `graph_snapshots` data when available
    (`snapshot` field), plus `duration_seconds` and a `cache_hit` flag
    derived from the log. The UI uses these to render the new "Current
    build / History" card layout.
    """
    rows = (
        db.query(RebuildJob)
        .filter(RebuildJob.session_id == sid)
        .order_by(RebuildJob.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_job_to_response(j, db=db) for j in rows]


@router.post(
    "/{sid}/rebuilds/{job_id}/cancel",
    responses={
        404: {"description": "No such rebuild job in this session"},
        409: {"description": "The rebuild job has already finished"},
    },
)
def cancel_rebuild(
    sid: str,
    job_id: uuid.UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> RebuildJobResponse:
    """Cancel a rebuild. A pending job is cancelled at once. A running one is
    flagged: the worker owns the build container, notices the flag within
    ~10 s, kills the container and records the job as `cancelled` (or `done`,
    if the build finished first). A finished job answers 409.

    A cancelled or failed build never touches the graph being served.
    """
    job = db.get(RebuildJob, job_id)
    if job is None or job.session_id != sid:
        raise HTTPException(404, "Rebuild job not found")
    now = datetime.now(UTC)
    who = actor.username or "an operator"
    if job.status == "pending":
        job.status = "cancelled"
        job.finished_at = now
        job.log = (job.log or "") + f"cancelled at {now.isoformat()} by {who} (never started)\n"
    elif job.status == "running":
        if job.cancel_requested_at is None:
            job.cancel_requested_at = now
            job.log = (job.log or "") + f"cancel requested at {now.isoformat()} by {who}\n"
    else:
        raise HTTPException(409, f"Rebuild job already {job.status}")

    audit.record(
        db,
        action="session.rebuild.cancelled",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
        metadata={"job_id": str(job.id), "status": job.status},
    )
    db.commit()
    db.refresh(job)
    return _job_to_response(job)


def _classify_rebuild_log(log: str | None) -> dict[str, Any]:
    """Pure: parse a rebuild log tail for human-actionable signals.

    v0.1.20 detects the streetGraph.obj cache-hit / cache-miss markers
    emitted by the OTP entrypoint (see `docker/otp/entrypoint.sh` lines
    227-235). All three are emitted near the *start* of the build, so
    they survive the 32k log truncation that grabs the tail.

    Marker strings (must match the entrypoint exactly):
      - "streetGraph.obj cache hit (key=..."           → cache_hit=True
      - "streetGraph.obj cache miss (key changed: ..." → cache_hit=False
      - "streetGraph.obj cache empty — building from scratch" → cache_hit=False
      - none of the above                              → cache_hit=None

    cache_hit=False is also surfaced as "first build / cache empty" by
    the entrypoint; we don't distinguish miss-after-key-change from
    first-build because operators see them the same way ("the slow path").

    Tolerant of missing markers — old logs from pre-v0.1.7 builds, builds
    that crashed before reaching the cache phase, and the 32k truncation
    chopping off the relevant line all yield None. UI must handle None
    gracefully (don't claim "cache miss" when we honestly don't know).
    """
    if not log:
        return {"cache_hit": None}
    if "streetGraph.obj cache hit" in log:
        return {"cache_hit": True}
    if "streetGraph.obj cache miss" in log or "streetGraph.obj cache empty" in log:
        return {"cache_hit": False}
    return {"cache_hit": None}


def _snapshot_to_info(snap: GraphSnapshot) -> SnapshotInfo:
    """Convert a GraphSnapshot ORM row into the wire-format SnapshotInfo."""
    return SnapshotInfo(
        built_at=snap.built_at.isoformat() if snap.built_at else "",
        feed_signature=snap.feed_signature or "",
        is_current=bool(snap.is_current),
        timetable_main_version=snap.timetable_main_version or "",
        timetable_update_version=int(snap.timetable_update_version or 0),
        service_period_start=(
            snap.service_period_start.isoformat() if snap.service_period_start else ""
        ),
        service_period_end=snap.service_period_end.isoformat() if snap.service_period_end else "",
        source_uploads=list(snap.source_uploads or []),
        main_version_source=snap.main_version_source or "auto",
    )


def _job_to_response(j: RebuildJob, db: DbSession | None = None) -> RebuildJobResponse:
    # v0.1.20 — join graph_snapshots when a db handle is provided. Callers
    # that don't have one (the POST /rebuilds endpoint that returns a freshly
    # enqueued job, which obviously has no snapshot yet) pass db=None and
    # get the bare-bones response. The list endpoint always passes db.
    snapshot_info: SnapshotInfo | None = None
    if db is not None:
        snap = db.execute(
            select(GraphSnapshot).where(GraphSnapshot.rebuild_job_id == j.id)
        ).scalar_one_or_none()
        if snap is not None:
            snapshot_info = _snapshot_to_info(snap)

    duration_seconds: int | None = None
    if j.started_at and j.finished_at:
        duration_seconds = int((j.finished_at - j.started_at).total_seconds())

    classification = _classify_rebuild_log(j.log)
    station_build = station_builds.status_from_log(j.log)
    if station_build and station_build[0] == station_builds.WAITING and j.status != "running":
        # A job that ended while it waited (a worker restart): it waits no more.
        station_build = None

    return RebuildJobResponse(
        id=str(j.id),
        session_id=j.session_id,
        status=j.status,
        log=j.log,
        created_at=j.created_at.isoformat() if j.created_at else "",
        started_at=j.started_at.isoformat() if j.started_at else None,
        finished_at=j.finished_at.isoformat() if j.finished_at else None,
        graph_path=j.graph_path,
        duration_seconds=duration_seconds,
        snapshot=snapshot_info,
        cache_hit=classification["cache_hit"],
        max_memory=bool(j.max_memory),
        cancel_requested=j.cancel_requested_at is not None,
        station_build=station_build[0] if station_build else None,
        station_build_text=station_build[1] if station_build else None,
    )


# ───────────────────────── promote ─────────────────────────


class PromoteResponse(BaseModel):
    state: str
    fragments_written: bool


@router.post(
    "/{sid}/promote",
    responses={
        400: {"description": "Session is not in state graph_built or serving."},
        404: {"description": "Session not found."},
    },
)
def promote_session(
    sid: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_platform_admin)],
) -> PromoteResponse:
    """Move a `graph_built` session to `serving`.

    Steps:
      1. Validate state == 'graph_built' (or 'serving' for re-trigger).
      2. Set state to 'serving' so the orchestrator includes it.
      3. Regenerate compose + nginx fragments
         (`app.sessions_orchestrator.regenerate`).
      4. Touch the reload-trigger file. The worker, on its next tick,
         runs `docker compose -p viator up -d` (which picks up the new
         `otp-<sid>` service) and `nginx -s reload` (which picks up the
         new `/otp/<sid>/` location), then deletes the trigger file.

    Steps 4's effect is **eventually consistent** with state=='serving' —
    there's a ≤15 s window between the state flip and the otp-<sid>
    container actually being routable. Phase-B will close that window by
    making the worker pick this up via DB state instead of the trigger.
    """
    s = db.get(SessionRow, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    if s.state not in (SessionState.GRAPH_BUILT.value, SessionState.SERVING.value):
        raise HTTPException(
            400,
            f"Session must be in state 'graph_built' to promote (current: {s.state!r})",
        )

    s.state = SessionState.SERVING.value

    sessions_orchestrator.regenerate(db)
    _RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    _RELOAD_TRIGGER.write_text(datetime.now(UTC).isoformat())

    audit.record(
        db,
        action="session.promoted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="session",
        target_id=sid,
    )
    db.commit()
    return PromoteResponse(state=s.state, fragments_written=True)
