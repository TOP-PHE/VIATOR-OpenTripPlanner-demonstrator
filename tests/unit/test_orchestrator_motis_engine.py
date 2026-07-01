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
    with the same id can't accidentally cross-read each other's data.

    Post Phase-0.5: `motis server` takes `--data <dir>`, NOT `--config <file>`.
    The dir is the import output path; config.yml lives inside it."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    assert "--data" in out
    assert "/var/motis-graphs/motis/x/current" in out
    # Verify we're NOT passing the wrong flag (caught by Phase-0.5 spike).
    assert "--config" not in out.split("motis-x:")[1].split("\n\n")[0]


def test_motis_healthcheck_uses_root_endpoint_not_api_v6():
    """Phase-0.5 spike: `GET /api/v6/` returns 400 (the endpoint exists but
    rejects empty params). A failing probe would treat that as failure and
    crash-loop the container. `GET /` returns 200 (the splash page MOTIS
    prints on boot) — that's the cheapest healthcheck signal."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    motis_block = out.split("motis-x:")[1].split("\n\n")[0]
    # Right endpoint is `/`. Wrong endpoint is `/api/v6/` (would 400).
    assert "http://localhost:8080/" in motis_block
    assert "localhost:8080/api/v6/" not in motis_block
    assert "localhost:8080/otp/" not in motis_block


def test_motis_healthcheck_uses_wget_not_curl():
    """The upstream ghcr.io/motis-project/motis image is Alpine-based and
    ships wget but NOT curl. The previous `curl -fsS …` probe therefore
    failed on every run, so every MOTIS container reported (unhealthy)
    and docker could not auto-restart MOTIS when it actually hung.
    `wget --spider` issues a HEAD-like request, ships in the base image,
    and exits non-zero on HTTP error — exactly what docker's healthcheck
    contract needs."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    motis_block = out.split("motis-x:")[1].split("\n\n")[0]
    # Pull out the `test:` line — the actual healthcheck command — so the
    # negative `curl` assertion isn't tripped by explanatory YAML comments
    # that mention curl in passing.
    test_line = next(line for line in motis_block.splitlines() if line.lstrip().startswith("test:"))
    # The MOTIS image has no curl — the probe must use wget.
    assert "wget" in test_line
    assert "--spider" in test_line
    # Negative assertion: no leftover curl invocation in the actual probe.
    assert "curl" not in test_line


def test_motis_service_overrides_container_user_to_root():
    """Phase-0.5 spike: the MOTIS image runs as `User: motis` by default.
    Without `user: "0:0"`, the serve container can't map the read-only
    data dir cleanly (silent failures + permission quirks). The worker's
    build runs already pass `--user 0:0`; the serve template mirrors it."""
    out = render_compose([_StubSession(id="x", engine="motis")])
    motis_block = out.split("motis-x:")[1].split("\n\n")[0]
    assert 'user: "0:0"' in motis_block


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


def test_motis_service_carries_autoheal_optin_label():
    """The 2026-06 eu19-transit-motis incident: MOTIS container reported
    (unhealthy) for ~10h at 99% CPU and docker did nothing about it. The
    fix is a two-part opt-in: (1) the willfarrell/autoheal watchdog service
    in docker/docker-compose.yml, (2) this label on every serve container
    that should be restarted when its healthcheck flips. Postgres and other
    stateful services are deliberately NOT labelled."""
    out = render_compose([_StubSession(id="x", engine="motis", state=SessionState.SERVING.value)])
    motis_block = out.split("motis-x:")[1].split("\n\n")[0]
    assert "viator.autoheal" in motis_block
    assert '"true"' in motis_block


def test_otp_service_carries_autoheal_optin_label():
    """Same opt-in pattern as MOTIS — the per-session OTP serve container
    has its own healthcheck (curl /otp/) and the same failure-mode risk,
    so it opts into autoheal the same way. Prevents an asymmetry where
    only MOTIS sessions get watchdog coverage."""
    out = render_compose([_StubSession(id="x", engine="otp", state=SessionState.SERVING.value)])
    otp_block = out.split("otp-x:")[1].split("\n\n")[0]
    assert "viator.autoheal" in otp_block
    assert '"true"' in otp_block


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
