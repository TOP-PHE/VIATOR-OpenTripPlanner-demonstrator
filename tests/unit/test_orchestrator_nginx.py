"""Tests for the v0.1.15 dynamic-upstream nginx fragment.

The static `proxy_pass http://otp-<sid>:8080/otp/;` form caused 502s on
every per-session OTP container restart because nginx cached the IP at
config-load time. This test file pins the variable-based pattern that
forces re-resolution via Docker's embedded DNS.

If a future refactor accidentally reverts to the static form, the
"every redeploy needs a manual `docker compose restart nginx`" pain
will silently come back. These tests are the trip-wire.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.sessions import SessionState


@dataclass
class _StubSession:
    """Just enough of SessionRow to drive render_nginx without the ORM."""

    id: str
    state: str = SessionState.SERVING.value


def test_serving_session_uses_variable_proxy_pass():
    """The whole point of v0.1.15: proxy_pass must reference a variable
    so the docker resolver fires on every request, not just at config
    load time. A literal `proxy_pass http://otp-<sid>:8080/...` would
    cache forever."""
    from app.sessions_orchestrator import render_nginx

    out = render_nginx([_StubSession(id="nap-fr-rail")])

    assert "proxy_pass http://$otp_upstream_nap_fr_rail;" in out, (
        "proxy_pass must reference a variable so nginx re-resolves the "
        "container hostname on each request — the v0.1.15 fix"
    )
    # Belt-and-braces: the hardcoded form must NOT appear.
    assert "proxy_pass http://otp-nap-fr-rail:8080" not in out, (
        "regression: static-host proxy_pass would re-cache the upstream IP"
    )


def test_session_id_with_hyphens_is_sanitised_for_variable_name():
    """nginx variable names only accept [A-Za-z0-9_]; session ids allow
    `-`. We swap `-` for `_` so a session called 'nap-fr-rail' becomes
    `$otp_upstream_nap_fr_rail`. This test pins that mapping."""
    from app.sessions_orchestrator import render_nginx

    out = render_nginx([_StubSession(id="nap-fr-rail-2026-q2")])

    # The variable name is the dash-free form.
    assert "set $otp_upstream_nap_fr_rail_2026_q2" in out
    assert "proxy_pass http://$otp_upstream_nap_fr_rail_2026_q2;" in out
    # The location prefix and rewrite still use the literal session id
    # (with dashes, because that's the URL path).
    assert "location /otp/nap-fr-rail-2026-q2/ {" in out
    assert "rewrite ^/otp/nap-fr-rail-2026-q2/(.*)$ /otp/$1 break;" in out


def test_rewrite_preserves_static_form_routing_semantics():
    """Pre-v0.1.15 the static form was:
        proxy_pass http://otp-<sid>:8080/otp/;
    which mapped /otp/<sid>/foo → upstream /otp/foo.

    The variable form needs an explicit rewrite to do the same — we
    can't add a trailing URI to `proxy_pass http://$var`. This test
    pins the rewrite so a refactor can't drop it.
    """
    from app.sessions_orchestrator import render_nginx

    out = render_nginx([_StubSession(id="demo")])

    # The rewrite must strip the SID prefix and replant under /otp/.
    assert "rewrite ^/otp/demo/(.*)$ /otp/$1 break;" in out


def test_non_serving_sessions_are_excluded():
    """Same as before — only sessions in state='serving' get a block.
    Defensive: this test guards against a refactor that emits blocks
    for, say, 'graph_built' sessions whose container doesn't exist yet.
    """
    from app.sessions_orchestrator import render_nginx

    sessions = [
        _StubSession(id="ready", state=SessionState.SERVING.value),
        _StubSession(id="building", state=SessionState.GRAPH_BUILT.value),
        _StubSession(id="empty", state=SessionState.CREATED.value),
    ]
    out = render_nginx(sessions)

    assert "location /otp/ready/" in out
    assert "location /otp/building/" not in out
    assert "location /otp/empty/" not in out


def test_empty_input_produces_only_header():
    """Zero serving sessions = just the DO-NOT-EDIT header. nginx's
    `include` directive tolerates a file with no location blocks."""
    from app.sessions_orchestrator import render_nginx

    out = render_nginx([])

    assert "DO NOT EDIT BY HAND" in out
    assert "location" not in out  # no per-session blocks


# The planners have no authentication of their own: nginx asks web first.


@dataclass
class _EngineSession:
    """A serving session with its engine, for the blocks of both planners."""

    id: str
    engine: str
    state: str = SessionState.SERVING.value


def _blocks(out: str) -> list[str]:
    """The `location ... { ... }` blocks of a rendered fragment."""
    return ["location " + block for block in out.split("location ")[1:]]


def test_every_engine_location_asks_web_before_it_forwards():
    """A planner answers whoever reaches it. nginx must ask web first
    (`auth_request`, as for /grafana/ and /prometheus/), in the block of each
    engine, and send a caller who is not signed in to the login page."""
    from app.sessions_orchestrator import render_nginx

    out = render_nginx(
        [_EngineSession(id="nap-fr-rail", engine="otp"), _EngineSession(id="eu19", engine="motis")]
    )

    blocks = _blocks(out)
    assert len(blocks) == 2
    for block in blocks:
        assert "    auth_request /api/auth/proxy-validate;\n" in block
        assert "    error_page 401 = @auth_redirect;\n" in block
        assert block.index("auth_request") < block.index("proxy_pass")


def test_the_callers_session_is_not_forwarded_to_a_planner():
    """The cookie and a bearer token authenticate the caller to VIATOR. A
    planner has no use for either, so neither leaves nginx."""
    from app.sessions_orchestrator import render_nginx

    out = render_nginx(
        [_EngineSession(id="nap-fr-rail", engine="otp"), _EngineSession(id="eu19", engine="motis")]
    )

    for block in _blocks(out):
        assert '    proxy_set_header Cookie "";\n' in block
        assert '    proxy_set_header Authorization "";\n' in block


def test_nginx_conf_declares_what_the_engine_locations_refer_to():
    """`auth_request` and `error_page` name two locations of nginx.conf. If
    either went missing, nginx would refuse the whole configuration at its
    next restart; and they must sit in the server block that includes the
    generated fragment."""
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "docker" / "nginx" / "nginx.conf").read_text(
        encoding="utf-8"
    )

    https_server = conf[conf.index("listen 443 ssl") :]
    assert "location = /api/auth/proxy-validate {" in https_server
    assert "location @auth_redirect {" in https_server
    assert "include /etc/nginx/conf.d/sessions/*.conf;" in https_server
