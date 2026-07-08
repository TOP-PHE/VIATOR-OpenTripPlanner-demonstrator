"""Journey endpoints — fanout (default), plan (single session), searches/<id>."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import concurrency, config_service
from ..db import get_db
from ..journey import hafas_client, ojp_client, planner_dispatch, recorder, trip_normalize
from ..models import GraphSnapshot
from ..models import Session as SessionRow
from ..models.sessions import SessionState
from ..security import CurrentUser, client_ip, require_logged_in

router = APIRouter(prefix="/api/journey", tags=["journey"])


# ────────────────────────── schemas ──────────────────────────


class Coord(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str | None = None
    # UIC code from master_stations, set by the journey UI when the
    # operator picks a station from the dropdown. When present, the
    # server builds an OTP stop id (`<feedId>:<uic>`) and routes via
    # `planConnection`'s stopLocation input — bypassing the lat/lon →
    # walk-graph snap, which fails for small/border stations whose
    # walking neighbourhood was stripped by rail-focused OSM filtering.
    # Optional: searches without it fall through to coordinate routing
    # unchanged. See app/journey/otp_client.py and docs/nap-ch-rail.md §9.1.
    uic: str | None = None


class FanoutBody(BaseModel):
    from_: Coord = Field(alias="from")
    to: Coord
    depart_at: datetime | None = None
    arrive_by: datetime | None = None
    modes: list[str] = Field(default_factory=lambda: ["TRANSIT", "WALK"])
    # When true AND the platform has OJP comparison configured
    # (OJP_COMPARISON_ENABLED + OJP_API_TOKEN), the fanout also queries
    # the external reference OJP endpoint and returns its itineraries
    # under `ojp_reference` for side-by-side display. Off by default —
    # opt-in per search, see docs/ojp-reference-comparison-design.md.
    compare_ojp: bool = False
    # When true AND HAFAS_COMPARISON_ENABLED is set, the fanout also
    # queries ÖBB's public HAFAS backend (`fahrplan.oebb.at/bin/
    # mgate.exe`) and returns its itineraries under `hafas_reference`.
    # HAFAS needs no API token (the embedded Scotty app id is public),
    # so the feature is enabled by default at the platform level and
    # the operator-side checkbox is the only gate. Covers DACH +
    # cross-border + Eurostar/TGV/AVE/Iberian + Nordic-cross-border —
    # see app/network_coverage/external_verify.py for the empirical
    # 43-pair validation.
    compare_hafas: bool = False
    # P2 MOTIS — optional engine filter. None = no filter (default, fan out
    # across every fanout-enabled session regardless of engine). When set,
    # the fanout restricts to sessions whose `engine` column matches.
    # Used by the search form's engine dropdown to compare planner outputs.
    engine: str | None = None

    model_config = {"populate_by_name": True}


class PlanBody(FanoutBody):
    session_id: str


# ────────────────────────── helpers ──────────────────────────


def _resolve_when(body: FanoutBody) -> tuple[str, datetime]:
    if body.arrive_by is not None:
        return "arrive_by", body.arrive_by
    return "depart_at", body.depart_at or datetime.now(UTC)


def _primary_feed_id(session: SessionRow) -> str | None:
    """Return the first provider's OTP feedId from session.config.

    Per nap-fr-rail.md §2.1, each provider's `id` IS the OTP feedId
    namespace prefix on every stop_id from that feed. For single-provider
    sessions (the common case for the nap-*-rail demonstrators) this is
    unambiguous. For multi-provider sessions we pick the first one and
    rely on otp_client's coordinate fallback for the cases where the
    chosen feedId doesn't match the stop being routed to.

    A future improvement would be to build a per-session UIC→stop_id
    index by querying OTP after each successful build, which would let
    us route exactly across feeds — out of scope for now; the fallback
    is good enough for the demonstrator.
    """
    providers = ((session.config or {}).get("sources") or {}).get("providers") or []
    for p in providers:
        if isinstance(p, dict):
            fid = p.get("id")
            if isinstance(fid, str) and fid:
                return fid
    return None


def _stop_id_for(session: SessionRow, uic: str | None) -> str | None:
    """Build an OTP stop id of the form `<feedId>:<uic>` from a UIC code.

    SBB's GTFS uses UIC codes as stop_ids directly, so `SBB:8771500`
    resolves cleanly to Pontarlier without any feed-specific mapping.
    Feeds that don't key stops by UIC (notably SNCF, which uses
    `OCETrain-NNNNNNNN` style ids) won't resolve via this naive
    construction — `planConnection` returns LOCATION_NOT_FOUND for those
    and otp_client falls back to coordinate routing. Net effect:
    SBB-style feeds get stop-id routing; others keep coordinate routing.
    """
    if not uic:
        return None
    feed_id = _primary_feed_id(session)
    if not feed_id:
        return None
    return f"{feed_id}:{uic}"


def _session_timezone(session: SessionRow) -> str | None:
    """The session's configured OTP timezone, if any.

    Passed to otp_client so a naive depart time (the journey UI's
    `datetime-local` input has no offset) is localised to the graph's
    timezone for `planConnection`'s `earliestDeparture` — preserving the
    "operator picks 12:51 → OTP searches 12:51 graph-local" semantics
    the legacy `plan` query had implicitly.
    """
    tz = (session.config or {}).get("otp_timezone")
    return tz if isinstance(tz, str) and tz else None


async def _query_session(
    db: DbSession,
    session: SessionRow,
    body: FanoutBody,
    timeout_ms: int,
    *,
    num_itineraries: int,
    search_window_seconds: int,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], int]:
    """Returns (status, raw, trips, response_ms).

    `num_itineraries` / `search_window_seconds` come from platform_config
    (OTP_NUM_ITINERARIES / OTP_SEARCH_WINDOW_SECONDS, both runtime-editable).
    Both engines accept the knobs (motis_client.fetch_plan mirrors otp_client's
    signature) so the comparison view can show the same time window on each side.
    """
    start = time.monotonic()
    try:
        _when_kind, when = _resolve_when(body)
        raw, trips = await planner_dispatch.get_planner(session).fetch_plan(
            session_id=session.id,
            from_lat=body.from_.lat,
            from_lon=body.from_.lon,
            to_lat=body.to.lat,
            to_lon=body.to.lon,
            when=when,
            timeout_ms=timeout_ms,
            num_itineraries=num_itineraries,
            search_window_seconds=search_window_seconds,
            from_stop_id=_stop_id_for(session, body.from_.uic),
            to_stop_id=_stop_id_for(session, body.to.uic),
            session_timezone=_session_timezone(session),
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return ("ok" if trips else "no_route"), raw, trips, elapsed
    except (TimeoutError, httpx.TimeoutException):
        return "timeout", {}, [], int((time.monotonic() - start) * 1000)
    except httpx.HTTPError:
        return "error", {}, [], int((time.monotonic() - start) * 1000)


async def _query_ojp_reference(
    cfg: dict[str, Any],
    body: FanoutBody,
    when: datetime,
) -> dict[str, Any]:
    """Query the external OJP reference endpoint for a side-by-side compare.

    Returns a result dict shaped for the fanout response payload
    (`{status, trips, response_ms, error?}`) and **never raises** — a
    failing reference call must not affect VIATOR's own results. The
    caller only invokes this once it has confirmed the feature is
    enabled and a token is set.

    The result is intentionally not persisted (see ojp_client's module
    docstring — `journey_search_executions.session_id` is FK'd to
    `sessions.id`; OJP isn't a session). Phase 1 is live display only.
    """
    start = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        # v0.1.35.06 — anchor-time pagination. OJP's TripRequest caps by
        # alternative count (~6), not by time window; OTP's planConnection
        # covers `searchWindow` (currently 6h). Without pagination, the
        # comparison strip shows spurious `otp_only` itineraries in OTP's
        # tail. fetch_reference_paginated issues up to 4 sequential OJP
        # requests, anchored at successively-later times, deduplicating
        # boundary trips via transit_fingerprint. Target window matches
        # OTP's fetch_plan default (6h = 21600s).
        trips, _ojp_total_ms, pages = await ojp_client.fetch_reference_paginated(
            from_lat=body.from_.lat,
            from_lon=body.from_.lon,
            to_lat=body.to.lat,
            to_lon=body.to.lon,
            when=when,
            timeout_ms=int(cfg["OJP_TIMEOUT_MS"]),
            endpoint=str(cfg["OJP_API_ENDPOINT"]),
            token=str(cfg["OJP_API_TOKEN"]),
            from_name=body.from_.label,
            to_name=body.to.label,
            target_window_seconds=21600,
            max_pages=4,
        )
        result: dict[str, Any] = {
            "status": "ok" if trips else "no_route",
            "trips": trips,
            "response_ms": _ms(),
        }
        # Surface page count so the UI / operator can see whether
        # pagination actually fired. >1 = OJP needed multiple calls to
        # catch up to OTP's window.
        if pages > 1:
            result["pages"] = pages
        return result
    except (TimeoutError, httpx.TimeoutException):
        return {"status": "timeout", "trips": [], "response_ms": _ms()}
    except httpx.HTTPStatusError as exc:
        # 429 surfaced distinctly so the UI can say "rate-limited" rather
        # than a flat error — the OJP free tier is 50 req/min.
        code = exc.response.status_code
        return {
            "status": "rate_limited" if code == 429 else "error",
            "trips": [],
            "response_ms": _ms(),
            "error": f"OJP endpoint returned HTTP {code}",
        }
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "trips": [],
            "response_ms": _ms(),
            "error": f"OJP request failed: {type(exc).__name__}",
        }


async def _query_hafas_reference(
    cfg: dict[str, Any],
    body: FanoutBody,
    when: datetime,
) -> dict[str, Any]:
    """Query ÖBB's public HAFAS backend for a side-by-side compare.

    Sibling of `_query_ojp_reference` — same return-shape contract
    (`{status, trips, response_ms, error?}`), same never-raises
    discipline (a failing reference call must not affect VIATOR's
    own results). Caller invokes this only after confirming the
    feature is enabled at the platform level.

    Unlike OJP, HAFAS needs no API token (the embedded Scotty app
    id is public — see external_verify module docstring); the only
    gate is `HAFAS_COMPARISON_ENABLED` + the per-search checkbox.

    HAFAS adapters return errors via `raw.status` / `raw.error`
    rather than raising, so the exception fan-out below is mostly
    defensive — catches any future regression in the underlying
    client that lets an httpx exception escape."""
    start = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        # v0.1.45 — anchor-time pagination (same fix as OJP's
        # fetch_reference_paginated). HAFAS's TripSearch is hardcoded to
        # numF=5 connections from one anchor time, not a time span, so
        # a single fetch_plan call stops far short of MOTIS/OTP's wider
        # searchWindow — the ÖBB panel reads as "found far less" when it
        # was only ever asked about a narrow slice of the day. Target
        # window matches the SAME OTP_SEARCH_WINDOW_SECONDS config used
        # for VIATOR's own fanout, so both sides were asked about
        # (approximately) the same span. This runs in parallel with the
        # VIATOR session fanout (not after it), so the target window is
        # this configured default rather than VIATOR's actual result
        # span — see the post-fanout truncation in `fanout()` for the
        # step that clips HAFAS down to VIATOR's ACTUAL last departure.
        hafas_timeout_ms = int(cfg.get("HAFAS_TIMEOUT_MS", 10_000))
        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=body.from_.lat,
            from_lon=body.from_.lon,
            to_lat=body.to.lat,
            to_lon=body.to.lon,
            when=when,
            timeout_ms=hafas_timeout_ms,
            from_name=body.from_.label,
            to_name=body.to.label,
            target_window_seconds=int(cfg["OTP_SEARCH_WINDOW_SECONDS"]),
            max_pages=4,
            # `hafas_task` is awaited bare inside the journey concurrency
            # semaphore, so an unbounded paginated call (4 pages x the
            # per-page timeout = 40 s) would stall /fanout far past the
            # single-page latency this endpoint was sized for. Cap the
            # whole paginated call at 2x one page's budget; pagination
            # returns whatever it collected when the budget runs out.
            total_timeout_ms=hafas_timeout_ms * 2,
        )
        # `raw.status` is the engine-level verdict ("ok" / "no_route" /
        # "error"). Lift it into the response so the journey UI's
        # comparison panel can colour-code it without re-deriving from
        # `trips` length.
        status = str(raw.get("status") or ("ok" if trips else "no_route"))
        result: dict[str, Any] = {
            "status": status,
            "trips": trips,
            "response_ms": _ms(),
        }
        if raw.get("error"):
            result["error"] = str(raw["error"])
        # A page-2+ failure keeps the trips collected so far but means
        # ÖBB's coverage is short of the target window — say so instead
        # of letting the panel imply a complete result set.
        if raw.get("partial"):
            result["partial"] = True
        # Surface page count so the UI / operator can see whether
        # pagination actually fired — same convention as OJP's `pages`.
        if raw.get("pages"):
            result["pages"] = raw["pages"]
        return result
    except (TimeoutError, httpx.TimeoutException):
        return {"status": "timeout", "trips": [], "response_ms": _ms()}
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "trips": [],
            "response_ms": _ms(),
            "error": f"HAFAS request failed: {type(exc).__name__}",
        }


def _current_snapshot(db: DbSession, sid: str) -> GraphSnapshot | None:
    return db.execute(
        select(GraphSnapshot)
        .where(GraphSnapshot.session_id == sid)
        .where(GraphSnapshot.is_current.is_(True))
        .limit(1)
    ).scalar_one_or_none()


def _build_comparison(
    merged_trips: list[dict[str, Any]], ojp_reference: dict[str, Any] | None
) -> dict[str, int] | None:
    """Bucket OTP and OJP itineraries into common / OTP-only / OJP-only.

    Returns a `{common, otp_only, ojp_only}` count summary, or None when
    there's nothing to compare (no OJP reference at all, or the OJP call
    didn't succeed). **Mutates `merged_trips` and the trip dicts inside
    `ojp_reference["trips"]`** by attaching a `comparison` key with one
    of `'common' | 'otp_only' | 'ojp_only' | 'uncomparable'` — the
    journey UI renders that as a per-card badge.

    Why the per-itinerary tag *plus* the summary: the summary tells the
    operator "the engines agree on N journeys" at a glance; the per-
    itinerary tag answers "is THIS specific card one of those?". An
    `'uncomparable'` tag is attached to any itinerary whose transit
    fingerprint is empty (walk-only or no transit legs) so the UI can
    grey it out instead of mis-classifying it.

    Counts in the summary are of distinct fingerprints, not raw card
    counts — within-engine duplicates collapse so "Common: 2" really
    means "2 distinct trains both engines agree on".
    """
    if ojp_reference is None or ojp_reference.get("status") != "ok":
        return None
    # Imported here rather than at module scope so the journey.py
    # import graph doesn't pull signature.py until a request actually
    # exercises this path. Same pattern as the existing trip_signature
    # import in the fanout body above.
    from ..journey.signature import transit_fingerprint

    ojp_trips: list[dict[str, Any]] = ojp_reference.get("trips") or []

    otp_fps = [transit_fingerprint(mt["best"].get("legs") or []) for mt in merged_trips]
    ojp_fps = [transit_fingerprint(ot.get("legs") or []) for ot in ojp_trips]

    otp_set = {fp for fp in otp_fps if fp}
    ojp_set = {fp for fp in ojp_fps if fp}
    common_set = otp_set & ojp_set

    def _tag(fp: str, only_label: str) -> str:
        if not fp:
            return "uncomparable"
        return "common" if fp in common_set else only_label

    for mt, fp in zip(merged_trips, otp_fps, strict=True):
        mt["comparison"] = _tag(fp, "otp_only")
    for ot, fp in zip(ojp_trips, ojp_fps, strict=True):
        ot["comparison"] = _tag(fp, "ojp_only")

    return {
        "common": len(common_set),
        "otp_only": len(otp_set - common_set),
        "ojp_only": len(ojp_set - common_set),
    }


def _origin_flag(found_in: list[str], all_fanout: list[str]) -> str:
    """ALL / NAP_ONLY / MERITS_ONLY / <session>_ONLY / SUBSET."""
    s = set(found_in)
    if s == set(all_fanout):
        return "ALL"
    if len(s) == 1:
        return f"{next(iter(s)).upper()}_ONLY"
    return "SUBSET"


def _boarding_ts(trip: dict[str, Any]) -> float | None:
    """The trip's BOARDING instant as an epoch float, or None.

    Prefers `first_transit_leg_departure_utc` — the repo's canonical
    "when did this itinerary actually board" field, computed on a
    consistent UTC basis by every engine client (see
    `trip_normalize.first_transit_leg_departure_utc`). Falls back to the
    itinerary-level `departure_at` only when no transit leg exists (a
    walk-only itinerary), since `departure_at` is the START of the whole
    trip — usually a walk leg — and comparing one engine's walk start
    against another's would shift the boundary by the access-walk length.
    """
    return trip_normalize.max_dep_ts(
        None, trip.get("first_transit_leg_departure_utc") or trip.get("departure_at")
    )


def _truncate_hafas_to_viator_window(
    hafas_reference: dict[str, Any] | None, viator_trips: list[dict[str, Any]]
) -> None:
    """Mutate `hafas_reference['trips']` in place, dropping any ÖBB trip
    boarding after VIATOR's own latest boarding.

    v0.1.45 — `hafas_client.fetch_plan_paginated` targets a FIXED window
    (`OTP_SEARCH_WINDOW_SECONDS`) chosen before VIATOR's own fanout even
    starts (the two run concurrently — see `_query_hafas_reference`).
    If ÖBB's pagination overshoots VIATOR's ACTUAL result span (VIATOR
    returned fewer/earlier trips than the configured window would
    suggest, e.g. MOTIS ran dry after 3 sessions), the side-by-side
    comparison would show ÖBB with MORE coverage in a time range VIATOR
    was never even displayed for — the mirror image of the original
    "ÖBB looks artificially worse" complaint this pagination fixes, and
    just as misleading. Clips to what VIATOR's fanout actually
    returned, not what it was merely configured to search for.

    Compares BOARDING times (`_boarding_ts`) on both sides, not the
    walk-inclusive `departure_at`: the two engines produce different
    access walks for the same physical train, so a `departure_at`
    boundary would clip ÖBB trips VIATOR did in fact cover (or keep
    ones it didn't) by however many minutes the walks differ.
    """
    if not hafas_reference or not viator_trips:
        return
    latest_viator_ts = None
    for t in viator_trips:
        ts = _boarding_ts(t.get("best") or {})
        if ts is not None and (latest_viator_ts is None or ts > latest_viator_ts):
            latest_viator_ts = ts
    if latest_viator_ts is None:
        return
    trips = hafas_reference.get("trips") or []
    kept = []
    dropped = 0
    for t in trips:
        ts = _boarding_ts(t)
        if ts is None or ts <= latest_viator_ts:
            kept.append(t)
        else:
            dropped += 1
    if dropped:
        hafas_reference["trips"] = kept
        hafas_reference["trimmed_to_viator_window"] = True


# ────────────────────── P2 MOTIS — engine filter helpers ──────────────────────
#
# Extracted as pure helpers (rather than inline in the route) so they're
# unit-testable without spinning up the full FastAPI + Postgres + auth
# stack. The actual SQL execution stays in the route — the helpers cover
# the validation and SQL-builder shape, which is where Phase-2 review focus
# lives.


def _validate_engine_filter(engine: str | None) -> None:
    """Raise HTTPException(400) when `engine` is set but not a known
    SessionEngine value. None = no filter (legacy behaviour) — never raises.

    Validated here (rather than as a Pydantic constraint) so unknown values
    surface as a 400 with a human-readable message rather than as a 422
    with Pydantic's validator-noise envelope.
    """
    from ..models.sessions import SessionEngine

    if engine is not None and engine not in {e.value for e in SessionEngine}:
        valid = sorted(e.value for e in SessionEngine)
        raise HTTPException(400, f"Invalid engine {engine!r}. Must be one of {valid}")


def _no_serving_sessions_message(engine: str | None) -> str:
    """409 message body. Differentiates the no-sessions-at-all case from the
    no-sessions-with-this-engine case so operators can spot which filter
    they need to relax."""
    if engine is None:
        return "No serving sessions are enabled for fanout"
    return f"No serving fanout-enabled sessions with engine={engine!r}"


def _select_fanout_sessions(db: DbSession, engine: str | None) -> list[SessionRow]:
    """Resolve the serving + fanout-enabled session list, optionally
    restricted by engine. Pulled out of the route so the SQL shape is
    obvious from one place — but kept thin (no caching, no extra joins)
    so the route's flow stays linear."""
    stmt = (
        select(SessionRow)
        .where(SessionRow.state == SessionState.SERVING.value)
        .where(SessionRow.include_in_fanout.is_(True))
    )
    if engine is not None:
        stmt = stmt.where(SessionRow.engine == engine)
    return list(db.execute(stmt).scalars().all())


# ────────────────────────── routes ──────────────────────────


@router.post(
    "/fanout",
    summary="Run a search across every fanout-enabled session",
    responses={
        # P2 MOTIS — declared so SonarPython S8415 is satisfied at the
        # decorator level (rule asks for OpenAPI doc on every status code
        # the handler may raise).
        400: {"description": "Invalid engine filter value."},
        409: {"description": "No serving fanout-enabled sessions matched the query."},
    },
)
async def fanout(
    body: FanoutBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    cfg = config_service.get_all(db)
    _validate_engine_filter(body.engine)
    sessions = _select_fanout_sessions(db, body.engine)
    if not sessions:
        raise HTTPException(409, _no_serving_sessions_message(body.engine))

    when_kind, when = _resolve_when(body)
    overall_start = time.monotonic()

    try:
        async with concurrency.semaphores.journey.acquire_or_fail():
            search = recorder.begin_search(
                db,
                user_id=user.id,
                ip=client_ip(request),
                endpoint="fanout",
                origin_lat=body.from_.lat,
                origin_lon=body.from_.lon,
                origin_label=body.from_.label,
                dest_lat=body.to.lat,
                dest_lon=body.to.lon,
                dest_label=body.to.label,
                requested_time_kind=when_kind,
                requested_time=when,
                modes=",".join(body.modes),
            )
            timeout_ms = int(cfg["FANOUT_TIMEOUT_MS"])
            num_itineraries = int(cfg["OTP_NUM_ITINERARIES"])
            search_window_seconds = int(cfg["OTP_SEARCH_WINDOW_SECONDS"])

            # v0.1.35 — optional external OJP reference comparison. Kicked
            # off as a task so it runs concurrently with the OTP session
            # calls; awaited after. Only when the operator opted in AND
            # the platform has the feature enabled with a token set.
            ojp_task: asyncio.Task[dict[str, Any]] | None = None
            if body.compare_ojp and cfg.get("OJP_COMPARISON_ENABLED") and cfg.get("OJP_API_TOKEN"):
                ojp_task = asyncio.create_task(_query_ojp_reference(cfg, body, when))

            # Sibling — ÖBB HAFAS comparison. Same concurrent-task model
            # as OJP. No API-token gate (HAFAS credentials embedded in
            # external_verify are public), so only the per-search opt-in
            # + the platform-level enabled flag govern whether it runs.
            hafas_task: asyncio.Task[dict[str, Any]] | None = None
            if body.compare_hafas and cfg.get("HAFAS_COMPARISON_ENABLED"):
                hafas_task = asyncio.create_task(_query_hafas_reference(cfg, body, when))

            results = await asyncio.gather(
                *[
                    _query_session(
                        db,
                        s,
                        body,
                        timeout_ms,
                        num_itineraries=num_itineraries,
                        search_window_seconds=search_window_seconds,
                    )
                    for s in sessions
                ]
            )
            # Neither `_query_ojp_reference` nor `_query_hafas_reference`
            # raises — both convert errors into status fields — so safe
            # to await bare without exception handling here.
            ojp_reference = await ojp_task if ojp_task is not None else None
            hafas_reference = await hafas_task if hafas_task is not None else None
    except concurrency.ConcurrencyExceeded as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    by_signature: dict[str, dict[str, Any]] = {}
    executions_summary: list[dict[str, Any]] = []
    any_error = False
    any_ok = False
    sids_in_fanout = [s.id for s in sessions]

    for session, (status, raw, trips, response_ms) in zip(sessions, results, strict=True):
        snap = _current_snapshot(db, session.id)
        # Note: a missing graph_snapshots row is NOT an error — it just
        # means the worker hasn't written a snapshot record yet (Phase-3
        # wiring). The OTP query itself succeeded. Pre-this-fix we forced
        # status="error" whenever snap was None, which made every search
        # render "(error)" in the journey UI even when the itineraries
        # were perfect. Now we just leave snapshot_id NULL on the
        # execution row and trust `status` from `_query_session`.
        if status == "ok":
            any_ok = True
        else:
            any_error = True

        exe = recorder.record_execution(
            db,
            search_id=search.id,
            session_id=session.id,
            graph_snapshot_id=snap.id if snap else None,
            status=status,
            response_ms=response_ms,
            raw_response=raw if cfg.get("STORE_RAW_RESPONSE", True) else None,
            error_message=None,
            trips=trips,
        )
        executions_summary.append(
            {
                "session_id": session.id,
                # P2 MOTIS — engine surfaced so the journey UI can switch
                # into the OTP-vs-MOTIS comparison view when both engines
                # actually participated in the fanout (rather than guessing
                # from session id naming conventions).
                "engine": getattr(session, "engine", "otp") or "otp",
                "graph_snapshot_id": (
                    str(exe.graph_snapshot_id) if exe.graph_snapshot_id else None
                ),
                "status": status,
                "num_itineraries": exe.num_itineraries,
                "response_ms": response_ms,
            }
        )

        # Merge trips by signature for the response payload.
        for trip in trips:
            from .. import journey as journey_pkg  # noqa: F401  (keep package import explicit)
            from ..journey.signature import trip_signature

            sig = trip_signature(db, session_id=session.id, legs=trip.get("legs", []))
            slot = by_signature.setdefault(
                sig,
                {
                    "signature": sig,
                    "found_in_sessions": [],
                    "by_session": {},
                    "best": trip,
                },
            )
            slot["found_in_sessions"].append(session.id)
            slot["by_session"][session.id] = {
                "duration_seconds": trip["duration_seconds"],
                "departure_at": trip["departure_at"],
                "arrival_at": trip["arrival_at"],
            }
            if trip["duration_seconds"] < slot["best"]["duration_seconds"]:
                slot["best"] = trip

    overall_ms = int((time.monotonic() - overall_start) * 1000)

    if any_ok and any_error:
        status = "partial"
    elif any_ok or by_signature:
        status = "ok"
    elif not any_error:
        status = "no_route"
    else:
        status = "error"

    recorder.finish_search(
        db,
        search,
        total_response_ms=overall_ms,
        total_trips_unique=len(by_signature),
        status=status,
    )
    db.commit()

    merged_trips = []
    for slot in by_signature.values():
        merged_trips.append(
            {
                **slot,
                "origin_flag": _origin_flag(slot["found_in_sessions"], sids_in_fanout),
            }
        )

    # v0.1.45 — clip ÖBB HAFAS's paginated results down to VIATOR's own
    # actual result window (not merely its configured search window —
    # see _truncate_hafas_to_viator_window's docstring). Must run AFTER
    # merged_trips is final; hafas_reference was fetched concurrently
    # with the fanout above and can't know VIATOR's actual span until now.
    _truncate_hafas_to_viator_window(hafas_reference, merged_trips)

    # v0.1.41 — federated fallback (hub-and-spoke). When no single session
    # returned an end-to-end itinerary, try stitching a domestic leg onto the
    # cross-border spine at a data-derived hub (a stop both sessions serve).
    # Only when the form sent UIC endpoints — the planner queries stations by
    # UIC. Best-effort: a failure here never 500s the whole search.
    # See docs/federated-planner-design.md.
    federated_trips: list[dict[str, Any]] = []
    if not merged_trips and body.from_.uic and body.to.uic:
        import logging

        from ..journey import federated_planner

        try:
            federated_trips = await federated_planner.plan_federated(
                db,
                origin_uic=body.from_.uic,
                dest_uic=body.to.uic,
                when=when,
                sessions=list(sessions),
                timeout_ms=timeout_ms,
                session_timezone_for={s.id: _session_timezone(s) for s in sessions},
            )
        except Exception:
            logging.getLogger(__name__).exception("federated planner failed (non-fatal)")
            federated_trips = []
        if federated_trips and status != "error":
            status = "ok"

    # v0.1.36 — Phase 2 structured comparison. When the OJP reference
    # returned itineraries, fingerprint each itinerary's *transit* leg
    # spine on both sides (walks stripped — coords rounded to ~11 m so
    # OTP's `SBB:…` and OJP's `ch:1:sloid:…` stop references match by
    # location). Bucket into common / OTP-only / OJP-only and attach
    # both a per-trip tag and a summary count. The journey UI renders a
    # one-line strip + a small badge on each card so the operator can
    # see at a glance "VIATOR and the reference agree on N journeys,
    # disagree on M+K". Phase 1 was the side-by-side display; Phase 2
    # is the structured diff. Design: docs/ojp-reference-comparison-
    # design.md §9.
    comparison_summary = _build_comparison(merged_trips, ojp_reference)

    response: dict[str, Any] = {
        "search_id": str(search.id),
        "status": status,
        "trips": merged_trips,
        "executions": executions_summary,
    }
    # Present only when the operator opted into the OJP comparison and the
    # feature is configured — the journey UI renders it as a separate
    # "Reference (Swiss OJP)" panel. Never merged into `trips`/`executions`
    # and never persisted (Phase 1 — live display only).
    if ojp_reference is not None:
        response["ojp_reference"] = ojp_reference
    # Same shape, separate panel — ÖBB HAFAS as a second comparison
    # engine alongside OJP. Operators can toggle either or both per
    # search; the UI renders one panel per reference.
    if hafas_reference is not None:
        response["hafas_reference"] = hafas_reference
    if comparison_summary is not None:
        response["comparison_summary"] = comparison_summary
    # Stitched cross-session itineraries (hub-and-spoke fallback). Rendered as a
    # separate "Federated (via hub)" group in the journey UI — never merged into
    # `trips`, since each spans 2+ sessions and a hub.
    if federated_trips:
        response["federated_trips"] = federated_trips
    return response


@router.post("/plan", summary="Plan against a single explicit session")
async def plan(
    body: PlanBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    s = db.get(SessionRow, body.session_id)
    if s is None or s.state != SessionState.SERVING.value:
        raise HTTPException(404, f"No serving session {body.session_id!r}")

    cfg = config_service.get_all(db)
    when_kind, when = _resolve_when(body)

    try:
        async with concurrency.semaphores.journey.acquire_or_fail():
            search = recorder.begin_search(
                db,
                user_id=user.id,
                ip=client_ip(request),
                endpoint="plan",
                origin_lat=body.from_.lat,
                origin_lon=body.from_.lon,
                origin_label=body.from_.label,
                dest_lat=body.to.lat,
                dest_lon=body.to.lon,
                dest_label=body.to.label,
                requested_time_kind=when_kind,
                requested_time=when,
                modes=",".join(body.modes),
            )
            status, raw, trips, response_ms = await _query_session(
                db,
                s,
                body,
                int(cfg["JOURNEY_TIMEOUT_MS"]),
                num_itineraries=int(cfg["OTP_NUM_ITINERARIES"]),
                search_window_seconds=int(cfg["OTP_SEARCH_WINDOW_SECONDS"]),
            )
    except concurrency.ConcurrencyExceeded as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    snap = _current_snapshot(db, s.id)
    # Same as the fanout path — missing graph_snapshots row isn't an error,
    # just deferred Phase-3 wiring. Don't poison status with "error" here.
    recorder.record_execution(
        db,
        search_id=search.id,
        session_id=s.id,
        graph_snapshot_id=snap.id if snap else None,
        status=status,
        response_ms=response_ms,
        raw_response=raw if cfg.get("STORE_RAW_RESPONSE", True) else None,
        error_message=None,
        trips=trips,
    )
    recorder.finish_search(
        db,
        search,
        total_response_ms=response_ms,
        total_trips_unique=len(trips),
        status=status,
    )
    db.commit()

    return {"search_id": str(search.id), "status": status, "trips": trips}


@router.get("/searches/{search_id}")
def get_search(
    search_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_logged_in)],
) -> dict[str, Any]:
    from ..models import JourneySearch, JourneySearchExecution, JourneyTrip

    s = db.get(JourneySearch, search_id)
    if s is None:
        raise HTTPException(404, "Search not found")
    if user.role != "platform_admin" and s.user_id != user.id:
        raise HTTPException(403, "Not your search")

    execs = (
        db.execute(select(JourneySearchExecution).where(JourneySearchExecution.search_id == s.id))
        .scalars()
        .all()
    )
    out: dict[str, Any] = {
        "id": str(s.id),
        "endpoint": s.endpoint,
        "status": s.status,
        "executions": [],
    }
    for exe in execs:
        trips = (
            db.execute(
                select(JourneyTrip)
                .where(JourneyTrip.execution_id == exe.id)
                .order_by(JourneyTrip.rank_in_response)
            )
            .scalars()
            .all()
        )
        out["executions"].append(
            {
                "session_id": exe.session_id,
                "status": exe.status,
                "num_itineraries": exe.num_itineraries,
                "response_ms": exe.response_ms,
                "trips": [
                    {
                        "signature": t.trip_signature,
                        "duration_seconds": t.duration_seconds,
                        "num_transfers": t.num_transfers,
                        "departure_at": t.departure_at.isoformat(),
                        "arrival_at": t.arrival_at.isoformat(),
                        "modes": t.modes,
                        "legs": t.legs,
                    }
                    for t in trips
                ],
            }
        )
    return out


# Note: previously this module had `_placeholder_snapshot_id()` returning the
# all-zero UUID for sessions without a recorded snapshot. That was a known
# FK-violation footgun (the all-zero UUID isn't in graph_snapshots → every
# fanout call against a freshly-built session crashed with IntegrityError).
# Removed in favour of `graph_snapshot_id=None` on the model, with the
# corresponding alembic migration `20260429_0700_exec_snap_nullable`. The
# proper fix — having the worker write a graph_snapshots row after every
# successful build — remains a Phase-3 milestone.
