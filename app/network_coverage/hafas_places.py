"""The places ÖBB's HAFAS planner answered in coverage runs: the cumulative list VIATOR sends the
station module as a stop list (the module's design 22.20, the owner's decisions 69 to 73 of
2026-11-13; decision 70, way a: the ÖBB journeys VIATOR already keeps).

**What a run gives.** The ÖBB check stores ÖBB's connections of each cell as canonical journeys
(`network_coverage_results.external_journeys`, every page since #448). Its `TripSearch` asks no
pass list (`getPasslist: false`), so each leg keeps its two ends: where a train is boarded, left
or changed, never the stops in between. Each end keeps ÖBB's number (`codes.OEBB_EXT`, HAFAS's
`extId`), its name, its position and its lid; the UIC its `gidL` states is in `codes.HAFAS_UIC`
since this change, and before it only where it equals the number (`uic`, #456).

**Which places.** One per HAFAS place of type station (`A=1` in its lid) that is an end of a
train leg (`train_leg`): a bus stop beside a station (HAFAS types it a station too) is never
listed, so never placed on the rail station (22.20, point 2). Its number is the key: digits, as
HAFAS gives it (`008500010` stays so: the lookup asks the number as the journey carries it,
`station_resolver._oebb_number`).

**Times seen** is the number of train calls seen there (a change of trains is two: one
arriving, one leaving), the extract's `n_calls`. **Last seen** is the time the check got the
answer (`external_verified_at`). The name, position and UIC are the latest answer's (a UIC
never seen again is kept); the operators of the legs are added up (at most `OPERATORS_MAX`).

**Once per run, never shrinking.** A run is added when it has ended (completed, cancelled or
failed: its check writes nothing more), in one transaction that first records it in
`hafas_place_runs` (`collect_run`): a run is never counted twice, and two processes adding the
same run wait for each other. A run deleted later keeps its places in the list.

**Logs**: counts only, never a number, a name or a position (`DATA-POLICY.md`).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnClause, case, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from .. import osm_geo
from ..models import HafasPlace, HafasPlaceRun, NetworkCoverageResult, NetworkCoverageRun
from .oebb_journeys import oebb_mode

log = logging.getLogger(__name__)

STOP_SOURCE = "hafas:oebb"  # `hafas_adapter.STOP_SOURCE`, the ÖBB stops of a journey
OWN_NUMBER = "OEBB_EXT"  # the code of HAFAS's number (`extId`)
STATED_UIC = "HAFAS_UIC"  # `hafas_adapter.STATED_UIC_CODE`, the `U\u00d7` of `gidL`
# The runs whose check writes nothing more.
ENDED = ("completed", "cancelled", "failed")
NAME_MAX = 200
NUMBER_MAX = 15
OPERATORS_MAX = 20
# Rows of one upsert statement (nine parameters a row, far below PostgreSQL's 65,535).
UPSERT_ROWS = 1000
# Result rows read at a time while a run is added.
READ_ROWS = 200
# The two countries ÖBB's numbers were already answered in (the series `DELFI_stop_key`).
HOME_COUNTRIES = frozenset({"DE", "AT"})

# ÖBB's mode families that are trains, and the one a category the old mode table did not know
# (FR, OGV, IRY, RB, RER, S-Bahn filed as `DB`) became before #456: OTHER, a train when the
# coverage's own rule (`oebb_mode`: an unknown category is RAIL) says so.
_RAIL = "RAIL"
_OTHER = "OTHER"


@dataclass
class SeenPlace:
    """One place as the journeys of a run saw it."""

    number: str
    name: str
    lat: float
    lon: float
    seen_at: datetime
    first_seen_at: datetime
    uic: str | None = None
    calls: int = 0
    operators: set[str] = field(default_factory=set)

    def see(
        self, name: str, lat: float, lon: float, uic: str | None, operator: str | None, at: datetime
    ) -> None:
        """One more call: the latest answer's name, position and UIC (a UIC never lost)."""
        self.calls += 1
        if at >= self.seen_at:
            self.name, self.lat, self.lon, self.seen_at = name, lat, lon, at
            self.uic = uic or self.uic
        else:
            self.uic = self.uic or uic
        self.first_seen_at = min(self.first_seen_at, at)
        if operator and len(self.operators) < OPERATORS_MAX:
            self.operators.add(operator)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def train_leg(leg: Mapping[str, Any]) -> bool:
    """Whether a stored leg is a ride on a train: mode family RAIL, or OTHER for a category the
    coverage's rule takes as a train (the runs before #456; `oebb_mode` RAIL, a category given)."""
    if leg.get("kind") != "timed":
        return False
    service = leg.get("service")
    if not isinstance(service, Mapping):
        return False
    mode = service.get("mode")
    if mode == _RAIL:
        return True
    if mode != _OTHER:
        return False
    category = service.get("product_category")
    code = category.get("code") if isinstance(category, Mapping) else None
    return bool(_text(code)) and oebb_mode(code) == _RAIL


