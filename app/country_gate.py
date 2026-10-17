"""The country gate of a session's providers (D-R12 B of the station module's design).

A country a provider declares (`country_iso`) is **known** when VIATOR's
Trainline list (`master_stations`) has stations in it, or when the station
module serves stations in it (`station_module.countries`, asked only for
the countries the Trainline list lacks).

Where it is asked:

- **Saving providers** (creating a session, Save config, the bulk import
  from a NAP, `scripts/session_providers.py`), for the countries the save
  adds. A country neither knows is **accepted with a warning** when VIATOR
  uses the station module (`new_country_warning`): the module serves a
  country only once it has received, and its administrator accepted, a
  feed of that country from VIATOR, which needs the provider saved and
  fetched first (the deadlock the owner hit with a first Estonian feed,
  17 October). When VIATOR does not use the module, nothing could ever
  serve it but the Trainline list: refused, as before (409
  `GATE_ERROR`).
- **Building a session's graph** (`unserved`, the worker), for every
  country the session declares, after the stop extracts were sent and the
  module's station build waited for: the graph is **built all the same**
  (it reads no station list; such a country routes by coordinates), and
  the job's log gets a warning that says what to do (`build_warning`). The
  same when the module did not answer (the countries are then "not
  checked"): up or down, a build is never stopped by the gate (owner's
  decision 6: the module never stops a rebuild).
- **The Sessions page** (`page_notices`), at each load: a lasting notice on
  each session that declares such a country. Nothing is stored.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from . import station_module
from .models import MasterStation

log = logging.getLogger(__name__)

# The word the pages, the scripts and the build log read.
GATE_ERROR = "missing_master_stations_for_countries"


def declared_countries(config: Mapping[str, Any] | None) -> set[str]:
    """The countries a session's providers declare (upper case), read as
    saved, without validating them."""
    providers = ((config or {}).get("sources") or {}).get("providers") or []
    if not isinstance(providers, list):
        return set()
    return {
        p["country_iso"].upper()
        for p in providers
        if isinstance(p, dict) and isinstance(p.get("country_iso"), str) and p["country_iso"]
    }


def countries_without_stations(db: Any, declared: set[str]) -> set[str]:
    """The countries of `declared` with no row in `master_stations` (one
    GROUP BY; none asked for an empty set)."""
    if not declared:
        return set()
    rows = db.execute(
        select(MasterStation.country_iso, func.count())
        .where(MasterStation.country_iso.in_(declared))
        .group_by(MasterStation.country_iso)
    ).all()
    present = {ci for ci, count in rows if count > 0}
    return declared - present


def unserved_by_module(missing: set[str], served: frozenset[str] | None) -> set[str]:
    """The countries of `missing` (without a Trainline row) that the station
    module does not serve either; `served` None (the module not used,
    paused or not answering): `missing` as it is."""
    if served is None:
        return missing
    return missing - served


def new_country_warning(countries: set[str]) -> str:
    """The warning of a save that adds countries neither the Trainline list
    nor the station module knows yet, when VIATOR uses the module."""
    return (
        f"New country {sorted(countries)}: VIATOR's Trainline list has no station there and "
        "the station module does not serve it yet. Its stations will come from the station "
        "module once the feed is accepted there: refresh the provider (its stop extract is "
        "sent to the module), accept the feed in the module and let its station build run. "
        "Until then, the graph is built and these countries route by coordinates."
    )


@dataclass(frozen=True)
class Unserved:
    """Countries without a station reference: `countries` (sorted);
    `checked` False when the module is used but did not answer (they lack
    Trainline rows; whether the module serves them is not known)."""

    countries: tuple[str, ...] = ()
    checked: bool = True


def _unserved_of(missing: set[str], served: frozenset[str] | None) -> Unserved:
    """`missing` (no Trainline row) less what the module serves, as an
    `Unserved`; the module not used: `missing`, checked."""
    if not missing:
        return Unserved()
    if not station_module.enabled():
        return Unserved(tuple(sorted(missing)))
    if served is None:
        return Unserved(tuple(sorted(missing)), checked=False)
    return Unserved(tuple(sorted(missing - served)))


def unserved(db: Any, config: Mapping[str, Any] | None) -> Unserved:
    """The countries a session declares that have no station reference, for
    its graph build: no Trainline row and, when VIATOR uses the station
    module, not served by it (asked now, not from the cache: the module's
    build may just have ended)."""
    missing = countries_without_stations(db, declared_countries(config))
    if not missing or not station_module.enabled():
        return _unserved_of(missing, None)
    return _unserved_of(missing, station_module.countries_blocking(fresh=True))


def build_warning(found: Unserved) -> str:
    """The build log's warning line for `found` ('' when none): the graph
    is built, and what to do."""
    if not found.countries:
        return ""
    names = list(found.countries)
    if not station_module.enabled():
        return (
            f"[viator] warning: {GATE_ERROR} {names}: no Trainline stations, and VIATOR does "
            "not use the station module; the graph is built, these countries route by "
            "coordinates until imported from Trainline (Admin > Stations, Refresh from "
            "Trainline).\n"
        )
    if not found.checked:
        return (
            f"[viator] warning: {GATE_ERROR} {names} not checked: no Trainline stations, and "
            "the station module did not answer; the graph is built, these countries route by "
            "coordinates until the station module serves them.\n"
        )
    return (
        f"[viator] warning: {GATE_ERROR} {names}: routes by coordinates until the station "
        "module serves it; accept the feed in the module and let its station build run.\n"
    )


def page_notices(
    db: Any, configs: Iterable[tuple[str, Mapping[str, Any] | None]]
) -> dict[str, str]:
    """The Sessions page's lasting notice of each session (by id) that
    declares a country without a station reference, computed at each load
    from the declared countries, the Trainline rows and the module's
    countries (one query, the module asked at most once, its 60 s cache
    allowed). Nothing is stored; a failure gives no notice."""
    declared = {sid: declared_countries(config) for sid, config in configs}
    every = set().union(*declared.values()) if declared else set()
    try:
        missing = countries_without_stations(db, every)
        served = station_module.countries_blocking() if missing else None
    except Exception as exc:  # a notice must never break the page
        log.warning("country_gate.notices_failed reason=%s", type(exc).__name__)
        return {}
    notices: dict[str, str] = {}
    for sid, countries in declared.items():
        found = _unserved_of(countries & missing, served)
        if found.countries:
            notices[sid] = _notice(found)
    return notices


def _notice(found: Unserved) -> str:
    names = ", ".join(found.countries)
    if not station_module.enabled():
        return (
            f"Country {names}: no Trainline stations, and VIATOR does not use the station "
            "module. It routes by coordinates until imported from Trainline."
        )
    if not found.checked:
        return (
            f"Country {names}: no Trainline stations; the station module did not answer, so "
            "whether it serves it is not known."
        )
    return (
        f"New country {names}: not yet served by the station module. It routes by coordinates "
        "until its feed is accepted in the module and the module's station build has run."
    )
