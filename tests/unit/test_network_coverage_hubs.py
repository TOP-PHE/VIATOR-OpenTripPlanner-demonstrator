"""Unit tests for v0.1.27 hub list + pair generators."""

from __future__ import annotations

from app.network_coverage.hubs import (
    HUBS,
    HUBS_BY_ID,
    all_pairs,
    unordered_pairs,
)


def test_hub_count_matches_design():
    """26 hubs as of v0.1.28 (added Paris Austerlitz + Saint-Lazare and
    Batz-sur-Mer to the original 23). If this changes, the matrix grows
    quadratically — check whether the runtime budget still works
    (n=26 -> 650 directional pairs -> ~13 min wallclock at concurrency=5)."""
    assert len(HUBS) == 26


def test_all_hub_ids_unique():
    """ID is the primary key in network_coverage_results — duplicates
    would corrupt the matrix render."""
    ids = [h.id for h in HUBS]
    assert len(ids) == len(set(ids))


def test_all_hub_ids_url_safe():
    """IDs travel through URLs (the click-cell drilldown) and DB JSONB.
    Lowercase + digits + hyphens only keeps them safe."""
    import re

    pat = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
    for h in HUBS:
        assert pat.match(h.id), f"hub id {h.id!r} is not url-safe"


def test_lat_lon_within_france_bounds():
    """Defensive: all hubs should be within continental France's
    bounding box. Coords swapped or off by a degree would silently
    misroute searches; this catches it at CI time."""
    for h in HUBS:
        assert 41.0 <= h.lat <= 51.5, f"{h.id} lat {h.lat} outside FR"
        # France spans roughly -5.2 (Brest) to 8.5 (Strasbourg) longitude.
        assert -5.5 <= h.lon <= 8.5, f"{h.id} lon {h.lon} outside FR"


def test_hubs_by_id_lookup():
    """Constant-time lookup table matches the list."""
    for h in HUBS:
        assert HUBS_BY_ID[h.id] is h


def test_all_pairs_is_n_times_n_minus_1():
    """All-pairs (directional) = n x (n-1). For 26 hubs -> 650 pairs."""
    pairs = all_pairs()
    assert len(pairs) == 26 * 25  # 650
    # No self-pairs.
    for a, b in pairs:
        assert a.id != b.id


def test_unordered_pairs_is_n_choose_2():
    """Unordered pairs = n x (n-1) / 2. For 26 hubs -> 325 pairs."""
    pairs = unordered_pairs()
    assert len(pairs) == 26 * 25 // 2  # 325
    # No self-pairs.
    for a, b in pairs:
        assert a.id != b.id


def test_unordered_pairs_no_duplicates():
    """If A→B is in the list, B→A must NOT also be (that's the point
    of "unordered"). Detects regressions in the iteration logic."""
    pairs = unordered_pairs()
    seen: set[frozenset] = set()
    for a, b in pairs:
        key = frozenset([a.id, b.id])
        assert key not in seen, f"duplicate unordered pair {a.id} ↔ {b.id}"
        seen.add(key)


def test_paris_terminals_all_present():
    """Curated minimum: the six Paris terminals (excluding tiny Bercy)
    must always be in the list — they're the radial heart of the
    network. v0.1.27 had four; v0.1.28 added Austerlitz + Saint-Lazare."""
    paris_ids = {h.id for h in HUBS if h.id.startswith("paris-")}
    assert paris_ids == {
        "paris-gdl",
        "paris-nord",
        "paris-est",
        "paris-mont",
        "paris-aust",
        "paris-stl",
    }


def test_user_explicit_additions_present():
    """Operator-explicit additions across versions:
      v0.1.27 → Brest, Clermont-Ferrand, Narbonne
      v0.1.28 → Paris-Austerlitz, Paris-Saint-Lazare, Batz-sur-Mer
    Pin so they don't get dropped in a future hub-list refactor."""
    must_be_in = {"brest", "clermont", "narbonne", "paris-aust", "paris-stl", "batz"}
    actual = {h.id for h in HUBS}
    assert must_be_in.issubset(actual), f"missing: {must_be_in - actual}"


def test_pairs_subset_passes_through():
    """all_pairs(custom_list) honours the override — useful for
    unit-testing slices and for v0.1.28+ when operators can pick a
    custom hub set."""
    subset = HUBS[:3]
    pairs = all_pairs(subset)
    assert len(pairs) == 3 * 2  # 6
    for a, b in pairs:
        assert a in subset and b in subset
