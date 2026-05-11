"""OSM PBF filter presets — keeps build-heap requirements down.

OTP graph builds are RAM-bound by the OSM PBF: France-wide raw (~5 GB,
13.6 M ways) needs ~40 GB heap during the "Build street graph" phase
because every Way object and every cross-reference is held simultaneously.
Filtering out non-routing-relevant ways (driveways, agricultural tracks,
private paths) cuts ~40 % of the data without affecting transit-station
routing accuracy, bringing the same build comfortably into 24 GB heap.

Four operator-facing scopes:

  transit-focused   default. highway= primary/secondary/tertiary/residential/
                    pedestrian/footway/path/steps/cycleway, all railway, all
                    public_transport, parking entrances. Drops `highway=service`
                    (driveways), `highway=track` (agricultural), construction/
                    proposed/abandoned. Saves ~40% size.
  multi-modal       same plus highway=service. Saves ~10-20% size. Use when
                    last-mile detail in dense city centers matters (parking
                    lots' internal lanes, service alleys).
  rail-focused      v0.1.30 — drops *all* driving infrastructure (motorways,
                    primary, residential, service, cycleway). Keeps railways,
                    public_transport, and walking-only highway types
                    (footway / path / steps / pedestrian) so OTP can still
                    snap coords to a station entrance. ~80% smaller than the
                    raw PBF — the only scope that lets a 10-country EU merge
                    fit in a 24-30 GB build heap. Required for multi-country
                    sessions on commodity VPS hardware (e.g. 47 GB RAM).
                    Trade-off: OTP can't compute walking from arbitrary
                    addresses to stations, so the journey-UI free-text
                    address search loses precision — but the demonstrator's
                    actual flows (city dropdown → station-to-station rail)
                    are unaffected.
  comprehensive     no filter. Original PBF unchanged. Use when you need car
                    routing or are debugging OSM coverage issues.

Filtering is performed at OTP build time (in `docker/otp/entrypoint.sh`)
via osmium-tool, which is installed into the otp-build image. The scope
is plumbed through as `OTP_OSM_SCOPE` env in `docker compose run`. The
operator sets it via the session UI's "OSM scope" dropdown; backend
stores it as `session.config.osm_scope`.

This module is the *single source of truth* for the preset definitions.
The shell side reads them at runtime by env var; the Python side
validates writes via `validate_scope()`.
"""

from __future__ import annotations

from typing import Any, cast

# Sentinel used by the shell to mean "no filter — copy the PBF as-is".
SCOPE_COMPREHENSIVE = "comprehensive"
DEFAULT_SCOPE = "transit-focused"


