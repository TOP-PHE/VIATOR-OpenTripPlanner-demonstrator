"""Track whether a session's downloaded files are stale relative to its
configured URLs.

Operator workflow problem we're solving:

  1. Operator clicks Save config (changes a URL)
  2. Operator forgets / skips Refresh sources
  3. Operator clicks Rebuild graph
  4. Build runs against the OLD downloaded data — silently. The operator
     thinks they got a build of their new config; they didn't.

Two timestamps in `session.config._meta` answer "is the downloaded data
in sync with what the operator most recently configured":

  sources_changed_at         — bumped on every save that changes the
                               `sources` subtree of config (URL edits,
                               provider added/removed, OSM URL change).
                               Edits that don't affect download URLs
                               (osm_scope, session name, fanout flag)
                               do NOT bump this.

  last_refresh_completed_at  — bumped after refresh-sources or per-
                               provider refresh succeeds (i.e. files
                               actually got downloaded and staged).

Definition: a session is **stale** iff
    sources_changed_at > last_refresh_completed_at
    OR (sources_changed_at is set AND last_refresh_completed_at is None)

Both timestamps are stored as ISO-8601 UTC strings, so lexicographic
comparison is correct.

The check is **soft** — used to drive UI warnings, never to block.
The Rebuild API itself still enforces the harder guard (inputs must be
on disk, regardless of timestamps).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast


def _now_iso() -> str:
    """Return the current UTC time in ISO-8601 format (with offset)."""
    return datetime.now(UTC).isoformat()


def _meta(config: dict[str, Any]) -> dict[str, Any]:
    """Return the `_meta` subdict, creating it if absent. Mutates config."""
    meta = config.setdefault("_meta", {})
    if not isinstance(meta, dict):
        # Operator hand-edited via raw API to a non-dict — replace.
        config["_meta"] = {}
        meta = config["_meta"]
    # `setdefault` is typed Any; the isinstance guard narrows to dict but
    # mypy still can't infer the value type, so the explicit cast is the
    # cheapest way to satisfy --strict. The runtime contract is unchanged.
    return cast("dict[str, Any]", meta)


def mark_sources_changed(config: dict[str, Any]) -> None:
    """Bump `sources_changed_at` to now. Idempotent within the same second."""
    _meta(config)["sources_changed_at"] = _now_iso()


def mark_refresh_completed(config: dict[str, Any]) -> None:
    """Bump `last_refresh_completed_at` to now. Called after every successful
    refresh — both session-wide and per-provider. Per-provider refreshes
    bumping a session-wide timestamp is intentionally lossy: if the operator
    refreshes only IDFM but had also changed the SNCF URL, this clears the
    staleness flag prematurely. Acceptable trade for the simpler model;
    re-tightening to per-URL hashes is v0.1.8 work if needed.
    """
    _meta(config)["last_refresh_completed_at"] = _now_iso()


def staleness_warning(config: dict[str, Any]) -> str | None:
    """Return a UI-friendly warning string if the session is stale, else None.

    Stale means: the operator edited a URL after the last successful refresh
    (or has never refreshed). Returns None for sessions that have never
    saved a sources block (no `sources_changed_at` yet) — those warn through
    a different channel (input-presence check at rebuild time).
    """
    meta = config.get("_meta") or {}
    if not isinstance(meta, dict):
        return None
    changed_at = meta.get("sources_changed_at")
    refreshed_at = meta.get("last_refresh_completed_at")
    if not isinstance(changed_at, str):
        # No sources_changed_at recorded yet — pre-staleness-tracking session
        # or operator's never edited the sources. Nothing to compare; warn
        # only if there's also no refresh history (input-presence guards
        # the actual rebuild).
        return None
    if not isinstance(refreshed_at, str):
        return (
            "Sources have been configured but never refreshed. "
            "Click 'Refresh all sources' to download the URLs before building."
        )
    if changed_at > refreshed_at:
        return (
            "URL(s) were edited after the last refresh. The on-disk files are "
            "stale relative to the current configuration. Click 'Refresh all "
            "sources' (or 'Refresh this provider' for the affected ones) "
            "before Rebuild graph, otherwise the build will use the old data."
        )
    return None


def sources_subtree_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Compare two configs' `sources` subtrees for equality.

    Used by the PATCH endpoint to decide whether to bump
    sources_changed_at — we only bump when the `sources` part actually
    changed (not for osm_scope edits, name renames, etc.). Sorts keys
    where order is irrelevant.
    """
    # Wrap in bool() — `_normalize_sources` returns Any (recursive structures
    # don't have a tight type), and `Any == Any` is also Any, which mypy in
    # --strict mode flags as a no-any-return on a function declared `-> bool`.
    return bool(_normalize_sources(a) == _normalize_sources(b))


def _normalize_sources(config: dict[str, Any] | None) -> Any:
    """Recursive helper for stable equality comparison of the sources tree."""
    if config is None:
        return None
    sources = (config or {}).get("sources")
    if sources is None:
        return None
    if isinstance(sources, dict):
        return {k: sources[k] for k in sorted(sources)}
    return sources
