#!/usr/bin/env python3
"""List a session's providers with their source, flag duplicates, remove some.

Two providers that fetch the same file (same tdg dataset/resource, same URL,
same resolver) load every trip twice. A regional *Agrégat* that already
contains a network also loaded on its own is the same problem, but only a
human can see that: the listing prints each provider's NAP dataset title so
the overlap is readable.

Runs inside the web container, which has the app code and the database:

    docker compose -p viator exec -T web python - --session eu19-transit-motis < scripts/session_providers.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis --remove A,B < scripts/session_providers.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis --remove A,B --apply < scripts/session_providers.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis --add all < scripts/session_providers.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis --add CP,VR --apply < scripts/session_providers.py

`--remove` is a dry run unless `--apply` is given. Saving validates the new
config (normalize_providers), sets the staleness flag and writes a
`session.providers.removed` audit row whose `removed` keeps every removed
provider in full, so it can be put back by hand. The removed providers' files
stay in the inbox until the next "Refresh providers", which renames them to
`<id>.zip.orphaned` (never deletes) so the build stops loading them.

`--add` takes providers from app/data/europe_extension_providers.json
(`all`, or comma-separated ids) and is a dry run unless `--apply` is given.
Each line shows the entry's provenance - NAP, official (no NAP listing
confirmed) or COMMUNITY - which the labels also carry. Countries with no
master_stations rows are refused, as on the admin page: import them from
Trainline first. Saving writes a `session.providers.added` audit row.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import func, select

from app import audit, ingestion, provider_catalogue, staleness
from app.db import SessionLocal
from app.models import MasterStation
from app.models import Session as SessionRow

TDG_API = "https://transport.data.gouv.fr/api/datasets"


def _source_key(tt: dict[str, Any]) -> str:
    """What the provider fetches; two providers with the same key load the same file."""
    resolver = tt.get("resolver")
    if tt.get("source") == "nap" and isinstance(resolver, dict):
        if resolver.get("type") == "tdg":
            return f"tdg:{resolver.get('dataset_id')}/{resolver.get('resource_id')}"
        return "nap:" + json.dumps(resolver, sort_keys=True)
    if tt.get("source") == "url":
        return "url:" + (tt.get("url") or "")
    return f"{tt.get('source')}:"  # uploads have no comparable source


def _tdg_titles() -> dict[str, str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=120) as c:
            return {d["id"]: d.get("title") or "" for d in c.get(TDG_API).raise_for_status().json()}
    except httpx.HTTPError as exc:
        print(f"[warn] NAP titles unavailable ({exc}); listing without them", file=sys.stderr)
        return {}


def list_providers(providers: list[dict[str, Any]], titles: dict[str, str]) -> None:
    by_key: dict[str, list[str]] = defaultdict(list)
    for p in sorted(providers, key=lambda p: (p.get("country_iso") or "", p["id"])):
        tt = p.get("timetable") or {}
        key = _source_key(tt)
        if not key.endswith(":"):
            by_key[key].append(p["id"])
        resolver = tt.get("resolver") or {}
        title = (
            titles.get(resolver.get("dataset_id", ""), "") if resolver.get("type") == "tdg" else ""
        )
        print(
            f"{p.get('country_iso') or '--':2} {p['id']:20} {tt.get('format', 'gtfs'):11} "
            f"{tt.get('source', '?'):6} | {p.get('label') or ''}"
            + (f"  [NAP: {title}]" if title else "")
        )
    dupes = {k: ids for k, ids in by_key.items() if len(ids) > 1}
    print(f"\n{len(providers)} providers.")
    if dupes:
        print("Same source loaded more than once:")
        for k, ids in sorted(dupes.items()):
            print(f"  {', '.join(ids)}  <- {k}")
    else:
        print("No two providers fetch the same source.")


def remove(db, s, ids: set[str], apply: bool) -> int:
    config = s.config or {}
    providers = (config.get("sources") or {}).get("providers") or []
    present = {p.get("id") for p in providers if isinstance(p, dict)}
    missing = sorted(ids - present)
    if missing:
        print(f"not in {s.id}: {', '.join(missing)} - nothing written", file=sys.stderr)
        return 1
    removed = [p for p in providers if isinstance(p, dict) and p.get("id") in ids]
    for p in removed:
        print(f"  REMOVE {p['id']:20} | {p.get('label') or ''}")
    if not apply:
        print("Dry run - nothing written. Re-run with --apply to save.")
        return 0
    new_config = json.loads(json.dumps(config))
    new_config["sources"]["providers"] = [
        p
        for p in new_config["sources"]["providers"]
        if not (isinstance(p, dict) and p.get("id") in ids)
    ]
    new_config["sources"]["providers"] = ingestion.normalize_providers(new_config)
    if not staleness.sources_subtree_equal(s.config, new_config):
        staleness.mark_sources_changed(new_config)
    s.config = new_config
    audit.record(
        db,
        action="session.providers.removed",
        target_kind="session",
        target_id=s.id,
        metadata={"removed": removed, "via": "scripts/session_providers.py"},
    )
    db.commit()
    print(
        f"saved: {len(removed)} provider(s) removed. Click 'Refresh providers' so their files are set aside."
    )
    return 0


def _countries_without_stations(db, countries: set[str]) -> set[str]:
    rows = db.execute(
        select(MasterStation.country_iso, func.count())
        .where(MasterStation.country_iso.in_(countries))
        .group_by(MasterStation.country_iso)
    ).all()
    return countries - {ci for ci, n in rows if n > 0}


def add(db, s, wanted: set[str] | None, apply: bool) -> int:
    config = s.config or {}
    providers = (config.get("sources") or {}).get("providers") or []
    existing = {p.get("id") for p in providers if isinstance(p, dict)}
    to_add, present, unknown = provider_catalogue.plan_add(
        provider_catalogue.load(), existing, wanted
    )
    if unknown:
        print(f"not in the catalogue: {', '.join(unknown)} - nothing written", file=sys.stderr)
        return 1
    for pid in present:
        print(f"  SKIP   {pid:20} already in {s.id}")
    for e in to_add:
        print(
            f"  ADD    {e['country_iso']:2} {e['id']:15} {e['provenance'].upper():9} | {e['label']}"
        )
    if not to_add:
        print("Nothing to add.")
        return 0
    missing = _countries_without_stations(db, {e["country_iso"] for e in to_add})
    if missing:
        print(
            f"no master_stations rows for {', '.join(sorted(missing))} - import them from "
            "Trainline first (Admin > Master stations), then re-run. Nothing written.",
            file=sys.stderr,
        )
        return 1
    if not apply:
        print("Dry run - nothing written. Re-run with --apply to save.")
        return 0
    new_config = json.loads(json.dumps(config))
    sources = new_config.setdefault("sources", {})
    sources["providers"] = list(sources.get("providers") or []) + [
        provider_catalogue.session_provider(e) for e in to_add
    ]
    sources["providers"] = ingestion.normalize_providers(new_config)
    if not staleness.sources_subtree_equal(s.config, new_config):
        staleness.mark_sources_changed(new_config)
    s.config = new_config
    audit.record(
        db,
        action="session.providers.added",
        target_kind="session",
        target_id=s.id,
        metadata={"added": to_add, "via": "scripts/session_providers.py"},
    )
    db.commit()
    print(f"saved: {len(to_add)} provider(s) added. Click 'Refresh providers' to download them.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True)
    ap.add_argument("--remove", help="comma-separated provider ids to remove")
    ap.add_argument(
        "--add", help="'all' or comma-separated ids from app/data/europe_extension_providers.json"
    )
    ap.add_argument("--apply", action="store_true", help="save the change (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        s = db.get(SessionRow, args.session)
        if s is None:
            print(f"session {args.session} not found", file=sys.stderr)
            return 1
        if args.add:
            wanted = (
                None
                if args.add.strip().lower() == "all"
                else {i.strip() for i in args.add.split(",") if i.strip()}
            )
            return add(db, s, wanted, args.apply)
        if args.remove:
            return remove(
                db, s, {i.strip() for i in args.remove.split(",") if i.strip()}, args.apply
            )
        list_providers(ingestion.normalize_providers(s.config or {}), _tdg_titles())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
