"""Client of the Multimodal Station Mapping module (MSMM).

The module is a separate service on VIATOR's Docker network. It answers
four calls VIATOR uses, under `/internal/v1/`, reached at
`settings.station_module_url` (normally `http://msmm-web:8000`, plain HTTP
inside the network like VIATOR's calls to MOTIS):

- `search(q, user_id)` — `POST /internal/v1/stations/search`, at most ten
  stations whose name or MERITS code matches `q`.
  `search_outcome(q, user_id)` is the same call in its detailed form (an
  `Outcome`: the rows, the reason word, the `Retry-After` of a 429);
  `search()` returns its rows. The journey typeahead (`POST
  /api/stations/suggest`) and the "Stations" page search use
  `search_outcome` (app/api/station_suggest.py `find_stations`), so that a
  text the module refuses (`TEXT_REFUSED`) is told from a failure.
  `admin_search_outcome(q, user_id)` is the same call made by an admin action (the coverage hubs' resolve route, when
  the near call finds nothing around a hub): it follows the lookup's pause
  rule below.
- `lookup(uics, user_id)` — `POST /internal/v1/stations/lookup`, the served
  stations of 1 to 20 distinct MERITS codes, each with its `parent_uic`;
  every code counts as one call on the person's limits. An `Outcome` too.
  Used by the coverage hubs' confirm and check routes
  (app/api/admin/network_coverage.py), never by the typeahead.
- `near_outcome(lat, lon, user_id, radius_m)` — `POST
  /internal/v1/stations/near`, at most five served stations within
  `radius_m` metres (1 to 300) of a position, nearest first, each with its
  `parent_uic` and its `distance_m`. One call on the person's limits, and
  one on the module's own near quota (30 a minute and 200 a day for one
  person, 500 a day for everybody). An `Outcome`. Used by the coverage
  hubs' resolve route ("Propose station codes"), which looks a hub up by
  its position first, and by its name (`admin_search_outcome`) only when
  the near call finds no station.
- `attribution()` — `GET /internal/v1/attribution`, the licence text of the
  module's sources, kept 60 s. Asynchronous, like search, so that its
  deadline is real. Awaited by `journey_page` (app/api/pages.py) for the
  licence notice under the title of /journey.

`search()` and `attribution()` return `None` on any failure, and the caller
then uses VIATOR's own behaviour (the master_stations list; no notice); the
typeahead does so on every failure of `search_outcome` but a refused text.
VIATOR must keep working without the module.

Rules this client keeps on purpose:

- **Its own headers, built from nothing.** `Authorization: Bearer <token>`,
  `Accept-Encoding: identity`, `X-Viator-User-Id` (search, lookup, near) and
  `Content-Type: application/json` (search, lookup, near). Nothing of the incoming request is ever forwarded, so a
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
  for everyone. **A lookup never starts the pause** (but honours one that
  runs): the pause protects every user's typeahead from a module that
  hangs, and a rare admin action must not switch it off for everyone, in
  particular when an older module answers 404 for the lookup's path. **A
  near call follows the lookup's rule**, for the same reasons (an older
  module answers 404 for `/stations/near`), **and so does an admin
  search** (`admin_search_outcome`).
- **A real deadline and a size cap**: 1 s for a whole search, lookup or near
  call, 0.5 s for an attribution (httpx's timeouts bound each read, not the
  call), and a body larger than 64 KiB (search, lookup, near) or 256 KiB
  (attribution) is refused unread;
  so is a compressed answer, which could inflate past the cap at once.
  All count as failures that pause. Any other error (a bad address, a
  token httpx cannot encode) falls back too, as `network`.
- **A bad row is dropped, never the whole answer.**
- **The log never holds the text, a body, the token or an exception's
  text**: one line `station_module.fallback reason=<word>` per fallback
  (at INFO for `off`, `paused`, `busy` and a 429; at WARNING otherwise);
  `station_module.refused reason=status_422` at WARNING when the module
  refuses a search text, which falls back to nothing (the typeahead's text
  rule mirrors the module's, so a refusal means the two have drifted)
  (`station_module.lookup_failed reason=<word>` for a lookup,
  `station_module.near_failed reason=<word>` for a near call,
  `station_module.admin_search_failed reason=<word>` for an admin search),
  never a code, never a name and never a position.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeGuard

import httpx

from .settings import settings

log = logging.getLogger(__name__)

SEARCH_PATH = "/internal/v1/stations/search"
LOOKUP_PATH = "/internal/v1/stations/lookup"
NEAR_PATH = "/internal/v1/stations/near"
ATTRIBUTION_PATH = "/internal/v1/attribution"
USER_HEADER = "X-Viator-User-Id"

# A lookup: 1 to 20 distinct codes, each 3 to 20 characters (the module's
# rule, its design 21.5 and 18.28 entry 256).
LOOKUP_MAX = 20
CODE_MIN = 3
CODE_MAX = 20

# A near call: a radius of 1 to 300 whole metres, at most five stations (the
# module's rule, its design 21.13 and decision 62).
NEAR_RADIUS_MIN = 1
NEAR_RADIUS_MAX = 300
NEAR_MAX_ROWS = 5
LATITUDE_MAX = 90.0
LONGITUDE_MAX = 180.0

# The `Retry-After` of a 429 is kept when it is a plain number of seconds in
# this range; a date, or anything else, gives None.
RETRY_AFTER_MAX = 86_400
# ASCII digits only: `\d` alone would also take digits of other scripts.
_DIGITS = re.compile(r"\d{1,6}", re.ASCII)
# The window words of a module 429 (its design 20.4 and 21.13) kept in an
# `Outcome`; any other `code` gives None. The last three refuse the near call
# alone: search and lookup go on.
LIMIT_CODES = frozenset(
    {
        "user_minute",
        "user_day",
        "all_minute",
        "all_day",
        "near_user_minute",
        "near_user_day",
        "near_all_day",
    }
)

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

# A scheme check of a link shown on a page, never a connection: S5332 does not apply.
_URL_SCHEMES = ("https://", "http://")  # NOSONAR python:S5332

# States and one-request refusals, not faults: logged at INFO.
_QUIET_REASONS = frozenset({"off", "paused", "busy", "status_429"})
# The reason of a search text the module refuses (its 422: fewer than 3
# characters once folded, for one). The typeahead's own text rule
# (app/api/station_suggest.py `normalise_query`) mirrors the module's and
# keeps such texts from being sent, so a refusal that still happens means
# the copied rules or the contract have drifted: a typeahead search logs it
# at WARNING as `station_module.refused`, never with the text. No fallback
# follows it, and no pause.
TEXT_REFUSED = "status_422"

# The clock of the pause and of the attribution cache; tests replace it.
clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Outcome:
    """The detailed result of a search, a lookup or a near call.

    `rows`: the checked rows, or None on failure. `reason`: `ok`, or the
    reason word of the failure (`off`, `paused`, `timeout`, `network`,
    `status_<n>`, `busy`, `shape`). `retry_after`: on a 429, the whole
    seconds of the module's `Retry-After` when it is a plain number from 1
    to 86,400, else None. `code`: on a 429, the module's window word (its
    `code`, such as `user_minute` or `near_user_day`) when it is one of
    `LIMIT_CODES`, else None: it tells a refusal of the near call alone from
    one of every call."""

    rows: list[dict[str, Any]] | None
    reason: str
    retry_after: int | None = None
    code: str | None = None


class _Failure(Exception):
    """A call that ends in the fallback: the reason word, whether it pauses,
    and the `Retry-After` and window word of a 429."""

    def __init__(
        self,
        reason: str,
        *,
        pause: bool,
        retry_after: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pause = pause
        self.retry_after = retry_after
        self.code = code

    def outcome(self) -> Outcome:
        return Outcome(None, self.reason, self.retry_after, self.code)


class _Answer:
    """The status, the (capped) body and the `Retry-After` of one answer."""

    def __init__(self, status: int, body: bytes, retry_after: int | None = None) -> None:
        self.status = status
        self.body = body
        self.retry_after = retry_after


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


def paused() -> bool:
    """True while the pause after a failure runs: a caller can then answer
    "the module did not answer" without making a call (the hub routes)."""
    return _paused()


def code_refused(code: str) -> str | None:
    """Why a station code would be refused by the module's lookup, as one
    word, or None. The module's own rule, mirrored (its design, 18.28 entry
    256): a surrogate first, then a control character (category Cc, NUL, tab
    and line end included), white space as Python counts it (a no-break space
    included), and a length outside 3 to 20 characters. The word names the
    rule, never the code."""
    if any(unicodedata.category(character) == "Cs" for character in code):
        return "surrogate"
    if any(unicodedata.category(character) == "Cc" for character in code):
        return "control character"
    if any(character.isspace() for character in code):
        return "white space"
    if not CODE_MIN <= len(code) <= CODE_MAX:
        return "length"
    return None


def _log_failure(event: str, reason: str) -> None:
    # `off`, `paused`, 429 and `busy` are states or one-request refusals, not
    # faults: INFO, so a VIATOR without the module, a paused one, or a busy
    # one, does not fill the log with warnings.
    level = logging.INFO if reason in _QUIET_REASONS else logging.WARNING
    log.log(level, "%s reason=%s", event, reason)


def _fail(failure: _Failure, event: str = "station_module.fallback") -> None:
    """Log one fallback line, start the pause when the failure calls for it,
    and forget the cached attribution: a module that just failed shows no notice."""
    if failure.pause:
        with _state.lock:
            _state.paused_until = clock() + PAUSE_SECONDS
            _state.attribution = None
    _log_failure(event, failure.reason)


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
    retry_after = _retry_after(response) if response.status_code == 429 else None
    return _Answer(response.status_code, bytes(body), retry_after)


def _retry_after(response: httpx.Response) -> int | None:
    """The `Retry-After` of an answer as whole seconds, 1 to 86,400, when it
    is a plain number; None for a date, an empty or any other value."""
    value = response.headers.get("retry-after", "").strip()
    if not _DIGITS.fullmatch(value):
        return None
    seconds = int(value)
    return seconds if 1 <= seconds <= RETRY_AFTER_MAX else None


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
    if status == 429:
        code = _error_code(answer)
        word = code if code in LIMIT_CODES else None
        return _Failure("status_429", pause=False, retry_after=answer.retry_after, code=word)
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


def _plain(value: str) -> bool:
    """True when the text holds no surrogate (Cs) and no control character
    (Cc). A lone surrogate, which JSON can carry, cannot be written as UTF-8:
    VIATOR's own JSON answer (or a database write) would fail with a 500."""
    return not any(unicodedata.category(character) in ("Cs", "Cc") for character in value)


