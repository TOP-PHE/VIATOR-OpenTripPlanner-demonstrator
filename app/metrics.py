"""Prometheus metrics — observability triad part 2 (audit #14).

Three pieces:

1. **HTTP metrics middleware** (`PrometheusHttpMiddleware`) — records
   request count + latency histogram per route + method + status. Uses
   the matched route's *path template* (e.g. ``/api/sessions/{sid}``)
   rather than the rendered URL, so per-id values don't blow up
   cardinality. Excluded paths (``/metrics``, ``/healthz*``, ``/static/*``)
   keep meta-traffic out of latency-percentile dashboards.

2. **DB-derived gauges** (custom collector) — queue depth, active
   sessions, lifetime rebuild counts. Computed at scrape time from
   small indexed tables; cost is a few ms per scrape at demonstrator
   scale.

3. **`/metrics` endpoint** — emits both in Prometheus exposition format.

We deliberately use `prometheus_client` directly rather than
`prometheus-fastapi-instrumentator`: PFI 7.x pins ``starlette<1.0.0``
which collides with our ``starlette==1.0.0`` (CVE-2025-62727 floor).
The middleware below is ~30 LOC and matches the subset of PFI behaviour
we actually need.

Worker-side build-duration histograms are intentionally out of scope here
(filed as Phase 1.5 follow-up). The worker runs in its own container, so
its in-process metrics aren't visible from web's `/metrics`. Adding
multiprocess shared-filesystem metric storage is worth it once operators
ask for build-duration distributions, not before.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# DB imports are deferred to inside the collector's `collect()` method.
# Importing app.db at module-load time triggers SQLAlchemy `create_engine()`,
# which in turn imports psycopg and (on Windows without the VC++ Redistributable)
# fails to load libpq — making this module un-importable in unit-test contexts
# that don't need a real DB. Inside collect() the import is paid only on the
# first /metrics scrape, which is fine.

if TYPE_CHECKING:
    from fastapi import FastAPI
    from prometheus_client.core import Metric


# ─── HTTP metrics ──────────────────────────────────────────────────────────

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Count of HTTP requests, labelled by method, route template, and status.",
    labelnames=("method", "handler", "status"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, labelled by method and route template. "
    "Buckets cover sub-millisecond up to 30s — the OTP-search outliers are above.",
    labelnames=("method", "handler"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# These regexes match the route templates we want to keep out of the HTTP
# histograms. Compiled once. Match against the matched-route's `path`
# attribute (e.g. ``/metrics``, ``/healthz/version``, ``/static/branding/x.svg``).
_EXCLUDED_HANDLER_PATTERNS = [
    re.compile(r"^/metrics/?$"),
    re.compile(r"^/healthz(/.*)?$"),
    re.compile(r"^/static(/.*)?$"),
]


def _is_excluded_handler(handler: str) -> bool:
    return any(p.match(handler) for p in _EXCLUDED_HANDLER_PATTERNS)


class PrometheusHttpMiddleware(BaseHTTPMiddleware):
    """Records request count + latency for every non-excluded route.

    Uses the matched-route path template (not the rendered URL) for the
    `handler` label — keeps cardinality bounded as session ids and
    other path parameters proliferate.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        # Resolve the matched route's path template. If the request didn't
        # match any route (404 on an unknown URL), bucket as "<unmatched>"
        # so a flood of probe traffic doesn't create a unique handler
        # label per garbage URL.
        route = request.scope.get("route")
        handler = getattr(route, "path", "<unmatched>") if route else "<unmatched>"
        if _is_excluded_handler(handler):
            return response

        method = request.method
        status = str(response.status_code)
        HTTP_REQUESTS_TOTAL.labels(method=method, handler=handler, status=status).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, handler=handler).observe(elapsed)
        return response


# ─── DB-derived gauges (queried at scrape time) ────────────────────────────


