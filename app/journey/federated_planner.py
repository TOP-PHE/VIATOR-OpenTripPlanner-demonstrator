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

The pure helpers (`served_uics`, `connection_hubs`, `assemble_stitch`,
`earliest_next_departure`, `dedup_and_rank`) carry the logic and are unit-tested;
`plan_federated` is the async orchestration that drives per-session OTP queries.
"""

from __future__ import annotations

import logging
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
MAX_HUBS = 6
MAX_LEG1_OPTIONS = 3
MAX_RESULTS = 5


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


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant (otp_client emits `…Z`) to an aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def earliest_next_departure(
    prev_arrival_iso: str, mct_seconds: int = DEFAULT_MCT_SECONDS
) -> datetime:
    """When the onward leg may earliest depart: previous arrival + MCT."""
    return _parse_iso(prev_arrival_iso) + timedelta(seconds=mct_seconds)


def assemble_stitch(
    leg_trips: list[dict[str, Any]],
    *,
    via_hubs: list[str],
    session_ids: list[str],
) -> dict[str, Any]:
    """Join a chain of per-leg trips into a single stitched itinerary.

    Total duration is wall-clock (last arrival minus first departure, so transfer
    wait is counted); transfers = the sum within each leg plus one per stitch.
    """
    first, last = leg_trips[0], leg_trips[-1]
    departure_at = first["departure_at"]
    arrival_at = last["arrival_at"]
    legs = [leg for t in leg_trips for leg in t.get("legs", [])]
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


def dedup_and_rank(
    stitches: list[dict[str, Any]],
    *,
    existing_fingerprints: set[str] | None = None,
    limit: int = MAX_RESULTS,
) -> list[dict[str, Any]]:
    """Rank stitched itineraries (earliest arrival, then shortest, then fewest
    transfers) and drop any that duplicate each other or an itinerary a session
    already returned (by `transit_fingerprint`)."""
    seen = set(existing_fingerprints or set())
    out: list[dict[str, Any]] = []
    for s in sorted(
        stitches,
        key=lambda s: (s["arrival_at"], s["duration_seconds"], s["num_transfers"]),
    ):
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

    from ..models import MasterStation

    served = {s.id: _session_served_uics(s) for s in sessions}
    origin_sessions = [s for s in sessions if origin_uic in served[s.id]]
    dest_sessions = [s for s in sessions if dest_uic in served[s.id]]
    if not origin_sessions or not dest_sessions:
        return []

    # Resolve coordinates once for origin, destination, and all candidate hubs —
    # we query OTP by coordinate (robust across a session's multiple providers,
    # whose feed-id prefixes differ from the hub's home feed).
    candidate_hubs: set[str] = set()
    for so in origin_sessions:
        for sd in dest_sessions:
            if so.id != sd.id:
                candidate_hubs |= connection_hubs(served[so.id], served[sd.id])
    if not candidate_hubs:
        return []

    wanted_uics = {origin_uic, dest_uic} | candidate_hubs
    coords: dict[str, tuple[float, float]] = {}
    for ms in db.query(MasterStation).filter(MasterStation.uic.in_(wanted_uics)).all():
        if ms.latitude is not None and ms.longitude is not None:
            coords[ms.uic] = (ms.latitude, ms.longitude)
    if origin_uic not in coords or dest_uic not in coords:
        return []  # can't query OTP without endpoint coordinates

    from . import otp_client

    tz_for = session_timezone_for or {}

    async def _leg(session: SessionRow, frm: str, to: str, dep: datetime) -> list[dict[str, Any]]:
        if frm not in coords or to not in coords:
            return []
        (flat, flon), (tlat, tlon) = coords[frm], coords[to]
        try:
            _raw, trips = await otp_client.fetch_plan(
                session_id=session.id,
                from_lat=flat,
                from_lon=flon,
                to_lat=tlat,
                to_lon=tlon,
                when=dep,
                timeout_ms=timeout_ms,
                session_timezone=tz_for.get(session.id),
            )
        except Exception as exc:  # network/OTP error on one leg shouldn't 500 the query
            log.warning("federated leg %s %s→%s failed: %s", session.id, frm, to, exc)
            return []
        return trips

    stitches: list[dict[str, Any]] = []
    for so in origin_sessions:
        for sd in dest_sessions:
            if so.id == sd.id:
                continue
            hubs = sorted(connection_hubs(served[so.id], served[sd.id]) & coords.keys())[:MAX_HUBS]
            for hub in hubs:
                leg1 = await _leg(so, origin_uic, hub, when)
                for t1 in leg1[:MAX_LEG1_OPTIONS]:
                    nxt = earliest_next_departure(t1["arrival_at"], mct_seconds)
                    leg2 = await _leg(sd, hub, dest_uic, nxt)
                    if leg2:
                        stitches.append(
                            assemble_stitch(
                                [t1, leg2[0]],
                                via_hubs=[hub],
                                session_ids=[so.id, sd.id],
                            )
                        )

    return dedup_and_rank(stitches, existing_fingerprints=existing_fingerprints)
