#!/usr/bin/env python3
"""Side-by-side OTP vs MOTIS for the same query.

Usage from the repo root (so the `app` package imports):

    python motis-spike/compare.py \\
        --otp-url    http://localhost:8080 \\
        --motis-url  http://localhost:8081 \\
        --from       48.844,2.374 \\
        --to         43.295,5.376 \\
        --when       2026-06-01T08:00:00Z

Drives `app.journey.otp_client.fetch_plan` and `app.journey.motis_client.
fetch_plan` with identical inputs and prints each engine's top itineraries
plus query latency. OTP's per-session DNS resolver (`_otp_base`) is
monkey-patched for the duration of the call so the harness can point at any
reachable OTP container without touching production code.

What "agreement" looks like here: for each engine, top-3 itineraries are
printed with their leg spine (mode · dep -> arr · from -> to). Eyeball
whether they pick the same trains; the dispatcher in Phase 1 only needs
the response *shape* to match — quality divergence is a separate question
the spike is designed to surface.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Allow `python motis-spike/compare.py …` from the repo root: when Python runs
# a script, sys.path[0] is the script's directory, NOT the CWD — so `app.*`
# isn't importable without a small bootstrap. Insert the repo root (one level
# above this file) at the front so the in-repo packages resolve cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.journey import motis_client, otp_client


def _parse_latlon(s: str) -> tuple[float, float]:
    lat, lon = s.split(",", 1)
    return float(lat), float(lon)


def _parse_when(s: str) -> datetime:
    # Accept '...Z' as a synonym for '+00:00'; the datetime parser pre-3.11
    # was strict about it, the workaround is harmless on modern Pythons.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def _query(engine_name, coro):
    """Run `coro` and measure latency; return (engine_name, ms, raw, trips, error)."""
    t0 = time.monotonic()
    try:
        raw, trips = await coro
        ms = int((time.monotonic() - t0) * 1000)
        return engine_name, ms, raw, trips, None
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return engine_name, ms, None, None, f"{type(exc).__name__}: {exc}"


def _short(s, n=40):
    """Truncate long names for tabular display."""
    if s is None:
        return "-"
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_trips(engine_name: str, latency_ms: int, trips, error: str | None, limit: int = 3):
    print(f"\n=== {engine_name}  ({latency_ms} ms) ===")
    if error:
        print(f"  ERROR: {error}")
        return
    if not trips:
        print("  (no itineraries)")
        return
    for i, trip in enumerate(trips[:limit], start=1):
        dep = trip.get("departure_at", "?")
        arr = trip.get("arrival_at", "?")
        dur = trip.get("duration_seconds", 0)
        xfers = trip.get("num_transfers", 0)
        modes = trip.get("modes", "")
        h, m = divmod(dur // 60, 60)
        print(f"  [{i}] {dep} -> {arr}  ·  {h}h{m:02d}  ·  {xfers} change(s)  ·  {modes}")
        for leg in trip.get("legs", []) or []:
            mode = _short(leg.get("mode"), 12)
            ldep = _short(leg.get("departure"), 25)
            larr = _short(leg.get("arrival"), 25)
            frm = _short(leg.get("from_name"), 35)
            to_ = _short(leg.get("to_name"), 35)
            route = leg.get("route_short_name")
            tag = f"[{_short(route, 8)}]" if route else ""
            print(f"        {mode:<6} {ldep} -> {larr}  {frm} -> {to_}  {tag}")


def _summary(rows):
    """Cross-engine peek: same number of trips? overlap of route_short_names on the top trip?"""
    by_engine = {r[0]: r for r in rows}
    otp = by_engine.get("OTP")
    motis = by_engine.get("MOTIS")
    if not otp or not motis:
        return
    otp_trips = otp[3] or []
    motis_trips = motis[3] or []
    if not (otp_trips and motis_trips):
        return
    otp_routes = {
        lg.get("route_short_name")
        for lg in (otp_trips[0].get("legs") or [])
        if lg.get("route_short_name")
    }
    motis_routes = {
        lg.get("route_short_name")
        for lg in (motis_trips[0].get("legs") or [])
        if lg.get("route_short_name")
    }
    overlap = otp_routes & motis_routes
    print("\n--- agreement (top itinerary) ---")
    print(f"  OTP routes:   {sorted(otp_routes) or '-'}")
    print(f"  MOTIS routes: {sorted(motis_routes) or '-'}")
    print(f"  overlap:      {sorted(overlap) or '-'}")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OTP vs MOTIS side-by-side")
    ap.add_argument(
        "--otp-url", required=True, help="Base URL of an OTP container, e.g. http://localhost:8080"
    )
    ap.add_argument(
        "--motis-url", required=True, help="Base URL of the MOTIS spike, e.g. http://localhost:8081"
    )
    ap.add_argument("--from", dest="from_", required=True, metavar="LAT,LON", type=_parse_latlon)
    ap.add_argument("--to", required=True, metavar="LAT,LON", type=_parse_latlon)
    ap.add_argument(
        "--when",
        required=True,
        type=_parse_when,
        help="ISO-8601 departure time (UTC, e.g. 2026-06-01T08:00:00Z)",
    )
    ap.add_argument("--num", type=int, default=5, help="Itineraries per engine (default: 5)")
    ap.add_argument(
        "--window", type=int, default=21600, help="Search window in seconds (default: 21600 = 6h)"
    )
    ap.add_argument(
        "--timeout-ms",
        type=int,
        default=30_000,
        help="Per-engine HTTP timeout in ms (default: 30000)",
    )
    args = ap.parse_args(argv)

    f_lat, f_lon = args.from_
    t_lat, t_lon = args.to

    # Point the OTP client at the URL the user passed in. The session id is
    # a dummy here — it's only used as a DNS placeholder. Production callers
    # supply a real session_id and let `_otp_base` resolve via docker DNS.
    original_otp_base = otp_client._otp_base
    otp_client._otp_base = lambda _sid: args.otp_url.rstrip("/")  # type: ignore[assignment]

    common = {
        "from_lat": f_lat,
        "from_lon": f_lon,
        "to_lat": t_lat,
        "to_lon": t_lon,
        "when": args.when if args.when.tzinfo else args.when.replace(tzinfo=UTC),
        "timeout_ms": args.timeout_ms,
        "num_itineraries": args.num,
        "search_window_seconds": args.window,
    }
    try:
        rows = await asyncio.gather(
            _query("OTP", otp_client.fetch_plan(session_id="spike", **common)),
            _query(
                "MOTIS",
                motis_client.fetch_plan(session_id="spike", base_url=args.motis_url, **common),
            ),
        )
    finally:
        otp_client._otp_base = original_otp_base  # type: ignore[assignment]

    for engine, ms, _raw, trips, err in rows:
        _print_trips(engine, ms, trips, err)
    _summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
