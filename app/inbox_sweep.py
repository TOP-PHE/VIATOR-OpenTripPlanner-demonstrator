"""Orphan inbox-file detection + quarantine (PR #33).

When an operator removes a provider from `sessions.config.sources.providers`
(via the UI's "remove" button or a SQL UPDATE), the corresponding `<feed_id>.zip`
file lingers in `/data/inbox/<sid>/gtfs/`. The OTP entrypoint's
`gtfs/*.zip` glob picks the orphan up at build time and bakes its stale
data into the next graph — surfaced 2026-05-11 with BrittanyFerries
(operator removed via UI, file stayed, build failed on BrittanyFerries'
multi-line `stop_desc` CSV bug).

The sweep runs at the end of `POST /sources/refresh`: list
`inbox/<sid>/gtfs/` + `inbox/<sid>/netex/`, rename any `*.zip` whose
stem isn't in the current provider set to `*.zip.orphaned`. Idempotent
(already-`.orphaned` files are ignored) and lossless (renames rather
than deletes, so an operator who removed a provider by mistake can
recover by renaming back).

Lives in its own module so unit tests can import the helpers without
pulling in the FastAPI router stack (which transitively imports
`app.db` → psycopg native).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import ingestion

log = logging.getLogger(__name__)

# Suffix appended to orphaned filenames. Chosen so the OTP entrypoint's
# `gtfs/*.zip` glob doesn't pick the file up (it's now `*.zip.orphaned`,
# not `*.zip`). Matches the existing `*.zip.old` / `*.zip.broken`
# rotation conventions used elsewhere in the codebase.
ORPHAN_SUFFIX = ".orphaned"


def expected_provider_filenames(config: dict[str, Any]) -> set[str]:
    """Return the set of inbox `<feed_id>.zip` filenames the current
    provider list expects to find.

    Returns an empty set on a malformed provider list — defensive
    choice that means the sweep won't rename anything (rather than
    aggressively quarantining everything because a single bad provider
    broke parsing).
    """
    try:
        providers = ingestion.normalize_providers(config)
    except ValueError:
        return set()
    expected: set[str] = set()
    for p in providers:
        pid = p["id"]
        tt = p.get("timetable") or {}
        fmt = tt.get("format", "gtfs")
        try:
            expected.add(ingestion.staged_filename_for_format(pid, fmt))
        except ValueError:
            # Unknown format — skip rather than crash. Same defensive
            # posture as the try/except above.
            continue
    return expected


def sweep_orphaned_provider_files(
    session_inbox: Path,
    expected: set[str],
) -> list[str]:
    """Rename `<feed_id>.zip` files in gtfs/ and netex/ subdirs whose
    stem isn't in `expected` to `<feed_id>.zip.orphaned`.

    Returns the list of human-readable rename events for the API
    response. Empty list means no orphans were found.

    Idempotent: files already ending in `.orphaned` (or anything other
    than exactly `.zip`) are ignored. Multiple consecutive runs produce
    the same end state.

    Best-effort on individual rename failures — logs and continues so
    one stuck file (e.g. EACCES, locked by another process) doesn't
    abort the whole sweep. Lossless: never deletes.
    """
    events: list[str] = []
    # Both transit format subdirs use the same `<feed_id>.zip` convention,
    # so the sweep logic is symmetric. The entrypoint scans them separately
    # — leftover files in either would get picked up.
    for subdir_name in ("gtfs", "netex"):
        subdir = session_inbox / subdir_name
        if not subdir.is_dir():
            continue
        for entry in subdir.iterdir():
            # Only consider exact `.zip` filenames; skip `.zip.old`,
            # `.zip.broken`, `.zip.orphaned`, README.md, etc.
            if not entry.is_file() or entry.suffix != ".zip":
                continue
            if entry.name in expected:
                continue
            dst = entry.with_suffix(".zip" + ORPHAN_SUFFIX)
            try:
                entry.rename(dst)
                events.append(f"{subdir_name}/{entry.name} → {dst.name}")
            except OSError as exc:
                log.warning("could not rename orphan %s → %s: %s", entry, dst, exc)
    return events
