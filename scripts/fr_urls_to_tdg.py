#!/usr/bin/env python3
"""Switch French providers from a data.gouv.fr download URL to the `tdg` NAP resolver.

A provider whose timetable is `source: "url"` with a data.gouv.fr resource link
(`https://www.data.gouv.fr/api/1/datasets/r/<uuid>`) or a transport.data.gouv.fr
one (`https://transport.data.gouv.fr/resources/<id>/download`) downloads the
same file the `tdg` resolver does, but the Automated NAP sources panel cannot
see it. This script looks each link up on the French NAP and replaces it with
`{"type": "tdg", "dataset_id": ..., "resource_id": ...}` — the same edit an
operator makes by hand on the provider card.

Runs inside the web container, which has the app code and the database:

    docker compose -p viator exec -T web python - < scripts/fr_urls_to_tdg.py
    docker compose -p viator exec -T web python - --apply < scripts/fr_urls_to_tdg.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis --apply < scripts/fr_urls_to_tdg.py

Dry run by default: prints the plan and writes nothing. `--apply` saves each
changed session the way the panel's switch does (nap_source_map.apply,
normalize_providers, staleness flag, `session.nap_sources.applied` audit row
whose `previous` keeps every replaced timetable). Nothing is downloaded:
refresh afterwards. A link that matches no resource, or more than one, or a
resource of another format, is reported and left alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

import httpx

from app import audit, ingestion, nap_source_map, staleness
from app.db import SessionLocal
from app.models import Session as SessionRow

TDG_API = "https://transport.data.gouv.fr/api/datasets"
DATAGOUV_RESOURCE_API = "https://www.data.gouv.fr/api/2/datasets/resources/{uuid}/"
_DATAGOUV_RE = re.compile(r"data\.gouv\.fr/api/1/datasets/r/([0-9a-fA-F-]{36})")
_TDG_RE = re.compile(r"transport\.data\.gouv\.fr/resources/(\d+)")
# Provider format -> tdg resource format.
_FORMATS = {"gtfs": "GTFS", "netex_epip": "NETEX", "netex_nordic": "NETEX"}

Entry = tuple[str, int, str, str]  # dataset_id, resource_id, dataset title, resource format


def _index(datasets: list[dict[str, Any]]) -> tuple[dict[int, Entry], dict[str, list[Entry]]]:
    """By tdg resource id, and by every string a resource carries (its url,
    original_url, datagouv id...) so a data.gouv uuid can be found in it."""
    by_rid: dict[int, Entry] = {}
    by_text: dict[str, list[Entry]] = {}
    for d in datasets:
        for r in d.get("resources") or []:
            entry = (d["id"], r["id"], d.get("title") or "", (r.get("format") or "").upper())
            by_rid[r["id"]] = entry
            for v in r.values():
                if isinstance(v, str) and v:
                    by_text.setdefault(v, []).append(entry)
    return by_rid, by_text


def _by_uuid(uuid: str, by_text: dict[str, list[Entry]], client: httpx.Client) -> list[Entry]:
    hits = {e for text, entries in by_text.items() if uuid in text for e in entries}
    if hits:
        return sorted(hits)
    # tdg does not always echo the data.gouv uuid: ask data.gouv for the
    # resource's real URL and match on that instead.
    resp = client.get(DATAGOUV_RESOURCE_API.format(uuid=uuid))
    if resp.status_code != 200:
        return []
    real_url = ((resp.json() or {}).get("resource") or {}).get("url") or ""
    return sorted(set(by_text.get(real_url, [])))


def _lookup(url: str, by_rid, by_text, client) -> tuple[Entry | None, str]:
    if m := _TDG_RE.search(url):
        entry = by_rid.get(int(m.group(1)))
        return (entry, "") if entry else (None, "resource not on the NAP")
    if m := _DATAGOUV_RE.search(url):
        hits = _by_uuid(m.group(1).lower(), by_text, client)
        if len(hits) == 1:
            return hits[0], ""
        return None, (
            "no NAP resource for this link" if not hits else f"{len(hits)} NAP resources match"
        )
    return None, "not a data.gouv.fr link"


def plan_session(providers, by_rid, by_text, client) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (source_map for apply(), report lines) for one session."""
    source_map: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for p in providers:
        tt = p.get("timetable") or {}
        url = tt.get("url") or ""
        if tt.get("source") != "url" or "data.gouv.fr" not in url:
            continue
        fmt = tt.get("format", "gtfs")
        entry, why = _lookup(url, by_rid, by_text, client)
        if entry and entry[3] != _FORMATS.get(fmt, ""):
            entry, why = None, f"NAP resource is {entry[3]}, provider is {fmt}"
        if entry is None:
            lines.append(f"  SKIP   {p['id']:20} {why}")
            continue
        resolver = {"type": "tdg", "dataset_id": entry[0], "resource_id": entry[1]}
        source_map[p["id"]] = {"format": fmt, "source": "nap", "resolver": resolver}
        lines.append(f"  SWITCH {p['id']:20} -> {entry[0]}/{entry[1]}  ({entry[2]})")
    return source_map, lines


def _save(db, s, new_config: dict[str, Any], changed: list[str], skipped) -> None:
    """Same steps as api/admin/sessions.py::_save_nap_switch, minus the HTTP request."""
    new_config["sources"]["providers"] = ingestion.normalize_providers(new_config)
    if not staleness.sources_subtree_equal(s.config, new_config):
        staleness.mark_sources_changed(new_config)
    previous = {
        p.get("id"): p.get("timetable")
        for p in ((s.config or {}).get("sources") or {}).get("providers") or []
        if isinstance(p, dict) and p.get("id") in changed
    }
    s.config = new_config
    audit.record(
        db,
        action="session.nap_sources.applied",
        target_kind="session",
        target_id=s.id,
        metadata={
            "changed": changed,
            "skipped": skipped,
            "previous": previous,
            "via": "scripts/fr_urls_to_tdg.py",
        },
    )
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", help="only this session id (default: every session)")
    ap.add_argument("--apply", action="store_true", help="save the changes (default: dry run)")
    args = ap.parse_args()

    with httpx.Client(follow_redirects=True, timeout=120) as client:
        datasets = client.get(TDG_API).raise_for_status().json()
        by_rid, by_text = _index(datasets)
        print(f"[nap] {len(datasets)} datasets on transport.data.gouv.fr")

        db = SessionLocal()
        try:
            rows = db.query(SessionRow).order_by(SessionRow.id).all()
            if args.session:
                rows = [s for s in rows if s.id == args.session]
                if not rows:
                    print(f"session {args.session} not found", file=sys.stderr)
                    return 1
            total = 0
            for s in rows:
                config = s.config or {}
                if not isinstance((config.get("sources") or {}).get("providers"), list):
                    continue
                providers = ingestion.normalize_providers(config)
                source_map, lines = plan_session(providers, by_rid, by_text, client)
                if not lines:
                    continue
                print(f"\n== {s.id}")
                print("\n".join(lines))
                if not (args.apply and source_map):
                    continue
                new_config, changed, skipped = nap_source_map.apply(
                    config, set(source_map), source_map
                )
                if changed:
                    _save(db, s, new_config, changed, skipped)
                    total += len(changed)
                    print(f"  saved: {len(changed)} provider(s) switched")
        finally:
            db.close()

    if args.apply:
        print(f"\n[done] {total} provider(s) switched. Refresh the sessions to download.")
    else:
        print("\nDry run - nothing written. Re-run with --apply to save.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
