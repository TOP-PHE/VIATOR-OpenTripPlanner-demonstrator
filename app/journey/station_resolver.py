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
    `COVERAGE_OEBB_STOP_SERIES` (`DELFI_stop_key`: measured 27 Oct, CLAUDE.md §3);
  * anything else (an OJP stop, a provider with no feed key: a derived feed, an upload whose
    file is unknown): not asked.

**What is kept**: a key answered with exactly one station gives it; a code of several stations
gives none (the matching treats it as unknown). Every answer, a key placed nowhere included,
is kept `CACHE_SECONDS` in this process (`_cache`), so a coverage run or a search asks each
stop once a while; a call that failed keeps nothing, and asks no more in that resolution.

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


class _Cache:
    """The module's answers, by key: a station, or None for a key placed nowhere; each kept
    `CACHE_SECONDS`, the oldest dropped beyond `CACHE_MAX`. Under one lock: a coverage run
    resolves in the worker's executor thread, a search on the web's event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[ModuleKey, tuple[float, ResolvedStation | None]] = OrderedDict()

    def get(self, key: ModuleKey) -> tuple[bool, ResolvedStation | None]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            if entry[0] <= clock():
                del self._entries[key]
                return False, None
            return True, entry[1]

    def put(self, key: ModuleKey, station: ResolvedStation | None) -> None:
        with self._lock:
            self._entries[key] = (clock() + CACHE_SECONDS, station)
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
    stop: CanonicalStopPlace, feeds: SessionFeeds, oebb_series: str | None
) -> list[ModuleKey]:
    """The keys `stop` is asked by, the one to trust first first; none when the module cannot
    know it."""
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
    if stop.source == OEBB_SOURCE and oebb_series is not None:
        number = _oebb_number(stop)
        return [] if number is None else [(CODE, oebb_series, number)]
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


def _stations_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, ResolvedStation | None]:
    """Each key's station: the one station the module answered, or None for a key of several."""
    found: dict[str, dict[str, str | None]] = {}
    for row in rows:
        found.setdefault(row["key"], {})[row["viator_id"]] = row["parent_viator_id"]
    stations: dict[str, ResolvedStation | None] = {}
    for key, ids in found.items():
        if len(ids) == 1:
            ((vid, parent),) = ids.items()
            stations[key] = ResolvedStation(station_id=vid, parent_id=parent)
        else:
            stations[key] = None
    return stations


def resolver_for_session(
    config: Mapping[str, Any], upload_sha256: Mapping[str, str], oebb_series: str | None
) -> ModuleStationResolver:
    """The resolver of a session's journeys: its `config` (`sessions.config`), the sha256 of
    the latest file uploaded for each provider (`station_extract_upload.latest_upload_sha256`)
    and the code series of ÖBB's numbers. A configuration that does not read gives a resolver
    that places only ÖBB's stops."""
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
    series of `COVERAGE_OEBB_STOP_SERIES`, None to leave ÖBB's stops out)."""

    def __init__(self, feeds: SessionFeeds, oebb_series: str | None) -> None:
        self._feeds = feeds
        self._oebb_series = oebb_series

    async def resolve(
        self, stops: Sequence[CanonicalStopPlace]
    ) -> Mapping[StopKey, ResolvedStation]:
        """The stations of the stops the module places; a stop it does not is absent."""
        wanted = {
            (stop.source, stop.ref): keys_of(stop, self._feeds, self._oebb_series) for stop in stops
        }
        answers: dict[ModuleKey, ResolvedStation | None] = {}
        missing: list[ModuleKey] = []
        for keys in wanted.values():
            for key in keys:
                known, station = _cache.get(key)
                if known:
                    answers[key] = station
                elif key not in answers:
                    missing.append(key)
        asked, reason = await self._ask_missing(missing, answers)
        placed: dict[StopKey, ResolvedStation] = {}
        for stop_key, keys in wanted.items():
            for key in keys:
                station = answers.get(key)
                if station is not None:
                    placed[stop_key] = station
                    break
        log.info(
            "station_resolver.resolved stops=%d placed=%d asked=%d reason=%s",
            len(wanted),
            len(placed),
            asked,
            reason,
        )
        return placed

    async def _ask_missing(
        self, missing: list[ModuleKey], answers: dict[ModuleKey, ResolvedStation | None]
    ) -> tuple[int, str]:
        """Ask the keys not kept, call by call, keeping every answer; stop at the first call
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
                station = stations.get(value)
                _cache.put(key, station)
                answers[key] = station
        return asked, "ok"
