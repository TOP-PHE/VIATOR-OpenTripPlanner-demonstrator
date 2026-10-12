"""One rule for every URL VIATOR writes to a log line or a trace span.

A URL's query string can carry a secret or what a person typed:

- a feed credential of auth type `query` is added to the feed's URL as
  `?<param>=<value>` (`app/credentials.py`), and the same goes for a NAP
  catalogue fetched with such a credential;
- the journey form's station fields send the typed text to
  `/api/geocode?q=…`, which VIATOR forwards to MOTIS as `?text=…`;
- the account confirmation page checks its token with
  `/api/auth/check-token?t=…`;
- the reports filter by `?user=…`.

Some paths carry a secret too: the e-mailed links `/confirm/<token>` and
`/reset/<token>`, and the share link `/share/coverage/<run id>`, whose run
id is the capability (`app/api/coverage_share.py`).

So every URL that reaches a log line (`app/logging_config.py`) or an
OpenTelemetry span (`app/tracing.py`) goes through `redact_url` /
`redact_text` first:

- the value of every query parameter becomes `REDACTED`. On a request
  target VIATOR received (a path, no scheme or host) the parameters named in
  `SAFE_QUERY_KEYS` keep their value (paging, a date, a session id: never
  something typed, never a secret); on an absolute URL — what VIATOR sends
  out, where an administrator chooses a query credential's name — no value
  is kept. A parameter whose name looks odd (too long, unexpected
  characters) or has no `=` is replaced whole. A **new route's parameter is
  redacted until someone adds it to the allow-list**, so forgetting it
  costs a debugging detail, never a secret;
- an encoded `%3F` in the path is taken as the start of a query, and the
  value of a matrix parameter (`;key=value` in a segment) is redacted;
- the path segment after each prefix of `TOKEN_PATH_PREFIXES` becomes
  `REDACTED`, unless it is a route template such as `{token}`;
- `user:password@` in front of a host becomes `REDACTED@`, and a fragment
  becomes `#REDACTED`.

The patterns that find URLs in free text start only at a boundary and
consume each candidate once, so the cost stays linear in the length of the
text: an access line for a long, hostile path cannot stall the event loop.

nginx cannot call Python: `docker/nginx/nginx.conf` mirrors the rule with
the `viator_redacted` log format (path without its query string, a
`?REDACTED` marker when there was one, the same token prefixes), and
`tests/unit/test_nginx_access_log.py` keeps the two lists equal.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import SplitResult, unquote_plus, urlsplit, urlunsplit

REDACTED: Final = "REDACTED"

# Query parameters whose value stays in logs and spans, on request targets
# VIATOR received only. Every name here is a query parameter of a VIATOR
# route (tests/unit/test_url_redaction.py checks it against the OpenAPI
# schema), and none of them carries a secret or text a person typed.
# Compared without case. app/credentials.py refuses them as the name of a
# query credential.
SAFE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "from_snapshot",
        "include_inactive",
        "limit",
        "max_memory",
        "page",
        "precision",
        "session",
        "since",
        "size",
        "status",
        "to_snapshot",
    }
)

# Paths whose next segment is a secret: the e-mailed confirmation and reset
# tokens, and the share link's run id. Mirrored in docker/nginx/nginx.conf.
TOKEN_PATH_PREFIXES: Final[tuple[str, ...]] = ("/confirm/", "/reset/", "/share/coverage/")

# A parameter name kept as it is: short, plain characters. Anything else
# (a secret pasted as a bare `?abc…` or used as a name) is replaced whole.
_KEY_SHAPE: Final = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,40}")

# A route template segment (`{token}`), kept as it is.
_TEMPLATE: Final = re.compile(r"\{[A-Za-z_]\w*\}", re.ASCII)

# URLs inside free text (an exception message, a log line). The look-behind
# is what keeps a scan linear: a match can start only at a boundary (not
# inside a word or a path), so each character is read by at most one
# attempt. Without it, a failed attempt at every position of a long word
# reads the rest of the word again: quadratic, whatever the quantifiers
# (the bodies are possessive only to stop backtracking within one match). An
# absolute URL: a scheme (consumed whole, of any length) and `://`. A relative one:
# a whole token starting with `/`, redacted when it holds a query, a matrix
# parameter, an encoded character, a fragment or a token prefix. A literal
# escape sequence (`one_line` writes a line break as the two characters
# `\n`) ends a URL and counts as a boundary before the next one.
_URL_BODY: Final = r"""(?:[^\s'"<>`\\]|\\(?![nrt]))"""
_AFTER_ESCAPE: Final = r"(?<=\\[nrt])"
_ABSOLUTE_URL: Final = re.compile(
    rf"(?:(?<![A-Za-z0-9+.\-])|{_AFTER_ESCAPE})[A-Za-z][A-Za-z0-9+.\-]*+://{_URL_BODY}++"
)
_RELATIVE_URL: Final = re.compile(rf"(?:(?<![\w/.])|{_AFTER_ESCAPE})/{_URL_BODY}*+")
_TOKEN_PATH: Final = re.compile(
    "(" + "|".join(re.escape(p) for p in TOKEN_PATH_PREFIXES) + r""")([^\s'"<>`/?#;\\]+)"""
)
_MATRIX_PARAM: Final = re.compile(r";([^;/=]*)=([^;/]*)")
_ENCODED_QUESTION_MARK: Final = re.compile(r"%3f", re.IGNORECASE)


def _redact_pair(pair: str, safe_keys: frozenset[str]) -> str:
    key, sep, _value = pair.partition("=")
    if not sep or not _KEY_SHAPE.fullmatch(key):
        return REDACTED
    if unquote_plus(key).lower() in safe_keys:
        return pair
    return f"{key}={REDACTED}"


def redact_query(query: str, *, keep_safe_keys: bool = True) -> str:
    """`query` (without its `?`) with every value replaced by REDACTED,
    except, when `keep_safe_keys`, those of SAFE_QUERY_KEYS. `&` and `;`
    both separate pairs."""
    if not query:
        return query
    safe_keys = SAFE_QUERY_KEYS if keep_safe_keys else frozenset()
    parts = re.split(r"([&;])", query)
    return "".join(p if p in ("&", ";", "") else _redact_pair(p, safe_keys) for p in parts)


def _redact_token_segment(m: re.Match[str]) -> str:
    if _TEMPLATE.fullmatch(m.group(2)):
        return m.group(0)
    return m.group(1) + REDACTED


def _redact_matrix(m: re.Match[str]) -> str:
    if _KEY_SHAPE.fullmatch(m.group(1)):
        return f";{m.group(1)}={REDACTED}"
    return f";{REDACTED}"


def redact_path(path: str) -> str:
    """`path` with the segment after each TOKEN_PATH_PREFIXES prefix (unless
    a route template) and every matrix parameter's value replaced by
    REDACTED, and everything after an encoded `%3F` too."""
    tail = ""
    encoded = _ENCODED_QUESTION_MARK.search(path)
    if encoded:
        path, tail = path[: encoded.start()], encoded.group(0) + REDACTED
    path = _TOKEN_PATH.sub(_redact_token_segment, path)
    return _MATRIX_PARAM.sub(_redact_matrix, path) + tail


def _redact_netloc(parts: SplitResult) -> str:
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rpartition('@')[2]}"
    return netloc


def redact_url(url: str) -> str:
    """`url` (absolute, or a path with its query) made safe to log.

    Never raises: a URL `urlsplit` refuses (a broken IPv6 host) keeps what
    comes before its first `?`, `#` or `@`, followed by REDACTED.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        head = re.split(r"[?#@]", url, maxsplit=1)[0]
        if head == url:
            return redact_path(head)
        return f"{redact_path(head)}?{REDACTED}"
    inbound = not (parts.scheme or parts.netloc)
    return urlunsplit(
        (
            parts.scheme,
            _redact_netloc(parts),
            redact_path(parts.path),
            redact_query(parts.query, keep_safe_keys=inbound),
            REDACTED if parts.fragment else "",
        )
    )


def _split_trailer(url: str) -> tuple[str, str]:
    """`url` cut before closing parentheses it does not open: they belong
    to the text around it."""
    unmatched = url.count(")") - url.count("(")
    end = len(url)
    while unmatched > 0 and end > 0 and url[end - 1] == ")":
        end -= 1
        unmatched -= 1
    return url[:end], url[end:]


def _redact_absolute(m: re.Match[str]) -> str:
    url, trailer = _split_trailer(m.group(0))
    return redact_url(url) + trailer


def _redact_relative(m: re.Match[str]) -> str:
    url, trailer = _split_trailer(m.group(0))
    if any(c in url for c in "?;%#") or _TOKEN_PATH.search(url):
        url = redact_url(url)
    return url + trailer


def redact_text(text: str) -> str:
    """`text` with every URL found in it passed through `redact_url`. For
    free text: exception messages, tracebacks, log messages, span attribute
    values."""
    if not text or "/" not in text:
        return text
    text = _ABSOLUTE_URL.sub(_redact_absolute, text)
    text = _RELATIVE_URL.sub(_redact_relative, text)
    # A token path glued to a word (`x/confirm/…`): its prefixes are
    # literal, so this pass is linear too.
    return _TOKEN_PATH.sub(_redact_token_segment, text)
