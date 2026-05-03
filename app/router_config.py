"""Per-session router-config.json generation (v0.1.7).

OTP reads `router-config.json` at graph-load time and uses it to wire up
real-time updaters, routing defaults, and the API server. Pre-v0.1.7 we
shipped a single hardcoded config in the otp image baked with SNCF's
GTFS-RT URLs. With multi-provider sessions (v0.1.6) each session can
have N providers, each with their own GTFS-RT alerts / trip-updates /
vehicle-positions URLs — so the config has to be per-session.

The worker writes a session-specific config to `inbox/<sid>/router-config.json`
before invoking otp-build. The entrypoint copies that config (if present)
into BUILD_DIR for OTP to consume; falls back to the baked one otherwise.

OTP serving (per-session otp-<sid> containers) load the same config when
they read the graph — so the GTFS-RT updaters fire continuously while the
graph is live.

Updater types we generate:
  - real-time-alerts        ← provider.gtfs_rt.alerts_url
  - stop-time-updater       ← provider.gtfs_rt.trip_updates_url
  - vehicle-positions       ← provider.gtfs_rt.vehicle_positions_url

`feedId` on each updater MUST match the provider's id from build-config's
`transitFeeds[i].feedId` so OTP knows which feed the updates apply to.
"""

from __future__ import annotations

import json
from typing import Any

# Defaults baked into every per-session config. Mirrors the static
# `docker/otp/router-config.json` to preserve behaviour for sessions
# without GTFS-RT URLs configured.
_DEFAULT_SERVER = {"apiProcessingTimeout": "10s"}
_DEFAULT_ROUTING_DEFAULTS = {
    "numItineraries": 5,
    "transferSlack": "2m",
    # OTP 2.x: how far OTP will walk to reach the first / leave the last
    # transit stop. Without a tight bound, OTP silently routes to the
    # nearest transit-reachable place when the requested coordinate is
    # far from any transit stop — e.g. asking "Paris → Cagnes-sur-Mer"
    # against a TGV-only feed returns Paris→Marseille trips because
    # Marseille is the closest reachable point and OTP fakes a (huge)
    # walk to the destination. With this bound, OTP refuses the route
    # and our journey API surfaces a clean LOCATION_NOT_FOUND through
    # the routingErrors[] array instead — operators see the gap rather
    # than a misleading partial result.
    #
    # 20 minutes ≈ 1.5 km walking. Covers ~every urban demonstrator
    # (RER stations are dense in Paris, and stations are usually <1 km
    # apart in city centres). Increase per-session via session.config
    # routing overrides if the operator has a rural use case.
    "maxAccessEgressDurationForMode": {
        "WALK": "20m",
    },
}


def render_router_config(providers: list[dict[str, Any]]) -> str:
    """Build a router-config.json document for one session.

    `providers` is the canonical list returned by
    `app.ingestion.normalize_providers()`. Provider entries without any
    GTFS-RT URLs contribute zero updaters; the order of updaters in the
    output mirrors the operator-declared provider order.
    """
    updaters = []
    for p in providers:
        feed_id = p.get("id")
        rt = p.get("gtfs_rt") or {}
        if not feed_id or not isinstance(rt, dict):
            continue
        if rt.get("alerts_url"):
            updaters.append(
                {
                    "type": "real-time-alerts",
                    "feedId": feed_id,
                    "url": rt["alerts_url"],
                    "frequency": "1m",
                }
            )
        if rt.get("trip_updates_url"):
            updaters.append(
                {
                    "type": "stop-time-updater",
                    "feedId": feed_id,
                    "url": rt["trip_updates_url"],
                    "frequency": "1m",
                }
            )
        if rt.get("vehicle_positions_url"):
            updaters.append(
                {
                    "type": "vehicle-positions",
                    "feedId": feed_id,
                    "url": rt["vehicle_positions_url"],
                    "frequency": "1m",
                }
            )

    config: dict[str, Any] = {
        "server": _DEFAULT_SERVER,
        "routingDefaults": _DEFAULT_ROUTING_DEFAULTS,
    }
    # Only include the updaters key when we have at least one — keeps
    # the file small and avoids OTP noise about empty arrays.
    if updaters:
        config["updaters"] = updaters
    return json.dumps(config, indent=2, sort_keys=False)
