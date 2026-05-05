"""Curated French rail-hub list for the v0.1.27 network-coverage feature.

Drawn from SNCF's "Le Réseau Ferré en France" (March 2026 edition) —
specifically the upper-cased "Gare et autre point d'arrêt desservi
d'intérêt national" tier in the legend. 23 stations chosen to give:

  * **All four Paris terminals** for radial coverage
  * **TGV major regional capitals** for primary network
  * **Mediterranean transversal** (Marseille / Aix / Avignon / Montpellier
    / Narbonne / Nice) which exercises the SNCF Sud-Est + Trenitalia
    France overlap zones
  * **Western/Atlantic axis** (Nantes / Rennes / Brest / Le Mans) where
    TER and TGV interleave heavily
  * **Cross-cutting non-Paris pairs** the matrix exposes automatically:
    Bordeaux↔Rennes, Nantes↔Lyon, Strasbourg↔Marseille, etc — those are
    where coverage gaps hide because all-pairs forces them into view
  * **Two non-LGV centers** (Clermont-Ferrand, Brest) to surface where
    TER coverage is weak

26 x 25 / 2 = 325 ordered pairs (we run both directions to surface
asymmetric data — a Paris→Marseille that works while Marseille→Paris
fails is a real bug we want to catch). Total pair count in the matrix
= 26 x 26 = 676 cells minus 26 diagonal = 650 routed cells — but we
only RUN the 325 unique unordered pairs and double-render in the
matrix view. (Or, optionally, run all 650 — see runner.py.)

Coordinates are approximate (within ~50 m, sufficient for OTP's
location-resolver). They're hardcoded here rather than looked up from
master_stations because:
  - this list IS the curated reference set (we want it stable across
    sessions and master_stations refreshes)
  - OTP routes by lat/lon anyway — the existing fanout endpoint takes
    coords directly, no station-id translation needed
  - it lets us render the matrix correctly even when master_stations
    hasn't loaded a particular city yet (ZenBus-only sessions etc)
"""

from __future__ import annotations

from typing import NamedTuple


class Hub(NamedTuple):
    """One row in the hub preset. `id` is a stable slug we use as the
    primary key in `network_coverage_results` — keep it short and
    URL-safe so it survives JSON round-trips and CSV exports."""

    id: str  # slug: paris-gdl, lyon-pd, marseille-stc, ...
    name: str  # operator-facing label rendered in the matrix
    short: str  # 3-5 char code for the matrix column header
    region: str  # rough geography for grouping (Paris / NE / SE / SW / W / Center)
    lat: float
    lon: float


