"""docker/nginx/nginx.conf and docker-compose.yml: who reaches Grafana and
Prometheus, and which address nginx gives web for a client.

Static checks (CI runs no nginx and no Grafana):

- `/grafana/` and `/prometheus/` ask the administrators' validation endpoint
  and do not pass the client's `Authorization` header on;
- the endpoint every signed-in role may ask is still there for the locations
  outside this file (the station mapping module's include);
- each path nginx asks is a route of the application;
- every location that passes a request to web sets `X-Real-IP` from the
  connection, and no location forwards the client's own `X-Forwarded-For`;
- web publishes no port, which is what `security.client_ip` relies on;
- Grafana's own password logins are switched off.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.api.auth import routes as auth_routes
from app.security import PROXY_CLIENT_ADDRESS_HEADER

_ROOT = Path(__file__).resolve().parents[2]
_CONF = _ROOT / "docker" / "nginx" / "nginx.conf"
_COMPOSE = _ROOT / "docker" / "docker-compose.yml"

ADMIN_ONLY = "/api/auth/proxy-validate-admin"
ANY_ROLE = "/api/auth/proxy-validate"
OBSERVABILITY = ("/grafana/", "/prometheus/")


def _directives() -> str:
    """The config without its comments."""
    return "\n".join(
        line.split("#", 1)[0] for line in _CONF.read_text(encoding="utf-8").splitlines()
    )


def _blocks(text: str, opening: str) -> list[tuple[str, str]]:
    """Each `<opening> <name> { … }` of `text`: its name and its body."""
    found = []
    for match in re.finditer(rf"\b{opening}\b([^{{;]*)\{{", text):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        found.append((" ".join(match.group(1).split()), text[match.end() : end - 1]))
    return found


def _locations() -> dict[str, list[str]]:
    """Each `location` of the HTTPS server: its name (modifier included, as
    written) and its directives, one per entry, without the semicolon. The
    port-80 server only redirects to it."""
    (https,) = [body for _, body in _blocks(_directives(), "server") if "listen 443" in body]
    found: dict[str, list[str]] = {}
    for name, body in _blocks(https, "location"):
        assert name not in found, name
        found[name] = [" ".join(d.split()) for d in body.split(";") if d.strip()]
    return found


def _compose() -> dict[str, dict[str, object]]:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    assert isinstance(services, dict)
    return services


# ───────────────────── who reaches Grafana and Prometheus ─────────────────────


@pytest.mark.parametrize("location", OBSERVABILITY)
def test_the_observability_locations_ask_the_administrators_endpoint(location: str) -> None:
    directives = _locations()[location]

    assert [d for d in directives if d.startswith("auth_request ")] == [
        f"auth_request {ADMIN_ONLY}"
    ]
    # No session: the login page. Not an administrator: nginx's own 403.
    assert "error_page 401 = @auth_redirect" in directives
    assert not [d for d in directives if d.startswith("error_page 403")]


@pytest.mark.parametrize("location", OBSERVABILITY)
def test_the_clients_authorization_header_stops_at_nginx(location: str) -> None:
    assert 'proxy_set_header Authorization ""' in _locations()[location]


def test_grafana_knows_the_person_from_the_validated_session_only() -> None:
    directives = _locations()["/grafana/"]

    assert "auth_request_set $proxy_user $upstream_http_x_forwarded_user" in directives
    assert "proxy_set_header X-WEBAUTH-USER $proxy_user" in directives


@pytest.mark.parametrize("path", [ANY_ROLE, ADMIN_ONLY])
def test_each_validation_endpoint_is_internal_and_served_by_web(path: str) -> None:
    directives = _locations()[f"= {path}"]

    assert "internal" in directives
    assert 'set $web_upstream "web:8000"' in directives
    assert "proxy_pass http://$web_upstream" in directives
    assert "proxy_pass_request_body off" in directives


def test_every_path_nginx_asks_is_a_route_of_the_application() -> None:
    asked = {
        directive.split()[1]
        for directives in _locations().values()
        for directive in directives
        if directive.startswith("auth_request ")
    }
    routes = {getattr(route, "path", None) for route in auth_routes.router.routes}

    assert asked == {ADMIN_ONLY}  # the others' locations are generated or included
    assert {ANY_ROLE, ADMIN_ONLY} <= routes


# ─────────────────────── the client's address, for web ───────────────────────


def test_every_location_that_reaches_web_sets_the_address_nginx_saw() -> None:
    to_web = {
        name: directives
        for name, directives in _locations().items()
        if "proxy_pass http://$web_upstream" in directives
    }

    assert set(to_web) == {"/", f"= {ANY_ROLE}", f"= {ADMIN_ONLY}"}
    for name, directives in to_web.items():
        assert f"proxy_set_header {PROXY_CLIENT_ADDRESS_HEADER} $remote_addr" in directives, name


def test_no_location_forwards_the_clients_own_forwarded_for() -> None:
    conf = _directives()

    assert "$proxy_add_x_forwarded_for" not in conf
    assert "$http_x_forwarded_for" not in conf
    assert "$http_x_real_ip" not in conf
    forwarded = [
        directive
        for directives in _locations().values()
        for directive in directives
        if directive.startswith("proxy_set_header X-Forwarded-For ")
    ]
    assert forwarded
    assert set(forwarded) == {"proxy_set_header X-Forwarded-For $remote_addr"}


def test_nothing_makes_nginx_take_its_client_address_from_a_header() -> None:
    """`real_ip_header` would turn `$remote_addr` into a value read from the
    request: only correct behind a second proxy, with `set_real_ip_from`
    naming that proxy alone (docs/architecture.md, "Invariants & traps")."""
    assert "real_ip_header" not in _directives()


def test_web_publishes_no_port() -> None:
    """What `security.client_ip` relies on: nginx is the only way in from
    outside, so `X-Real-IP` is nginx's."""
    services = _compose()

    assert "ports" not in services["web"]
    assert [name for name, service in services.items() if "ports" in service] == ["nginx"]


# ─────────────────────── Grafana's own password logins ───────────────────────


def test_grafana_accepts_the_proxy_header_and_no_password() -> None:
    env = _compose()["grafana"]["environment"]
    assert isinstance(env, dict)

    assert env["GF_AUTH_PROXY_ENABLED"] == "true"
    assert env["GF_AUTH_BASIC_ENABLED"] == "false"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert env["GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION"] == "true"
    # Not set: a password given here would only matter to an account that
    # is no longer created.
    assert "GF_SECURITY_ADMIN_PASSWORD" not in env
