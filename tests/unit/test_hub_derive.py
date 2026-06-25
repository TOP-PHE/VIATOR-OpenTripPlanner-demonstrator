"""Tests for `app.network_coverage.hub_derive` — the server-side
derivation powering the Promote-to-Hub flow in the journey UI.

The fixtures are the operator's actual real-world targets from the
2026-06-26 "add a batch of hubs" request — Saint-Exupéry (FR tram),
Burgfelderhof (CH tram terminus before the French border), Firenze
Santa Maria Novella, Latour-de-Carol-Enveitg (FR-ES Cerdagne border),
Santiago de Compostela, Torelló (Catalonia), Erding (Munich
metropolitan), Rimini, Irun (FR-ES Atlantic border).

Pinning these specific names means a future refactor of the slug or
short heuristic can't silently mangle the labels these hubs already
go by in the operator's matrix.
"""

from __future__ import annotations

import pytest

from app.network_coverage import hub_derive

# ─────────────────────── slugify ───────────────────────


@pytest.mark.parametrize(
    ("name", "expected_slug"),
    [
        ("Saint-Louis Gare", "saint-louis-gare"),
        ("Saint-Exupéry", "saint-exupery"),  # accent stripped
        ("Burgfelderhof", "burgfelderhof"),
        ("Firenze Santa Maria Novella", "firenze-santa-maria-novella"),
        ("Rimini", "rimini"),
        ("Irun", "irun"),
        ("Latour-de-Carol – Enveitg", "latour-de-carol-enveitg"),  # noqa: RUF001  (en-dash collapses)
        ("Santiago de Compostela", "santiago-de-compostela"),
        ("Torelló", "torello"),
        ("Erding", "erding"),
        # Edge cases:
        ("", ""),  # empty input → empty output (modal asks operator manually)
        ("  Bruxelles-Midi  ", "bruxelles-midi"),  # leading/trailing whitespace
        ("Genève", "geneve"),  # accent on first letter
        ("Köln Hbf", "koln-hbf"),  # German umlaut
    ],
)
def test_slugify(name: str, expected_slug: str) -> None:
    assert hub_derive.slugify(name) == expected_slug


def test_slugify_validates_against_slug_regex() -> None:
    """Every derived slug must pass the HubCreate.id regex
    `^[a-z0-9][a-z0-9-]*$`. If it doesn't, the modal save would 400."""
    import re

    valid = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    for name in [
        "Saint-Louis Gare",
        "Burgfelderhof",
        "Firenze Santa Maria Novella",
        "Latour-de-Carol – Enveitg",  # noqa: RUF001
        "Torelló",
        "Genève",
        "Köln Hbf",
    ]:
        slug = hub_derive.slugify(name)
        assert valid.match(slug), f"slug {slug!r} from {name!r} fails the validator regex"


def test_slugify_caps_at_60_chars() -> None:
    """Slugs are stored in a VARCHAR(64) column; cap at 60 to leave a
    bit of headroom for any future suffix the operator might add."""
    long_name = "A very long station name that nobody would actually type but who knows what we will see in a real NAP feed someday"
    assert len(hub_derive.slugify(long_name)) <= 60


# ─────────────────────── shorten ───────────────────────


@pytest.mark.parametrize(
    ("name", "expected_short"),
    [
        # Already short — passes through verbatim.
        ("Rimini", "Rimini"),
        ("Irun", "Irun"),
        ("Erding", "Erding"),
        # Known abbreviation folds:
        ("Firenze Santa Maria Novella", "Firenze SMN"),
        # Stopword drop:
        ("Saint-Louis Gare", "Saint-Louis"),
        # Long names without applicable abbreviation fall through to
        # hard truncation. The result is uncomfortable but operators
        # know they can rename it in the modal before saving. The
        # truncation prefers a hyphen boundary near the cap, otherwise
        # cuts at exactly max_len.
        # Latour-de-Carol Enveitg with a literal en-dash separator (U+2013).
        # The hard truncation falls on the hyphen boundary closest to the cap.
        ("Latour-de-Carol – Enveitg", "Latour-de"),  # noqa: RUF001
        ("Burgfelderhof", "Burgfelderho"),  # no separator → hard cut at 12
    ],
)
def test_shorten_real_world_names(name: str, expected_short: str) -> None:
    out = hub_derive.shorten(name)
    assert out == expected_short
    assert len(out) <= 12


def test_shorten_empty_input() -> None:
    assert hub_derive.shorten("") == ""


def test_shorten_respects_custom_max_len() -> None:
    """The 12-char default isn't sacred — surfaces that want shorter
    (e.g. mobile) can ask for less."""
    assert len(hub_derive.shorten("Saint-Louis Gare", max_len=8)) <= 8


# ─────────────────────── country_from_coords ───────────────────────


@pytest.mark.parametrize(
    ("name", "lat", "lon", "expected_country"),
    [
        # Each pair is a real hub coordinate from the operator's batch.
        # The bundled Natural Earth 50m boundaries place them in the
        # listed country — regression locks for the v1 country set.
        ("Saint-Louis Gare", 47.5876, 7.5571, "FR"),
        ("Burgfelderhof", 47.5631, 7.5443, "CH"),
        ("Firenze SMN", 43.7768, 11.2483, "IT"),
        ("Rimini", 44.0671, 12.5683, "IT"),
        ("Irun", 43.3392, -1.7884, "ES"),
        ("Latour-de-Carol", 42.4733, 1.9111, "FR"),
        ("Santiago de Compostela", 42.8709, -8.5446, "ES"),
        ("Torelló", 42.0497, 2.2628, "ES"),
        ("Erding", 48.3061, 11.9056, "DE"),
        # Outside the v1 country set → None.
        ("middle of nowhere", 45.0, -30.0, None),  # mid-Atlantic
    ],
)
def test_country_from_coords(
    name: str, lat: float, lon: float, expected_country: str | None
) -> None:
    """The hub label is for the test-failure message only — the
    assertion is on the country code resolved from the coords."""
    got = hub_derive.country_from_coords(lat, lon)
    assert got == expected_country, f"{name} at ({lat}, {lon}) resolved to {got!r}"


