"""No query string, token path or header value leaves VIATOR in a span.

Real FastAPI and httpx instrumentations, real SDK spans, exported through
`RedactingSpanExporter` into an in-memory exporter; every string the
exporter receives is searched for the invented secrets.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from app.tracing import RedactingSpanExporter, redact_span

_SECRETS = ("ZZSECRET99", "Zzville", "ZZ99", "ZZTOKEN99", "ZZPASS99")


def _all_strings(span: ReadableSpan) -> str:
    """Everything an exporter could send for `span`, as one string."""
    parts: list[Any] = [span.name, span.status.description, dict(span.attributes or {})]
    for event in span.events:
        parts += [event.name, dict(event.attributes or {})]
    for link in span.links:
        parts.append(dict(link.attributes or {}))
    return json.dumps(parts, default=str)


def _assert_clean(spans: tuple[ReadableSpan, ...]) -> str:
    assert spans
    blob = "\n".join(_all_strings(s) for s in spans)
    for secret in _SECRETS:
        assert secret not in blob, secret
    return blob


@pytest.fixture
def provider() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    memory = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(memory)))
    yield tp, memory
    tp.shutdown()


def _feed_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/missing.zip":
        return httpx.Response(404, request=request)
    return httpx.Response(200, json={"features": []}, request=request)


def _app(tp: TracerProvider) -> FastAPI:
    app = FastAPI()

    @app.get("/api/geocode")
    def geocode(q: str) -> dict[str, str]:
        with httpx.Client(transport=httpx.MockTransport(_feed_transport)) as client:
            HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tp)
            client.get("https://motis.zz.invalid/api/v1/geocode", params={"text": q})
        return {"ok": "yes"}

    @app.get("/refresh")
    def refresh() -> dict[str, str]:
        with httpx.Client(transport=httpx.MockTransport(_feed_transport)) as client:
            HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tp)
            client.get("https://feeds.zz.invalid/missing.zip?apikey=ZZSECRET99").raise_for_status()
        return {"ok": "no"}

    @app.get("/confirm/{token}")
    def confirm(token: str) -> dict[str, int]:
        return {"n": len(token)}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tp)
    return app


def test_inbound_request_and_outbound_httpx_call_carry_no_typed_text(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, memory = provider
    client = TestClient(_app(tp))
    assert client.get("/api/geocode", params={"q": "Zzville", "size": 5}).status_code == 200
    spans = memory.get_finished_spans()
    blob = _assert_clean(spans)
    kinds = {s.kind.name for s in spans}
    assert {"SERVER", "CLIENT"} <= kinds
    # The redaction kept the URLs readable: path and the allow-listed size.
    assert "/api/geocode?q=REDACTED&size=5" in blob
    assert "https://motis.zz.invalid/api/v1/geocode?text=REDACTED" in blob


def test_a_failing_outbound_feed_download_leaves_no_key_in_any_span(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """The httpx error's message (`… for url 'https://…?apikey=…'`) lands in
    the server span's exception event and stack trace: redacted too."""
    tp, memory = provider
    client = TestClient(_app(tp), raise_server_exceptions=False)
    assert client.get("/refresh?code=ZZ99&state=ZZ99").status_code == 500
    spans = memory.get_finished_spans()
    blob = _assert_clean(spans)
    assert "exception" in blob
    assert "missing.zip?apikey=REDACTED" in blob


