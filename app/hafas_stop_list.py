"""ÖBB's stop list: the places ÖBB's HAFAS planner answered, sent to the station module (MSMM) as
a feed of their own (its design 22.20, the owner's decisions 69 to 73 of 2026-11-13).

VIATOR compares its journeys with ÖBB's and asks the module the station of each ÖBB stop by its
HAFAS number (`extId`). ÖBB's NAP timetable carries no such number, and the series
`DELFI_stop_key` answers it in Germany and Austria only. So VIATOR keeps the places ÖBB's planner
answered in its coverage runs (`app/network_coverage/hafas_places.py`, decision 70, way a), and
sends them to the module, which matches them to its stations (steps 1 to 3 of its 22.6, the
review queue for what is not certain) and does nothing else with them: no station minted, no
coverage or presence, no rule, no `FD` code (its 22.20, point 3). VIATOR then asks an ÖBB stop
by `{"feed": "hafas:…", "stops": [numbers]}` first (`station_resolver.OEBB_STOP_LIST`).

**The extract** (the module's 22.2, as every feed's: `station_extract.write_listed_extract`):
one row per place of the list; `stop_key` the HAFAS number; `stop_name`, `lat`, `lon` as HAFAS
last gave them; `iso2_point` from the position; `modes` `RAIL_MAIN`; `n_calls` the train calls
seen there; `codes` `extId=<number>`, and `uic=<code>` when ÖBB's `gidL` stated one; the
provider columns `OEBB_HAFAS`, its label, `AT`, no provenance (VIATOR's own collection).

- **Feed key** `hafas:` and 16 hexadecimal digits of the sha256 of the planner's host and path
  (`station_module.hafas_identity` of `external_verify.OEBB_ENDPOINT`): one key from one list to
  the next, so each list is a new version of one feed.
- **Version** the sha256 of the list's places (`list_version`): a list that has not changed is
  "already held" and not sent again; the extractor version is the extractor's and
  `+hafas-stop-list-<n>` (`EXTRACTOR_VERSION`), raised when the list's form changes.
- **Operators**: one, `ÖBB`, on every row (`OPERATOR`). The module holds a new version of a feed
  for review when its set of operators changes (its 22.4, point 5; 22.20 "Left open", 1): with
  the operators of the legs, the list would gain one at most runs and every such version would
  wait for an administrator. A stop list has no rule for operators to scope. The table keeps the
  operators of the legs (`hafas_places.operators`).

**When**: the worker's thread (`start_in_worker`) adds the ended runs not yet in the list, at
start (the runs already stored) and every `POLL_SECONDS`, and sends the list when its version is
not the one last held by the module, by the two calls of every extract
(`station_extract_upload.send_listed_feed`: `HEAD`, then `PUT` when the module lacks it, its
retries and its one pass at a time). A list the module lacks after a failure is sent again at
the next poll. Without `STATION_MODULE_URL` and `STATION_MODULE_TOKEN` nothing is sent; the list
is kept all the same.

**The module uses it once an administrator accepts it** on its "Timetable feeds" screen, with its
licence and attribution (decision 69); until then it is `pending` and answers nothing.

**By hand** (`python -m app.hafas_stop_list`): add the runs, send the list, print the totals and
the outcome; `--totals` prints the totals only (counts, never a number with its station).

**Logs**: counts, the outcome and its reason word; never a number, a name or a position.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import station_module
from .journey.station_resolver import OEBB_STOP_LIST
from .models import HafasPlace
from .network_coverage import hafas_places
from .station_extract_common import RAIL_MAIN, Station

log = logging.getLogger(__name__)

PROVIDER_ID = "OEBB_HAFAS"
PROVIDER_LABEL = "ÖBB HAFAS stop list (places of VIATOR's coverage runs)"
PROVIDER_COUNTRY = "AT"
OPERATOR = "ÖBB"
# Raised whenever the rows of the list change form (another code, another column's rule): the
# module then takes the same places again as a new version.
LIST_FORM = "hafas-stop-list-1"
# A pass may wait this long for the module (its busy answers and its half-hour window).
SEND_BUDGET_SECONDS = 30 * 60.0
POLL_SECONDS = 10 * 60.0
FILE_NAME = "hafas-stop-list.csv.gz"

# The outcome words of a send besides the sender's (`station_extract_upload` states).
OFF = "off"
EMPTY = "empty"
NOT_WRITTEN = "not_written"


@dataclass(frozen=True)
class ListedPlace:
    """One place of the list as the extract writes it."""

    number: str
    name: str
    lat: float
    lon: float
    uic: str | None
    calls: int


def load_places(db: Any) -> list[ListedPlace]:
    """The list's places, by number."""
    rows = db.execute(
        select(
            HafasPlace.ext_id,
            HafasPlace.name,
            HafasPlace.lat,
            HafasPlace.lon,
            HafasPlace.uic,
            HafasPlace.times_seen,
        ).order_by(HafasPlace.ext_id)
    ).all()
    return [ListedPlace(*row) for row in rows]


