"""app/url_redaction.py — the one rule for URLs in logs and spans.

Invented values only: `apikey=ZZSECRET99`, `q=Zzville`, `code=ZZ99`.
"""

from __future__ import annotations

import re

import pytest

from app.url_redaction import (
    REDACTED,
    SAFE_QUERY_KEYS,
    TOKEN_PATH_PREFIXES,
    redact_path,
    redact_query,
    redact_text,
    redact_url,
)

_SECRETS = ("ZZSECRET99", "Zzville", "ZZ99", "ZZTOKEN99", "ZZPASS99", "ZZRUN99")


def _clean(text: str) -> bool:
    return not any(s in text for s in _SECRETS)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://feeds.zz.invalid/gtfs.zip?apikey=ZZSECRET99",
            "https://feeds.zz.invalid/gtfs.zip?apikey=REDACTED",
        ),
        ("/api/geocode?q=Zzville&size=5", "/api/geocode?q=REDACTED&size=5"),
        ("/cb?code=ZZ99&state=ZZ99", "/cb?code=REDACTED&state=REDACTED"),
        ("/api/auth/check-token?t=ZZTOKEN99", "/api/auth/check-token?t=REDACTED"),
        ("/api/reports/searches?user=ZZ99&page=2", "/api/reports/searches?user=REDACTED&page=2"),
        ("/x?LIMIT=5&Token=ZZ99", "/x?LIMIT=5&Token=REDACTED"),
        ("/x?key=ZZ99;password=ZZPASS99", "/x?key=REDACTED;password=REDACTED"),
        ("/x?ZZSECRET99", "/x?REDACTED"),
        ("/x?a=1&&b", "/x?a=REDACTED&&REDACTED"),
        ("/x?" + "k" * 41 + "=1", "/x?REDACTED"),
        ("/x?ZZ%20SECRET99=1", "/x?REDACTED"),
        ("https://u:ZZPASS99@h.zz.invalid/p", "https://REDACTED@h.zz.invalid/p"),
        ("https://h.zz.invalid/p#ZZTOKEN99", "https://h.zz.invalid/p#REDACTED"),
        ("/confirm/ZZTOKEN99", "/confirm/REDACTED"),
        ("/reset/ZZTOKEN99?x=1", "/reset/REDACTED?x=REDACTED"),
        (
            "/share/coverage/ZZRUN99/cells/a/b/trips",
            "/share/coverage/REDACTED/cells/a/b/trips",
        ),
        ("/journey", "/journey"),
        ("/api/geocode", "/api/geocode"),
        ("", ""),
    ],
    ids=[
        "feed-apikey",
        "typed-q-keeps-size",
        "oauth-code-state",
        "check-token",
        "report-user",
        "key-case",
        "semicolon",
        "bare-token",
        "empty-pair-and-bare",
        "long-key",
        "odd-key",
        "userinfo",
        "fragment",
        "confirm-path",
        "reset-path-and-query",
        "share-path",
        "no-query-page",
        "no-query-api",
        "empty",
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_redact_url_never_raises_on_a_url_urlsplit_refuses() -> None:
    out = redact_url("http://[zz/reset/ZZTOKEN99?apikey=ZZSECRET99")
    assert _clean(out)
    assert out.endswith(REDACTED)


def test_redact_url_is_idempotent() -> None:
    once = redact_url("/reset/ZZTOKEN99?q=Zzville&size=5")
    assert redact_url(once) == once


def test_redact_query_alone() -> None:
    assert redact_query("q=Zzville&limit=3") == "q=REDACTED&limit=3"
    assert redact_query("") == ""


def test_redact_path_alone() -> None:
    assert redact_path("/confirm/ZZTOKEN99/x") == "/confirm/REDACTED/x"
    assert redact_path("/confirmed/ZZ99") == "/confirmed/ZZ99"


@pytest.mark.parametrize(
    "text",
    [
        "Client error '404 Not Found' for url 'https://feeds.zz.invalid/f.zip?apikey=ZZSECRET99'",
        'request: "GET /api/geocode?q=Zzville HTTP/1.1"',
        "blocked request to http://h.zz.invalid/x?token=ZZ99: refused",
        "see /reset/ZZTOKEN99 for details",
        "url=<https://h.zz.invalid/?code=ZZ99>",
        "(https://u:ZZPASS99@h.zz.invalid/p?key=ZZ99)",
    ],
    ids=["httpx-status", "request-line", "blocked", "token-path", "angle", "parens-userinfo"],
)
def test_redact_text_finds_urls_in_free_text(text: str) -> None:
    out = redact_text(text)
    assert _clean(out)
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    ["plain words", "what? yes", "and/or? maybe", "2026-10-11T18:00:00Z", ""],
    ids=["words", "question", "slash-question", "timestamp", "empty"],
)
def test_redact_text_leaves_text_without_urls_alone(text: str) -> None:
    assert redact_text(text) == text


# ─────────── the lists stay honest against the real routes ───────────

_NEVER_SAFE = {"q", "t", "key", "apikey", "api_key", "token", "code", "state", "password"}
_NEVER_SAFE |= {"next", "user", "text", "secret", "access_token", "sig", "signature"}


def test_no_secret_or_typed_parameter_is_ever_allow_listed() -> None:
    assert not (SAFE_QUERY_KEYS & _NEVER_SAFE)


def _openapi() -> dict:
    from app.main import app

    return app.openapi()


def test_every_allow_listed_key_is_a_query_parameter_of_a_route() -> None:
    """An allow-list entry no route uses is a key someone could start using
    for a secret, already allow-listed."""
    used = {
        p["name"].lower()
        for ops in _openapi()["paths"].values()
        for op in ops.values()
        for p in op.get("parameters", [])
        if p["in"] == "query"
    }
    assert SAFE_QUERY_KEYS.issubset(used)


def test_every_route_with_a_secret_in_its_path_is_covered() -> None:
    """A route whose path parameter is a token or the share link's run id
    must start with a TOKEN_PATH_PREFIXES prefix."""
    secret_path = re.compile(r"\{(token|[a-z_]*token[a-z_]*)\}")
    paths = list(_openapi()["paths"])
    for path in paths:
        if secret_path.search(path) or path.startswith("/share/"):
            assert path.startswith(TOKEN_PATH_PREFIXES), path
    assert any(p.startswith("/share/coverage/") for p in paths)
    assert any(p.startswith("/confirm/") for p in paths)
    assert any(p.startswith("/reset/") for p in paths)
