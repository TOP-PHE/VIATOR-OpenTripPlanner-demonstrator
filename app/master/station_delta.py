"""What changed between two versions of a register. Pure: no database.

The delta of screen B is a set difference between two `source_version_id`s of
`crd_location` or `era_operational_point`, sorted into five kinds:

    created      a location the newer version has and the older one lacks
    removed      the reverse
    renamed      same key, another name; both versions name the location
    moved        same key, a position further away than a threshold
    renumbered   a removed and a created location that are plainly the same
                 place under a new code: same name, same position

A location can be both renamed and moved. `renumbered` is a reading of the
data, not a fact it states: neither register says "this code replaces that
one" in the columns the tables keep, so a pair is proposed only when name and
position both agree, and each location is paired once at most.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

EARTH_RADIUS_M = 6_371_000.0
DEFAULT_THRESHOLD_M = 100.0


@dataclass(frozen=True)
class RegisterRow:
    """One location of a register version, reduced to what the delta compares."""

    key: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Delta:
    created: list[RegisterRow] = field(default_factory=list)
    removed: list[RegisterRow] = field(default_factory=list)
    renamed: list[tuple[RegisterRow, RegisterRow]] = field(default_factory=list)
    # (older, newer, metres between them)
    moved: list[tuple[RegisterRow, RegisterRow, float]] = field(default_factory=list)
    renumbered: list[tuple[RegisterRow, RegisterRow]] = field(default_factory=list)
    unchanged: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "removed": len(self.removed),
            "renamed": len(self.renamed),
            "moved": len(self.moved),
            "renumbered": len(self.renumbered),
            "unchanged": self.unchanged,
        }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def distance_m(a: RegisterRow, b: RegisterRow) -> float | None:
    """Metres between two rows, or None when either has no position."""
    if a.lat is None or a.lon is None or b.lat is None or b.lon is None:
        return None
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def _same_name(a: RegisterRow, b: RegisterRow) -> bool:
    return (a.name or "").strip() == (b.name or "").strip()


def _name_key(row: RegisterRow) -> str:
    return (row.name or "").strip().casefold()


def _same_place(old: RegisterRow, new: RegisterRow, threshold_m: float) -> bool:
    apart = distance_m(old, new)
    return apart is not None and apart <= threshold_m


def _pair_renumbered(
    removed: list[RegisterRow], created: list[RegisterRow], threshold_m: float
) -> list[tuple[RegisterRow, RegisterRow]]:
    """Pair removed with created rows that carry the same name at the same
    place. Paired rows leave both lists; each row is paired once."""
    by_name: dict[str, list[RegisterRow]] = {}
    for row in removed:
        if _name_key(row) and row.has_position:
            by_name.setdefault(_name_key(row), []).append(row)
    pairs: list[tuple[RegisterRow, RegisterRow]] = []
    for new in list(created):
        candidates = by_name.get(_name_key(new), [])
        match = next((old for old in candidates if _same_place(old, new, threshold_m)), None)
        if match is None:
            continue
        candidates.remove(match)
        removed.remove(match)
        created.remove(new)
        pairs.append((match, new))
    return pairs


def register_delta(
    old: Iterable[RegisterRow],
    new: Iterable[RegisterRow],
    *,
    threshold_m: float = DEFAULT_THRESHOLD_M,
) -> Delta:
    """The delta from an older version of a register to a newer one.

    Rows are matched on `key`. A position that appears or disappears is not a
    move: a move needs both positions. A name that appears or disappears is
    not a rename either: a rename needs both names. A location retired in CRD
    has neither in the CRD register from then on (the file carries ERA's), and
    CRD changed its validity, not its name.
    """
    old_by_key = {row.key: row for row in old}
    new_by_key = {row.key: row for row in new}
    delta = Delta()
    for key in sorted(old_by_key.keys() & new_by_key.keys()):
        before, after = old_by_key[key], new_by_key[key]
        named_in_both = bool(_name_key(before) and _name_key(after))
        renamed = named_in_both and not _same_name(before, after)
        apart = distance_m(before, after)
        moved = apart is not None and apart > threshold_m
        if renamed:
            delta.renamed.append((before, after))
        if moved and apart is not None:
            delta.moved.append((before, after, apart))
        if not (renamed or moved):
            delta.unchanged += 1
    delta.removed = [old_by_key[key] for key in sorted(old_by_key.keys() - new_by_key.keys())]
    delta.created = [new_by_key[key] for key in sorted(new_by_key.keys() - old_by_key.keys())]
    delta.renumbered = _pair_renumbered(delta.removed, delta.created, threshold_m)
    return delta
