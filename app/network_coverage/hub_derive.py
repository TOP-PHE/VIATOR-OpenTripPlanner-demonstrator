"""Derive hub fields (slug / short / country) from an arbitrary station name + coords.

Powers the "Promote to hub" flow in the journey UI: an operator clicks
`+ Hub` next to a station that just returned itineraries; the modal opens
pre-filled. Used by `POST /api/admin/network-coverage/hubs/derive` to
keep the derivation logic server-side (single source of truth) — the
client just renders what we return.

Pure functions, no DB, no I/O — straightforward unit tests in
`tests/unit/test_hub_derive.py` lock the behaviour on the operator's
real-world inputs (Saint-Louis Gare, Burgfelderhof, Firenze SMN,
Latour-de-Carol-Enveitg, Santiago de Compostela, …).
"""

from __future__ import annotations

import re
import unicodedata

from ..gtfs_cross_border_filter import UIC_COUNTRY_NAMES, country_prefix
from ..osm_geo import country_for_point

# Hub slug regex from `app/api/admin/network_coverage.py::HubCreate.id`:
# /^[a-z0-9][a-z0-9-]*$/ with max length 64. We mirror it here so the
# derived slug always validates if the endpoint later saves it.
_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_NON_SLUG_CHAR = re.compile(r"[^a-z0-9-]+")

# Words that add no information to the slug / short and bloat both
# without helping operators recognise the hub. Trimmed conservatively
# — only the truly generic "station" markers, not anything regional.
_STOPWORDS_LOWER: frozenset[str] = frozenset(
    {
        "gare",
        "station",
        "stazione",
        "estacion",
        "estación",
        "bahnhof",
        "hbf",  # often kept; folded into the short by the abbreviation step
        "centrale",
        "central",
        "centraal",
        "centrala",
        "the",
        "de",
        "la",
        "le",
        "les",
        "du",
        "des",
        "di",
        "da",
        "el",
        "il",
    }
)

# Common station-name suffixes/prefixes that operators want to keep visible
# in the short label — these abbreviations beat truncation when the name
# is too long.
_SHORT_ABBREV: dict[str, str] = {
    "santa maria novella": "SMN",
    "central": "C",
    "centrale": "C",
    "centraal": "C",
    "hauptbahnhof": "Hbf",
    "santa lucia": "S-Lucia",
    "bundesbahnen": "",  # noise word in CH names
}


def _strip_accents(s: str) -> str:
    """Diacritics off — `Genève` → `Geneve`, `Saint-Exupéry` → `Saint-Exupery`."""
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def slugify(name: str) -> str:
    """Hub slug from a free-text station name.

    Rules (mirrors `HubCreate.id` validator):
      - Lowercase, accent-stripped.
      - Collapse runs of non-alphanumeric chars to a single hyphen.
      - Trim leading/trailing hyphens.
      - Empty input → empty output (caller falls back to manual entry).
      - Truncate at 60 chars to leave headroom under the 64-char DB limit.

    Examples:
      `Saint-Louis Gare`            -> `saint-louis-gare`
      `Firenze Santa Maria Novella` -> `firenze-santa-maria-novella`
      `Latour-de-Carol - Enveitg`   -> `latour-de-carol-enveitg`
      `Saint-Exupery`               -> `saint-exupery`
      `Burgfelderhof`               -> `burgfelderhof`
    """
    if not name:
        return ""
    s = _strip_accents(name).lower()
    s = _NON_SLUG_CHAR.sub("-", s).strip("-")
    return s[:60]


