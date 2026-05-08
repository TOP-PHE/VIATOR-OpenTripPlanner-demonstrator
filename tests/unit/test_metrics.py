"""Audit #14 — Prometheus metrics.

Two thin layers:

1. The custom DB-derived collector — verified directly with a mocked
   `SessionLocal` so we exercise the SQL-shape and gauge-emission logic
   without standing up Postgres.
2. The instrumentator wiring — verified by calling `setup_metrics()` on a
   fresh FastAPI app and asserting the `/metrics` endpoint responds with
   the Prometheus exposition-format content type.

Worker-side build-duration histograms are deliberately out of scope here
(filed as Phase 1.5 follow-up). See `app/metrics.py` module docstring.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.registry import REGISTRY

from app.metrics import _ViatorDbCollector, setup_metrics


def _mock_session_factory(counts: dict[str, int]) -> Any:
    """Build a fake SessionLocal context manager factory whose
    `.scalar()` calls return successive values from `counts.values()` in
    iteration order.

    The collector issues 4 scalar() reads in this order:
      1. queue_depth
      2. active_sessions
      3. rebuilds_total
      4. rebuilds_failed_total
    """
    expected_keys = ("queue_depth", "active_sessions", "rebuilds_total", "rebuilds_failed_total")
    assert tuple(counts.keys()) == expected_keys, "test fixture mismatch"

    db = MagicMock()
    db.execute.return_value.scalar.side_effect = list(counts.values())

    @contextmanager
    def fake_session_local() -> Any:
        yield db

    return fake_session_local


# ───────────────────────────── DB collector ────────────────────────────────


def test_collector_emits_four_gauges_with_correct_values() -> None:
    counts = {
        "queue_depth": 3,
        "active_sessions": 2,
        "rebuilds_total": 47,
        "rebuilds_failed_total": 5,
    }
    metrics = list(_ViatorDbCollector(session_factory=_mock_session_factory(counts)).collect())

    by_name = {m.name: m for m in metrics}
    assert set(by_name) == {
        "viator_rebuild_queue_depth",
        "viator_active_sessions_total",
        "viator_rebuilds_total",
        "viator_rebuilds_failed_total",
    }
    assert by_name["viator_rebuild_queue_depth"].samples[0].value == 3
    assert by_name["viator_active_sessions_total"].samples[0].value == 2
    assert by_name["viator_rebuilds_total"].samples[0].value == 47
    assert by_name["viator_rebuilds_failed_total"].samples[0].value == 5


def test_collector_handles_zero_rows_cleanly() -> None:
    """Fresh DB → all gauges report 0, none are missing."""
    counts = {
        "queue_depth": 0,
        "active_sessions": 0,
        "rebuilds_total": 0,
        "rebuilds_failed_total": 0,
    }
    metrics = list(_ViatorDbCollector(session_factory=_mock_session_factory(counts)).collect())

    assert len(metrics) == 4
    assert all(m.samples[0].value == 0 for m in metrics)


def test_collector_swallows_db_errors_silently() -> None:
    """A DB hiccup must NOT poison the whole /metrics response — that
    would also hide the working HTTP metrics. Collector returns nothing
    for that scrape; next scrape retries."""

    def broken_session_factory() -> Any:
        msg = "connection refused"
        raise ConnectionError(msg)

    metrics = list(_ViatorDbCollector(session_factory=broken_session_factory).collect())

    assert metrics == []


# ───────────────────────────── instrumentator ──────────────────────────────


@pytest.fixture
def metrics_app() -> Iterator[FastAPI]:
    """Build a tiny FastAPI app and register metrics on it with a mocked
    session factory — keeps the test independent of Postgres.

    Unregisters any pre-existing _ViatorDbCollector before this test runs:
    otherwise side_effect from a prior test's mock factory is exhausted and
    this test's collector silently emits nothing."""
    # Snapshot first, then iterate — REGISTRY.unregister mutates the
    # underlying dict, so a direct iteration would `RuntimeError`.
    stale = [c for c in REGISTRY._collector_to_names if isinstance(c, _ViatorDbCollector)]
    for c in stale:
        REGISTRY.unregister(c)

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "pong"}

    counts = {
        "queue_depth": 1,
        "active_sessions": 4,
        "rebuilds_total": 12,
        "rebuilds_failed_total": 1,
    }
    setup_metrics(app, session_factory=_mock_session_factory(counts))
    yield app


def test_metrics_endpoint_responds_with_prometheus_format(metrics_app: FastAPI) -> None:
    client = TestClient(metrics_app)
    # Hit the app once so HTTP-metric counters have something to report.
    assert client.get("/ping").status_code == 200
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # HTTP-metric (auto-collected) — verifies instrumentator middleware fired.
    assert "http_request" in body
    # Custom DB-derived gauges — verifies collector registered + emitted.
    assert "viator_rebuild_queue_depth" in body
    assert "viator_active_sessions_total" in body
    assert "viator_rebuilds_total" in body
    assert "viator_rebuilds_failed_total" in body


def test_metrics_endpoint_excludes_metric_path_from_its_own_latency_histogram(
    metrics_app: FastAPI,
) -> None:
    """/metrics, /healthz, /static/* are excluded from the HTTP-metric
    histograms — otherwise the meta-paths would inflate p95 dashboards
    and dilute signal from real request work."""
    client = TestClient(metrics_app)
    # Scrape /metrics a few times to give the (excluded) handler chances
    # to be sampled.
    for _ in range(3):
        client.get("/metrics")
    body = client.get("/metrics").text
    # Non-excluded route appears in the HTTP-request histogram bucket lines.
    # (The exact format is `http_request_duration_seconds_bucket{handler="/ping",...}`.)
    # We shouldn't see /metrics labelled as a handler in those buckets.
    bucket_lines = [
        line for line in body.splitlines() if "http_request_duration_seconds_bucket" in line
    ]
    assert all('handler="/metrics"' not in line for line in bucket_lines)
