"""docker/nginx/nginx.conf writes no query string and no token path.

A static check (CI runs no nginx): the `viator_redacted` log format uses
none of the variables that carry a query string or the previous page's
URL, every `access_log` uses it, its token prefixes are the ones of
app/url_redaction.py, the error log stays at `crit`, and the generated
per-session fragments set no log of their own. The format was also run in
nginx 1.27-alpine when it was written (PR body of the follow-up to #339).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.models.sessions import SessionState
from app.url_redaction import TOKEN_PATH_PREFIXES

_CONF = Path(__file__).resolve().parents[2] / "docker" / "nginx" / "nginx.conf"


def _directives() -> str:
    """The config without its comments."""
    return "\n".join(line.split("#", 1)[0] for line in _CONF.read_text().splitlines())


def _log_format() -> str:
    m = re.search(r"log_format\s+viator_redacted\s+(.*?);", _directives(), re.S)
    assert m, "log_format viator_redacted is missing"
    return m.group(1)


def test_the_format_carries_no_query_string_nor_referer() -> None:
    fmt = _log_format()
    for var in ("$request ", '$request"', "$request_uri", "$args", "$query_string"):
        assert var not in fmt, var
    assert "$arg_" not in fmt
    assert "$http_referer" not in fmt
    assert not re.search(r"\$request\b(?!_)", fmt)


def test_the_format_keeps_what_an_operator_needs() -> None:
    fmt = _log_format()
    for var in (
        "$viator_log_path",
        "$viator_log_query",
        "$status",
        "$body_bytes_sent",
        "$request_time",
        "$upstream_response_time",
        "$upstream_status",
        "$upstream_addr",
        "$host",
        "$upstream_http_x_request_id",
        "$request_id",
    ):
        assert var in fmt, var


def test_every_access_log_uses_the_redacted_format() -> None:
    logs = re.findall(r"^\s*access_log\s+([^;]*);", _directives(), re.M)
    assert logs, "no access_log: nginx would fall back to `combined`"
    for value in logs:
        parts = value.split()
        assert parts == ["off"] or (len(parts) >= 2 and parts[1] == "viator_redacted"), value


def test_the_access_log_is_set_at_http_level() -> None:
    """Set in `http`, so both servers and the included session fragments
    inherit it."""
    text = _directives()
    http_start = text.index("http {")
    first_server = text.index("server {", http_start)
    assert re.search(r"access_log\s+\S+\s+viator_redacted", text[http_start:first_server])


_RAW_QUERY_SOURCES = ("$request_uri", "$args", "$query_string", "$is_args", "$arg_", "$request ")


def _maps() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """`{variable: (source, [(key, value), ...])}` of every `map` block."""
    out: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for src, var, body in re.findall(
        r"map\s+(\S+)\s+\$(\w+)\s*\{(.*?)\n\s*\}", _directives(), re.S
    ):
        entries = re.findall(r'^\s*("[^"]*"|\S+)\s+("[^"]*"|\S+)\s*;', body, re.M)
        out[var] = (src.strip('"'), [(k.strip('"'), v.strip('"')) for k, v in entries])
    return out


def _expand(template: str, env: dict[str, str], groups: tuple[str, ...] = ()) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.isdigit():
            return groups[int(name) - 1] if int(name) <= len(groups) else ""
        return _value(name, env)

    # nginx reads one digit after `$` as a capture (`$1REDACTED`).
    return re.sub(r"\$([1-9]|[A-Za-z_]\w*)", sub, template)


def _value(name: str, env: dict[str, str]) -> str:
    """nginx's evaluation of a map variable: the first regex that matches,
    in order (exact strings first), else the default."""
    maps = _maps()
    if name not in maps:
        return env[name]
    src, entries = maps[name]
    subject = _expand(src, env)
    default = ""
    for key, value in entries:
        if key == "default":
            default = value
        elif not key.startswith("~") and key == subject:
            return _expand(value, env)
    for key, value in entries:
        if key.startswith("~"):
            m = re.search(key[1:], subject)
            if m:
                return _expand(value, env, m.groups())
    return _expand(default, env)


def _logged(request_uri: str, uri: str) -> str:
    env = {"request_uri": request_uri, "uri": uri, "args": request_uri.partition("?")[2]}
    return _value("viator_log_path", env) + _value("viator_log_query", env)


@pytest.mark.parametrize(
    ("request_uri", "uri", "expected"),
    [
        ("/otp/zzs1/otp/routers?x=ZZ99", "/otp/otp/routers", "/otp/zzs1/otp/routers?REDACTED"),
        (
            "/motis/zzm1/api/v1/plan?fromPlace=ZZ99",
            "/api/v1/plan",
            "/motis/zzm1/api/v1/plan?REDACTED",
        ),
        ("/api/geocode?q=Zzville&size=5", "/api/geocode", "/api/geocode?REDACTED"),
        ("/confirm/ZZ99", "/confirm/ZZ99", "/confirm/REDACTED"),
        ("/reset/ZZ99?x=1", "/reset/ZZ99", "/reset/REDACTED?REDACTED"),
        (
            "/share/coverage/ZZ99/cells/a/b/trips",
            "/share/coverage/ZZ99/cells/a/b/trips",
            "/share/coverage/REDACTED/cells/a/b/trips",
        ),
        ("/confir%6D/ZZ99", "/confirm/ZZ99", "/REDACTED"),
        ("/a/../reset/ZZ99", "/reset/ZZ99", "/REDACTED"),
        ("/api/x%3Fq=ZZ99", "/api/x?q=ZZ99", "/api/x%3FREDACTED"),
        ("/api/y%3fapikey=ZZ99?page=2", "/api/y?apikey=ZZ99", "/api/y%3FREDACTED?REDACTED"),
        ("/journey", "/journey", "/journey"),
    ],
    ids=[
        "otp-session",
        "motis-session",
        "geocode",
        "confirm",
        "reset",
        "share",
        "encoded-prefix",
        "dot-dot",
        "encoded-q",
        "encoded-q-lower",
        "page",
    ],
)
def test_the_maps_write_the_path_the_client_sent_redacted(
    request_uri: str, uri: str, expected: str
) -> None:
    """The maps evaluated as nginx does (first matching regex in order),
    with `$uri` as nginx has it at log time: after a session fragment's
    `rewrite … break`, decoded and normalised. Checked against nginx
    1.27-alpine itself when written (PR #346)."""
    out = _logged(request_uri, uri)
    assert out == expected
    assert "ZZ99" not in out
    assert "Zzville" not in out


def test_the_maps_mirror_the_token_prefixes() -> None:
    maps = _maps()
    for var in ("viator_req_path_redacted", "viator_uri_is_token", "viator_log_path"):
        regexes = [k for k, _ in maps[var][1] if k.startswith("~")]
        m = re.search(r"\(([^)]*)\)", regexes[0])
        assert m, var
        assert tuple(m.group(1).split("|")) == TOKEN_PATH_PREFIXES, var


def test_no_map_writes_a_raw_query_source() -> None:
    """Only `$viator_req_path` reads `$request_uri`, and each of its regexes
    stops at the first `?`; no map value names a raw query source."""
    for var, (src, entries) in _maps().items():
        for _key, value in entries:
            assert not any(q in value + " " for q in _RAW_QUERY_SOURCES), (var, value)
        if var == "viator_req_path":
            assert src == "$request_uri"
            assert all(k.startswith("~^([^?]*") for k, _ in entries), entries
        elif var == "viator_log_query":
            assert src == "$args"
        else:
            assert not any(q in src + " " for q in _RAW_QUERY_SOURCES), (var, src)


def test_the_query_map_writes_a_marker_never_the_query() -> None:
    m = re.search(r"map \$args \$viator_log_query \{(.*?)\}", _directives(), re.S)
    assert m
    body = m.group(1)
    assert '"" "";' in body
    assert 'default "?REDACTED";' in body
    assert "$args" not in body


def test_the_error_log_stays_at_crit() -> None:
    """At `error` nginx writes `request: "GET /path?query …"` for every
    failed upstream call."""
    levels = re.findall(r"^\s*error_log\s+\S+\s*(\w*)\s*;", _directives(), re.M)
    assert levels == ["crit"]


@dataclass
class _StubSession:
    id: str
    state: str = SessionState.SERVING.value
    engine: str = "otp"


def test_the_session_fragments_set_no_log_of_their_own() -> None:
    from app.sessions_orchestrator import render_nginx

    out = render_nginx([_StubSession(id="zz-otp"), _StubSession(id="zz-motis", engine="motis")])
    assert "location" in out
    for directive in ("access_log", "error_log", "log_format"):
        assert directive not in out


def test_the_deploy_checks_then_restarts_nginx_after_the_pull() -> None:
    """nginx.conf is a single-file bind mount: `git pull` replaces the file
    and only a restart loads it (`nginx -s reload` keeps the old one). The
    new file is checked first, so a broken config stops the deploy."""
    script = (Path(__file__).resolve().parents[2] / "bin" / "viator-deploy.sh").read_text()
    lines = [line.split("#", 1)[0].strip() for line in script.splitlines()]
    check = lines.index("docker compose run --rm --no-deps -T nginx nginx -t")
    restart = lines.index("docker compose restart nginx")
    pull = lines.index("git pull --ff-only --autostash")
    assert pull < check < restart
    assert "set -euo pipefail" in lines