class _ViatorDbCollector(Collector):
    """Custom collector that queries the DB on each Prometheus scrape.

    Emits four gauges. All are lightweight COUNT(*) queries against
    indexed columns, so total scrape cost is on the order of a few ms at
    demonstrator scale.

    DB failures are caught and the metric line is omitted for that scrape
    — a single bad scrape shouldn't crash the whole `/metrics` response,
    which would also hide the working HTTP metrics.

    `session_factory` is None in production (the collector lazy-imports
    `app.db.SessionLocal` on first scrape) and an explicit callable in
    tests (lets unit tests inject a mocked factory without forcing a real
    DB-driver import at module load).
    """

    def __init__(self, *, session_factory: Callable[[], Any] | None = None) -> None:
        super().__init__()
        self._session_factory = session_factory

    def describe(self) -> Iterable[Metric]:
        """Return metric metadata WITHOUT touching the DB.

        prometheus_client's REGISTRY.register() calls describe() to discover
        what metric names this collector emits. If describe() isn't defined,
        register() falls back to invoking collect() — which would exhaust a
        mocked side_effect in tests, AND issue a real DB query at registration
        time in production. Implementing describe() avoids both.
        """
        yield GaugeMetricFamily("viator_rebuild_queue_depth", "")
        yield GaugeMetricFamily("viator_active_sessions_total", "")
        yield GaugeMetricFamily("viator_rebuilds_total", "")
        yield GaugeMetricFamily("viator_rebuilds_failed_total", "")

    def collect(self) -> Iterable[Metric]:
        # Lazy import — see module-level rationale.
        from sqlalchemy import func, select

        if self._session_factory is None:
            from .db import SessionLocal as _SessionLocal

            session_factory: Callable[[], Any] = _SessionLocal
        else:
            session_factory = self._session_factory

        from .models import RebuildJob
        from .models import Session as SessionRow
        from .models.sessions import SessionState

        try:
            with session_factory() as db:
                queue_depth = (
                    db.execute(
                        select(func.count())
                        .select_from(RebuildJob)
                        .where(RebuildJob.status.in_(["pending", "running"]))
                    ).scalar()
                    or 0
                )

                active_sessions = (
                    db.execute(
                        select(func.count())
                        .select_from(SessionRow)
                        .where(SessionRow.state == SessionState.SERVING.value)
                    ).scalar()
                    or 0
                )

                rebuilds_total = (
                    db.execute(select(func.count()).select_from(RebuildJob)).scalar() or 0
                )

                rebuilds_failed_total = (
                    db.execute(
                        select(func.count())
                        .select_from(RebuildJob)
                        .where(RebuildJob.status == "failed")
                    ).scalar()
                    or 0
                )
        except Exception:
            # Defensive: don't let a DB hiccup nuke the whole /metrics
            # response. Returning an empty iterator means HTTP metrics
            # still emit, and the next scrape retries.
            return

        yield GaugeMetricFamily(
            "viator_rebuild_queue_depth",
            "Number of rebuild_jobs in pending or running state.",
            value=queue_depth,
        )
        yield GaugeMetricFamily(
            "viator_active_sessions_total",
            "Number of sessions in 'serving' state.",
            value=active_sessions,
        )
        yield GaugeMetricFamily(
            "viator_rebuilds_total",
            "Lifetime count of rebuild_jobs rows. Use rate() in PromQL "
            "for builds-per-time-window. Resets only if the DB is wiped.",
            value=rebuilds_total,
        )
        yield GaugeMetricFamily(
            "viator_rebuilds_failed_total",
            "Lifetime count of rebuild_jobs rows with status='failed'.",
            value=rebuilds_failed_total,
        )


def setup_metrics(
    app: FastAPI,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> None:
    """Register HTTP middleware + DB collector + /metrics endpoint.

    Idempotent on the route addition (second call on the same app no-ops
    the route). DB collector registration is also idempotent — duplicate
    registrations are detected by walking the global REGISTRY.

    Call once at app startup, after other middleware are registered so
    request_id (audit #13) is bound before the metrics middleware fires.

    `session_factory`: tests pass an explicit factory to avoid importing
    `app.db` (which triggers the SQLAlchemy engine + psycopg DLL load).
    Production callers omit it; the collector lazy-imports at first scrape.
    """
    app.add_middleware(PrometheusHttpMiddleware)

    # Register the DB collector only if no _ViatorDbCollector is already
    # in the global REGISTRY. Guards against double-registration when
    # multiple FastAPI apps are constructed in the same process (rare in
    # production, common in tests).
    if not any(isinstance(c, _ViatorDbCollector) for c in REGISTRY._collector_to_names):
        REGISTRY.register(_ViatorDbCollector(session_factory=session_factory))

    # Add the /metrics endpoint only if it doesn't already exist (so a
    # second setup_metrics() in tests is a no-op for the route).
    if not any(getattr(r, "path", None) == "/metrics" for r in app.router.routes):

        @app.get("/metrics", include_in_schema=False)
        def metrics() -> Response:
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