def _lid_type(ref: str) -> str | None:
    """The `A=` field of a HAFAS lid (`A=1@O=…@X=…@Y=…@U=…@L=…@`): 1 a station."""
    for part in ref.split("@"):
        name, sep, value = part.partition("=")
        if sep and name == "A":
            return value
    return None


def _number(value: object) -> str | None:
    text = _text(value)
    return text if text.isdigit() and text.isascii() and len(text) <= NUMBER_MAX else None


def _uic(value: object) -> str | None:
    text = _text(value)
    return text if len(text) == 7 and text.isdigit() and text.isascii() else None


def _position(*candidates: object) -> tuple[float, float] | None:
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        lat, lon = candidate.get("lat"), candidate.get("lon")
        if (
            isinstance(lat, int | float)
            and isinstance(lon, int | float)
            and not isinstance(lat, bool)
            and not isinstance(lon, bool)
            and math.isfinite(lat)
            and math.isfinite(lon)
            and abs(lat) <= 90
            and abs(lon) <= 180
        ):
            return float(lat), float(lon)
    return None


@dataclass(frozen=True)
class _End:
    number: str
    name: str
    lat: float
    lon: float
    uic: str | None


def station_end(end: object) -> _End | None:
    """The place of one end of a leg when it is an ÖBB station with a number, a name and a
    position; None for anything else (a coordinate, another source, a bus stop's type, a lid
    of another type, a number not of digits)."""
    if not isinstance(end, Mapping):
        return None
    stop = end.get("stop_place")
    if not isinstance(stop, Mapping) or stop.get("source") != STOP_SOURCE:
        return None
    ref = _text(stop.get("ref"))
    if _lid_type(ref) != "1":
        return None
    codes = stop.get("codes")
    codes = codes if isinstance(codes, Mapping) else {}
    number = _number(codes.get(OWN_NUMBER))
    name = _text(stop.get("name")) or _text(end.get("name"))
    position = _position(stop.get("position"), end.get("position"))
    if number is None or not name or position is None:
        return None
    uic = _uic(codes.get(STATED_UIC)) or _uic(stop.get("uic"))
    return _End(number, name[:NAME_MAX].rstrip(), position[0], position[1], uic)


def _operator(leg: Mapping[str, Any]) -> str | None:
    service = leg.get("service")
    operator = service.get("operator") if isinstance(service, Mapping) else None
    if not isinstance(operator, Mapping):
        return None
    text = _text(operator.get("name")) or _text(operator.get("ref"))
    return text[:NAME_MAX].rstrip() or None


def add_journeys(places: dict[str, SeenPlace], journeys: object, at: datetime) -> int:
    """Add the train calls of one cell's stored journeys (`external_journeys`, a list of
    canonical journey JSON) to `places`; the number of calls added. Anything that does not read
    is passed over."""
    calls = 0
    for leg in _train_legs(journeys):
        operator = _operator(leg)
        for side in ("origin", "destination"):
            end = station_end(leg.get(side))
            if end is not None:
                _see(places, end, operator, at)
                calls += 1
    return calls