# Operator-meaningful presets. The `tags` field is the list of osmium-tool
# `tags-filter` arguments — kept as a list so the entrypoint can iterate it
# safely, with each element passed as one shell-quoted arg to osmium.
#
# osmium-tool semantics:
#   - Each filter arg is OR'd with the others.
#   - Within one arg, `key=val1,val2,val3` matches val1 OR val2 OR val3.
#   - `key` alone matches any value.
#   - `n/`, `w/`, `r/`, `nw/`, `nwr/` prefixes scope by element type
#     (we omit these — osmium picks sensible defaults; e.g. railway=*
#     covers both ways and relations).
#   - All referenced nodes are kept by default (way geometry stays valid).
OSM_SCOPE_PRESETS: dict[str, dict[str, Any]] = {
    "transit-focused": {
        "label": "Transit-focused (recommended)",
        # v0.1.32 — short one-liner used in the session form's <select>
        # dropdown. The longer `description` is kept as the operator-
        # facing rationale we surface in tooltips / audit logs.
        "dropdown_hint": "drops driveways, agricultural, private. Saves ~40% memory.",
        "description": (
            "Keeps walking, cycling, transit-relevant ways and rail. Drops "
            "driveways (highway=service), agricultural tracks, construction/"
            "abandoned ways. Cuts ~40 % of OSM data; lets France-wide builds "
            "fit in 24 GB heap."
        ),
        "tags": [
            "highway=motorway,trunk,primary,secondary,tertiary,unclassified,"
            "residential,living_street,pedestrian,footway,path,steps,"
            "cycleway,road,motorway_link,trunk_link,primary_link,"
            "secondary_link,tertiary_link",
            "railway",
            "public_transport",
            "amenity=parking,parking_entrance",
            "highway=bus_stop",
        ],
    },
    "multi-modal": {
        "label": "Multi-modal (transit + walking + cycling detail)",
        "dropdown_hint": "keeps service roads + cycling/walking detail. ~10-20% saving.",
        "description": (
            "Keeps everything in transit-focused plus highway=service "
            "(driveways, parking lot internal roads). ~10-20 % savings vs. "
            "comprehensive. Use when last-mile detail in dense urban areas "
            "matters."
        ),
        "tags": [
            # `highway` alone keeps every highway= value, including service.
            "highway",
            "railway",
            "public_transport",
            "amenity=parking,parking_entrance",
        ],
    },
    "rail-focused": {
        "label": "Rail-focused (multi-country / low-RAM)",
        "dropdown_hint": "drops ALL driving roads. Required for EU-scale builds on commodity VPS.",
        "description": (
            "Drops ALL driving infrastructure — no motorways, no residential, "
            "no service, no cycleway. Keeps only railways, public-transport "
            "polygons, walking-only highway types (footway/path/steps/"
            "pedestrian) and station forecourts. ~80 % smaller than raw PBF; "
            "the only scope that lets a 10-country European merge fit in "
            "~24-28 GB build heap on a 47 GB box. Trade-off: OTP can't compute "
            "walking from arbitrary addresses (no driveable roads in the graph) "
            "— fine for station-to-station rail flows, breaks free-text address "
            "search."
        ),
        "tags": [
            # All rail infrastructure — tracks, stations, halts, tram stops,
            # platforms-as-railway, level crossings, signals.
            "railway",
            # Platform / stop_area / station polygons. OTP uses these to
            # snap GTFS stop coords onto the right physical structure.
            "public_transport",
            # Walking-only highway types. Just enough for OTP to compute a
            # path from a station entrance polygon to its platform, and from
            # one platform to another within an interchange. Crucially:
            # NO motorway, NO primary/secondary/tertiary, NO residential,
            # NO service. That's where the 80 % reduction comes from — the
            # driveable street graph is the bulk of every OSM PBF and the
            # main driver of OTP's heap explosion.
            "highway=footway,path,steps,pedestrian,corridor,elevator",
            # Station forecourt access points so OTP can snap city-centre
            # coords onto a station entrance even when there's no
            # walking footway directly under the coord.
            "amenity=parking_entrance",
        ],
    },
    "comprehensive": {
        "label": "Comprehensive (no filter — original PBF)",
        "dropdown_hint": "original PBF, no filter. Memory-heavy: France-wide needs ≥40 GB heap.",
        "description": (
            "Original PBF passes through unchanged. Required for car routing "
            "(motorways are kept by transit-focused too, but residential and "
            "service streets matter for door-to-door car queries) or when "
            "debugging an OSM coverage gap. Memory-heavy: France-wide needs "
            "≥40 GB heap."
        ),
        "tags": None,  # sentinel — entrypoint skips osmium for this scope
    },
}


VALID_SCOPES: frozenset[str] = frozenset(OSM_SCOPE_PRESETS.keys())


def validate_scope(value: object | None) -> str:
    """Return a normalized scope string. Raises ValueError on bad input.

    Empty / None → default scope (transit-focused). Unknown strings → error
    with the list of valid options for a friendly UI message.
    """
    if value is None or value == "":
        return DEFAULT_SCOPE
    if not isinstance(value, str):
        raise ValueError(f"osm_scope must be a string, got {type(value).__name__}")
    if value not in VALID_SCOPES:
        raise ValueError(
            f"osm_scope={value!r} is not recognised. Valid options: {sorted(VALID_SCOPES)}"
        )
    return value


def osmium_args(scope: str) -> list[str] | None:
    """Return the list of osmium tags-filter args for a scope, or None when
    no filter is needed (comprehensive). Called by the worker side ONLY for
    informational logging; the entrypoint shell reads the same data via env.
    """
    scope = validate_scope(scope)
    # OSM_SCOPE_PRESETS is dict[str, Any] (mixed value types per preset:
    # str labels + Optional[list[str]] tag arrays), so ["tags"] is Any.
    # Cast to satisfy --strict mypy; the preset table is the single source
    # of truth and is statically defined, so the runtime shape is guaranteed.
    tags = OSM_SCOPE_PRESETS[scope]["tags"]
    if tags is None:
        return None
    return cast("list[str]", tags)


def scope_label(scope: str) -> str:
    """Operator-facing label for a scope value. Used in audit logs / UI."""
    scope = validate_scope(scope)
    return str(OSM_SCOPE_PRESETS[scope]["label"])
