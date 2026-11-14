"""The station module's stations of journeys' stops (#303 PR 5, design D8).

`ModuleStationResolver` is the `StationResolver` (app/journey/canonical/station_resolution.py)
backed by the station module (MSMM): each stop of a canonical journey gets the VIATOR id of its
station and its parent's (`station_id`, `station_parent_id`), from the module's call for the
stations of many stops (`station_module.stop_stations_outcome`, its design 22.18, decision 68).
Nothing compares `station_id` yet: the matching engine of #304 will (its design §6).

**Which key a stop is asked by** (`keys_of`):

  * a MOTIS stop (`motis:<dataset>`, `<dataset>_<local>`): the dataset is the provider's file
    stem, its id in small letters (`ingestion.staged_filename_for_format`), so the provider and
    its MSMM feed key (`station_extract.feed_key_for`, the key VIATOR sends its extracts under)
    are known; the module's member rows are the stations of the feed's extract (a GTFS
    `parent_station`, a NeTEx root `StopPlace`), so the stop's parent (`parentId`) is asked
    first, then the stop itself (a stop without a parent is its own station);
  * an OTP stop (`otp:<feed>`, `<feed>:<local>`): the provider of that id, the stop itself;
  * an ÖBB HAFAS stop (`hafas:oebb`): its number (`extId`, else the number of its `UIC:` or
    `OEBB:` token, as the coverage cell modal asks it) in the code series of
    `COVERAGE_OEBB_STOP_SERIES`, in their order (`DELFI_stop_key`: measured 27 Oct, CLAUDE.md §3;
    then `Trainline_EVA`, the module's design 22.19, for the numbers outside Germany and
    Austria); a series is asked only for the numbers the series before it places nowhere (the
    module's decision 72), so a code of several stations in one series is never settled by the
    next;
  * anything else (an OJP stop, a provider with no feed key: a derived feed, an upload whose
    file is unknown): not asked.

**What is kept**: a key answered with exactly one station gives it; a code of several stations
gives none (the matching treats it as unknown). Every answer, a key placed nowhere and a key of
several stations included, is kept `CACHE_SECONDS` in this process (`_cache`), so a coverage
run or a search asks each stop once a while; a call that failed keeps nothing, and asks no more
in that resolution.

**Never fails**: the module off, paused, at its ceiling or not answering leaves the stops it
did not place without a station, and the engine falls back to codes (design #304 §6). Nothing
of a stop, a key or an id is logged: one line per resolution, with counts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .. import station_module
from .canonical.canonical_stop_place import CanonicalStopPlace
from .canonical.station_resolution import ResolvedStation, StopKey

log = logging.getLogger(__name__)

# How long an answer is kept: the module builds once a day, and an id it gives is stable
# (an alias answers its survivor).
CACHE_SECONDS = 6 * 3600.0
# At most this many answers kept (a full eu19 coverage run asks a few thousand keys).
CACHE_MAX = 100_000

MOTIS_PREFIX = "motis:"
OTP_PREFIX = "otp:"
OEBB_SOURCE = "hafas:oebb"
OEBB_EXT = "OEBB_EXT"

# A key of the module: ("stop", feed key, stop key) or ("code", series, code).
ModuleKey = tuple[str, str, str]
STOP = "stop"
CODE = "code"

clock: Callable[[], float] = time.monotonic


class Unplaced(Enum):
    """An answer of the module that gives a key no station."""

    NOWHERE = "nowhere"  # no station carries the key
    SEVERAL = "several"  # several stations carry it: which one is unknown


# The module's answer to a key: its one station, or why it gives none.
Answer = ResolvedStation | Unplaced


def oebb_stop_series(value: object) -> tuple[str, ...]:
    """The code series of `COVERAGE_OEBB_STOP_SERIES`, in the order they are asked: its names
    separated by commas, trimmed, each once (`"DELFI_stop_key,Trainline_EVA"`; one name alone
    is one series). None for an empty setting: ÖBB's stops are then left out."""
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))


class _Cache:
    """The module's answers, by key, each kept `CACHE_SECONDS`, the oldest dropped beyond
    `CACHE_MAX`. Under one lock: a coverage run resolves in the worker's executor thread, a
    search on the web's event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[ModuleKey, tuple[float, Answer]] = OrderedDict()

    def get(self, key: ModuleKey) -> Answer | None:
        """The answer kept for `key`, or None when none is kept."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry[0] <= clock():
                del self._entries[key]
                return None
            return entry[1]

    def put(self, key: ModuleKey, answer: Answer) -> None:
        with self._lock:
            self._entries[key] = (clock() + CACHE_SECONDS, answer)
            self._entries.move_to_end(key)
            while len(self._entries) > CACHE_MAX:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _Cache()


