"""Federated planner — stitch a domestic leg onto the cross-border spine.

Phase 1 (hub-and-spoke, single transfer): when **no single serving session**
can route an origin→destination, find a session that serves the origin and a
session that serves the destination which **share a connection hub** — the UIC
intersection of their served stops, *derived from the feeds, not curated* — then
route origin→hub in one session and hub→destination in the other, time-
coordinated across the transfer, and stitch the two legs into one itinerary.

For **Paris → Fribourg** the origin-session is `nap-eu-corridors` (it serves
Paris and routes the international middle in-graph) and the destination-session
is `nap-ch-rail`; their shared hubs are the Swiss gateways (Basel, Bern, …) —
exactly the corridors-as-spine model in docs/federated-planner-design.md.

This is a **fallback**: `fanout()` calls it only when every session failed to
return an end-to-end itinerary, so through-trains and single-session results
always win and the common path stays fast.

The pure helpers (`served_uics`, `connection_hubs`, `rank_hubs`, `assemble_stitch`,
`earliest_next_departure`, `dedup_and_rank`) carry the logic and are unit-tested;
`plan_federated` is the async orchestration that drives per-session OTP queries.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .signature import _uic_from_stop_id, transit_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DbSession

    from ..models import Session as SessionRow

log = logging.getLogger(__name__)

# Phase-1 knobs. Flat minimum connection time (rail↔rail, same station); per-hub
# MCT comes later from the enrichment layer. The fan-out bounds cap how many OTP
# calls one federated query can make (hubs x leg-1 options).
DEFAULT_MCT_SECONDS = 600  # 10 min
MAX_HUBS = 8  # per session pair; hubs are proximity-ranked first (see rank_hubs)
MAX_LEG1_OPTIONS = 3
MAX_RESULTS = 5
# A change is "worth" this much ride time when ranking: a clean 1-change journey
# should beat a 3-change slog that merely arrives a few minutes earlier.
TRANSFER_PENALTY_SECONDS = 1200  # 20 min


# ───────────────────────────── pure helpers ────────────────────────────────
def served_uics(stops: list[tuple[str | None, float | None, float | None]]) -> set[str]:
    """UIC codes a session serves, parsed from its GTFS `(stop_id, lat, lon)` rows.

    Rail feeds key stops by UIC (SBB 7-digit, SNCF 8-digit, …); `_uic_from_stop_id`
    extracts the canonical 7-digit code. Stops with no parseable UIC are skipped —
    they can't be a cross-feed hub anyway (a hub must be the *same* station in two
    feeds, which only the UIC identifies).
    """
    out: set[str] = set()
    for stop_id, _lat, _lon in stops:
        uic = _uic_from_stop_id(stop_id)
        if uic:
            out.add(uic)
    return out


def connection_hubs(served_a: set[str], served_b: set[str]) -> set[str]:
    """The connection hubs between two sessions = the stations both serve (UIC)."""
    return served_a & served_b


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def rank_hubs(
    hub_uics: set[str],
    coords: dict[str, tuple[float, float]],
    origin_uic: str,
    dest_uic: str,
) -> list[str]:
    """Order candidate hubs: destination-country hubs first, then by detour.

    Two signals, in priority order:

    1. **Destination country first.** The spoke (the destination session's
       hub->dest leg) is only dense *inside the destination country*. Cutting at
       an origin- or third-country border station forces that network to claw
       back across the border on sparse regional lines — which made
       Paris->Fribourg stitch via Besancon (FR) over 12 regional legs instead of
       via a Swiss gateway. A UIC's first two digits encode its country.
    2. **Detour cost** within each tier: great-circle (origin->hub) + (hub->dest)
       in km. A hub on the direct line scores near the direct distance, an
       off-route one scores far more. Tie-broken by UIC for determinism.

    A hub with no resolved coordinates is dropped (it can be neither scored nor
    routed). If no hub is in the destination country, every hub lands in tier 2
    and this degrades to pure proximity ranking. (Both signals replace the
    original lexicographic-by-UIC order, which dropped Swiss `85...` gateways
    behind every lower-numbered code.)
    """
    origin = coords.get(origin_uic)
    dest = coords.get(dest_uic)
    if origin is None or dest is None:
        return []
    dest_country = dest_uic[:2]
    scored: list[tuple[int, float, str]] = []
    for uic in hub_uics:
        hub = coords.get(uic)
        if hub is None:
            continue
        outside_dest_country = 0 if uic[:2] == dest_country else 1
        detour = _haversine_km(*origin, *hub) + _haversine_km(*hub, *dest)
        scored.append((outside_dest_country, detour, uic))
    scored.sort()  # (tier, detour, uic) — tier first, then proximity, then uic
    return [uic for _tier, _detour, uic in scored]


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant (otp_client emits `…Z`) to an aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def earliest_next_departure(
    prev_arrival_iso: str, mct_seconds: int = DEFAULT_MCT_SECONDS
) -> datetime:
    """When the onward leg may earliest depart: previous arrival + MCT."""
    return _parse_iso(prev_arrival_iso) + timedelta(seconds=mct_seconds)


def _join_legs_dropping_hub_walks(leg_trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate the per-leg trips' legs, dropping the phantom walks at each
    stitch boundary.

    Each non-final trip's trailing WALK (egress to the hub) and each non-first
    trip's leading WALK (access from the hub) are the two halves of a single
    platform change — real inside OTP's per-leg search, but doubled and
    mislabelled ("-> Destination" / "Origin ->") once stitched, since each leg
    treats the hub as its own endpoint. The genuine origin-access and
    destination-egress walks (first trip's first leg, last trip's last leg) are
    kept. The hub change itself still counts as one transfer (see assemble_stitch).
    """
    last_index = len(leg_trips) - 1
    joined: list[dict[str, Any]] = []
    for i, trip in enumerate(leg_trips):
        legs = list(trip.get("legs", []))
        if i > 0 and legs and legs[0].get("mode") == "WALK":
            legs = legs[1:]
        if i < last_index and legs and legs[-1].get("mode") == "WALK":
            legs = legs[:-1]
        joined.extend(legs)
    return joined


