"""Client of the Multimodal Station Mapping module (MSMM).

The module is a separate service on VIATOR's Docker network. It answers
two calls VIATOR uses, under `/internal/v1/`, reached at
`settings.station_module_url` (normally `http://msmm-web:8000`, plain HTTP
inside the network like VIATOR's calls to MOTIS):

- `search(q, user_id)` — `POST /internal/v1/stations/search`, at most ten
  stations whose name or MERITS code matches `q`. Used by the journey
  typeahead through `POST /api/stations/suggest` (app/api/station_suggest.py).
- `attribution()` — `GET /internal/v1/attribution`, the licence text of the
  module's sources, kept 60 s. Asynchronous, like search, so that its
  deadline is real. No caller yet: the notice on /journey that will await
  it comes in a later change.

Both return `None` on any failure, and the caller then uses VIATOR's own
behaviour (the master_stations list; no notice). VIATOR must keep working
without the module.

Rules this client keeps on purpose:

- **Its own headers, built from nothing.** `Authorization: Bearer <token>`,
  `Accept-Encoding: identity`, `X-Viator-User-Id` (search only) and
  `Content-Type: application/json` (search only). Nothing of the incoming request is ever forwarded, so a
  browser cannot set the user id: it comes from the JWT user of
  `require_logged_in`. `trust_env=False` keeps proxy variables and `.netrc`
  out of the call.
- **The search text travels in the JSON body, never in the URL**: VIATOR's
  httpx tracing records the method, the URL and the status of every call.
- **A pause of 30 s after a failure that says the module is unreachable or
  misconfigured** (network error, timeout, 401, 403, 404, 405, 413, 415,
  500, 503 `no_build` or `database`, a wrong top-level shape): no call to
  the module during the pause, so a module that hangs costs one keystroke a
  timeout, not each. **No pause after 429, 422 or 503 `busy`**: those concern
  one request, and pausing on them would let one user switch the module off
  for everyone.
- **A real deadline and a size cap**: 1 s for a whole search, 0.5 s for an
  attribution (httpx's timeouts bound each read, not the call), and a body
  larger than 64 KiB (search) or 256 KiB (attribution) is refused unread;
  so is a compressed answer, which could inflate past the cap at once.
  All count as failures that pause. Any other error (a bad address, a
  token httpx cannot encode) falls back too, as `network`.
- **A bad row is dropped, never the whole answer.**
- **The log never holds the text, a body, the token or an exception's
  text**: one line `station_module.fallback reason=<word>` per fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeGuard

import httpx

from .settings import settings

log = logging.getLogger(__name__)

SEARCH_PATH = "/internal/v1/stations/search"
ATTRIBUTION_PATH = "/internal/v1/attribution"
USER_HEADER = "X-Viator-User-Id"

# The typeahead is on the keystroke path: one second at most for the whole
# call (SEARCH_DEADLINE, enforced around it: httpx's own timeouts bound each
# read, not the call, so a server trickling bytes would outlast them), and
# 0.3 s to connect. The module itself stops a statement after 0.8 s.
SEARCH_DEADLINE = 1.0
SEARCH_TIMEOUT = httpx.Timeout(SEARCH_DEADLINE, connect=0.3)
# The attribution is read while /journey renders: half a second for the whole
# call, enforced the same way.
ATTRIBUTION_DEADLINE = 0.5
ATTRIBUTION_TIMEOUT = httpx.Timeout(ATTRIBUTION_DEADLINE)

# The largest body read from the module, decoded. A search answer is ten
# short rows (a few KiB); an attribution is at most 20 groups of texts of 500
# characters. Anything larger is refused without being read to its end. The
# cap counts the bytes as they arrive: the client asks for no compression
# and refuses a compressed answer, which could inflate far past the cap
# inside a single chunk.
SEARCH_MAX_BYTES = 64 * 1024
ATTRIBUTION_MAX_BYTES = 256 * 1024

PAUSE_SECONDS = 30.0
ATTRIBUTION_TTL_SECONDS = 60.0

MAX_ROWS = 10
MAX_SOURCES = 20
MAX_TEXT = 500

# Answers that say the module is unreachable or misconfigured: they start the pause.
_PAUSING_STATUSES = frozenset({401, 403, 404, 405, 413, 415, 500})
_PAUSING_503_CODES = frozenset({"no_build", "database"})

_URL_SCHEMES = ("https://", "http://")

# States and one-request refusals, not faults: logged at INFO.
_QUIET_REASONS = frozenset({"off", "paused", "busy", "status_429"})

# The clock of the pause and of the attribution cache; tests replace it.
clock: Callable[[], float] = time.monotonic


class _Failure(Exception):
    """A call that ends in the fallback: the reason word, and whether it pauses."""

    def __init__(self, reason: str, *, pause: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pause = pause


class _Answer:
    """The status and the (capped) body of one answer of the module."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


