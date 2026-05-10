"""OpenTelemetry distributed tracing — audit-2026-05 #19 (Phase 2.7).

Closes the observability triad alongside structlog (#13, since v0.1.32.9)
and Prometheus + Grafana (#14, Phase 1 in v0.1.32.15, Phase 2 in v0.1.32.16+).
Logs answer "what happened?", metrics answer "how much / how fast?", and
traces answer "what was this single request doing for those 1200ms?".

Why traces matter for VIATOR specifically: a /api/journey/search request
typically fans out to one OTP container, queries Postgres for session +
provider configuration, and serialises a non-trivial response. When p95
latency creeps up, the metrics dashboard tells you THAT it's slow but
not WHERE the time went. With a trace, the operator clicks one slow
request in Grafana and sees the timing breakdown across FastAPI →
SQLAlchemy → OTP HTTP → response serialisation in a single waterfall.

Architecture:
  - OTLP gRPC exporter → tempo:4317 (in-network, no host port binding)
  - Auto-instrumentation for FastAPI (request spans), SQLAlchemy
    (query spans), httpx (outbound HTTP spans). No manual
    `with tracer.start_span(...)` calls in app/ — instrumentation
    wraps the existing call sites transparently.
  - `LoggingInstrumentor` injects trace_id + span_id into stdlib log
    records. Combined with structlog's `add_log_level` + JSON renderer,
    every log line gets a `otelTraceID` / `otelSpanID` field that
    promtail then ingests into Loki. The Grafana Loki datasource is
    pre-wired with a `trace_id` derived field that turns the ID into a
    clickable link → opens the trace in Tempo. One-click pivot from
    log line to full trace.

Sampling:
  - Default: 100% (sample every request). Sized for a single-VPS
    demonstrator with low-RPM traffic — every trace is worth keeping
    for the operator's mental model.
  - Production at higher traffic would want head-based sampling
    (e.g. 10%) configured via OTEL_TRACES_SAMPLER env var. Documented
    in admin-guide §5.5.

Disabled by default in test environments — the OTLP exporter would
try to dial `tempo:4317` from every TestClient call and pollute test
output with connection errors. Enabled when `OTEL_EXPORTER_OTLP_ENDPOINT`
is set in the environment (compose sets it; tests don't).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def setup_tracing(*, service_name: str = "viator-web") -> None:
    """Configure OpenTelemetry: SDK + OTLP exporter + auto-instrumentation.

    Idempotent — calling twice is a no-op (OTel itself guards against
    double-setup, but we also short-circuit early if the OTLP endpoint
    env var isn't set, so test runs don't try to dial a nonexistent
    Tempo).

    Call from `app.main._startup` after the FastAPI app is constructed
    but before any requests are served. The auto-instrumentations need
    to wrap the FastAPI app instance + the SQLAlchemy engine BEFORE
    those start handling work.
    """
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not otlp_endpoint:
        log.info(
            "tracing.disabled",
            extra={"reason": "OTEL_EXPORTER_OTLP_ENDPOINT not set"},
        )
        return

    # Lazy imports — the OTel packages are ~12 MB and not needed on a
    # test run that doesn't exercise tracing. Importing inside the
    # function keeps test startup snappy.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # 1. Tracer provider with a service-name resource. Service name
    #    appears in Tempo as the top-level filter — "viator-web" vs
    #    "viator-worker" vs (future) "viator-otp-build" can be
    #    inspected separately.
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    # 2. OTLP exporter shipping to Tempo over gRPC. `insecure=True`
    #    because tempo:4317 is on the docker bridge network, no TLS.
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    # 3. Inject trace_id + span_id into stdlib log records. Structlog's
    #    `wrap_logger` / `ProcessorFormatter` chain (see app/logging_config.py)
    #    picks these up as extra fields and emits them in the JSON
    #    output → promtail → Loki → clickable in Grafana via the
    #    derived field on the Loki datasource.
    LoggingInstrumentor().instrument(set_logging_format=False)

    # 4. Outbound httpx — wraps every AsyncClient and Client so calls to
    #    OTP / Trainline / NAP endpoints emit child spans of the
    #    incoming request's trace.
    HTTPXClientInstrumentor().instrument()

    log.info(
        "tracing.enabled",
        extra={
            "service_name": service_name,
            "otlp_endpoint": otlp_endpoint,
        },
    )


def instrument_fastapi_app(app: FastAPI | Any) -> None:
    """Wrap a FastAPI app with OTel's request-level instrumentation.

    Called separately from `setup_tracing()` because we need to wait
    until the FastAPI app instance exists. Idempotent + no-op if
    tracing isn't enabled.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # `excluded_urls` keeps the trace volume sane — /metrics is hit by
    # Prometheus every 15s and /healthz/* by docker every few seconds.
    # Neither has interesting trace content; excluding them avoids
    # drowning real-traffic spans in scrape noise.
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/metrics,/healthz.*,/api/auth/proxy-validate",
    )


def instrument_sqlalchemy_engine(engine: Engine | Any) -> None:
    """Wrap a SQLAlchemy engine with query-level instrumentation.

    Called from `app.db` after the engine is created. Each `execute()`
    emits a span with the SQL statement (truncated) + duration. Visible
    in the trace waterfall as child of the FastAPI request span.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return

    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine)