def assemble_stitch(
    leg_trips: list[dict[str, Any]],
    *,
    via_hubs: list[str],
    session_ids: list[str],
) -> dict[str, Any]:
    """Join a chain of per-leg trips into a single stitched itinerary.

    Total duration is wall-clock (last arrival minus first departure, so transfer
    wait is counted); transfers = the sum within each leg plus one per stitch. The
    phantom egress/access walks at each hub are dropped (see
    `_join_legs_dropping_hub_walks`) so the change reads as one platform change.
    """
    first, last = leg_trips[0], leg_trips[-1]
    departure_at = first["departure_at"]
    arrival_at = last["arrival_at"]
    legs = _join_legs_dropping_hub_walks(leg_trips)
    modes = sorted({m for t in leg_trips for m in (t.get("modes") or "").split(",") if m})
    internal_transfers = sum(int(t.get("num_transfers", 0)) for t in leg_trips)
    return {
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "duration_seconds": int(
            (_parse_iso(arrival_at) - _parse_iso(departure_at)).total_seconds()
        ),
        "num_transfers": internal_transfers + (len(leg_trips) - 1),
        "modes": ",".join(modes),
        "legs": legs,
        "via_hubs": list(via_hubs),
        "stitched_from_sessions": list(session_ids),
        "federated": True,
    }


def _rank_key(stitch: dict[str, Any]) -> tuple[int, str]:
    """Sort key: generalized time (ride seconds + a penalty per change), then
    earliest arrival as the tie-break. A clean 1-change journey thus outranks a
    transfer-heavy one that merely arrives a little earlier."""
    transfers = int(stitch.get("num_transfers", 0))
    generalized = int(stitch.get("duration_seconds", 0)) + TRANSFER_PENALTY_SECONDS * transfers
    return (generalized, stitch.get("arrival_at", ""))


def dedup_and_rank(
    stitches: list[dict[str, Any]],
    *,
    existing_fingerprints: set[str] | None = None,
    limit: int = MAX_RESULTS,
) -> list[dict[str, Any]]:
    """Rank stitched itineraries by generalized time (ride time plus a fixed
    penalty per change, so a clean journey beats a transfer-heavy one that merely
    arrives a touch earlier), then drop any that duplicate each other or an
    itinerary a session already returned (by `transit_fingerprint`)."""
    seen = set(existing_fingerprints or set())
    out: list[dict[str, Any]] = []
    for s in sorted(stitches, key=_rank_key):
        fp = transit_fingerprint(s.get("legs", []))
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        out.append(s)
        if len(out) >= limit:
            break
    return out


# ───────────────────────── IO: served-stop sets ────────────────────────────
# Cache a session's served-UIC set so repeated federated queries don't re-parse
# the GTFS. Keyed by session id; invalidated on a fresh promote (the worker
# rewrites the inbox) — for the demonstrator a process-lifetime cache is fine.
_SERVED_UICS_CACHE: dict[str, set[str]] = {}


