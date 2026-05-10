"""Unit tests for app.tracing — audit-2026-05 #19.

Pure environment-variable based behaviour tests. No actual OTLP exporter
gets wired up because we deliberately keep `OTEL_EXPORTER_OTLP_ENDPOINT`
unset during tests — otherwise the SDK would try to dial `tempo:4317`
from every test, polluting test output with connection errors and
slowing the suite down via the retry-backoff.

The behaviour we test here:
  - `setup_tracing()` is a no-op when the OTLP endpoint env var is
    unset (the deliberate test-mode short-circuit).
  - `instrument_fastapi_app()` is a no-op when tracing is disabled
    (so test client setup doesn't double-wrap or fail).
  - `instrument_sqlalchemy_engine()` is a no-op when tracing is
    disabled (same reason).

These are the contract guarantees that keep the test suite fast +
reliable. The actual "spans are emitted on real requests" behaviour
is covered by integration tests that hit a running Tempo, which run
on the deployed VPS — out of scope for unit tests.
"""

from __future__ import annotations

import pytest

from app.tracing import (
    instrument_fastapi_app,
    instrument_sqlalchemy_engine,
    setup_tracing,
)


def test_setup_tracing_is_noop_when_otlp_endpoint_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the env var, the function should short-circuit before
    importing any of the heavy OTel modules. No exception, no side
    effects observable from the caller's perspective."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Should not raise.
    setup_tracing(service_name="viator-test")


def test_setup_tracing_is_noop_when_otlp_endpoint_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-string env var (e.g. an operator who set OTEL_EXPORTER_OTLP_ENDPOINT=
    in .env to disable) should be treated identically to unset."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    setup_tracing(service_name="viator-test")


def test_setup_tracing_strips_whitespace_around_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: an operator who accidentally types `OTEL_..._ENDPOINT=   `
    (just whitespace) should get the same short-circuit. Otherwise the
    SDK would try to connect to "   " and crash on URL parsing."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    setup_tracing(service_name="viator-test")


def test_instrument_fastapi_app_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tracing isn't configured, instrumenting the FastAPI app must
    not raise. A test that builds a TestClient without setting the env
    var should be unaffected by the call."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Pass a sentinel — the function shouldn't even touch it when disabled.
    instrument_fastapi_app(object())


def test_instrument_sqlalchemy_engine_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above for the SQLAlchemy engine."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    instrument_sqlalchemy_engine(object())