def _train_legs(journeys: object) -> Iterator[Mapping[str, Any]]:
    """The train legs of a list of canonical journey JSON; anything that does not read is
    passed over."""
    if not isinstance(journeys, list):
        return
    for journey in journeys:
        legs = journey.get("legs") if isinstance(journey, Mapping) else None
        for leg in legs if isinstance(legs, list) else ():
            if isinstance(leg, Mapping) and train_leg(leg):
                yield leg


def _see(places: dict[str, SeenPlace], end: _End, operator: str | None, at: datetime) -> None:
    place = places.get(end.number)
    if place is None:
        place = places[end.number] = SeenPlace(end.number, end.name, end.lat, end.lon, at, at)
    place.see(end.name, end.lat, end.lon, end.uic, operator, at)


# ── the database ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunAdded:
    """What one run added: its train calls at listed places, and those places."""

    run_id: uuid.UUID
    calls: int
    places: int


def pending_runs(db: DbSession) -> list[uuid.UUID]:
    """The ended runs with an ÖBB check whose journeys are not in the list yet, oldest first."""
    added = select(HafasPlaceRun.run_id)
    return list(
        db.execute(
            select(NetworkCoverageRun.id)
            .where(
                NetworkCoverageRun.status.in_(ENDED),
                NetworkCoverageRun.verify_externally.is_(True),
                NetworkCoverageRun.id.not_in(added),
            )
            .order_by(NetworkCoverageRun.started_at, NetworkCoverageRun.id)
        ).scalars()
    )


def collect_run(
    db: DbSession, run_id: uuid.UUID, *, now: datetime | None = None
) -> RunAdded | None:
    """Add one run's ÖBB journeys to the list, in one transaction; None (nothing written) when
    another process added it first. The caller commits nothing: this commits."""
    claimed = db.execute(
        pg_insert(HafasPlaceRun)
        .values(run_id=run_id, calls=0, places=0)
        .on_conflict_do_nothing(index_elements=[HafasPlaceRun.run_id])
        .returning(HafasPlaceRun.run_id)
    ).first()
    if claimed is None:
        db.rollback()
        return None
    fallback = now or datetime.now(UTC)
    places: dict[str, SeenPlace] = {}
    calls = 0
    rows = db.execute(
        select(NetworkCoverageResult.external_journeys, NetworkCoverageResult.external_verified_at)
        .where(NetworkCoverageResult.run_id == run_id)
        .execution_options(yield_per=READ_ROWS)
    )
    for journeys, verified_at in rows:
        calls += add_journeys(places, journeys, verified_at or fallback)
    upsert_places(db, list(places.values()))
    db.execute(
        update(HafasPlaceRun)
        .where(HafasPlaceRun.run_id == run_id)
        .values(calls=calls, places=len(places))
    )
    db.commit()
    return RunAdded(run_id, calls, len(places))


# The operators of a place once merged: each once, in order, at most OPERATORS_MAX (20; a
# constant text, no value of a row in it).
_MERGED_OPERATORS: ColumnClause[Any] = literal_column(
    "ARRAY(SELECT DISTINCT o FROM unnest(hafas_places.operators || excluded.operators) AS o "
    "ORDER BY o LIMIT 20)"
)


def upsert_places(db: DbSession, places: list[SeenPlace]) -> None:
    """Add `places` to the list: a new number makes a row; a known one adds its calls, keeps the
    earliest first and the latest last seen, takes the newer answer's name and position and its
    UIC (never losing one), and adds its operators."""
    table = HafasPlace.__table__
    for start in range(0, len(places), UPSERT_ROWS):
        batch = places[start : start + UPSERT_ROWS]
        statement = pg_insert(HafasPlace).values(
            [
                {
                    "ext_id": place.number,
                    "name": place.name,
                    "lat": place.lat,
                    "lon": place.lon,
                    "uic": place.uic,
                    "times_seen": place.calls,
                    "first_seen_at": place.first_seen_at,
                    "last_seen_at": place.seen_at,
                    "operators": sorted(place.operators),
                }
                for place in batch
            ]
        )
        new = statement.excluded
        newer = new.last_seen_at >= table.c.last_seen_at
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.ext_id],
            set_={
                "name": case((newer, new.name), else_=table.c.name),
                "lat": case((newer, new.lat), else_=table.c.lat),
                "lon": case((newer, new.lon), else_=table.c.lon),
                "uic": case(
                    (newer, func.coalesce(new.uic, table.c.uic)),
                    else_=func.coalesce(table.c.uic, new.uic),
                ),
                "times_seen": table.c.times_seen + new.times_seen,
                "first_seen_at": func.least(table.c.first_seen_at, new.first_seen_at),
                "last_seen_at": func.greatest(table.c.last_seen_at, new.last_seen_at),
                "operators": _MERGED_OPERATORS,
            },
        )
        db.execute(statement)