def test_country_from_coords_handles_missing_inputs() -> None:
    """Modal pre-fill must not crash when called with no coords (would
    only happen via a malformed leg). Returns None → operator fills
    country manually."""
    assert hub_derive.country_from_coords(None, None) is None
    assert hub_derive.country_from_coords(47.5, None) is None
    assert hub_derive.country_from_coords(None, 7.5) is None


# ─────────────────────── country_from_stop_id ───────────────────────


@pytest.mark.parametrize(
    ("stop_id", "expected_country"),
    [
        # OTP stop_id format is `<feedId>:<uic_code>`. The first 2 digits
        # of the numeric tail are the country prefix per UIC 920-14.
        ("SNCF:8711300", "FR"),  # Paris Nord
        ("EUROSTAR:8821006", "BE"),  # Bruxelles-Midi (88 = SNCB/Belgium)
        ("DB:8000105", "DE"),  # Frankfurt Hbf
        ("RENFE:7100000", "ES"),  # Madrid Atocha
        ("TRENITALIA:8300003", "IT"),  # Roma Termini
        ("SBB:8503000", "CH"),  # Zürich Hbf
        ("OBB:8100173", "AT"),  # Wien Hbf
        ("NMBS:8814001", "BE"),  # Bruxelles-Nord
        # Inputs that should NOT resolve via UIC prefix:
        (None, None),
        ("", None),
        ("BURGFELDERHOF", None),  # no colon, no numeric tail
        ("VLB:tram_3", None),  # non-UIC stop id (BVB tram)
    ],
)
def test_country_from_stop_id(stop_id: str | None, expected_country: str | None) -> None:
    """The UIC numeric prefix is operator-assigned and exact — preferred
    over coordinate point-in-polygon for border stations where the lat/
    lon may sit on the wrong side of the boundary."""
    assert hub_derive.country_from_stop_id(stop_id) == expected_country


def test_derive_prefers_stop_id_over_coords() -> None:
    """Saint-Louis Gare coords sit ~200 m from the CH border; if the
    Natural-Earth polygon were imprecise (it's not, but defensively),
    the UIC prefix `87` from a SNCF stop_id would still resolve to FR.
    Lock this preference order — stop_id wins."""
    out = hub_derive.derive(
        "Saint-Louis Gare",
        lat=47.5876,
        lon=7.5571,
        stop_id="SNCF:8722300",  # 87 = FR
    )
    assert out["country"] == "FR"


def test_derive_falls_back_to_coords_when_stop_id_lacks_uic_prefix() -> None:
    """The Basel BVB tram doesn't expose UIC stop ids — promotions
    from those legs must still get a country via the coordinate lookup."""
    out = hub_derive.derive(
        "Burgfelderhof",
        lat=47.5631,
        lon=7.5443,
        stop_id="VLB:tram3_burgfelderhof",  # no UIC prefix
    )
    assert out["country"] == "CH"


# ─────────────────────── derive (full payload) ───────────────────────


def test_derive_full_payload_for_saint_exupery() -> None:
    """End-to-end check that the modal pre-fill matches the operator's
    intuition for one of the trickier cases — `Saint-Exupéry` (the FR
    tram stop near the Bâle border) needs accent-stripping for the
    slug, French country detection, and a 12-char-max short. The short
    retains the accent (not stripped by `shorten`)."""
    out = hub_derive.derive("Saint-Exupéry", lat=47.5950, lon=7.5300)
    assert out["name"] == "Saint-Exupéry"
    assert out["slug"] == "saint-exupery"
    assert out["short"] == "Saint-Exupér"  # hard truncated at 12
    assert out["country"] == "FR"
    assert out["lat"] == 47.5950
    assert out["lon"] == 7.5300
    assert out["tier"] == "main"
    assert out["sort_order"] == 100


def test_derive_full_payload_for_burgfelderhof() -> None:
    """Burgfelderhof — short Swiss tram terminus, no special cases.
    Mostly checks that the country flips to CH at coords near
    Saint-Exupéry's (proves the Natural-Earth boundary is granular
    enough to distinguish FR from CH on this border)."""
    out = hub_derive.derive("Burgfelderhof", lat=47.5631, lon=7.5443)
    assert out["slug"] == "burgfelderhof"
    assert out["short"] == "Burgfelderho"
    assert out["country"] == "CH"


def test_derive_empty_country_when_coords_outside_v1_set() -> None:
    """Coordinates outside the v1 country list (e.g. an arrest outside
    Europe entirely) → country falls back to empty string, modal asks
    the operator. We don't fail the request."""
    out = hub_derive.derive("Tokyo Station", lat=35.6812, lon=139.7671)
    assert out["country"] == ""
    # Slug + short still derive cleanly from the name.
    assert out["slug"] == "tokyo-station"
    assert out["short"] == "Tokyo"  # stopword "station" dropped