def forget() -> None:
    """Forget every answer kept (tests, and nothing else)."""
    _cache.clear()


@dataclass(frozen=True)
class SessionFeeds:
    """The MSMM feed key of each feed of a session, by the name a journey's stop carries:
    MOTIS's dataset (the provider id in small letters) and OTP's feed id (the provider id)."""

    motis: Mapping[str, str]
    otp: Mapping[str, str]


def session_feeds(
    providers: Iterable[Mapping[str, Any]], feed_key_of: Callable[[Mapping[str, Any]], str | None]
) -> SessionFeeds:
    """The feeds of a session's providers (`ingestion.normalize_providers`), each provider's
    key given by `feed_key_of` (`station_extract.feed_key_for`, with the sha256 of its upload
    for an uploaded file); a provider without a key is left out."""
    motis: dict[str, str] = {}
    otp: dict[str, str] = {}
    for provider in providers:
        pid = provider.get("id")
        key = feed_key_of(provider)
        if not isinstance(pid, str) or not pid or key is None:
            continue
        motis[pid.lower()] = key
        otp[pid] = key
    return SessionFeeds(motis, otp)


def _local(ref: str | None, feed: str, separator: str) -> str | None:
    """The stop key of `ref` within `feed` (`<feed><separator><local>`), or None."""
    if ref is None or not ref.startswith(feed + separator):
        return None
    local = ref[len(feed) + len(separator) :]
    return local if station_module.stop_key_refused(local) is None else None


def _oebb_number(stop: CanonicalStopPlace) -> str | None:
    """The number ÖBB gives a stop: its `extId`, else the number of its `UIC:` or `OEBB:`
    token (the coverage cell modal's rule)."""
    for candidate in (stop.codes.get(OEBB_EXT), stop.uic, _token_number(stop.unresolved_token)):
        if candidate and station_module.resolve_text_refused(candidate) is None:
            return candidate
    return None


def _token_number(token: str | None) -> str | None:
    if token is None or ":" not in token:
        return None
    return token.split(":", 1)[1] or None


def keys_of(
    stop: CanonicalStopPlace, feeds: SessionFeeds, oebb_series: tuple[str, ...]
) -> list[ModuleKey]:
    """The keys `stop` is asked by, the one to trust first first; none when the module cannot
    know it. An ÖBB stop's are its number in each series of `oebb_series`, in their order."""
    if stop.source.startswith(MOTIS_PREFIX):
        dataset = stop.source[len(MOTIS_PREFIX) :]
        feed = feeds.motis.get(dataset)
        if feed is None:
            return []
        locals_ = (_local(stop.parent_ref, dataset, "_"), _local(stop.ref, dataset, "_"))
        return [(STOP, feed, local) for local in dict.fromkeys(locals_) if local is not None]
    if stop.source.startswith(OTP_PREFIX):
        feed_id = stop.source[len(OTP_PREFIX) :]
        feed = feeds.otp.get(feed_id)
        local = _local(stop.ref, feed_id, ":") if feed is not None else None
        return [] if feed is None or local is None else [(STOP, feed, local)]
    if stop.source == OEBB_SOURCE and oebb_series:
        number = _oebb_number(stop)
        return [] if number is None else [(CODE, series, number) for series in oebb_series]
    return []


