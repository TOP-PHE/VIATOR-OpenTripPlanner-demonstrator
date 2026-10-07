"""Client of the Multimodal Station Mapping module (MSMM).

The module is a separate service on VIATOR's Docker network. It answers
two calls VIATOR uses, under `/internal/v1/`, reached at
`settings.station_module_url` (normally `http://msmm-web:8000`, plain HTTP
inside the network like VIATOR's calls to MOTIS):

- `search(q, user_id)` — `POST /internal/v1/stations/search`, at most ten
  stations whose name or MERITS code matches `q`. Used by the journey
  typeahead through `POST /api/stations/suggest` (app/api/station_suggest.py).
- `attribution()` — `GET /internal/v1/attribution`, the licence text of the
  module's sources, kept 60 s.

Both return `None` on any failure, and the caller then uses VIATOR's own
behaviour (the master_stations list; no notice). VIATOR must keep working
without the module.

Rules this client keeps on purpose:

- **Its own headers, built from nothing.** `Authorization: Bearer <token>`,
  `X-Viator-User-Id` (search only) and `Content-Type: application/json`
  (search only). Nothing of the incoming request is ever forwarded, so a
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
- **A bad row is dropped, never the whole answer.**
- **The log never holds the text, a body, the token or an exception's
  text**: one line `station_module.fallback reason=<word>` per fallback.
"""

from __future__ import annotations

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

# The typeahead is on the keystroke path: one second at most, of which 0.3 s
# to connect. The module itself stops a statement after 0.8 s.
SEARCH_TIMEOUT = httpx.Timeout(1.0, connect=0.3)
# The attribution is read while /journey renders: half a second at most.
ATTRIBUTION_TIMEOUT = 0.5

PAUSE_SECONDS = 30.0
ATTRIBUTION_TTL_SECONDS = 60.0

MAX_ROWS = 10
MAX_SOURCES = 20
MAX_TEXT = 500

# Answers that say the module is unreachable or misconfigured: they start the pause.
_PAUSING_STATUSES = frozenset({401, 403, 404, 405, 413, 415, 500})
_PAUSING_503_CODES = frozenset({"no_build", "database"})

_URL_SCHEMES = ("https://", "http://")

_QUIET_REASONS = frozenset({"off", "paused"})

# The clock of the pause and of the attribution cache; tests replace it.
clock: Callable[[], float] = time.monotonic


class _Failure(Exception):
    """A call that ends in the fallback: the reason word, and whether it pauses."""

    def __init__(self, reason: str, *, pause: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pause = pause


class _State:
    """The pause and the cached attribution, shared by the event loop (search)
    and the thread pool (attribution), under one lock."""

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
    return bool(settings.station_module_url and settings.station_module_token)


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
    # `off` and `paused` are states, not faults: INFO, so a VIATOR without the
    # module, or a paused one, does not fill the log with warnings.
    level = logging.INFO if failure.reason in _QUIET_REASONS else logging.WARNING
    log.log(level, "station_module.fallback reason=%s", failure.reason)


def _url(path: str) -> str:
    return settings.station_module_url.rstrip("/") + path


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.station_module_token}"}


def _error_code(response: httpx.Response) -> str | None:
    """The `code` word of a module error body, or None."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _status_failure(response: httpx.Response) -> _Failure:
    """The failure a status other than 200 means (20.16 of the module's design)."""
    status = response.status_code
    if status == 503:
        code = _error_code(response)
        if code == "busy":
            return _Failure("busy", pause=False)
        return _Failure("status_503", pause=code in _PAUSING_503_CODES)
    return _Failure(f"status_{status}", pause=status in _PAUSING_STATUSES)


def _json_body(response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise _status_failure(response)
    try:
        return response.json()
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


async def _post_search(q: str, user_id: uuid.UUID) -> httpx.Response:
    headers = {
        **_authorization(),
        USER_HEADER: str(user_id),
        "Content-Type": "application/json",
    }
    body = json.dumps({"q": q}).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, trust_env=False) as client:
            return await client.post(_url(SEARCH_PATH), content=body, headers=headers)
    except httpx.TimeoutException:
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
        response = await _post_search(q, user_id)
        return _stations(_json_body(response))
    except _Failure as failure:
        _fail(failure)
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


def _get_attribution() -> httpx.Response:
    try:
        with httpx.Client(timeout=ATTRIBUTION_TIMEOUT, trust_env=False) as client:
            return client.get(_url(ATTRIBUTION_PATH), headers=_authorization())
    except httpx.TimeoutException:
        raise _Failure("timeout", pause=True) from None
    except httpx.HTTPError:
        raise _Failure("network", pause=True) from None


def _cached_attribution() -> dict[str, Any] | None:
    with _state.lock:
        cached = _state.attribution
    if cached is not None and clock() - cached[0] < ATTRIBUTION_TTL_SECONDS:
        return cached[1]
    return None


def attribution() -> dict[str, Any] | None:
    """The module's attribution text, cached 60 s; `None` when the module is
    not configured, is paused or fails (and a failure forgets the cache)."""
    try:
        if not enabled():
            raise _Failure("off", pause=False)
        if _paused():
            raise _Failure("paused", pause=False)
        cached = _cached_attribution()
        if cached is not None:
            return cached
        answer = _attribution_of(_json_body(_get_attribution()))
    except _Failure as failure:
        with _state.lock:
            _state.attribution = None
        _fail(failure)
        return None
    with _state.lock:
        _state.attribution = (clock(), answer)
    return answer
