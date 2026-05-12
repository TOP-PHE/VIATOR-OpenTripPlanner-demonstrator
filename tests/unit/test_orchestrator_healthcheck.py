"""Tests for PR #32 — per-session healthcheck `start_period` in the
generated docker-compose fragment.

The hardcoded `HEALTHCHECK --start-period=120s` in `docker/otp/Dockerfile`
was the killer 2026-05-11: nap-fr-rail with SBB included needed ~4 min
for Raptor mapping, the healthcheck declared the container unhealthy
mid-startup, the orchestration restarted it, the new JVM started from
scratch, and the cycle never terminated.

These tests pin the new behaviour:
  1. The fragment includes a `healthcheck` block (Dockerfile-baked
     HEALTHCHECK was dropped — see docker/otp/Dockerfile)
  2. `start_period` comes from session.config.otp_serve_start_period
     when set, else falls back to the validator's default (300s)
  3. Garbage values get rejected at render time rather than producing
     malformed YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.sessions import SessionState


@dataclass
class _StubSession:
    """Just enough of SessionRow to drive render_compose without the ORM."""

    id: str
    config: dict | None = field(default_factory=dict)
    category: str = "NAP"
    state: str = SessionState.SERVING.value


def test_fragment_includes_healthcheck_block():
    """The generated fragment must contain a healthcheck block — the
    Dockerfile-baked one was dropped in PR #32 so the orchestrator owns it."""
    from app.sessions_orchestrator import render_compose

    out = render_compose([_StubSession(id="nap-fr-rail")])

    assert "healthcheck:" in out, (
        "render_compose must emit a healthcheck block for serve containers — "
        "the Dockerfile-baked HEALTHCHECK was removed in PR #32"
    )
    assert "curl -fsS http://localhost:8080/otp/" in out
    assert "interval: 30s" in out
    assert "timeout: 10s" in out
    assert "retries: 3" in out


def test_default_start_period_is_300_seconds():
    """Legacy sessions (config={}) inherit the validator's 300s default —
    enough for France-wide multi-NAP, not so much that genuinely-stuck
    JVMs hide for ages."""
    from app.sessions_orchestrator import render_compose

    out = render_compose([_StubSession(id="nap-fr-rail")])

    assert "start_period: 300s" in out, (
        "Sessions without an explicit otp_serve_start_period must inherit the 300s default"
    )


def test_explicit_start_period_overrides_default():
    """When the operator picks a value in the UI it must flow through to
    the rendered compose — that's the whole point of the field."""
    from app.sessions_orchestrator import render_compose

    out = render_compose([_StubSession(id="nap-fr-rail", config={"otp_serve_start_period": 600})])

    assert "start_period: 600s" in out
    # And the default isn't anywhere else (avoid double-render bugs)
    assert "start_period: 300s" not in out


def test_explicit_start_period_accepts_string_form():
    """The form submission flows through as a string; the validator
    normalises to int. Make sure render_compose handles either input
    (string from form → JSONB → DB read → here, OR int from DB)."""
    from app.sessions_orchestrator import render_compose

    out = render_compose([_StubSession(id="nap-fr-rail", config={"otp_serve_start_period": "480"})])

    assert "start_period: 480s" in out


def test_invalid_start_period_raises_at_render_time():
    """Garbage in the session config shouldn't produce malformed compose
    YAML — the orchestrator must fail fast at render time so the operator
    sees a 400/500 instead of `docker compose up` choking on a broken file."""
    import pytest

    from app.sessions_orchestrator import render_compose

    # Below the 30s floor
    with pytest.raises(ValueError, match="below minimum"):
        render_compose([_StubSession(id="nap-fr-rail", config={"otp_serve_start_period": 10})])

    # Non-numeric
    with pytest.raises(ValueError, match="must be an integer"):
        render_compose([_StubSession(id="nap-fr-rail", config={"otp_serve_start_period": "abc"})])


def test_multiple_sessions_get_distinct_start_periods():
    """Each session's start_period flows through independently — bug guard
    against the orchestrator accidentally caching one session's value and
    applying it to all."""
    from app.sessions_orchestrator import render_compose

    out = render_compose(
        [
            _StubSession(id="nap-fr-rail", config={"otp_serve_start_period": 300}),
            _StubSession(id="nap-ch-rail", config={"otp_serve_start_period": 180}),
            _StubSession(id="nap-de-rail"),  # default
        ]
    )

    # Each session's labelled block has its own start_period
    fr_idx = out.index("otp-nap-fr-rail:")
    ch_idx = out.index("otp-nap-ch-rail:")
    de_idx = out.index("otp-nap-de-rail:")

    # nap-fr-rail block ends just before nap-ch-rail begins
    assert "start_period: 300s" in out[fr_idx:ch_idx]
    assert "start_period: 180s" in out[ch_idx:de_idx]
    assert "start_period: 300s" in out[de_idx:]  # de inherits the 300s default