def batches(keys: Iterable[ModuleKey]) -> list[tuple[str, str, list[str]]]:
    """The calls that ask `keys`: (kind, feed key or series, keys), each within the module's 50
    keys and its body cap (`station_module.stop_stations_body`)."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for kind, owner, value in dict.fromkeys(keys):
        grouped.setdefault((kind, owner), []).append(value)
    calls: list[tuple[str, str, list[str]]] = []
    for (kind, owner), values in grouped.items():
        batch: list[str] = []
        for value in values:
            if batch and not _fits(kind, owner, [*batch, value]):
                calls.append((kind, owner, batch))
                batch = []
            batch.append(value)
        if batch:
            calls.append((kind, owner, batch))
    return calls


def _fits(kind: str, owner: str, values: list[str]) -> bool:
    if len(values) > station_module.STOP_STATIONS_MAX:
        return False
    payload = (
        {"feed": owner, "stops": values} if kind == STOP else {"series": owner, "codes": values}
    )
    return len(json.dumps(payload).encode("utf-8")) <= station_module.STOP_STATIONS_BODY_MAX


async def _ask(kind: str, owner: str, values: list[str]) -> station_module.Outcome:
    if kind == STOP:
        return await station_module.stop_stations_outcome(feed=owner, stops=values)
    return await station_module.stop_stations_outcome(series=owner, codes=values)


def _stations_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, Answer]:
    """Each answered key's station: the one station the module answered, or `SEVERAL`; a key
    with no row is not in it (placed nowhere)."""
    found: dict[str, dict[str, str | None]] = {}
    for row in rows:
        found.setdefault(row["key"], {})[row["viator_id"]] = row["parent_viator_id"]
    stations: dict[str, Answer] = {}
    for key, ids in found.items():
        if len(ids) == 1:
            ((vid, parent),) = ids.items()
            stations[key] = ResolvedStation(station_id=vid, parent_id=parent)
        else:
            stations[key] = Unplaced.SEVERAL
    return stations


def _walk(
    keys: Sequence[ModuleKey], answers: Mapping[ModuleKey, Answer]
) -> tuple[ResolvedStation | None, list[ModuleKey]]:
    """The station a stop's `keys` give with the `answers` known, and the keys to ask before it
    is settled. The keys are trusted in their order: the first with one station gives it. A
    feed's stop keys not known yet are asked together; a code (ÖBB's number in a series) only
    once the code before it is known to be placed nowhere, and a code of several stations ends
    the list: the next series answers only where the one before is silent (the module's
    decision 72)."""
    to_ask: list[ModuleKey] = []
    for key in keys:
        answer = answers.get(key)
        if answer is None:
            to_ask.append(key)
            if key[0] == CODE:
                break
        elif isinstance(answer, ResolvedStation):
            return answer, to_ask
        elif answer is Unplaced.SEVERAL and key[0] == CODE:
            break
    return None, to_ask


def resolver_for_session(
    config: Mapping[str, Any], upload_sha256: Mapping[str, str], oebb_series: tuple[str, ...]
) -> ModuleStationResolver:
    """The resolver of a session's journeys: its `config` (`sessions.config`), the sha256 of
    the latest file uploaded for each provider (`station_extract_upload.latest_upload_sha256`)
    and the code series of ÖBB's numbers, in their order (`oebb_stop_series`). A configuration
    that does not read gives a resolver that places only ÖBB's stops."""
    from .. import ingestion, station_extract

    try:
        providers = ingestion.normalize_providers(dict(config))
    except ValueError:
        providers = []

    def key_of(provider: Mapping[str, Any]) -> str | None:
        return station_extract.feed_key_for(
            provider, upload_sha256=upload_sha256.get(str(provider.get("id")))
        )

    return ModuleStationResolver(session_feeds(providers, key_of), oebb_series)


class ModuleStationResolver:
    """The `StationResolver` of one session's journeys (its feeds) and of ÖBB's (the code
    series of `COVERAGE_OEBB_STOP_SERIES`, in their order: `oebb_stop_series`; none to leave
    ÖBB's stops out)."""

    def __init__(self, feeds: SessionFeeds, oebb_series: tuple[str, ...]) -> None:
        self._feeds = feeds
        self._oebb_series = oebb_series

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        """The stations of the stops the module places; a stop it does not is absent. Asked in
        rounds: the answers kept are read first, then each round asks the keys still needed (a
        feed's stops at once, ÖBB's numbers one series a round, only those the series before
        placed nowhere), until every stop is settled or a call fails."""
        wanted = {
            (stop.source, stop.ref): keys_of(stop, self._feeds, self._oebb_series) for stop in stops
        }
        answers: dict[ModuleKey, Answer] = {}
        for keys in wanted.values():
            for key in keys:
                kept = _cache.get(key)
                if kept is not None:
                    answers[key] = kept
        asked, reason = 0, "ok"
        while reason == "ok":
            missing = [key for keys in wanted.values() for key in _walk(keys, answers)[1]]
            if not missing:
                break
            count, reason = await self._ask_missing(missing, answers)
            asked += count
        placed: dict[StopKey, ResolvedStation] = {}
        for stop_key, keys in wanted.items():
            station, _ = _walk(keys, answers)
            if station is not None:
                placed[stop_key] = station
        log.info(
            "station_resolver.resolved stops=%d placed=%d asked=%d reason=%s",
            len(wanted),
            len(placed),
            asked,
            reason,
        )
        return placed

    async def _ask_missing(
        self, missing: list[ModuleKey], answers: dict[ModuleKey, Answer]
    ) -> tuple[int, str]:
        """Ask the keys not known, call by call, keeping every answer; stop at the first call
        that fails (the module off, paused, at its ceiling, not answering). The number of keys
        asked, and `ok` or the failure's word."""
        asked = 0
        for kind, owner, values in batches(missing):
            try:
                outcome = await _ask(kind, owner, values)
            except ValueError:
                # A body the module would refuse (a series setting it does not take): no call.
                return asked, "refused"
            if outcome.rows is None:
                return asked, outcome.reason
            asked += len(values)
            stations = _stations_of(outcome.rows)
            for value in values:
                key = (kind, owner, value)
                answer = stations.get(value, Unplaced.NOWHERE)
                _cache.put(key, answer)
                answers[key] = answer
        return asked, "ok"
