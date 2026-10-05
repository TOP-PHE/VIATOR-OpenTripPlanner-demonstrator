#!/usr/bin/env python3
"""Report (and seed) the NeTEx structure check for the files already in place.

Every NeTEx download is fingerprinted and checked (app/netex_structure.py);
the Feed status panel shows the result. Files downloaded before that check
existed, or left unchanged since (HTTP 304), have no fingerprint yet, so the
next download would have nothing to compare with. This script fingerprints
the file currently in each NeTEx provider's slot, stores it where a download
would, and prints the assessment.

Read-only towards the timetable files; it only writes each provider's fetch
state (inbox/<sid>/_fetch_state/). Runs inside the web container:

    docker compose -p viator exec -T web python - < scripts/netex_structure.py
    docker compose -p viator exec -T web python - --session eu19-transit-motis < scripts/netex_structure.py
    docker compose -p viator exec -T web python - --force < scripts/netex_structure.py

Without --force a provider that already has a fingerprint is only reported.
DB's 2 GB zip takes a few minutes.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

from app import feed_fetch, ingestion, netex_structure
from app.db import SessionLocal
from app.models import Session as SessionRow
from app.settings import settings


def _netex_providers(config: dict) -> list[tuple[str, str]]:
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return []
    out = []
    for p in providers:
        fmt = (p.get("timetable") or {}).get("format", "gtfs")
        if fmt.startswith("netex"):
            out.append((p["id"], fmt))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", help="one session id (default: all)")
    parser.add_argument("--force", action="store_true", help="re-fingerprint every file")
    args = parser.parse_args()

    with SessionLocal() as db:
        rows = db.query(SessionRow).all()
        sessions = [
            (r.id, r.config or {}) for r in rows if not args.session or r.id == args.session
        ]

    for sid, config in sessions:
        state_dir = settings.inbox_dir / sid / "_fetch_state"
        for pid, fmt in _netex_providers(config):
            path = (
                settings.inbox_dir / sid / "netex" / ingestion.staged_filename_for_format(pid, fmt)
            )
            label = f"provider[{pid}].timetable({fmt})"
            if not path.is_file():
                print(f"{sid} {pid}: no file at {path}")
                continue
            state = feed_fetch.load_state(state_dir, label)
            structure = state.get("structure") or {}
            if args.force or not structure.get("fingerprint"):
                t0 = time.time()
                fp = netex_structure.fingerprint(path)
                result = netex_structure.assess(fp, None)
                structure = {
                    "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "fingerprint": fp.to_state(),
                    "assessment": result.to_state(),
                }
                state["structure"] = structure
                feed_fetch.save_state(state_dir, label, state)
                took = f" ({time.time() - t0:.0f}s, seeded)"
            else:
                took = " (already fingerprinted)"
            a = structure.get("assessment") or {}
            h = (structure.get("fingerprint") or {}).get("header") or {}
            print(
                f"{sid} {pid}: {a.get('level', '?')}{took}  version={h.get('version') or '-'}"
                f" profile={h.get('profile') or '-'} participant={h.get('participant') or '-'}"
            )
            for m in a.get("messages") or []:
                print(f"    - {m}")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
