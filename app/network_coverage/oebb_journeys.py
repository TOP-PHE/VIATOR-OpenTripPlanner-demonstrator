"""ÖBB's answer in a coverage cell, as canonical journeys (#303 PR 4a).

The check asks ÖBB HAFAS about each cell (`external_verify.fetch_oebb_two_step`, and its later
pages: `fetch_cell_answer` below). Its answer becomes
canonical journeys through the one HAFAS adapter (`canonical_adapters.hafas_adapter`, which makes
Vienna times UTC once), stored in `network_coverage_results.external_journeys`.

`scoring_legs` is the view of those journeys the alignment scorer compares, so that it keeps
today's rules (design D5): the values the check's own parser gave before #303, each end's stop
TOKEN from `external_verify.oebb_stop_id` (`UIC:…`, `OEBB:…`, or `UNKNOWN_ENDPOINT` when ÖBB gave no
id; never the lid, CLAUDE.md §3), the coverage mode rule (`oebb_mode`: an unknown category is
RAIL), the label HAFAS shows as route name, UTC times. The cell modal, the export and the share
page read the journeys themselves (`app/static/js/canonical_view.js`, #303 PR 4b).

Two differences from the parser this replaces, both fixes: a time past midnight (`01024500`, 02:45
the next day) was read as 01:02 on the connection's day; and a connection without a date was kept
without times, it is now dropped (the adapter's rule, the same as `/journey`'s).

**Later pages** (`fetch_cell_answer`). One `TripSearch` gives ÖBB's next 5 connections from the
run's departure time (`numF: 5`), a median of 3.8 h on run 4700065d, while VIATOR's journeys in the
same cell span a median of 6.3 h: a VIATOR journey after ÖBB's last connection is not compared
(`alignment.reference_window`, M9). So the check asks ÖBB again from just after its last
departure, one `TripSearch` a page between the stations page 1 resolved, until ÖBB's last
departure reaches VIATOR's last first boarding in the cell (`alignment.last_boarding_to_cover`),
or `PagePolicy.max_pages` (`COVERAGE_VERIFY_MAX_PAGES`) is reached. The loop mirrors
`hafas_client.fetch_plan_paginated` and shares its helpers (`trip_normalize`): a connection seen on
an earlier page is dropped, an empty or all-seen page ends it, the cell's pages share one time
budget, and a later page that fails keeps the earlier pages and is recorded (`page_error`), never
turned into the cell's error. The run's pause (`COVERAGE_VERIFY_SLEEP_MS`) is kept between pages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ..journey.canonical import CanonicalJourney, CanonicalLeg, CanonicalLocation, CanonicalTiming
from ..journey.canonical_adapters import hafas_adapter
from ..journey.trip_normalize import (
    dedup_batch_and_track_latest_dep,
    next_anchor_or_none,
    renumbered,
)
from . import external_verify
from .external_verify import HafasTripPayload, OebbStopId, VerifyResult, oebb_stop_id

log = logging.getLogger(__name__)

# An ÖBB end with NO id at all. A bare None would reach the fingerprint as the coordinate
# fallback `"?,?"`, exactly what a VIATOR leg with neither id nor position produces: with equal
# mode, minutes and route name the two would hash identically and score a false `agree` on zero
# endpoint evidence. This token is equal to nothing VIATOR emits
# (`test_two_id_less_itineraries_do_not_exact_match`).
UNKNOWN_ENDPOINT = "OEBB:?"

_NO_ID = OebbStopId(token=None, raw=None, source=None)


def journeys_from_answer(answer: HafasTripPayload) -> list[CanonicalJourney]:
    """ÖBB's connections as canonical journeys; none when ÖBB found none or could not be asked
    (the verdict says which)."""
    if not answer.verdict.ok or not answer.payload:
        return []
    return hafas_adapter.journeys_from_trip_search(answer.payload)


# Why a cell's check asked no further page (`CellAnswer.stopped`, kept in the cell's detail).
STOP_COVERED = "covered"  # ÖBB's last departure reached VIATOR's last first boarding
STOP_MAX_PAGES = "max_pages"  # COVERAGE_VERIFY_MAX_PAGES reached first
STOP_NO_MORE = "no_more"  # a later page brought no connection: ÖBB has none later
STOP_NO_PROGRESS = "no_progress"  # a later page brought only connections already seen
STOP_BUDGET = "budget"  # the cell's time budget was spent
STOP_ERROR = "error"  # a later page failed: the earlier pages are kept (`page_error`)

# Below this, what is left of a cell's budget is not worth a request (the twin of
# `hafas_client._MIN_PAGE_BUDGET_MS`).
_MIN_PAGE_BUDGET_S = 0.5


@dataclass(frozen=True)
class PagePolicy:
    """How far a cell's check may page, from the run's frozen knobs. The default asks one page,
    as before paging existed.

    `pause_s` is waited before each later page (`COVERAGE_VERIFY_SLEEP_MS`); `page_timeout_s` is a
    later page's own timeout (`COVERAGE_VERIFY_TIMEOUT_S`), cut to what is left of `budget_s`, the
    time all of a cell's pages may take, counted from its first request."""

    max_pages: int = 1
    pause_s: float = 0.0
    page_timeout_s: float = 30.0
    budget_s: float = 60.0


