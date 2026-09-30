"""Per-country switch of hand-uploaded providers to automated NAP sources.

Operator workflow problem we solve:

    `app/data/eu19_nap_sources.json` names, per provider id, the automated
    timetable source (a `nap` resolver or a fixed `url`) that replaces a
    file downloaded by hand from a national access point. Applying it used
    to mean running `scripts/switch_to_nap_sources.ps1` against the live
    API from an operator's PC, all providers at once. The session admin
    page now does it per country, so a country can be switched, refreshed
    and checked before the next one.

`plan()` compares a session's providers with the map; `apply()` returns a
new config with the chosen providers' timetables replaced. Neither
downloads anything: the operator refreshes afterwards, and each file
already in the inbox stays live until its replacement passes the format
check (app/feed_fetch.py). Same rule as the script: a provider whose
declared format differs from the map entry is never switched — the
mismatch needs a human.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import feed_resolvers

MAP_PATH = Path(__file__).parent / "data" / "eu19_nap_sources.json"

# "applied"          the provider already uses exactly the mapped source
# "available"        can be switched
# "format_mismatch"  session and map disagree on the format; switch by hand
Status = Literal["applied", "available", "format_mismatch"]


class ProviderPlan(BaseModel):
    id: str
    label: str | None = None
    format: str | None = None
    current_source: str | None = None
    proposed_source: str  # "nap" | "url"
    proposed_detail: str  # resolver type or host, for display
    status: Status


class CountryPlan(BaseModel):
    country_iso: str  # "—" when the provider declares no country
    providers: list[ProviderPlan]


@lru_cache(maxsize=1)
def load_map() -> dict[str, dict[str, Any]]:
    """Provider id -> timetable object. `_`-prefixed keys are comments."""
    raw = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _describe(timetable: dict[str, Any]) -> str:
    resolver = timetable.get("resolver")
    if timetable.get("source") == "nap" and isinstance(resolver, dict):
        return f"{resolver.get('type')} · {feed_resolvers.describe(resolver)}"
    return str(timetable.get("url") or "")


def _status(current: dict[str, Any], proposed: dict[str, Any]) -> Status:
    if current.get("format", "gtfs") != proposed.get("format", "gtfs"):
        return "format_mismatch"
    same = (
        current.get("source") == proposed.get("source")
        and current.get("resolver") == proposed.get("resolver")
        and current.get("url") == proposed.get("url")
    )
    return "applied" if same else "available"


def plan(
    providers: list[dict[str, Any]], source_map: dict[str, dict[str, Any]]
) -> list[CountryPlan]:
    """Group the session's mapped providers by country, sorted by country
    then provider id. `providers` is `ingestion.normalize_providers` output."""
    by_country: dict[str, list[ProviderPlan]] = {}
    for p in providers:
        proposed = source_map.get(p["id"])
        if proposed is None:
            continue
        current = p.get("timetable") or {}
        by_country.setdefault(p.get("country_iso") or "—", []).append(
            ProviderPlan(
                id=p["id"],
                label=p.get("label"),
                format=current.get("format"),
                current_source=current.get("source"),
                proposed_source=str(proposed.get("source")),
                proposed_detail=_describe(proposed),
                status=_status(current, proposed),
            )
        )
    return [
        CountryPlan(country_iso=c, providers=sorted(rows, key=lambda r: r.id))
        for c, rows in sorted(by_country.items())
    ]


def apply(
    config: dict[str, Any],
    provider_ids: set[str],
    source_map: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    """Return (new_config, changed_ids, skipped) — `config` is not mutated.

    Only `status == "available"` providers change; every other requested id
    is reported in `skipped` with the reason. The caller validates the new
    config (normalize_providers) before saving it.
    """
    new_config = copy.deepcopy(config)
    providers = (new_config.get("sources") or {}).get("providers") or []
    by_id = {p.get("id"): p for p in providers if isinstance(p, dict)}
    changed: list[str] = []
    skipped: list[dict[str, str]] = []
    for pid in sorted(provider_ids):
        provider = by_id.get(pid)
        proposed = source_map.get(pid)
        if provider is None or proposed is None:
            skipped.append({"id": pid, "reason": "not a mapped provider of this session"})
            continue
        status = _status(provider.get("timetable") or {}, proposed)
        if status == "format_mismatch":
            skipped.append({"id": pid, "reason": "format differs from the map — switch by hand"})
            continue
        if status == "applied":
            skipped.append({"id": pid, "reason": "already uses this source"})
            continue
        provider["timetable"] = copy.deepcopy(proposed)
        changed.append(pid)
    return new_config, changed, skipped
