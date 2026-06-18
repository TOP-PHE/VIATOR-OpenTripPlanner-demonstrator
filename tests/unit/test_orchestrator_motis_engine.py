"""P1 MOTIS — orchestrator engine branching.

Pins that:
  * MOTIS sessions render a `motis-<sid>` compose service, not `otp-<sid>`.
  * MOTIS sessions get `/motis/<sid>/` nginx routing, not `/otp/<sid>/`.
  * The two engines coexist in a mixed render with their own blocks each.
  * Legacy sessions with `engine=None` keep rendering as OTP (back-compat
    for any pre-migration row that hasn't been backfilled yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.sessions import SessionState
from app.sessions_orchestrator import render_compose, render_nginx


@dataclass
class _StubSession:
    """Just enough of SessionRow to drive the orchestrator."""

    id: str
    engine: str | None = "otp"
    category: str = "MERITS"
    state: str = SessionState.SERVING.value
    config: dict[str, Any] = field(default_factory=dict)


# ───────────────────────────── render_compose ─────────────────────────────


def test_motis_session_renders_motis_service_not_otp():
    out = render_compose([_StubSession(id="nap-de-rail", engine="motis")])
    # MOTIS-shaped service block, NOT the OTP heap/Java template.
    assert "motis-nap-de-rail:" in out
    assert "otp-nap-de-rail:" not in out
    assert "ghcr.io/motis-project/motis" in out
    # MOTIS doesn't take an OTP_HEAP — that env var is JVM-specific and
    # would be ignored at runtime, but its presence would signal a wrong
    # template was picked.
    assert "OTP_HEAP" not in out.split("motis-nap-de-rail:")[1].split("\n\n")[0]


def test_motis_service_points_at_engine_specific_data_dir():
    """The MOTIS serve container reads from the per-engine subtree
    (`graphs/motis/<sid>/current/`) so an OTP session and a MOTIS session
    with the same id can't accidentally cross-read each other's data."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    assert "/var/motis-graphs/motis/x/current/config.yml" in out


def test_motis_healthcheck_probes_motis_api_root():
    """Sonar/runtime correctness: the healthcheck must NOT use OTP's
    `/otp/` probe (returns 404 on MOTIS and would crash-loop the container)."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    assert "/api/v6/" in out
    assert "localhost:8080/otp/" not in out.split("motis-x:")[1].split("\n\n")[0]


def test_motis_healthcheck_start_period_respects_session_config():
    """Operators tune big-feed import warmup the same way they do for OTP —
    via `otp_serve_start_period`. The MOTIS template reuses the knob
    intentionally so we don't multiply per-engine config fields in P1."""
    out = render_compose(
        [
            _StubSession(
                id="x",
                engine="motis",
                config={"otp_serve_start_period": 600},
            )
        ]
    )
    assert "start_period: 600s" in out


def test_otp_and_motis_render_side_by_side_in_one_fragment():
    out = render_compose(
        [
            _StubSession(id="nap-fr-rail", engine="otp"),
            _StubSession(id="nap-de-rail", engine="motis"),
        ]
    )
    # Both services present, no cross-contamination.
    assert "otp-nap-fr-rail:" in out
    assert "motis-nap-de-rail:" in out
    assert "motis-nap-fr-rail:" not in out
    assert "otp-nap-de-rail:" not in out


def test_legacy_session_without_engine_renders_as_otp():
    """A pre-migration row with engine=None must NOT be silently dropped
    or rendered as MOTIS — operator expectation is bit-identical to
    pre-P1 behaviour."""
    out = render_compose([_StubSession(id="x", engine=None)])
    assert "otp-x:" in out
    assert "motis-x:" not in out


def test_non_serving_motis_session_renders_nothing():
    """Just like OTP, only SERVING sessions appear in the compose
    fragment — graph_built or earlier states are still being prepared."""
    out = render_compose(
        [_StubSession(id="x", engine="motis", state=SessionState.GRAPH_BUILT.value)]
    )
    assert "motis-x:" not in out
    assert "otp-x:" not in out


# ───────────────────────────── render_nginx ─────────────────────────────


def test_motis_session_routes_through_motis_prefix():
    out = render_nginx([_StubSession(id="nap-de-rail", engine="motis")])
    assert "location /motis/nap-de-rail/" in out
    assert "location /otp/nap-de-rail/" not in out


def test_motis_nginx_block_strips_prefix_in_rewrite():
    """MOTIS's API doesn't carry an /otp/ namespace; the proxied request
    must drop the `/motis/<sid>/` prefix entirely so the upstream sees
    `/api/v6/plan` (not `/motis/<sid>/api/v6/plan`)."""
    out = render_nginx([_StubSession(id="x", engine="motis")])
    assert "rewrite ^/motis/x/(.*)$ /$1 break;" in out


def test_motis_nginx_uses_dynamic_upstream_pattern():
    """Same v0.1.15 rationale as OTP: the proxy_pass must reference a
    variable so docker DNS re-resolves on every request — a per-session
    container restart shouldn't need an `nginx -s reload`."""
    out = render_nginx([_StubSession(id="nap-de-rail", engine="motis")])
    assert "proxy_pass http://$motis_upstream_nap_de_rail;" in out
    # The static form would be a regression of v0.1.15:
    assert "proxy_pass http://motis-nap-de-rail:8080" not in out


def test_otp_and_motis_render_separate_nginx_blocks_side_by_side():
    out = render_nginx(
        [
            _StubSession(id="nap-fr-rail", engine="otp"),
            _StubSession(id="nap-de-rail", engine="motis"),
        ]
    )
    assert "location /otp/nap-fr-rail/" in out
    assert "location /motis/nap-de-rail/" in out
    # No bleed-through.
    assert "location /motis/nap-fr-rail/" not in out
    assert "location /otp/nap-de-rail/" not in out
