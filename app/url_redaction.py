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

- the value of every query parameter becomes `REDACTED`, except the
  parameters named in `SAFE_QUERY_KEYS` (paging, a date, a session id: never
  something typed, never a secret). A parameter whose name looks odd (too
  long, unexpected characters) or has no `=` is replaced whole. A **new
  route's parameter is redacted until someone adds it to the allow-list**,
  so forgetting it costs a debugging detail, never a secret;
- the path segment after each prefix of `TOKEN_PATH_PREFIXES` becomes
  `REDACTED`;
- `user:password@` in front of a host becomes `REDACTED@`, and a fragment
  becomes `#REDACTED`.

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

# Query parameters whose value stays in logs and spans. Every name here is
# a query parameter of a VIATOR route (tests/unit/test_url_redaction.py
# checks it against the OpenAPI schema), and none of them carries a secret
# or text a person typed. Compared without case.
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

# A URL inside free text (an exception message, a log line): a scheme and
# everything up to a space, a quote or an angle bracket. Then a path with
# a query string (`/api/geocode?q=…`, as uvicorn and the ASGI spans write
# it), then a token path without a query.
_ABSOLUTE_URL: Final = re.compile(r"""[A-Za-z][A-Za-z0-9+.\-]*://[^\s'"<>`]+""")
_RELATIVE_URL: Final = re.compile(r"""(?<![\w/:.])/[^\s'"<>`?]*\?[^\s'"<>`]*""")
_TOKEN_PATH: Final = re.compile(
    "(" + "|".join(re.escape(p) for p in TOKEN_PATH_PREFIXES) + r""")[^\s'"<>`/?#]+"""
)


def _redact_pair(pair: str) -> str:
    key, sep, _value = pair.partition("=")
    if not sep or not _KEY_SHAPE.fullmatch(key):
        return REDACTED
    if unquote_plus(key).lower() in SAFE_QUERY_KEYS:
        return pair
    return f"{key}={REDACTED}"


def redact_query(query: str) -> str:
    """`query` (without its `?`) with every value outside SAFE_QUERY_KEYS
    replaced by REDACTED. `&` and `;` both separate pairs."""
    if not query:
        return query
    parts = re.split(r"([&;])", query)
    return "".join(p if p in ("&", ";", "") else _redact_pair(p) for p in parts)


def redact_path(path: str) -> str:
    """`path` with the segment after each TOKEN_PATH_PREFIXES prefix
    replaced by REDACTED."""
    return _TOKEN_PATH.sub(lambda m: m.group(1) + REDACTED, path)


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
        return redact_path(head) + ("" if head == url else f"?{REDACTED}")
    return urlunsplit(
        (
            parts.scheme,
            _redact_netloc(parts),
            redact_path(parts.path),
            redact_query(parts.query),
            REDACTED if parts.fragment else "",
        )
    )


def redact_text(text: str) -> str:
    """`text` with every URL found in it passed through `redact_url`, and
    every token path through `redact_path`. For free text: exception
    messages, tracebacks, log messages, span attribute values."""
    if not text or ("/" not in text and "?" not in text):
        return text
    text = _ABSOLUTE_URL.sub(lambda m: redact_url(m.group(0)), text)
    text = _RELATIVE_URL.sub(lambda m: redact_url(m.group(0)), text)
    return redact_path(text)
