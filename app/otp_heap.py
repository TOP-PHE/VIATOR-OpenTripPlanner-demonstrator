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

# Common values for the UI dropdown — we expose 12/16/20/24/28/32/36 g
# because that's the realistic range for VIATOR demos:
#   12g — single-provider sessions, regional OSM
#   24g — standard NAP-bulk-import sessions, France-wide OSM
#   36g — multi-NAP cross-border with dense urban (IDFM + Paris regions)
COMMON_HEAPS: list[tuple[str, str]] = [
    ("12g", "12 GB — light: single provider, regional OSM"),
    ("16g", "16 GB"),
    ("20g", "20 GB"),
    ("24g", "24 GB — standard: 3-8 providers, France-wide OSM"),
    ("28g", "28 GB"),
    ("32g", "32 GB"),
    ("36g", "36 GB — heavy: 10+ providers, cross-border"),
]

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
        raise ValueError(
            f"otp_build_heap must be a string, got {type(value).__name__}"
        )
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
