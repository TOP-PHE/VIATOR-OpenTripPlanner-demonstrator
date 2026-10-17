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
- **Building a session's graph** (`build_blocked`, the worker), for every
  country the session declares, after the stop extracts were sent and the
  module's station build waited for: a country neither knows stops the
  build, with the same word and what to do (`build_refusal`). The graph of
  a country without a station reference is what the gate protects. When
  the module is used but does not answer, the build is not stopped (the
  saved countries passed when they were saved; a module that is down must
  not stop every graph); the Trainline list alone decides only when VIATOR
  does not use the module.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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


def unserved(missing: set[str], served: frozenset[str] | None) -> set[str]:
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
        "Until then, the session's graph is not built."
    )


def build_blocked(db: Any, config: Mapping[str, Any] | None) -> set[str]:
    """The countries the session declares that block its graph build: no
    Trainline row and, when VIATOR uses the station module, not served by
    it (asked now, not from the cache: the module's build may just have
    ended). The module used but not answering: nothing blocks."""
    missing = countries_without_stations(db, declared_countries(config))
    if not missing:
        return set()
    if not station_module.enabled():
        return missing
    served = station_module.countries_blocking(fresh=True)
    if served is None:
        log.warning("country_gate.not_checked reason=module_unanswered")
        return set()
    return unserved(missing, served)


def build_refusal(countries: set[str]) -> str:
    """The build log's line for a graph not built for `countries`."""
    module = (
        "the station module does not serve them"
        if station_module.enabled()
        else "VIATOR does not use the station module"
    )
    return (
        f"[viator] graph not built: {GATE_ERROR} {sorted(countries)}: no Trainline stations, "
        f"and {module}. Accept the feed of each in the station module and let its station "
        "build run, or import them from Trainline (Admin > Stations, Refresh from Trainline); "
        "then Rebuild graph.\n"
    )