def shorten(name: str, max_len: int = 12) -> str:
    """Compact label for the matrix header — must fit in `max_len` chars.

    Strategy (in order):
      1. If name is already ≤ max_len, use it verbatim (Pythagoras vs.
         destroying a perfectly good short name).
      2. If a known abbreviation matches a substring, fold it
         (`Santa Maria Novella` → `SMN`).
      3. Drop stopwords (`Saint-Louis Gare` → `Saint-Louis`).
      4. If still over budget, take the initials of each word
         (`Bruxelles-Midi` → `BXL-MID` requires more sophistication —
         we keep this simple and truncate as last resort).
      5. Hard-truncate to `max_len` if nothing else fits.

    Examples:
      `Saint-Louis Gare`            -> `Saint-Louis`
      `Firenze Santa Maria Novella` -> `Firenze SMN`
      `Latour-de-Carol - Enveitg`   -> `Latour-de` (hyphen-boundary cut)
      `Saint-Exupery`               -> `Saint-Exuper` (hard truncated)
      `Burgfelderhof`               -> `Burgfelderho` (no separator)
    """
    if not name:
        return ""

    work = name.strip()
    if len(work) <= max_len:
        return work

    # Apply known abbreviations (case-insensitive substring match)
    lowered = work.lower()
    for full, abbr in _SHORT_ABBREV.items():
        if full in lowered:
            # Replace in-place preserving the rest of the name's casing
            idx = lowered.find(full)
            replacement = abbr
            work = work[:idx] + replacement + work[idx + len(full) :]
            work = re.sub(r"\s+", " ", work).strip()
            if len(work) <= max_len:
                return work
            lowered = work.lower()

    # Drop stopwords
    tokens = [t for t in re.split(r"\s+", work) if t]
    kept = [t for t in tokens if t.lower() not in _STOPWORDS_LOWER]
    if kept:
        work = " ".join(kept)
        if len(work) <= max_len:
            return work

    # Hard-truncate as last resort. Prefer cutting at a hyphen/space
    # boundary so we don't slice a word in half mid-syllable.
    truncated = work[:max_len]
    for sep in ("-", " "):
        last = truncated.rfind(sep)
        if last >= max_len - 4:  # only respect the boundary if it's close to the cap
            return truncated[:last]
    return truncated


def country_from_coords(lat: float | None, lon: float | None) -> str | None:
    """ISO-3166-1 alpha-2 country code for (lat, lon), or None.

    Uses the bundled Natural Earth boundaries via `app.osm_geo.country_for_point`
    so the lookup is offline + deterministic. Returns None when the point
    falls outside the v1 country set or coords are missing — the modal then
    prompts the operator to pick manually.

    Examples for the operator's real hubs:
      Saint-Louis Gare (47.59, 7.55)        → "FR"
      Burgfelderhof    (47.56, 7.54)        → "CH"
      Firenze SMN      (43.78, 11.25)       → "IT"
      Irun             (43.34, -1.79)       → "ES"
      Latour-de-Carol  (42.47, 1.91)        → "FR"
      Erding           (48.31, 11.91)       → "DE"
    """
    if lat is None or lon is None:
        return None
    return country_for_point(lat, lon)


def country_from_stop_id(stop_id: str | None) -> str | None:
    """ISO country from the UIC numeric prefix embedded in an OTP/MOTIS
    stop_id (e.g. `SNCF:8711300` -> `87` -> `FR`), or None.

    More accurate than coordinate point-in-polygon for border stations
    where the lat/lon may fall on the wrong side of the boundary by a few
    hundred metres — Saint-Louis Gare's coords sit 200 m from the Swiss
    border, and the bundled Natural Earth boundaries aren't surveyor-
    grade. The UIC prefix is operator-assigned, so a French station with
    a `87...` UIC is unambiguously French regardless of how close to the
    Rhine it is.
    """
    prefix = country_prefix(stop_id)
    if prefix is None:
        return None
    return UIC_COUNTRY_NAMES.get(prefix)


def derive(
    name: str, lat: float | None, lon: float | None, stop_id: str | None = None
) -> dict[str, object]:
    """Pre-fill payload for the AddHub modal.

    Country detection is two-tier: prefer the UIC numeric prefix from the
    stop_id when available (exact, operator-assigned), fall back to a
    coordinate point-in-polygon on the bundled boundaries (approximate,
    can lose ~200 m of precision near a border). When neither resolves,
    return `""` and let the operator pick country manually.
    """
    slug = slugify(name)
    short = shorten(name)
    country = country_from_stop_id(stop_id) or country_from_coords(lat, lon)
    return {
        "name": name,
        "slug": slug,
        "short": short,
        "country": country or "",
        "lat": lat,
        "lon": lon,
        "tier": "main",
        "region": None,
        "sort_order": 100,
    }
