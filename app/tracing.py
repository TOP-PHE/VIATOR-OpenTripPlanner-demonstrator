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

No query string leaves in a span (follow-up of #339): every span goes
through `RedactingSpanExporter` before the OTLP exporter, which passes the
URL attributes of the FastAPI (inbound) and httpx (outbound) spans —
`http.url`, `url.full`, `http.target`, `url.query`, … — through
`app.url_redaction`, and every other string (span name, status
description, exception message and stack trace of a span event) through
`redact_text`. Header attributes, which the instrumentations record only
when an `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` variable asks, are
replaced whole. The SQLAlchemy spans carry the statement with its bound
parameters as placeholders, never the values.

Disabled by default in test environments — the OTLP exporter would
try to dial `tempo:4317` from every TestClient call and pollute test
output with connection errors. Enabled when `OTEL_EXPORTER_OTLP_ENDPOINT`
is set in the environment (compose sets it; tests don't).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

from .url_redaction import REDACTED, redact_query, redact_text, redact_url

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

# Attributes whose whole value is a URL, a path with its query, or a query.
_URL_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"http.url", "url.full", "http.target", "url.path", "url.original"}
)
_QUERY_ATTRIBUTES: Final[frozenset[str]] = frozenset({"url.query"})
_HEADER_ATTRIBUTE_PREFIXES: Final[tuple[str, ...]] = (
    "http.request.header.",
    "http.response.header.",
)


def _redact_value(key: str, value: Any) -> Any:
    """One attribute value, made safe to export. Non-strings pass unchanged;
    a sequence of strings is redacted item by item."""
    if isinstance(value, (tuple, list)):
        return tuple(_redact_value(key, v) for v in value)
    if not isinstance(value, str):
        return value
    if key.startswith(_HEADER_ATTRIBUTE_PREFIXES):
        return REDACTED
    if key in _QUERY_ATTRIBUTES:
        return redact_query(value)
    if key in _URL_ATTRIBUTES:
        return redact_url(value)
    return redact_text(value)


def redact_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of span or event `attributes` with every value made safe."""
    return {k: _redact_value(k, v) for k, v in (attributes or {}).items()}


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """A copy of a finished `span` in which no URL query string, token
    path, URL user info or header value survives (app/url_redaction.py)."""
    status = span.status
    if status.description:
        status = Status(status.status_code, redact_text(status.description))
    return ReadableSpan(
        name=redact_text(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_attributes(span.attributes),
        events=[Event(e.name, redact_attributes(e.attributes), e.timestamp) for e in span.events],
        links=[Link(link.context, redact_attributes(link.attributes)) for link in span.links],
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """A SpanExporter that hands `redact_span` copies to the exporter it
    wraps. It sits between the BatchSpanProcessor and the OTLP exporter, so
    whatever an instrumentation records — today's FastAPI, httpx and
    SQLAlchemy spans, or a new one — is redacted on its way out."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([redact_span(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


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

    # Lazy imports — the exporter and the instrumentations are not needed
    # on a test run that doesn't exercise tracing. Importing inside the
    # function keeps test startup snappy. (The SDK's span types, imported
    # at the top, are what RedactingSpanExporter is built from.)
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
    #    Wrapped so that no URL query string, token path or header value
    #    leaves the process (see RedactingSpanExporter).
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter)))

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