class _State:
    """The pause and the cached attribution, under one lock: the calls run on
    the event loop, but the lock keeps the state safe from any thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.paused_until = 0.0
        self.attribution: tuple[float, dict[str, Any]] | None = None


_state = _State()


def reset() -> None:
    """Forget the pause and the cached attribution (tests, and nothing else)."""
    with _state.lock:
        _state.paused_until = 0.0
        _state.attribution = None


def enabled() -> bool:
    """True when the module is configured: both the address and the token are set."""
    return bool(settings.station_module_url and settings.station_module_token.get_secret_value())


def _paused() -> bool:
    with _state.lock:
        return clock() < _state.paused_until


def _fail(failure: _Failure) -> None:
    """Log one fallback line, start the pause when the failure calls for it,
    and forget the cached attribution: a module that just failed shows no notice."""
    if failure.pause:
        with _state.lock:
            _state.paused_until = clock() + PAUSE_SECONDS
            _state.attribution = None
    # `off`, `paused`, 429 and `busy` are states or one-request refusals, not
    # faults: INFO, so a VIATOR without the module, a paused one, or a busy
    # one, does not fill the log with warnings.
    level = logging.INFO if failure.reason in _QUIET_REASONS else logging.WARNING
    log.log(level, "station_module.fallback reason=%s", failure.reason)


def _url(path: str) -> str:
    return settings.station_module_url.rstrip("/") + path


def _headers() -> dict[str, str]:
    """The headers of every call: the token, and no compression (see the cap)."""
    return {
        "Authorization": f"Bearer {settings.station_module_token.get_secret_value()}",
        "Accept-Encoding": "identity",
    }


def _too_large(response: httpx.Response, limit: int) -> bool:
    """True when the answer announces a body larger than `limit`."""
    length = response.headers.get("content-length", "")
    return length.isdigit() and int(length) > limit


def _compressed(response: httpx.Response) -> bool:
    """True when the answer carries a Content-Encoding other than identity."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    return encoding not in ("", "identity")


async def _read(response: httpx.Response, limit: int) -> _Answer:
    """The answer, its body read as it arrives up to `limit` bytes, else a
    shape failure. A compressed answer is refused unread."""
    if _compressed(response) or _too_large(response, limit):
        raise _Failure("shape", pause=True)
    body = bytearray()
    # With no Content-Encoding (or identity), the decoded bytes are the bytes
    # as they arrive: nothing is inflated before the cap is checked.
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise _Failure("shape", pause=True)
    return _Answer(response.status_code, bytes(body))


def _error_code(answer: _Answer) -> str | None:
    """The `code` word of a module error body, or None."""
    try:
        body = json.loads(answer.body)
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _status_failure(answer: _Answer) -> _Failure:
    """The failure a status other than 200 means (20.16 of the module's design)."""
    status = answer.status
    if status == 503:
        code = _error_code(answer)
        if code == "busy":
            return _Failure("busy", pause=False)
        return _Failure("status_503", pause=code in _PAUSING_503_CODES)
    return _Failure(f"status_{status}", pause=status in _PAUSING_STATUSES)


