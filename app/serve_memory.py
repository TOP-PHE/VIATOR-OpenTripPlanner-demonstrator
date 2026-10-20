"""Measured memory of the OTP serve containers, for the Sessions page (issue #384).

The page shows each OTP serve session's heap and cap next to what its
container really uses, so an oversized heap is visible (a router using a
few GB under a cap of fifty). The measure is the one the "Container memory"
panel of the Resources dashboard draws: cAdvisor's
`container_memory_working_set_bytes`, read from Prometheus, which web already
reaches on the compose network (no host port, no credentials, no new mount).

Load: two fixed PromQL queries at most once a minute for the whole web
process, whoever asks and however many sessions there are; concurrent askers
wait for the same answer (the /api/sessions flood of #380). A failure is
cached for the same minute, so a Prometheus that is down costs one timeout a
minute, and the page then shows heap and cap only. The queries are sent in a
POST body, never in a URL, so no query string reaches a log or a span.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .settings import settings

log = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 3.0

# The compose service of a session's OTP serve container is `otp-<sid>`
# (app/sessions_orchestrator.py); cAdvisor exports that label because it is
# whitelisted (docker-compose.yml, cadvisor `--whitelisted_container_labels`).
# `otp-build` is the build service, not a router. A recreated container leaves
# its old series for a few minutes: max for the use, min for the uptime.
_SERVICE = 'container_label_com_docker_compose_service=~"otp-.+",container_label_com_docker_compose_service!="otp-build"'
_BY = "by (container_label_com_docker_compose_service)"
USED_QUERY = f"max {_BY} (container_memory_working_set_bytes{{{_SERVICE}}})"
UPTIME_QUERY = f"min {_BY} (time() - container_start_time_seconds{{{_SERVICE}}})"

_cache: tuple[float, dict[str, dict[str, float]] | None] | None = None
_lock = asyncio.Lock()


def _vector(body: Any) -> dict[str, float]:
    """`{sid: value}` from a Prometheus instant-vector answer; ValueError if it is not one."""
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("Prometheus did not answer success")
    out: dict[str, float] = {}
    for row in body["data"]["result"]:
        service = row["metric"].get("container_label_com_docker_compose_service", "")
        if service.startswith("otp-"):
            out[service.removeprefix("otp-")] = float(row["value"][1])
    return out


async def _query(client: httpx.AsyncClient, query: str) -> dict[str, float]:
    response = await client.post(
        f"{settings.prometheus_url.rstrip('/')}/api/v1/query", data={"query": query}
    )
    response.raise_for_status()
    return _vector(response.json())


async def _fetch(transport: httpx.AsyncBaseTransport | None = None) -> dict[str, dict[str, float]]:
    """`{sid: {"used_bytes": .., "uptime_seconds": ..}}` for every OTP serve container."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        used = await _query(client, USED_QUERY)
        uptime = await _query(client, UPTIME_QUERY)
    measured: dict[str, dict[str, float]] = {}
    for sid, used_bytes in used.items():
        measured[sid] = {"used_bytes": used_bytes}
        if sid in uptime:
            measured[sid]["uptime_seconds"] = uptime[sid]
    return measured


async def measured_by_session() -> dict[str, dict[str, float]] | None:
    """The cached measure, refreshed at most once a minute; None when it is not available."""
    global _cache
    if not settings.prometheus_url:
        return None
    async with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        measured: dict[str, dict[str, float]] | None
        try:
            measured = await _fetch()
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            # The page still shows heap and cap; one line a minute at most.
            log.warning("serve memory: Prometheus not available (%s)", type(exc).__name__)
            measured = None
        _cache = (now, measured)
        return measured


def reset_cache() -> None:
    """Forget the cached measure (tests)."""
    global _cache, _lock
    _cache = None
    _lock = asyncio.Lock()