def list_version(places: Iterable[ListedPlace]) -> str:
    """The version of a list: the sha256 of its places (number, name, position to the
    extract's seven decimals, UIC, calls), by number. Another value anywhere is another
    version; the same places are the same version."""
    digest = hashlib.sha256()
    for place in sorted(places, key=lambda one: one.number):
        fields = (
            place.number,
            place.name,
            format(place.lat, ".7f"),
            format(place.lon, ".7f"),
            place.uic or "",
            str(place.calls),
        )
        digest.update(("\t".join(fields) + "\n").encode("utf-8"))
    return digest.hexdigest()


def stations_of(places: Iterable[ListedPlace]) -> dict[str, Station]:
    """The list as the extract writer reads stations: each place's number as its key and in
    `extId=`, its UIC in `uic=`, its calls as rail calls, the one operator."""
    stations: dict[str, Station] = {}
    for place in places:
        station = Station(key=place.number, name=place.name, lat=place.lat, lon=place.lon)
        station.calls = Counter({RAIL_MAIN: place.calls})
        station.operators = {OPERATOR}
        station.add_code("extId", place.number)
        station.add_code("uic", place.uic)
        stations[place.number] = station
    return stations


def extractor_version() -> str:
    from . import station_extract

    return f"{station_extract.EXTRACTOR_VERSION}+{LIST_FORM}"


@dataclass(frozen=True)
class Sent:
    """What a send did: the sender's state (`sent`, `held`, `refused`, `unavailable`,
    `skipped`, `cancelled`) or `off`, `empty`, `not_written`; its reason word; the version and
    the places of the list."""

    state: str
    reason: str | None = None
    version: str | None = None
    places: int = 0

    @property
    def held(self) -> bool:
        """The module holds this version of the list."""
        return self.state in ("sent", "held")