def _text_or_null(value: Any) -> bool:
    """True for None or a plain text (see `_plain`)."""
    return value is None or (isinstance(value, str) and _plain(value))


def _station_row(item: Any) -> dict[str, Any] | None:
    """One row of the module's answer as the typeahead reads it, or None when
    it is not a usable row. The name is kept as sent (it may hold markup: the
    page escapes it), but a row whose name, code or country holds a surrogate
    or a control character is dropped."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    latitude = item.get("latitude")
    longitude = item.get("longitude")
    country = item.get("country_iso")
    uic = item.get("uic")
    if not isinstance(name, str) or not name or not _plain(name):
        return None
    if not (_is_number(latitude) and _is_number(longitude)):
        return None
    if not _text_or_null(country):
        return None
    if not isinstance(uic, str) or not _plain(uic):
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


async def _post(path: str, payload: dict[str, Any], user_id: uuid.UUID) -> _Answer:
    """One counted call (search, lookup or near): a POST of `payload` as JSON, on
    behalf of `user_id`, within the search's deadline and size cap."""
    headers = {
        **_headers(),
        USER_HEADER: str(user_id),
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8")
    try:
        async with (
            asyncio.timeout(SEARCH_DEADLINE),
            httpx.AsyncClient(timeout=SEARCH_TIMEOUT, trust_env=False) as client,
            client.stream("POST", _url(path), content=body, headers=headers) as response,
        ):
            return await _read(response, SEARCH_MAX_BYTES)
    except (TimeoutError, httpx.TimeoutException):
        raise _Failure("timeout", pause=True) from None
    except httpx.HTTPError:
        raise _Failure("network", pause=True) from None


async def search_outcome(q: str, user_id: uuid.UUID) -> Outcome:
    """At most ten stations of the module for `q` (already normalised by the
    caller), on behalf of the VIATOR user `user_id`, as an `Outcome`: the
    rows, or None with the reason word of the failure and, on a 429, the
    module's `Retry-After`. The pause rules of `search()`, which wraps it.
    A 422 (`TEXT_REFUSED`, the module does not take the text) starts no
    pause and is logged at WARNING as `station_module.refused`: the caller
    answers it without a fallback, and it means the copied text rules have
    drifted from the module's."""
    try:
        if not enabled():
            raise _Failure("off", pause=False)
        if _paused():
            raise _Failure("paused", pause=False)
        rows = _stations(_json_body(await _post(SEARCH_PATH, {"q": q}, user_id)))
    except _Failure as failure:
        refused = failure.reason == TEXT_REFUSED
        _fail(failure, "station_module.refused" if refused else "station_module.fallback")
        return failure.outcome()
    except Exception:
        # Anything else (an address httpx refuses, a token it cannot encode, a
        # JSON parser error) falls back too, and its text is never logged: an
        # exception about a header can quote the token.
        _fail(_Failure("network", pause=True))
        return Outcome(None, "network")
    return Outcome(rows, "ok")


async def search(q: str, user_id: uuid.UUID) -> list[dict[str, Any]] | None:
    """At most ten stations of the module for `q` (already normalised by the
    caller), on behalf of the VIATOR user `user_id`; `None` on any failure."""
    return (await search_outcome(q, user_id)).rows


async def admin_search_outcome(q: str, user_id: uuid.UUID) -> Outcome:
    """The same search as `search_outcome` (at most ten stations for `q`,
    already normalised by the caller), made by an admin action: the coverage
    hubs' resolve route, when the near call found nothing around a hub.

    **A failure never starts the pause**, as for `lookup` and `near_outcome`:
    the typeahead of every user must not switch to the fallback because of a
    rare admin click; a pause that runs is honoured (`paused`, no call).
    Failures are logged as `station_module.admin_search_failed reason=<word>`,
    never the text."""

    async def call() -> list[dict[str, Any]]:
        return _stations(_json_body(await _post(SEARCH_PATH, {"q": q}, user_id)))

    return await _without_pause("station_module.admin_search_failed", call)


def _checked_codes(uics: list[str]) -> list[str]:
    """The codes of a lookup, or ValueError: 1 to 20 codes, all distinct as
    written (capitals included), each passing `code_refused`. The module
    would refuse the whole call otherwise, after counting it."""
    if not 1 <= len(uics) <= LOOKUP_MAX:
        raise ValueError(f"a lookup takes 1 to {LOOKUP_MAX} codes")
    if len(set(uics)) != len(uics):
        raise ValueError("a lookup takes each code once")
    for code in uics:
        if not isinstance(code, str) or code_refused(code) is not None:
            raise ValueError("a code of the lookup is not one the module accepts")
    return list(uics)


def _lookup_row(item: Any, asked: set[str]) -> dict[str, Any] | None:
    """One row of a lookup answer: a search row whose code was asked for,
    plus `parent_uic` (a string or null); None when it is not a usable row."""
    row = _station_row(item)
    if row is None or row["uic"] not in asked:
        return None
    parent = item.get("parent_uic")
    if not _text_or_null(parent):
        return None
    row["parent_uic"] = parent
    return row


def _lookup_rows(payload: Any, codes: list[str]) -> list[dict[str, Any]]:
    """The usable rows of a lookup answer, at most one per code asked; a
    wrong top-level shape refuses the whole answer."""
    stations = payload.get("stations") if isinstance(payload, dict) else None
    if not isinstance(stations, list):
        raise _Failure("shape", pause=False)
    asked = set(codes)
    rows: list[dict[str, Any]] = []
    for item in stations:
        row = _lookup_row(item, asked)
        if row is not None:
            asked.discard(row["uic"])
            rows.append(row)
    return rows


async def lookup(uics: list[str], user_id: uuid.UUID) -> Outcome:
    """The module's served stations of `uics` (1 to 20 distinct codes), on
    behalf of the VIATOR user `user_id`, as an `Outcome`: at most one row per
    code, with its `parent_uic`; a code the module does not serve is simply
    absent. Each code counts as one call on the person's limits.

    Codes the module would refuse (more than 20, a repeated code, a code that
    fails `code_refused`) raise ValueError before any call: the caller sends
    codes it stored or that the module proposed, each once.

    **A failure never starts the pause** (the typeahead of every user must
    not switch to the fallback because of an admin action); a pause that runs
    is honoured (`paused`, no call). Failures are logged as
    `station_module.lookup_failed reason=<word>`, never a code."""
    codes = _checked_codes(uics)

    async def call() -> list[dict[str, Any]]:
        payload = _json_body(await _post(LOOKUP_PATH, {"uics": codes}, user_id))
        return _lookup_rows(payload, codes)

    return await _without_pause("station_module.lookup_failed", call)


async def _without_pause(
    event: str, call: Callable[[], Awaitable[list[dict[str, Any]]]]
) -> Outcome:
    """Run one admin call (lookup, near, admin search) as an `Outcome`: no call when the
    module is not configured (`off`) or the pause runs (`paused`); a failure
    is logged as `<event> reason=<word>` and **never starts the pause** (nor
    forgets the cached attribution), whatever its kind."""
    try:
        if not enabled():
            raise _Failure("off", pause=False)
        if _paused():
            raise _Failure("paused", pause=False)
        rows = await call()
    except _Failure as failure:
        _log_failure(event, failure.reason)
        return failure.outcome()
    except Exception:
        # As in search(): its text is never logged; and no pause.
        _log_failure(event, "network")
        return Outcome(None, "network")
    return Outcome(rows, "ok")


def position_refused(lat: Any, lon: Any) -> str | None:
    """Why a position would be refused by the module's near call, as a few
    words, or None: a latitude and a longitude must be finite numbers (not a
    truth value, not None) from -90 to 90 and from -180 to 180. The hubs'
    resolve route asks it before sending a hub's stored position."""
    if not (_is_number(lat) and -LATITUDE_MAX <= lat <= LATITUDE_MAX):
        return "a near call takes a latitude from -90 to 90"
    if not (_is_number(lon) and -LONGITUDE_MAX <= lon <= LONGITUDE_MAX):
        return "a near call takes a longitude from -180 to 180"
    return None


def _checked_position(lat: float, lon: float, radius_m: int) -> dict[str, Any]:
    """The body of a near call, or ValueError: a position `position_refused`
    accepts, and a whole radius of 1 to 300 metres. The module would refuse
    the call otherwise, after counting it."""
    refused = position_refused(lat, lon)
    if refused is not None:
        raise ValueError(refused)
    if (
        not isinstance(radius_m, int)
        or isinstance(radius_m, bool)
        or not NEAR_RADIUS_MIN <= radius_m <= NEAR_RADIUS_MAX
    ):
        raise ValueError(
            f"a near call takes a whole radius of {NEAR_RADIUS_MIN} to {NEAR_RADIUS_MAX} metres"
        )
    return {"lat": float(lat), "lon": float(lon), "radius_m": radius_m}


def _near_row(item: Any, radius_m: int) -> dict[str, Any] | None:
    """One row of a near answer: a search row with `parent_uic` (a string or
    null) and `distance_m` (a whole number of metres, 0 to the radius); None
    when it is not a usable row."""
    row = _station_row(item)
    if row is None:
        return None
    parent = item.get("parent_uic")
    distance = item.get("distance_m")
    if not _text_or_null(parent):
        return None
    if not isinstance(distance, int) or isinstance(distance, bool):
        return None
    if not 0 <= distance <= radius_m:
        return None
    row["parent_uic"] = parent
    row["distance_m"] = distance
    return row


def _near_rows(payload: Any, radius_m: int) -> list[dict[str, Any]]:
    """The usable rows of a near answer, in the module's order (nearest
    first), each code once, at most five; a wrong top-level shape refuses
    the whole answer."""
    stations = payload.get("stations") if isinstance(payload, dict) else None
    if not isinstance(stations, list):
        raise _Failure("shape", pause=False)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in stations:
        row = _near_row(item, radius_m)
        if row is None or row["uic"] in seen:
            continue
        seen.add(row["uic"])
        rows.append(row)
        if len(rows) >= NEAR_MAX_ROWS:
            break
    return rows


async def near_outcome(
    lat: float, lon: float, user_id: uuid.UUID, radius_m: int = NEAR_RADIUS_MAX
) -> Outcome:
    """The module's served stations within `radius_m` metres (1 to 300) of
    the position `lat`, `lon`, on behalf of the VIATOR user `user_id`, as an
    `Outcome`: at most five rows, nearest first, each with its `parent_uic`
    and its `distance_m`. One call on the person's limits at the module, and
    one on its near quota; a full near window is a 429 (`near_user_minute`,
    `near_user_day`, `near_all_day`) with its `Retry-After`, like any 429.

    A position or a radius the module would refuse raises ValueError before
    any call. **A failure never starts the pause**, as for `lookup`; a pause
    that runs is honoured. Failures are logged as
    `station_module.near_failed reason=<word>`, never the position."""
    body = _checked_position(lat, lon, radius_m)

    async def call() -> list[dict[str, Any]]:
        return _near_rows(_json_body(await _post(NEAR_PATH, body, user_id)), radius_m)

    return await _without_pause("station_module.near_failed", call)


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
    included. Awaited by `journey_page` (app/api/pages.py), only when the
    module is configured, for the licence notice under the title of
    /journey."""
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