@dataclass(frozen=True)
class CellAnswer:
    """ÖBB's answer for one cell, over every page asked.

    `verdict` is page 1's (an error or no route there ends the check), with the connections of
    the later pages counted in and their shortest considered. `journeys` are every page's, a
    connection seen on an earlier page left out, renumbered when a later page added any (each
    page numbers its own from 0). `stopped` is None when paging was not considered (ÖBB gave no
    connection, or VIATOR none to reach)."""

    verdict: VerifyResult
    journeys: list[CanonicalJourney]
    pages: int = 1
    stopped: str | None = None
    page_error: str | None = None

    def pages_detail(self) -> dict[str, Any] | None:
        """What the cell's detail keeps of the paging (`oebb_pages`); None when not considered."""
        if self.stopped is None:
            return None
        detail: dict[str, Any] = {"asked": self.pages, "stopped": self.stopped}
        if self.page_error is not None:
            detail["error"] = self.page_error
        return detail


async def fetch_cell_answer(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    depart_at: datetime,
    client: httpx.AsyncClient,
    until: datetime | None,
    policy: PagePolicy,
) -> CellAnswer:
    """Ask ÖBB about one cell from `depart_at`, then page on until its last departure reaches
    `until` (VIATOR's last first boarding to cover, `alignment.last_boarding_to_cover`; None asks
    one page) or a stop condition of the module docstring holds. Never raises: page 1's failure is
    its verdict's error, as before; a later page's is `page_error`."""
    started = time.monotonic()
    first = await external_verify.fetch_oebb_two_step(
        from_lat=from_lat,
        from_lon=from_lon,
        to_lat=to_lat,
        to_lon=to_lon,
        depart_at=depart_at,
        client=client,
    )
    journeys = journeys_from_answer(first)
    from_lid, to_lid = first.from_lid, first.to_lid
    if until is None or not journeys or not from_lid or not to_lid:
        return CellAnswer(first.verdict, journeys)
    pages = _Pages(journeys, until)
    asked, stopped, page_error = 1, None, None
    while stopped is None:
        anchor, stopped = pages.next_anchor(asked, policy.max_pages)
        if anchor is None:
            break
        timeout_s = await _pause_within_budget(policy, started)
        if timeout_s is None:
            stopped = STOP_BUDGET
            break
        page = await external_verify.fetch_oebb_trip_search(
            from_lid=from_lid,
            to_lid=to_lid,
            depart_at=anchor,
            client=client,
            timeout_s=timeout_s,
        )
        asked += 1
        stopped, page_error = pages.take(page)
    if page_error is not None:
        log.warning(
            "coverage.oebb_pages stopped at page %d (kept %d connections): %s",
            asked,
            len(pages.journeys),
            page_error,
        )
    return CellAnswer(
        _merged_verdict(first.verdict, pages.later),
        renumbered(pages.journeys) if pages.later else journeys,
        asked,
        stopped,
        page_error,
    )


async def _pause_within_budget(policy: PagePolicy, started: float) -> float | None:
    """Wait the run's pause before a later page and return that page's timeout, or None (without
    waiting) when the cell's budget would be spent before the page could be asked."""
    left = policy.budget_s - (time.monotonic() - started) - policy.pause_s
    if left < _MIN_PAGE_BUDGET_S:
        return None
    if policy.pause_s > 0:
        await asyncio.sleep(policy.pause_s)
    return min(policy.page_timeout_s, left)


