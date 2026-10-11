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


def test_a_url_urlsplit_refuses_without_query_keeps_its_path() -> None:
    assert redact_url("http://[zz/confirm/ZZTOKEN99") == "http://[zz/confirm/REDACTED"


# ─────────── follow-up review of #346 ───────────


@pytest.mark.parametrize(
    "key", sorted(SAFE_QUERY_KEYS), ids=[f"key-{k}" for k in sorted(SAFE_QUERY_KEYS)]
)
def test_an_absolute_url_keeps_no_value_even_for_an_allow_listed_key(key: str) -> None:
    """An administrator may name a query credential `session` or `page`: on
    an outbound (absolute) URL no value survives."""
    out = redact_url(f"https://motis.zz.invalid/geocode?text=Zzville&{key}=ZZSECRET99")
    assert out == f"https://motis.zz.invalid/geocode?text=REDACTED&{key}=REDACTED"
    assert _clean(redact_text(f"GET https://h.zz.invalid/x?{key.upper()}=ZZSECRET99 failed"))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/api/x%3Fq=Zzville", "/api/x%3FREDACTED"),
        ("/api/x%3fapikey=ZZSECRET99?page=2", "/api/x%3fREDACTED?page=2"),
        ("https://h.zz.invalid/f%3Fkey=ZZ99", "https://h.zz.invalid/f%3FREDACTED"),
        ("/a;jsessionid=ZZ99/b;v=ZZ99", "/a;jsessionid=REDACTED/b;v=REDACTED"),
        ("/a;" + "k" * 41 + "=ZZ99", "/a;REDACTED"),
        ("/confirm/{token}", "/confirm/{token}"),
        (
            "/share/coverage/{run_id}/cells/{a}/{b}/trips",
            "/share/coverage/{run_id}/cells/{a}/{b}/trips",
        ),
        ("/reset/{ZZ99", "/reset/REDACTED"),
    ],
    ids=[
        "encoded-q",
        "encoded-q-lower",
        "encoded-abs",
        "matrix",
        "matrix-odd-key",
        "template",
        "share-template",
        "not-a-template",
    ],
)
def test_encoded_query_matrix_parameters_and_templates(url: str, expected: str) -> None:
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(see https://h.zz.invalid/?k=ZZ99)", "(see https://h.zz.invalid/?k=REDACTED)"),
        ("(https://h.zz.invalid/a_(b)?k=ZZ99)", "(https://h.zz.invalid/a_(b)?k=REDACTED)"),
        (
            "line https://h.zz.invalid/?k=ZZ99\\nnext",
            "line https://h.zz.invalid/?k=REDACTED\\nnext",
        ),
        ("a\\nhttps://h.zz.invalid/?k=ZZ99", "a\\nhttps://h.zz.invalid/?k=REDACTED"),
        ("a\\n/x?q=Zzville\\tb", "a\\n/x?q=REDACTED\\tb"),
        ("route GET /confirm/{token}", "route GET /confirm/{token}"),
        ("path /x/y, done", "path /x/y, done"),
    ],
    ids=[
        "paren",
        "paren-inside",
        "escape-after",
        "escape-before",
        "escape-relative",
        "template",
        "plain-path",
    ],
)
def test_text_boundaries(text: str, expected: str) -> None:
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a" * 100_000,
        "-/" * 50_000,
        "-/" * 50_000 + "?a=1",
        "/" + "a/" * 50_000 + "?q=Zzville",
        # Shorter: each holds a match or a segment every few characters, so
        # the cost is per match (linear), and CI machines are slower.
        "https:/" * 3_000,
        "a\\n/" * 5_000,
        "/" + ";k=v" * 5_000,
        "/" + "%3F" * 7_000,
        "(http://h.zz.invalid/x" + ")" * 20_000,
    ],
    ids=[
        "letters",
        "dash-slash",
        "dash-slash-query",
        "long-path",
        "almost-scheme",
        "escapes",
        "matrix",
        "encoded",
        "parens",
    ],
)
def test_redaction_stays_linear_on_hostile_input(text: str) -> None:
    """A long hostile path reaches the access line of every request: the
    redaction must not stall the event loop (it took 60-120 ms for 8k
    characters with patterns that could start anywhere)."""
    import time

    start = time.perf_counter()
    redact_text(text)
    redact_url(text)
    assert time.perf_counter() - start < 0.05