@dataclass(frozen=True)
class Collected:
    """What one pass added: the runs, their calls, and the list's places after it."""

    runs: int = 0
    calls: int = 0
    listed: int = 0
    failed: int = 0


def collect_pending(session_factory: Callable[[], Any]) -> Collected:
    """Add every ended run not yet in the list, one transaction a run. A run that fails to read
    is logged (its type of error only) and tried again at the next pass."""
    runs = calls = failed = 0
    with session_factory() as db:
        pending = pending_runs(db)
    for run_id in pending:
        try:
            with session_factory() as db:
                added = collect_run(db, run_id)
        except Exception as exc:  # one run, never the pass
            failed += 1
            log.warning("hafas_places.run_failed error=%s", type(exc).__name__)
            continue
        if added is not None:
            runs += 1
            calls += added.calls
    with session_factory() as db:
        listed = int(db.execute(select(func.count()).select_from(HafasPlace)).scalar_one())
    if runs or failed:
        log.info(
            "hafas_places.collected runs=%d calls=%d listed=%d failed=%d",
            runs,
            calls,
            listed,
            failed,
        )
    return Collected(runs, calls, listed, failed)


# ── the measures (22.20, "Measured": totals only) ────────────────────────


@dataclass(frozen=True)
class Totals:
    """The list in counts: its places and calls, the places outside Germany and Austria (by
    the country of the position; `unknown`: no country, near a border or at sea), how many carry
    a UIC code, and the places by country."""

    places: int
    calls: int
    outside_de_at: int
    unknown_country: int
    with_uic: int
    outside_de_at_with_uic: int
    runs: int
    by_country: dict[str, int]

    def lines(self) -> list[str]:
        countries = ", ".join(f"{iso} {n}" for iso, n in sorted(self.by_country.items()))
        return [
            f"runs added: {self.runs}",
            f"places (HAFAS stations with a train call): {self.places}",
            f"train calls seen: {self.calls}",
            f"outside Germany and Austria: {self.outside_de_at}"
            f" (country unknown: {self.unknown_country})",
            f"with a UIC code: {self.with_uic}"
            f" (outside Germany and Austria: {self.outside_de_at_with_uic})",
            f"by country: {countries or 'none'}",
        ]


def totals(db: DbSession) -> Totals:
    rows = db.execute(
        select(HafasPlace.lat, HafasPlace.lon, HafasPlace.uic, HafasPlace.times_seen)
    ).all()
    by_country: Counter[str] = Counter()
    unknown = outside = outside_uic = with_uic = calls = 0
    for lat, lon, uic, seen in rows:
        calls += seen
        iso = osm_geo.country_of_position(lat, lon)
        if iso is None:
            unknown += 1
        else:
            by_country[iso] += 1
        away = iso is not None and iso not in HOME_COUNTRIES
        outside += away
        if uic is not None:
            with_uic += 1
            outside_uic += away
    runs = int(db.execute(select(func.count()).select_from(HafasPlaceRun)).scalar_one())
    return Totals(
        places=len(rows),
        calls=calls,
        outside_de_at=outside,
        unknown_country=unknown,
        with_uic=with_uic,
        outside_de_at_with_uic=outside_uic,
        runs=runs,
        by_country=dict(by_country),
    )