def send_list(
    places: Sequence[ListedPlace],
    *,
    budget: float = SEND_BUDGET_SECONDS,
    transport: Any = None,
    clock: Any = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Sent:
    """Write the list's extract to a temporary folder and send it (`HEAD`, then `PUT` when the
    module lacks that version). Never raises an `Exception`."""
    if not station_module.enabled():
        return Sent(OFF)
    if not places:
        return Sent(EMPTY)
    # The extractor and its sender need `fcntl`: imported where they are used.
    from . import station_extract, station_extract_upload

    version = list_version(places)
    feed = station_extract.ListedFeed(
        feed_key=OEBB_STOP_LIST,
        feed_version=version,
        extractor_version=extractor_version(),
        provider_id=PROVIDER_ID,
        provider_label=PROVIDER_LABEL,
        provider_country_iso=PROVIDER_COUNTRY,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="viator-hafas-") as folder:
            try:
                made = station_extract.write_listed_extract(
                    stations_of(places), feed, Path(folder) / FILE_NAME, extracted_at=now()
                )
            except station_extract.ExtractError as refused:
                return _logged(Sent(NOT_WRITTEN, refused.reason, version, len(places)))
            result = station_extract_upload.send_listed_feed(
                made,
                provider_id=PROVIDER_ID,
                budget=budget,
                patience=station_extract_upload.REFRESH_PATIENCE,
                transport=transport,
                clock=clock,
            )
    except Exception as exc:  # the temporary folder, never a reason to fail the caller
        log.warning("hafas_stop_list.failed error=%s", type(exc).__name__)
        return Sent("error", "error", version, len(places))
    if result.cancelled or not result.outcomes:
        return _logged(Sent("cancelled" if result.cancelled else "error", None, version))
    outcome = result.outcomes[0]
    return _logged(Sent(outcome.state, outcome.reason, version, len(places)))


def _logged(sent: Sent) -> Sent:
    log.info(
        "hafas_stop_list.%s places=%d reason=%s", sent.state, sent.places, sent.reason or "none"
    )
    return sent


class StopListWorker:
    """The worker's pass: add the ended runs, then send the list when the module does not hold
    its version yet. One instance a process; `pass_once` never raises."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        send: Callable[[Sequence[ListedPlace]], Sent] = send_list,
        collect: Callable[[Callable[[], Any]], Any] = hafas_places.collect_pending,
    ) -> None:
        self._factory = session_factory
        self._send = send
        self._collect = collect
        self.held_version: str | None = None

    def pass_once(self) -> Sent | None:
        """One pass: None when nothing was to be sent (the module off, an empty list, a version
        it already holds), else what the send did."""
        try:
            self._collect(self._factory)
        except Exception as exc:  # the table not there yet (a deploy before its migration)
            log.warning("hafas_places.pass_failed error=%s", type(exc).__name__)
            return None
        if not station_module.enabled():
            return None
        try:
            with self._factory() as db:
                places = load_places(db)
        except Exception as exc:
            log.warning("hafas_stop_list.read_failed error=%s", type(exc).__name__)
            return None
        if not places or list_version(places) == self.held_version:
            return None
        sent = self._send(places)
        if sent.held:
            self.held_version = sent.version
        return sent

    def serve_forever(self, poll_seconds: float = POLL_SECONDS) -> None:
        while True:
            try:
                self.pass_once()
            except Exception as exc:  # the thread must outlive any one pass
                log.warning("hafas_stop_list.pass_failed error=%s", type(exc).__name__)
            time.sleep(poll_seconds)


_started: threading.Thread | None = None


def start_in_worker(session_factory: Callable[[], Any] | None = None) -> threading.Thread:
    """Start the worker's stop-list thread (once a process): it adds the runs already stored
    at once, then the new ones and sends the list every `POLL_SECONDS`."""
    global _started
    if _started is not None and _started.is_alive():
        return _started
    if session_factory is None:
        from .db import SessionLocal

        session_factory = SessionLocal
    worker = StopListWorker(session_factory)
    _started = threading.Thread(target=worker.serve_forever, name="hafas-stop-list", daemon=True)
    _started.start()
    return _started


def main(argv: Iterable[str] | None = None) -> int:
    """`python -m app.hafas_stop_list [--totals]` (module docstring)."""
    parser = argparse.ArgumentParser(description="ÖBB's stop list for the station module")
    parser.add_argument(
        "--totals", action="store_true", help="print the list's totals; add and send nothing"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    from .db import SessionLocal

    sent: Sent | None = None
    if not args.totals:
        hafas_places.collect_pending(SessionLocal)
        with SessionLocal() as db:
            places = load_places(db)
        sent = send_list(places)
    with SessionLocal() as db:
        lines = hafas_places.totals(db).lines()
    for line in lines:
        print(line)
    if sent is not None:
        print(f"stop list: {sent.state}" + (f" ({sent.reason})" if sent.reason else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
