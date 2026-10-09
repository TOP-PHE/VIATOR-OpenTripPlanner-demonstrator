"""Recognise an engine's "this date is outside the loaded timetable" answer (#338).

A /journey search for a date the session's timetable does not cover used to
read "0 trips (error)" for MOTIS and "0 trips (no_route)" for OTP, while ÖBB
HAFAS, shown beside them, answered: the operator could not tell a date
problem from a routing gap. The two engines say it differently:

* MOTIS answers `GET /api/v6/plan` with **HTTP 400** and the JSON body
  `{"error": "query time <t> is outside of loaded timetable window [<from>, <to>["}`
  (MOTIS v2.11.2 `src/endpoints/routing.cc` raises `net::bad_request_exception`
  with that text; motis-project/net's query router serialises it as
  `{"error": e.what()}` with status 400). The window's bounds are UTC instants,
  the end excluded.
* OTP 2.9's `planConnection` answers **HTTP 200** with no edges and a
  `routingErrors` entry of code `OUTSIDE_SERVICE_PERIOD`. It names no window.

Nothing of the engine's text is passed on: the result is a fixed sentence,
plus the two instants re-printed from parsed numbers when MOTIS gave them.
VIATOR's own `graph_snapshots.service_period_*` is not used for the window:
it is read from the first GTFS file of a build only (and is the build day when
there is none), so it is not the window an engine has loaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

# The word the journey API and page key on.
OUTSIDE_TIMETABLE = "outside_timetable"

_MOTIS_PHRASE = "is outside of loaded timetable window"
# A MOTIS error longer than this is not the one recognised here.
_MAX_MESSAGE = 400
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")


@dataclass(frozen=True)
class OutsideTimetable:
    """The engine refused the date; the loaded window when the engine said it (UTC)."""

    valid_from: datetime | None = None
    valid_until: datetime | None = None

    def detail(self) -> str:
        """One line for the journey page and `journey_search_executions.error_message`."""
        text = "date outside the loaded timetable"
        if self.valid_from is not None and self.valid_until is not None:
            text += (
                f" (loaded: {self.valid_from:%Y-%m-%d %H:%M}"
                f" to {self.valid_until:%Y-%m-%d %H:%M} UTC)"
            )
        return text

    def window(self) -> dict[str, str] | None:
        """The window as ISO instants for the API, or None when unknown."""
        if self.valid_from is None or self.valid_until is None:
            return None
        return {"from": self.valid_from.isoformat(), "until": self.valid_until.isoformat()}


def _stamp(match: re.Match[str]) -> datetime | None:
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


def from_motis_refusal(response: httpx.Response) -> OutsideTimetable | None:
    """The refusal MOTIS sends for a date outside its timetable, or None for
    any other answer (another 400, another status, a body that is not JSON)."""
    if response.status_code != httpx.codes.BAD_REQUEST:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    message = body.get("error") if isinstance(body, dict) else None
    if not isinstance(message, str) or len(message) > _MAX_MESSAGE:
        return None
    _, phrase, window = message.partition(_MOTIS_PHRASE)
    if not phrase:
        return None
    stamps = [_stamp(m) for m in _STAMP.finditer(window)]
    if len(stamps) == 2:
        valid_from, valid_until = stamps
        if valid_from is not None and valid_until is not None and valid_from < valid_until:
            return OutsideTimetable(valid_from, valid_until)
    return OutsideTimetable()


def from_otp_answer(raw: dict[str, Any]) -> OutsideTimetable | None:
    """OTP's `OUTSIDE_SERVICE_PERIOD` routing error on an answer with no
    itinerary, or None."""
    data = raw.get("data") if isinstance(raw, dict) else None
    plan = data.get("planConnection") if isinstance(data, dict) else None
    if not isinstance(plan, dict) or plan.get("edges"):
        return None
    for error in plan.get("routingErrors") or []:
        if isinstance(error, dict) and error.get("code") == "OUTSIDE_SERVICE_PERIOD":
            return OutsideTimetable()
    return None