# 23 hubs, ordered so the matrix groups geographically (Paris first,
# then clockwise around France). Don't reorder casually — the matrix
# row/column order matches this list and operator muscle memory will
# build up over time.
HUBS: list[Hub] = [
    # ─── Paris terminals (radial heart of the network) ────────────────
    # All six SNCF Paris termini except Bercy (which is a small TGV-Sud
    # offshoot used mainly for night trains + Bourgogne services and
    # adds little distinct routing signal vs Gare de Lyon).
    Hub("paris-gdl", "Paris Gare de Lyon", "P-GdL", "Paris", 48.8443, 2.3739),
    Hub("paris-nord", "Paris Gare du Nord", "P-Nord", "Paris", 48.8809, 2.3554),
    Hub("paris-est", "Paris Gare de l'Est", "P-Est", "Paris", 48.8767, 2.3593),
    Hub("paris-mont", "Paris Montparnasse", "P-Mtp", "Paris", 48.8410, 2.3219),
    # v0.1.28: previously missed. Austerlitz is the gateway to south-
    # central France (the historic POLT line — Paris-Orléans-Limoges-
    # Toulouse). Saint-Lazare runs Normandie services (Caen, Rouen,
    # Le Havre) and dense Île-de-France suburbs.
    Hub("paris-aust", "Paris Gare d'Austerlitz", "P-Aust", "Paris", 48.8421, 2.3652),
    Hub("paris-stl", "Paris Saint-Lazare", "P-StL", "Paris", 48.8757, 2.3252),
    # ─── North / North-East ──────────────────────────────────────────
    Hub("lille-flandres", "Lille Flandres", "Lille", "NE", 50.6357, 3.0712),
    Hub("reims", "Reims", "Reims", "NE", 49.2585, 4.0335),
    Hub("strasbourg", "Strasbourg", "Strasbourg", "NE", 48.5852, 7.7344),
    Hub("nancy", "Nancy", "Nancy", "NE", 48.6900, 6.1741),
    # ─── Center-East / Burgundy / Lyon ───────────────────────────────
    Hub("dijon", "Dijon Ville", "Dijon", "CE", 47.3236, 5.0271),
    Hub("lyon-pd", "Lyon Part-Dieu", "Lyon-PD", "CE", 45.7607, 4.8593),
    Hub("clermont", "Clermont-Ferrand", "Clermont", "Center", 45.7708, 3.1024),
    # ─── Mediterranean / South-East ──────────────────────────────────
    Hub("avignon-tgv", "Avignon TGV", "Avignon", "SE", 43.9215, 4.7860),
    Hub("aix-tgv", "Aix-en-Provence TGV", "Aix-TGV", "SE", 43.4554, 5.3171),
    Hub("marseille-stc", "Marseille Saint-Charles", "Marseille", "SE", 43.3026, 5.3801),
    Hub("nice", "Nice Ville", "Nice", "SE", 43.7045, 7.2614),
    Hub("montpellier", "Montpellier Saint-Roch", "Montpellier", "SE", 43.6047, 3.8807),
    Hub("narbonne", "Narbonne", "Narbonne", "SE", 43.1909, 3.0058),
    # ─── South-West / Toulouse / Bordeaux ────────────────────────────
    Hub("toulouse", "Toulouse Matabiau", "Toulouse", "SW", 43.6112, 1.4537),
    Hub("bordeaux", "Bordeaux Saint-Jean", "Bordeaux", "SW", 44.8254, -0.5560),
    # ─── Atlantic / West / Brittany ──────────────────────────────────
    Hub("le-mans", "Le Mans", "Le Mans", "W", 47.9954, 0.1932),
    Hub("nantes", "Nantes", "Nantes", "W", 47.2173, -1.5424),
    Hub("rennes", "Rennes", "Rennes", "W", 48.1031, -1.6724),
    Hub("brest", "Brest", "Brest", "W", 48.3886, -4.4789),
    # v0.1.28: a personal pick — small TER halt on the Le Croisic branch
    # off the Saint-Nazaire / La Baule line, in the Guérande peninsula.
    # Useful counterweight to the all-major-hub matrix because it forces
    # the longer "Paris → big hub → small terminal" routing OTP often
    # struggles with on regional GTFS calendars.
    Hub("batz", "Batz-sur-Mer", "Batz", "W", 47.2774, -2.4844),
]


# Quick lookup by id. Built at import time; the list is small enough
# that recomputing it never matters but a constant dict is cheaper at
# render time when the matrix iterates 253 pairs.
HUBS_BY_ID: dict[str, Hub] = {h.id: h for h in HUBS}


def all_pairs(hubs: list[Hub] | None = None) -> list[tuple[Hub, Hub]]:
    """Generate ordered pairs (A, B) for A ≠ B.

    We run BOTH directions because asymmetric data is real:
      - SNCF declares Paris→Marseille TGV but not the return on some
        Sundays (rare but catchable here)
      - Eurostar timetable order differs A→B vs B→A on some service days
      - GTFS-RT delays can affect one direction's connectivity but not
        the other when the search-window crosses a delay window

    Caller decides whether to render both directions in the matrix or
    fold them. The runner persists each direction as its own result
    row so post-hoc analysis doesn't lose the asymmetric data.
    """
    h = hubs if hubs is not None else HUBS
    return [(a, b) for a in h for b in h if a.id != b.id]


def unordered_pairs(hubs: list[Hub] | None = None) -> list[tuple[Hub, Hub]]:
    """Generate unordered pairs — N x (N-1) / 2.

    Useful for a half-matrix view where A→B and B→A are aggregated,
    or for cutting run-time in half when the operator doesn't care
    about asymmetry.
    """
    h = hubs if hubs is not None else HUBS
    return [(a, b) for i, a in enumerate(h) for b in h[i + 1 :]]
