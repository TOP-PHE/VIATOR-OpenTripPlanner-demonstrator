"""Disk usage of the data volumes, and what can be deleted to free space.

Why this exists: on 2026-10-01 about fifteen failed Europe-wide MOTIS imports
left their partial output behind, filled the 678 GB disk, and took `web` and
`postgres` down. Nothing showed the disk filling up, and cleaning it meant
`sudo du` on the host. This module backs the admin Storage page: one scan of
the two data volumes (`settings.inbox_dir`, `settings.graph_dir`) that reports
usage per session and a list of clean-up *candidates*, each decided by a fixed
rule and never something in use.

What is never a candidate:
  * the build a session's `current` symlink points to (what is served);
  * anything of a session whose rebuild is running;
  * a live feed or `osm.pbf` (only `.old`, `.old.N` and `.orphaned` copies);
  * a `_staging/` file younger than a day (a refresh may be writing it).

Deletion re-runs the scan and deletes only ids present in the fresh result,
so the endpoint can never be pointed at an arbitrary path, and a candidate that
stopped qualifying since the page was loaded (a rebuild started, `current`
moved) is skipped rather than deleted.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_config import one_line

log = logging.getLogger(__name__)

# Builds are written to `<YYYYMMDD-HHMMSS>` folders (worker.run_build / run_build_motis).
_BUILD_DIR_RE = re.compile(r"^\d{8}-\d{6}$")
# Rotated feed copies: `sncf.zip.old`, `sncf.zip.old.2`, `osm.pbf.old.1`; removed providers: `.orphaned`.
_STALE_FILE_RE = re.compile(r"\.(?:old(?:\.\d+)?|orphaned)$")
_STAGING_MIN_AGE_S = 24 * 3600
# Top-level graph-volume names that are not session folders.
_GRAPH_ROOT_RESERVED = {"motis", "graph.obj", "router-config.json", "current", "lost+found"}

WARN_PERCENT = 85.0


@dataclass
class Candidate:
    id: str  # "graphs/<rel>" or "inbox/<rel>" — the only thing the delete call accepts
    category: str
    session_id: str | None
    size_bytes: int
    modified: float  # epoch seconds
    reason: str


@dataclass
class SessionUsage:
    session_id: str
    known: bool  # False: a folder with no matching session row
    graphs_bytes: int = 0
    inbox_bytes: int = 0
    current_build: str | None = None


@dataclass
class Report:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    warn: bool
    sessions: list[SessionUsage] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────── sizes ────────────────────────────


def path_size(path: Path) -> int:
    """Bytes on disk under `path`, not following symlinks (a `current`
    symlink must not count its build twice)."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def _current_target(session_dir: Path) -> str | None:
    link = session_dir / "current"
    if not link.is_symlink():
        return None
    try:
        return link.readlink().name
    except OSError:
        return None


def _is_complete_build(build_dir: Path) -> bool:
    """A MOTIS import writes `tt.bin`, an OTP build `graph.obj`."""
    return (build_dir / "tt.bin").exists() or (build_dir / "graph.obj").exists()


# ──────────────────────────── scan ────────────────────────────


def _build_candidates(
    root: Path, id_prefix: str, sid: str, busy: bool, usage: SessionUsage
) -> list[Candidate]:
    out: list[Candidate] = []
    current = _current_target(root)
    usage.current_build = current
    usage.graphs_bytes += path_size(root)
    if busy or not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.name == "current" or child.is_symlink() or not child.is_dir():
            continue
        if child.name == current or not _BUILD_DIR_RE.match(child.name):
            continue
        complete = _is_complete_build(child)
        out.append(
            Candidate(
                id=f"{id_prefix}/{child.name}",
                category="previous_build" if complete else "failed_build",
                session_id=sid,
                size_bytes=path_size(child),
                modified=_mtime(child),
                reason=(
                    "Earlier complete build, not the one being served (kept for rollback)"
                    if complete
                    else "Incomplete build: an import or build that failed or was interrupted"
                ),
            )
        )
    return out


def _inbox_candidates(root: Path, sid: str, busy: bool, now: float) -> list[Candidate]:
    out: list[Candidate] = []
    if busy or not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        in_staging = "_staging" in here.relative_to(root).parts
        for name in filenames:
            path = here / name
            if path.is_symlink():
                continue
            rel = path.relative_to(root.parent).as_posix()
            if in_staging:
                if now - _mtime(path) < _STAGING_MIN_AGE_S:
                    continue
                category, reason = "staging_leftover", "Left in _staging by an interrupted refresh"
            elif _STALE_FILE_RE.search(name):
                if name.endswith(".orphaned"):
                    category = "orphaned_feed"
                    reason = "Feed file of a provider removed from the session"
                else:
                    category = "old_copy"
                    reason = "Previous version kept after a refresh (rollback copy)"
            else:
                continue
            out.append(
                Candidate(
                    id=f"inbox/{rel}",
                    category=category,
                    session_id=sid,
                    size_bytes=path_size(path),
                    modified=_mtime(path),
                    reason=reason,
                )
            )
    return out