def _json_body(answer: _Answer) -> Any:
    if answer.status != 200:
        raise _status_failure(answer)
    try:
        return json.loads(answer.body)
    except ValueError:
        raise _Failure("shape", pause=True) from None


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _station_row(item: Any) -> dict[str, Any] | None:
    """One row of the module's answer as the typeahead reads it, or None when
    it is not a usable row. The name is kept as sent (it may hold markup: the
    page escapes it)."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    latitude = item.get("latitude")
    longitude = item.get("longitude")
    country = item.get("country_iso")
    uic = item.get("uic")
    if not isinstance(name, str) or not name:
        return None
    if not (_is_number(latitude) and _is_number(longitude)):
        return None
    if country is not None and not isinstance(country, str):
        return None
    if not isinstance(uic, str):
        return None
    return {
        "name": name,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "country_iso": country,
        "uic": uic,
    }


def _stations(payload: Any) -> list[dict[str, Any]]:
    """The usable rows of a search answer, at most ten; a wrong top-level
    shape refuses the whole answer."""
    stations = payload.get("stations") if isinstance(payload, dict) else None
    if not isinstance(stations, list):
        raise _Failure("shape", pause=True)
    rows: list[dict[str, Any]] = []
    for item in stations:
        row = _station_row(item)
        if row is not None:
            rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return rows


async def _post_search(q: str, user_id: uuid.UUID) -> _Answer:
    headers = {
        **_headers(),
        USER_HEADER: str(user_id),
        "Content-Type": "application/json",
    }
    body = json.dumps({"q": q}).encode("utf-8")
    try:
        async with (
            asyncio.timeout(SEARCH_DEADLINE),
            httpx.AsyncClient(timeout=SEARCH_TIMEOUT, trust_env=False) as client,
            client.stream("POST", _url(SEARCH_PATH), content=body, headers=headers) as response,
        ):
            return await _read(response, SEARCH_MAX_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise _Failure("timeout", pause=True) from None
    except httpx.HTTPError:
        raise _Failure("network", pause=True) from None


async def search(q: str, user_id: uuid.UUID) -> list[dict[str, Any]] | None:
    """At most ten stations of the module for `q` (already normalised by the
    caller), on behalf of the VIATOR user `user_id`; `None` on any failure."""
    try:
        if not enabled():
            raise _Failure("off", pause=False)
        if _paused():
            raise _Failure("paused", pause=False)
        return _stations(_json_body(await _post_search(q, user_id)))
    except _Failure as failure:
        _fail(failure)
        return None
    except Exception:
        # Anything else (an address httpx refuses, a token it cannot encode, a
        # JSON parser error) falls back too, and its text is never logged: an
        # exception about a header can quote the token.
        _fail(_Failure("network", pause=True))
        return None


def _short_text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise _Failure("shape", pause=True)
    return value


def _source_group(item: Any) -> dict[str, Any]:
    """One group of the attribution: a licence, its address when it is a web
    address (else dropped, the group kept), and the labels of its sources."""
    if not isinstance(item, dict):
        raise _Failure("shape", pause=True)
    labels = item.get("labels")
    if not isinstance(labels, list):
        raise _Failure("shape", pause=True)
    url = item.get("licence_url")
    if url is not None:
        url = _short_text(url)
    return {
        "licence": _short_text(item.get("licence")),
        "licence_url": url if url is not None and url.startswith(_URL_SCHEMES) else None,
        "labels": [_short_text(label) for label in labels],
    }


def _attribution_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _Failure("shape", pause=True)
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise _Failure("shape", pause=True)
    return {
        "statement": _short_text(payload.get("statement")),
        "sources": [_source_group(item) for item in sources],
    }


async def _get_attribution() -> _Answer:
    try:
        async with (
            asyncio.timeout(ATTRIBUTION_DEADLINE),
            httpx.AsyncClient(timeout=ATTRIBUTION_TIMEOUT, trust_env=False) as client,
            client.stream("GET", _url(ATTRIBUTION_PATH), headers=_headers()) as response,
        ):
            return await _read(response, ATTRIBUTION_MAX_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise _Failure("timeout", pause=True) from None
    except httpx.HTTPError:
        raise _Failure("network", pause=True) from None


def _cached_attribution() -> dict[str, Any] | None:
    with _state.lock:
        cached = _state.attribution
    if cached is not None and clock() - cached[0] < ATTRIBUTION_TTL_SECONDS:
        return cached[1]
    return None


def _forget_and_fail(failure: _Failure) -> None:
    with _state.lock:
        _state.attribution = None
    _fail(failure)


async def attribution() -> dict[str, Any] | None:
    """The module's attribution text, cached 60 s; `None` when the module is
    not configured, is paused or fails (and a failure forgets the cache).
    Asynchronous so that its deadline bounds the whole call, headers
    included. Nothing calls it yet: the notice that shows it comes in a
    later change, which will await it."""
    try:
        if not enabled():
            raise _Failure("off", pause=False)
        if _paused():
            raise _Failure("paused", pause=False)
        cached = _cached_attribution()
        if cached is not None:
            return cached
        answer = _attribution_of(_json_body(await _get_attribution()))
    except _Failure as failure:
        _forget_and_fail(failure)
        return None
    except Exception:
        # As in search(): any other error falls back, its text never logged.
        _forget_and_fail(_Failure("network", pause=True))
        return None
    with _state.lock:
        _state.attribution = (clock(), answer)
    return answer
