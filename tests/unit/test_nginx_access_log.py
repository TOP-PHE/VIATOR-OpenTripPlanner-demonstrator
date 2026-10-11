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


def test_the_path_map_mirrors_the_token_prefixes() -> None:
    m = re.search(r'map \$uri \$viator_log_path \{\s*"~\^\(([^)]*)\)', _directives())
    assert m, "map $uri $viator_log_path is missing"
    assert tuple(m.group(1).split("|")) == TOKEN_PATH_PREFIXES


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