def scan(
    inbox_dir: Path,
    graph_dir: Path,
    session_ids: set[str],
    busy_session_ids: set[str],
    *,
    now: float | None = None,
) -> Report:
    """`session_ids`: every session that exists. `busy_session_ids`: those
    with a rebuild running, whose files are left alone."""
    now = time.time() if now is None else now
    try:
        du = shutil.disk_usage(graph_dir)
        total, used, free = du.total, du.used, du.free
    except OSError:
        total = used = free = 0
    pct = round(100.0 * used / total, 1) if total else 0.0
    report = Report(total, used, free, pct, pct >= WARN_PERCENT)

    usages: dict[str, SessionUsage] = {}

    def usage_for(sid: str) -> SessionUsage:
        return usages.setdefault(sid, SessionUsage(sid, sid in session_ids))

    # Graph builds: OTP at graphs/<sid>/, MOTIS at graphs/motis/<sid>/.
    graph_roots: list[tuple[Path, str, str]] = []
    for base, prefix in ((graph_dir, "graphs"), (graph_dir / "motis", "graphs/motis")):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if base == graph_dir and child.name in _GRAPH_ROOT_RESERVED:
                continue
            graph_roots.append((child, f"{prefix}/{child.name}", child.name))

    for root, prefix, sid in graph_roots:
        usage = usage_for(sid)
        if sid not in session_ids:
            usage.graphs_bytes += path_size(root)
            report.candidates.append(_orphan(root, prefix, sid))
            continue
        report.candidates += _build_candidates(root, prefix, sid, sid in busy_session_ids, usage)

    # Leftovers of a failed OTP build, written at the volume root before being moved.
    if not busy_session_ids:
        for name in ("graph.obj", "router-config.json"):
            leftover = graph_dir / name
            if leftover.is_file() and not leftover.is_symlink():
                report.candidates.append(
                    Candidate(
                        id=f"graphs/{name}",
                        category="failed_build",
                        session_id=None,
                        size_bytes=path_size(leftover),
                        modified=_mtime(leftover),
                        reason="OTP build output never moved into a session (failed build)",
                    )
                )

    # Inbox: one folder per session.
    if inbox_dir.is_dir():
        for child in sorted(inbox_dir.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            sid = child.name
            usage = usage_for(sid)
            usage.inbox_bytes += path_size(child)
            if sid not in session_ids:
                report.candidates.append(_orphan(child, f"inbox/{sid}", sid))
                continue
            report.candidates += _inbox_candidates(child, sid, sid in busy_session_ids, now)

    report.sessions = sorted(
        usages.values(), key=lambda u: u.graphs_bytes + u.inbox_bytes, reverse=True
    )
    report.candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return report


def _orphan(path: Path, cid: str, sid: str) -> Candidate:
    return Candidate(
        id=cid,
        category="orphan_session",
        session_id=sid,
        size_bytes=path_size(path),
        modified=_mtime(path),
        reason="Folder of a session that no longer exists",
    )


# ──────────────────────────── delete ────────────────────────────


def resolve_candidate(candidate_id: str, inbox_dir: Path, graph_dir: Path) -> Path:
    """Map a candidate id back to its path, refusing anything outside the volume."""
    volume, _, rel = candidate_id.partition("/")
    base = {"inbox": inbox_dir, "graphs": graph_dir}.get(volume)
    if base is None or not rel:
        raise ValueError(f"not a storage candidate id: {candidate_id!r}")
    root = os.path.realpath(base)
    target = os.path.realpath(Path(root) / rel)
    if not target.startswith(root + os.sep):
        raise ValueError(f"candidate escapes its volume: {candidate_id!r}")
    return Path(target)


def delete(
    candidate_ids: list[str],
    report: Report,
    inbox_dir: Path,
    graph_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Delete the ids that are candidates in `report` (a fresh scan).
    Returns (deleted, skipped). Ids not in the report are skipped, never deleted."""
    by_id = {c.id: c for c in report.candidates}
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for cid in dict.fromkeys(candidate_ids):
        cand = by_id.get(cid)
        if cand is None:
            skipped.append({"id": cid, "reason": "no longer a clean-up candidate"})
            continue
        try:
            path = resolve_candidate(cid, inbox_dir, graph_dir)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except (OSError, ValueError) as exc:
            # The error text names paths and OS details: keep it in the server log,
            # never in the response (CodeQL py/stack-trace-exposure).
            log.warning("storage clean-up could not delete %s: %s", one_line(cid), one_line(exc))
            skipped.append({"id": cid, "reason": "could not delete (see the web server log)"})
            continue
        deleted.append({"id": cid, "category": cand.category, "size_bytes": cand.size_bytes})
    return deleted, skipped