class _Pages:
    """The bookkeeping of a cell's pages: the journeys so far, the fingerprints seen, ÖBB's latest
    departure and the departure to reach (`trip_normalize`'s helpers, as `/journey`'s paging)."""

    def __init__(self, first: list[CanonicalJourney], until: datetime) -> None:
        # Page 1's journeys are kept as they are (one page stays exactly as before paging); its
        # fingerprints are only remembered, so a later page's repeat of one is dropped.
        self.journeys = list(first)
        self.later: list[CanonicalJourney] = []
        self._seen: set[str] = set()
        _, self._latest = dedup_batch_and_track_latest_dep(first, self._seen)
        self._until_ts = until.timestamp()

    def next_anchor(self, asked: int, max_pages: int) -> tuple[datetime | None, str | None]:
        """The next page's departure time, or None and why no page is asked."""
        if self._latest is None:
            return None, STOP_NO_PROGRESS
        anchor = next_anchor_or_none(self._latest, self._until_ts)
        if anchor is None:
            return None, STOP_COVERED
        if asked >= max_pages:
            return None, STOP_MAX_PAGES
        return anchor, None

    def take(self, page: HafasTripPayload) -> tuple[str | None, str | None]:
        """Add a later page: (why to stop, the page's error), or (None, None) to go on."""
        if page.verdict.error is not None:
            return STOP_ERROR, page.verdict.error
        batch = journeys_from_answer(page)
        if not batch:
            return STOP_NO_MORE, None
        new, latest = dedup_batch_and_track_latest_dep(batch, self._seen)
        if latest is not None and (self._latest is None or latest > self._latest):
            self._latest = latest
        if not new:
            return STOP_NO_PROGRESS, None
        self.later.extend(new)
        self.journeys.extend(new)
        return None, None


def _merged_verdict(first: VerifyResult, later: list[CanonicalJourney]) -> VerifyResult:
    """Page 1's verdict with the later pages' connections counted in, and their shortest taken
    as the best when it is shorter (HAFAS's `dur` and `chg`, as page 1's summary reads them)."""
    if not later:
        return first
    best_duration, best_transfers = first.best_duration_seconds, first.best_transfers
    for journey in later:
        duration = journey.duration_seconds
        if duration > 0 and (best_duration is None or duration < best_duration):
            best_duration, best_transfers = duration, journey.source_transfer_count
    return first.model_copy(
        update={
            "num_connections": first.num_connections + len(later),
            "best_duration_seconds": best_duration,
            "best_transfers": best_transfers,
        }
    )


def oebb_mode(category: str | int | None) -> str:
    """A HAFAS product category ("ICE", "RJ", "S", "Bus") in the mode vocabulary the scorer's
    fingerprint hashes. Kept from the check's own parser, not the adapter's `map_category_to_mode`:
    an unknown category ("CJX") is RAIL here and so still matches an OTP `RAIL` leg (design D5;
    #304 compares mode families instead). HAFAS sometimes sends a number: `str()` first (#213)."""
    cat_upper = str(category or "").upper()
    if not cat_upper:
        return "TRANSIT"
    if "BUS" in cat_upper:
        return "BUS"
    if "TRAM" in cat_upper or cat_upper == "STR":
        return "TRAM"
    if "METRO" in cat_upper or "SUBWAY" in cat_upper or cat_upper in ("U", "U-BAHN"):
        return "SUBWAY"
    if "FERRY" in cat_upper or cat_upper in ("SHIP", "BOAT"):
        return "FERRY"
    return "RAIL"


def scoring_legs(journey: CanonicalJourney) -> list[dict[str, Any]]:
    """The leg dicts `transit_fingerprint` and the fuzzy tier read, walks included (the scorer
    strips them)."""
    return [
        {
            "mode": _mode(leg),
            "from_stop_id": _identity(leg.origin).token or UNKNOWN_ENDPOINT,
            "to_stop_id": _identity(leg.destination).token or UNKNOWN_ENDPOINT,
            "departure": _utc(leg.departure),
            "arrival": _utc(leg.arrival),
            "route_short_name": _route_name(leg),
        }
        for leg in journey.legs
    ]


def _mode(leg: CanonicalLeg) -> str:
    """A ride: its category by `oebb_mode`. A walk or transfer section: WALK. Any other section
    (none in ÖBB's answers so far): TRANSIT, which the scorer keeps, as before."""
    if leg.service is not None:
        category = leg.service.product_category
        return oebb_mode(category.code if category is not None else None)
    return "WALK" if leg.source_mode == "WALK" else "TRANSIT"


def _identity(end: CanonicalLocation) -> OebbStopId:
    """The stop token of one end, computed as the check's parser computed it: `oebb_stop_id` on
    ÖBB's `extId` and the lid."""
    stop = end.stop_place
    if stop is None:
        return _NO_ID
    return oebb_stop_id(stop.codes.get("OEBB_EXT"), lid=stop.ref)


def _route_name(leg: CanonicalLeg) -> str | None:
    """The label HAFAS shows ("RJ 1141"), else its line: the train number the fuzzy tier reads."""
    service = leg.service
    if service is None:
        return None
    return service.published_name or service.line


def _utc(value: CanonicalTiming | None) -> str | None:
    return value.timetabled.astimezone(UTC).isoformat() if value is not None else None
