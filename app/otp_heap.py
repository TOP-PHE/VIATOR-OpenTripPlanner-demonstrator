"""Validate the per-session `otp_build_heap` config field (v0.1.23).

Operators ran into a hard ceiling on `nap-fr-rail` after the NAP bulk-
import added 12 providers to a France-wide graph: the build OOM-killed
during Phase 2 (transit linking) at the default 12g heap. Until v0.1.23
the only fix was SSHing onto the VPS, editing `.env`, and restarting
the worker. This module gates a per-session UI knob so heap is sized
on the same screen where the operator picks providers + OSM scope +
timezone.

The accepted format mirrors the JVM's `-Xmx` syntax exactly — `<int><unit>`
where unit is `g` (gigabytes) or `m` (megabytes). We don't enforce a
maximum because the operator's VPS may legitimately have 64-128 GB; a
sanity-check is documented in the UI hint instead ("don't request
more than RAM minus ~8g headroom").
"""

from __future__ import annotations

import re

# Default for sessions that don't set the field. Matches the existing
# `settings.otp_build_heap` default so legacy sessions keep building
# unchanged with no operator action required.
DEFAULT_HEAP = "12g"

# Common values for the UI dropdown. Range covers the full spectrum from
# single-provider regional demos up to all-Europe rail-focused builds on a
# 96+ GB host:
#   12g  — single-provider sessions, regional OSM
#   24g  — standard NAP-bulk-import sessions, France-wide OSM
#   36g  — multi-NAP cross-border with dense urban (IDFM + Paris regions)
#   48g  — Europe-wide rail-focused (osm_filter strips drivable roads)
#   56g  — Europe-wide rail-focused with multi-NAP transit overlay
#   64g  — Europe-wide multi-modal (full street network + multi-country)
#   72g  — Europe-wide max; only safe with no other serving sessions on a
#          96 GB host (heap + ~12 GB native overhead ≈ 84 GB cgroup cap;
#          leaves <12 GB for OS + Postgres + page cache)
#
# Operators picking ≥48g must verify .env's OTP_BUILD_MEM_LIMIT is set
# proportionally (heap + ≥4-12 GB native overhead) — the cgroup cap will
# OOM-kill the JVM before -Xmx if it's too tight. See `.env.example`.
COMMON_HEAPS: list[tuple[str, str]] = [
    ("12g", "12 GB — light: single provider, regional OSM"),
    ("16g", "16 GB"),
    ("20g", "20 GB"),
    ("24g", "24 GB — standard: 3-8 providers, France-wide OSM"),
    ("28g", "28 GB"),
    ("32g", "32 GB"),
    ("36g", "36 GB — heavy: 10+ providers, cross-border"),
    ("48g", "48 GB — Europe-wide rail-focused"),
    ("56g", "56 GB — Europe-wide rail-focused + multi-NAP overlay"),
    ("64g", "64 GB — Europe-wide multi-modal (full street network)"),
    ("72g", "72 GB — Europe-wide max (96 GB host, no other serving sessions)"),
]


# ───────────────────────── Serve heap (audit-2026-05.md follow-up) ──────────
# The SERVE-time JVM -Xmx, distinct from `otp_build_heap` above. After the
# build completes, the per-session OTP serving container starts and loads
# `streetGraph.obj` + the transit overlay from disk. Memory needed at serve
# time is much smaller than at build time (parse-time intermediates are gone)
# but still proportional to the loaded graph size — and the previous default
# of 4g silently OOM-loops a France-wide graph (62k stops + street network).
#
# Surfaced 2026-05-10: operator picked 64g build heap, build succeeded, but
# the serve container crash-looped at the hidden 4g default. Fix: a separate
# UI dropdown so operators size serve heap explicitly + a `default_serve_heap`
# helper that proposes a sensible default proportional to the build heap.
COMMON_SERVE_HEAPS: list[tuple[str, str]] = [
    ("4g", "4 GB — IDF / single-region only"),
    ("8g", "8 GB — France regional (one or two NAPs)"),
    ("12g", "12 GB — France-wide rail-focused"),
    ("16g", "16 GB — France-wide standard (62k stops + street network)"),
    ("20g", "20 GB"),
    ("24g", "24 GB — France-wide multi-NAP cross-border"),
    ("32g", "32 GB — Europe-wide rail-focused"),
    ("48g", "48 GB — Europe-wide multi-modal"),
]


def default_serve_heap_for_build(build_heap: str | None) -> str:
    """Suggest a reasonable serve heap based on the build heap.

    Rule of thumb: serve memory is roughly 1/3 to 1/4 of build memory
    because the parse-time intermediates (OSM PBF buffers, GTFS staging,
    visibility-graph construction memory, etc.) are gone — only the
    loaded graph data structures remain. Floor at 4g (anything below
    that struggles even on tiny IDF graphs).

    Used as the orchestrator's default when a session has `otp_build_heap`
    set but no explicit `otp_heap`. Closes the silent-4g-default trap.
    """
    if not build_heap:
        return "4g"
    m = _HEAP_RE.match(build_heap.strip())
    if not m:
        return "4g"
    qty, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m":
        # Megabyte build heaps imply tiny graphs — keep serve at 4g floor.
        return "4g"
    # Round-up division: build_gb // 3, then floor at 4.
    serve_gb = max(4, (qty + 2) // 3)
    return f"{serve_gb}g"


# JVM -Xmx accepts integer + unit. We're strict (no float, no kilobytes)
# to keep the surface tiny and the UI dropdown matchable. Operators with
# more exotic needs can override via `.env` instead.
_HEAP_RE = re.compile(r"^(\d+)([gGmM])$")


def validate_heap(
    value: str | None,
    *,
    default: str = DEFAULT_HEAP,
) -> str:
    """Return a validated heap string (e.g. '24g'), or raise ValueError.

    None / empty → default (caller can pass `settings.otp_build_heap` so
    legacy sessions inherit the env-var default rather than the hard-coded
    constant). Whitespace is trimmed but a value of just whitespace fails
    the regex and raises — same shape as `app.otp_timezone.validate_timezone`.
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError(f"otp_build_heap must be a string, got {type(value).__name__}")
    stripped = value.strip()
    m = _HEAP_RE.match(stripped)
    if not m:
        raise ValueError(
            f"otp_build_heap={value!r} doesn't match the expected pattern "
            "(integer + 'g' or 'm', e.g. '12g', '24g', '8192m')"
        )
    # Lowercase the unit so '24G' and '24g' both serialise the same way.
    qty, unit = m.group(1), m.group(2).lower()
    return f"{qty}{unit}"
