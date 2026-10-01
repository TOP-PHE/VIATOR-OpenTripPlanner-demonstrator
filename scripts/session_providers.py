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

`--remove` is a dry run unless `--apply` is given. Saving validates the new
config (normalize_providers), sets the staleness flag and writes a
`session.providers.removed` audit row whose `removed` keeps every removed
provider in full, so it can be put back by hand. The removed providers' files
stay in the inbox until the next "Refresh providers", which renames them to
`<id>.zip.orphaned` (never deletes) so the build stops loading them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import httpx

from app import audit, ingestion, staleness
from app.db import SessionLocal
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True)
    ap.add_argument("--remove", help="comma-separated provider ids to remove")
    ap.add_argument("--apply", action="store_true", help="save the removal (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        s = db.get(SessionRow, args.session)
        if s is None:
            print(f"session {args.session} not found", file=sys.stderr)
            return 1
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