def test_a_token_path_is_redacted_in_the_server_span(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, memory = provider
    assert TestClient(_app(tp)).get("/confirm/ZZTOKEN99").status_code == 200
    blob = _assert_clean(memory.get_finished_spans())
    assert "/confirm/REDACTED" in blob


def test_redact_span_covers_name_status_events_links_and_headers(
    provider: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    tp, memory = provider
    tracer = tp.get_tracer("zz")
    with tracer.start_as_current_span("other") as other:
        other_context = other.get_span_context()
    with tracer.start_as_current_span(
        "GET /x?apikey=ZZSECRET99",
        links=[],
    ) as span:
        span.add_link(other_context, {"from": "https://h.zz.invalid/?code=ZZ99"})
        span.set_attribute("url.full", "https://h.zz.invalid/f?token=ZZ99")
        span.set_attribute("url.query", "q=Zzville&limit=3")
        # A raw space ends a URL for redact_text; a URL attribute is redacted whole.
        span.set_attribute("http.url", "https://h.zz.invalid/g?text=Zz ZZ99&size=2")
        span.set_attribute("http.target", "/reset/ZZTOKEN99")
        span.set_attribute("http.request.header.authorization", ("Bearer ZZSECRET99",))
        span.set_attribute("http.response.header.set_cookie", "zz=ZZ99")
        span.set_attribute("db.statement", "SELECT 1 WHERE a = %(a)s")
        span.set_attribute("zz.count", 3)
        span.set_attribute("zz.list", ["see https://h.zz.invalid/?key=ZZ99", "plain"])
        span.add_event("note", {"message": "fetch /x?password=ZZPASS99 failed"})
        span.set_status(Status(StatusCode.ERROR, "for url 'https://h.zz.invalid/?q=Zzville'"))
    spans = memory.get_finished_spans()
    _assert_clean(spans)
    exported = spans[-1]
    attrs = dict(exported.attributes or {})
    assert attrs["url.query"] == "q=REDACTED&limit=3"
    assert attrs["http.url"] == "https://h.zz.invalid/g?text=REDACTED&size=2"
    assert attrs["http.target"] == "/reset/REDACTED"
    assert attrs["http.request.header.authorization"] == ("REDACTED",)
    assert attrs["http.response.header.set_cookie"] == "REDACTED"
    assert attrs["db.statement"] == "SELECT 1 WHERE a = %(a)s"
    assert attrs["zz.count"] == 3
    assert attrs["zz.list"] == ("see https://h.zz.invalid/?key=REDACTED", "plain")
    assert exported.status.status_code is StatusCode.ERROR
    assert exported.name == "GET /x?apikey=REDACTED"
    assert exported.context == spans[-1].context


def test_redact_span_keeps_identity_and_timing() -> None:
    tp = TracerProvider()
    span = tp.get_tracer("zz").start_span("plain", attributes={"a": "b"})
    span.end()
    copy = redact_span(span)
    assert copy.context == span.context
    assert copy.start_time == span.start_time
    assert copy.end_time == span.end_time
    assert copy.kind == span.kind
    assert copy.resource == span.resource
    assert copy.instrumentation_scope == span.instrumentation_scope
    assert dict(copy.attributes or {}) == {"a": "b"}


class _Recorder(InMemorySpanExporter):
    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[int] = []

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed.append(timeout_millis)
        return True


def test_the_wrapper_forwards_flush_and_shutdown() -> None:
    inner = _Recorder()
    wrapper = RedactingSpanExporter(inner)
    assert wrapper.force_flush(123) is True
    assert inner.flushed == [123]
    wrapper.shutdown()
    tp = TracerProvider()
    span = tp.get_tracer("zz").start_span("x")
    span.end()
    # A shut-down in-memory exporter refuses new spans: the call reached it.
    assert wrapper.export([span]).name == "FAILURE"


def test_setup_tracing_puts_the_wrapper_in_front_of_the_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup_tracing itself, with the OTLP exporter, the global provider and
    the global instrumentations replaced by recorders."""
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as otlp
    import opentelemetry.trace as trace_api
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    from app import tracing

    received = InMemorySpanExporter()
    monkeypatch.setattr(otlp, "OTLPSpanExporter", lambda **_kw: received)
    providers: list[TracerProvider] = []
    monkeypatch.setattr(trace_api, "set_tracer_provider", providers.append)
    monkeypatch.setattr(HTTPXClientInstrumentor, "instrument", lambda *_a, **_k: None)
    monkeypatch.setattr(LoggingInstrumentor, "instrument", lambda *_a, **_k: None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo.zz.invalid:4317")

    tracing.setup_tracing(service_name="viator-test")

    (tp,) = providers
    with tp.get_tracer("zz").start_as_current_span("GET") as span:
        span.set_attribute("http.url", "https://h.zz.invalid/?apikey=ZZSECRET99")
    tp.force_flush()
    tp.shutdown()
    spans = received.get_finished_spans()
    _assert_clean(spans)
    assert dict(spans[0].attributes or {})["http.url"] == "https://h.zz.invalid/?apikey=REDACTED"