def _read_stop_ids(gtfs_dir: Path) -> list[str]:
    """Every `stop_id` across the GTFS zips in a directory (best-effort).

    Stdlib only — kept here rather than importing the admin API (which pulls
    the DB layer) so this module stays light and unit-testable.
    """
    import csv
    import io
    import zipfile

    out: list[str] = []
    if not gtfs_dir.exists():
        return out
    for zip_path in sorted(gtfs_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(zip_path) as zf, zf.open("stops.txt") as fh:
                for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")):
                    sid = row.get("stop_id")
                    if sid:
                        out.append(sid)
        except (KeyError, zipfile.BadZipFile, OSError, UnicodeDecodeError):
            continue
    return out


def _session_served_uics(session: SessionRow) -> set[str]:
    """Served-UIC set for a session, read from its staged GTFS (cached)."""
    cached = _SERVED_UICS_CACHE.get(session.id)
    if cached is not None:
        return cached
    from ..settings import settings

    gtfs_dir = Path(str(settings.inbox_dir)) / session.id / "gtfs"
    uics = served_uics([(sid, None, None) for sid in _read_stop_ids(gtfs_dir)])
    _SERVED_UICS_CACHE[session.id] = uics
    return uics


def invalidate_served_uics_cache(session_id: str | None = None) -> None:
    """Drop the served-UIC cache (a session was rebuilt). None ⇒ drop all."""
    if session_id is None:
        _SERVED_UICS_CACHE.clear()
    else:
        _SERVED_UICS_CACHE.pop(session_id, None)


# ───────────────────────────── orchestration ───────────────────────────────
@dataclass(frozen=True)
class _LegContext:
    """Shared inputs for the per-leg OTP calls (passed down so the helpers stay
    free of closures and each keeps a low, independently-measured complexity)."""

    coords: dict[str, tuple[float, float]]
    timeout_ms: int
    tz_for: dict[str, str | None]


def _candidate_hubs(
    origin_sessions: list[SessionRow],
    dest_sessions: list[SessionRow],
    served: dict[str, set[str]],
) -> set[str]:
    """Union of connection hubs across every (origin-session, dest-session) pair."""
    hubs: set[str] = set()
    for so in origin_sessions:
        for sd in dest_sessions:
            if so.id != sd.id:
                hubs |= connection_hubs(served[so.id], served[sd.id])
    return hubs


def _resolve_coords(db: DbSession, wanted_uics: set[str]) -> dict[str, tuple[float, float]]:
    """`(lat, lon)` for each requested UIC, from MasterStation.

    Coordinates are needed both to rank hubs (see `rank_hubs`) and as OTP's
    routing fallback when a feed doesn't key its stops by UIC.
    """
    from ..models import MasterStation

    coords: dict[str, tuple[float, float]] = {}
    for ms in db.query(MasterStation).filter(MasterStation.uic.in_(wanted_uics)).all():
        if ms.latitude is not None and ms.longitude is not None:
            coords[ms.uic] = (ms.latitude, ms.longitude)
    return coords


def _primary_feed_id(session: SessionRow) -> str | None:
    """First provider's OTP feedId (== the stop_id namespace prefix on that
    feed). Mirrors `app.api.journey._primary_feed_id`; kept local so this
    module needn't import the API layer. `getattr` tolerates the lightweight
    session stand-ins used in unit tests (which carry no `config`)."""
    config = getattr(session, "config", None) or {}
    providers = (config.get("sources") or {}).get("providers") or []
    for p in providers:
        if isinstance(p, dict):
            fid = p.get("id")
            if isinstance(fid, str) and fid:
                return fid
    return None


def _stop_id(feed_id: str | None, uic: str) -> str | None:
    """`<feedId>:<uic>` so a UIC-keyed feed (e.g. SBB) routes by the exact
    station. Feeds that don't key by UIC (e.g. SNCF's `OCETrain-…`) won't
    resolve and otp_client falls back to coordinate routing — so this is a
    strict improvement: precise where it can be, unchanged where it can't."""
    return f"{feed_id}:{uic}" if feed_id else None


async def _fetch_leg(
    ctx: _LegContext, session: SessionRow, frm: str, to: str, dep: datetime
) -> list[dict[str, Any]]:
    """One per-session OTP query (by stop-id with coordinate fallback); `[]` on
    a missing endpoint or error."""
    if frm not in ctx.coords or to not in ctx.coords:
        return []
    from . import otp_client

    (flat, flon), (tlat, tlon) = ctx.coords[frm], ctx.coords[to]
    feed_id = _primary_feed_id(session)
    try:
        _raw, trips = await otp_client.fetch_plan(
            session_id=session.id,
            from_lat=flat,
            from_lon=flon,
            to_lat=tlat,
            to_lon=tlon,
            when=dep,
            timeout_ms=ctx.timeout_ms,
            from_stop_id=_stop_id(feed_id, frm),
            to_stop_id=_stop_id(feed_id, to),
            session_timezone=ctx.tz_for.get(session.id),
        )
    except Exception as exc:  # network/OTP error on one leg shouldn't 500 the query
        log.warning("federated leg %s %s->%s failed: %s", session.id, frm, to, exc)
        return []
    return trips


async def _stitch_pair(
    ctx: _LegContext,
    so: SessionRow,
    sd: SessionRow,
    hubs: list[str],
    *,
    origin_uic: str,
    dest_uic: str,
    when: datetime,
    mct_seconds: int,
) -> list[dict[str, Any]]:
    """Stitches for one session pair: origin->hub (so) then hub->dest (sd), per hub."""
    out: list[dict[str, Any]] = []
    for hub in hubs:
        leg1 = await _fetch_leg(ctx, so, origin_uic, hub, when)
        for t1 in leg1[:MAX_LEG1_OPTIONS]:
            nxt = earliest_next_departure(t1["arrival_at"], mct_seconds)
            leg2 = await _fetch_leg(ctx, sd, hub, dest_uic, nxt)
            if leg2:
                out.append(
                    assemble_stitch([t1, leg2[0]], via_hubs=[hub], session_ids=[so.id, sd.id])
                )
    return out


async def _collect_stitches(
    ctx: _LegContext,
    origin_sessions: list[SessionRow],
    dest_sessions: list[SessionRow],
    served: dict[str, set[str]],
    *,
    origin_uic: str,
    dest_uic: str,
    when: datetime,
    mct_seconds: int,
) -> list[dict[str, Any]]:
    """Drive each distinct session pair through `_stitch_pair`, trying the
    MAX_HUBS most on-route shared hubs (proximity-ranked, so the natural
    gateway — e.g. Basel SBB on Paris->Fribourg — is always among them)."""
    stitches: list[dict[str, Any]] = []
    for so in origin_sessions:
        for sd in dest_sessions:
            if so.id == sd.id:
                continue
            hubs = rank_hubs(
                connection_hubs(served[so.id], served[sd.id]),
                ctx.coords,
                origin_uic,
                dest_uic,
            )[:MAX_HUBS]
            stitches.extend(
                await _stitch_pair(
                    ctx,
                    so,
                    sd,
                    hubs,
                    origin_uic=origin_uic,
                    dest_uic=dest_uic,
                    when=when,
                    mct_seconds=mct_seconds,
                )
            )
    return stitches


async def plan_federated(
    db: DbSession,
    *,
    origin_uic: str | None,
    dest_uic: str | None,
    when: datetime,
    sessions: list[SessionRow],
    timeout_ms: int,
    session_timezone_for: dict[str, str | None] | None = None,
    existing_fingerprints: set[str] | None = None,
    mct_seconds: int = DEFAULT_MCT_SECONDS,
) -> list[dict[str, Any]]:
    """Phase-1 single-transfer stitch. Returns ranked stitched itineraries.

    Requires UIC origin/destination (the form sends them when the operator picks
    from the station dropdown). Returns `[]` when there's no UIC, no hub, or no
    leg combination connects — a correct "no federated result".
    """
    if not origin_uic or not dest_uic or origin_uic == dest_uic:
        return []

    served = {s.id: _session_served_uics(s) for s in sessions}
    origin_sessions = [s for s in sessions if origin_uic in served[s.id]]
    dest_sessions = [s for s in sessions if dest_uic in served[s.id]]
    if not origin_sessions or not dest_sessions:
        return []

    candidate_hubs = _candidate_hubs(origin_sessions, dest_sessions, served)
    if not candidate_hubs:
        return []

    coords = _resolve_coords(db, {origin_uic, dest_uic} | candidate_hubs)
    if origin_uic not in coords or dest_uic not in coords:
        return []  # can't query OTP without endpoint coordinates

    ctx = _LegContext(coords=coords, timeout_ms=timeout_ms, tz_for=session_timezone_for or {})
    stitches = await _collect_stitches(
        ctx,
        origin_sessions,
        dest_sessions,
        served,
        origin_uic=origin_uic,
        dest_uic=dest_uic,
        when=when,
        mct_seconds=mct_seconds,
    )
    return dedup_and_rank(stitches, existing_fingerprints=existing_fingerprints)
