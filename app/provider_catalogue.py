"""Ready-made providers that extend a session to more countries.

Operator workflow problem we solve:

    Extending eu19-transit-motis to the rest of Europe means adding ~15
    providers, each with its own feed URL, country and format, and each
    with a different standing: listed on the country's National Access
    Point, published officially but not (yet) on a NAP, or built by
    volunteers where no official feed exists. Typing them into the admin
    page one by one loses that standing, and a reviewer cannot tell a NAP
    feed from a community one.

`app/data/europe_extension_providers.json` holds them as provider objects
plus three catalogue-only fields (`provenance`, `nap_reference`, `probe`).
`plan_add()` says which would be added to a session; `session_provider()`
strips the catalogue-only fields so the result passes
`ingestion.normalize_providers`. Every entry is URL- or NAP-sourced, so
the providers refresh automatically like any other. Applied with
`scripts/session_providers.py --add`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOGUE_PATH = Path(__file__).parent / "data" / "europe_extension_providers.json"

PROVENANCES = ("nap", "official", "community")
# Fields that describe the catalogue entry and never go into a session.
CATALOGUE_ONLY_FIELDS = ("provenance", "nap_reference", "probe")
# What a non-NAP label must carry so the standing shows wherever the
# provider is listed (admin page, rebuild log, scripts).
LABEL_MARKERS = {"official": "[official, ", "community": "[COMMUNITY - not NAP"}


@lru_cache(maxsize=1)
def load() -> dict[str, dict[str, Any]]:
    """Provider id -> catalogue entry, in file order."""
    raw = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    return {p["id"]: p for p in raw["providers"]}


def session_provider(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry as a session provider (catalogue-only fields removed)."""
    return {k: v for k, v in entry.items() if k not in CATALOGUE_ONLY_FIELDS}


def plan_add(
    catalogue: dict[str, dict[str, Any]], existing_ids: set[str], wanted: set[str] | None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(entries to add, ids already in the session, ids not in the catalogue).

    `wanted=None` means the whole catalogue. Entries come back in id order."""
    ids = set(catalogue) if wanted is None else wanted
    unknown = sorted(ids - set(catalogue))
    present = sorted((ids & set(catalogue)) & existing_ids)
    to_add = [catalogue[i] for i in sorted(ids) if i in catalogue and i not in existing_ids]
    return to_add, present, unknown
