"""Per-session router-config.json generation (v0.1.7, credentials in v0.1.10).

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

**Credentials (v0.1.10):** when a provider declares `gtfs_rt_credential_id`,
the caller passes a `credentials` mapping (id → (auth_type, plaintext,
param_name)) and we apply it via `app.credentials.apply_to_request` to each
URL. For `bearer/basic/header` auth, the resulting headers go in OTP's
`headers` dict on the updater. For `query` auth, the param is appended to
the URL — OTP fetches the auth-stamped URL transparently.

This module stays pure (no DB, no crypto). The worker resolves DB +
decrypts and passes the materialised mapping in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .credentials import AuthType, apply_to_request

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


# Type alias for the credentials map the caller passes in.
# Key:    credential_id (UUID as str, matches what's in provider config).
# Value:  (auth_type, plaintext, param_name_or_None).
# The caller is responsible for decryption; we just apply.
ResolvedCredentials = Mapping[str, tuple[AuthType, str, str | None]]


def _apply_url_auth(
    url: str,
    credential_id: str | None,
    credentials: ResolvedCredentials | None,
) -> tuple[str, dict[str, str]]:
    """Return (final_url, headers) after applying optional credential.

    If credential_id is None or not in the map (e.g. credential was
    deleted between save and config-render), we silently fall back to
    the bare URL with no headers. The refresh path will surface the
    "credential not found" error separately.
    """
    if not credential_id or not credentials or credential_id not in credentials:
        return url, {}
    auth_type, plaintext, param_name = credentials[credential_id]
    if auth_type == "none":
        return url, {}
    return apply_to_request(
        url,
        auth_type=auth_type,
        plaintext=plaintext,
        param_name=param_name,
    )


def render_router_config(
    providers: list[dict[str, Any]],
    *,
    credentials: ResolvedCredentials | None = None,
) -> str:
    """Build a router-config.json document for one session.

    `providers` is the canonical list returned by
    `app.ingestion.normalize_providers()`. Provider entries without any
    GTFS-RT URLs contribute zero updaters; the order of updaters in the
    output mirrors the operator-declared provider order.

    `credentials` (v0.1.10) is optional. When provided, GTFS-RT updaters
    whose provider declares `gtfs_rt_credential_id` get the credential
    applied — query-style → URL gets the param appended; header/bearer/
    basic → an OTP `headers` dict is emitted on the updater entry.
    """
    updaters = []
    for p in providers:
        feed_id = p.get("id")
        rt = p.get("gtfs_rt") or {}
        if not feed_id or not isinstance(rt, dict):
            continue
        cred_id = p.get("gtfs_rt_credential_id")

        for url_key, otp_type in (
            ("alerts_url", "real-time-alerts"),
            ("trip_updates_url", "stop-time-updater"),
            ("vehicle_positions_url", "vehicle-positions"),
        ):
            url = rt.get(url_key)
            if not url:
                continue
            final_url, headers = _apply_url_auth(url, cred_id, credentials)
            entry: dict[str, Any] = {
                "type": otp_type,
                "feedId": feed_id,
                "url": final_url,
                "frequency": "1m",
            }
            # OTP 2.x accepts `headers` on real-time updaters. Omit the
            # key entirely when there are none (some OTP versions warn
            # on empty objects).
            if headers:
                entry["headers"] = headers
            updaters.append(entry)

    config: dict[str, Any] = {
        "server": _DEFAULT_SERVER,
        "routingDefaults": _DEFAULT_ROUTING_DEFAULTS,
    }
    # Only include the updaters key when we have at least one — keeps
    # the file small and avoids OTP noise about empty arrays.
    if updaters:
        config["updaters"] = updaters
    return json.dumps(config, indent=2, sort_keys=False)
