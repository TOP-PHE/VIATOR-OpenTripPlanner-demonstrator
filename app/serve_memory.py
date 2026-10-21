"""Measured memory of the OTP serve containers, for the Sessions page (issue #384).

The page shows each OTP serve session's heap and cap next to what its
container really uses, so an oversized heap is visible (a router using a
few GB under a cap of fifty). The measure is the figure the "Container
memory" panel of the Resources dashboard draws, cAdvisor's
`container_memory_working_set_bytes`, read from Prometheus, which web already
reaches on the compose network (no host port, no credentials, no new mount).
It is the PEAK of the running container over the last day, or since it
started when that is shorter: a router that gives memory back when idle
(OTP_SERVE_GC_MODE=g1-uncommit) shows its lowest use at a quiet moment, and
a heap sized on that would exit under load (-XX:+ExitOnOutOfMemoryError).

Load: two fixed PromQL queries, sent together, at most once a minute for the
whole web process, whoever asks and however many sessions there are;
concurrent askers wait for the same answer (the /api/sessions flood of #380).
The whole fetch has a FETCH_LIMIT_SECONDS limit and a failure is cached for
the same minute, so a Prometheus that is down or stalled costs at most that
once a minute, and the page then shows heap and cap only. The queries are
sent in a POST body, never in a URL, so no query string reaches a log or a
span.
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
TIMEOUT_SECONDS = 3.0  # per request
FETCH_LIMIT_SECONDS = 4.0  # both queries together, so the lock is never held longer

# The compose service of a session's OTP serve container is `otp-<sid>`
# (app/sessions_orchestrator.py); cAdvisor exports that label because it is
# whitelisted (docker-compose.yml, cadvisor `--whitelisted_container_labels`).
# `otp-build` is the build service, not a router. `and on (id)` keeps the
# series of the containers running now: a container recreated within the day
# has a new id, so its peak is "since it started", and the previous
# container's series (still in the 1-day range) is left out. Min for the
# uptime: the newest container of the service.
_SERVICE = 'container_label_com_docker_compose_service=~"otp-.+",container_label_com_docker_compose_service!="otp-build"'
_BY = "by (container_label_com_docker_compose_service)"
_WORKING_SET = f"container_memory_working_set_bytes{{{_SERVICE}}}"
PEAK_QUERY = f"max {_BY} (max_over_time({_WORKING_SET}[1d]) and on (id) {_WORKING_SET})"
UPTIME_QUERY = f"min {_BY} (time() - container_start_time_seconds{{{_SERVICE}}})"

_cache: tuple[float, dict[str, dict[str, float]] | None] | None = None
_lock = asyncio.Lock()


def _vector(body: Any) -> dict[str, float]:
    """`{sid: value}` from a Prometheus instant-vector answer; ValueError if it is not one."""
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("Prometheus did not answer success")
    out: dict[str, float] = {}
    for row in body["data"]["result"]:
        if not isinstance(row, dict) or not isinstance(row.get("metric"), dict):
            raise ValueError("Prometheus answered an unexpected shape")
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
    """`{sid: {"peak_bytes": .., "uptime_seconds": ..}}` for every OTP serve container."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        peak, uptime = await asyncio.gather(
            _query(client, PEAK_QUERY), _query(client, UPTIME_QUERY)
        )
    measured: dict[str, dict[str, float]] = {}
    for sid, peak_bytes in peak.items():
        measured[sid] = {"peak_bytes": peak_bytes}
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
            measured = await asyncio.wait_for(_fetch(), FETCH_LIMIT_SECONDS)
        except (
            TimeoutError,
            httpx.HTTPError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
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
