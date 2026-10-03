"""NAP station information (screen A): the two lists that do the daily work.

    GET /api/master/station-links/summary          counts, and the values the filters offer
    GET /api/master/station-links/unmatched        stops no reference row could be matched to
    GET /api/master/station-links/contradictions   stops whose code contradicts the reference

Authorization: content_manager or platform_admin, declared on every route.
Read-only. Both lists are rendered from `station_ref_link`, the adjudication
ladder at the grain of the offline links file; there is no per-stop record
until step 2 creates `nap_stop`.

**Unmatched**: a link row with no station. By default the list shows `Rail`
and `Multimodal` stops only: the `Urban` ones are tram and bus stops that were
never expected to match a railway location.

**Contradicting**: a link the offline chain did *not* assert although the stop
carries a code. The code said "this reference row"; the distance or the name
said otherwise, so the match was refused. That is the definition used here,
because it is the only one `station_ref_link` can answer on its own.

See docs/station-panel-design.md section 4A.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session as DbSession

from ...db import get_db
from ...models import StationRef, StationRefLink
from ...security import CurrentUser, require_content_manager
from .station_ref import like_pattern

router = APIRouter(prefix="/api/master/station-links", tags=["master", "station-links"])

# The stops worth an operator's time by default.
DEFAULT_LABELS: tuple[str, ...] = ("Rail", "Multimodal")
ALL_LABELS = "all"
FEED_SEPARATOR = "|"
_ESCAPE = "\\"

_UNMATCHED_COLUMNS = (
    "id",
    "offline_station_id",
    "stop_name",
    "feed_key",
    "iso2",
    "label",
    "lat",
    "lon",
    "reason",
    "nearest_plc",
    "nearest_distance_m",
)
_CONTRADICTION_COLUMNS = (
    "id",
    "station_id",
    "offline_station_id",
    "feed_key",
    "stop_key",
    "stop_name",
    "code_value",
    "code_series",
    "tier",
    "match_method",
    "distance_m",
    "name_sim",
    "note",
)


# ────────────────────────────── pure helpers ──────────────────────────────


def split_feed_counts(rows: Iterable[tuple[str | None, int]]) -> list[dict[str, Any]]:
    """Feed facet: a stop listed for `A|B` counts once for A and once for B."""
    counts: dict[str, int] = {}
    for feeds, count in rows:
        for feed in (feeds or "").split(FEED_SEPARATOR):
            if feed:
                counts[feed] = counts.get(feed, 0) + count
    return [{"value": feed, "count": counts[feed]} for feed in sorted(counts)]


def feed_clause(feed: str) -> ColumnElement[bool]:
    """Rows whose `feed_key` is `feed` or lists it among several (`A|B|C`)."""
    escaped = like_pattern(feed)[1:-1]  # the escaped term, without the surrounding wildcards
    column = StationRefLink.feed_key
    sep = FEED_SEPARATOR
    return or_(
        column == feed,
        column.like(f"{escaped}{sep}%", escape=_ESCAPE),
        column.like(f"%{sep}{escaped}", escape=_ESCAPE),
        column.like(f"%{sep}{escaped}{sep}%", escape=_ESCAPE),
    )


def label_clause(labels: Sequence[str] | None) -> ColumnElement[bool] | None:
    """The label filter of the unmatched list: Rail and Multimodal unless the
    caller names labels, and no filter at all for `all`."""
    if not labels:
        return StationRefLink.label.in_(DEFAULT_LABELS)
    if ALL_LABELS in labels:
        return None
    return StationRefLink.label.in_(list(labels))


def unmatched_clauses(
    *,
    labels: Sequence[str] | None = None,
    feed: str | None = None,
    country: str | None = None,
    q: str | None = None,
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [StationRefLink.station_id.is_(None)]
    by_label = label_clause(labels)
    if by_label is not None:
        clauses.append(by_label)
    if feed:
        clauses.append(feed_clause(feed))
    if country:
        clauses.append(StationRefLink.iso2 == country.upper())
    if q:
        clauses.append(StationRefLink.stop_name.ilike(like_pattern(q), escape=_ESCAPE))
    return clauses


def contradiction_clauses(
    *, feed: str | None = None, country: str | None = None, q: str | None = None
) -> list[ColumnElement[bool]]:
    """A matched link the offline chain did not assert although the stop
    carries a code. The country is the reference row's: a matched link has no
    country of its own."""
    clauses: list[ColumnElement[bool]] = [
        StationRefLink.station_id.is_not(None),
        StationRefLink.asserted.is_(False),
        StationRefLink.code_value.is_not(None),
    ]
    if feed:
        clauses.append(feed_clause(feed))
    if country:
        in_country = select(StationRef.id).where(StationRef.iso2 == country.upper())
        clauses.append(StationRefLink.station_id.in_(in_country))
    if q:
        like = like_pattern(q)
        clauses.append(
            or_(
                StationRefLink.stop_name.ilike(like, escape=_ESCAPE),
                StationRefLink.stop_key == q,
                StationRefLink.code_value == q,
            )
        )
    return clauses


# ────────────────────────────── db helpers ──────────────────────────────


def _page(
    db: DbSession,
    response: Response,
    columns: Sequence[str],
    clauses: Sequence[ColumnElement[bool]],
    order: Sequence[Any],
    page: int,
    size: int,
) -> list[dict[str, Any]]:
    """One page of link rows, with the total in `X-Total-Count`."""
    total = db.execute(
        select(func.count()).select_from(StationRefLink).where(*clauses)
    ).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    rows = db.execute(
        select(*[getattr(StationRefLink, name) for name in columns])
        .where(*clauses)
        .order_by(*order)
        .offset(page * size)
        .limit(size)
    ).all()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _stations(db: DbSession, clause: ColumnElement[bool]) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            StationRef.id, StationRef.plc, StationRef.era_uopid, StationRef.name, StationRef.iso2
        )
        .where(clause)
        .order_by(StationRef.plc, StationRef.era_uopid)
    ).all()
    return [{"id": r[0], "plc": r[1], "era_uopid": r[2], "name": r[3], "iso2": r[4]} for r in rows]


def _facet(
    db: DbSession, column: Any, clauses: Sequence[ColumnElement[bool]]
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(column, func.count()).where(*clauses).group_by(column).order_by(column)
    ).all()
    return [{"value": row[0], "count": int(row[1])} for row in rows]


def _feed_facet(db: DbSession, clauses: Sequence[ColumnElement[bool]]) -> list[dict[str, Any]]:
    rows = db.execute(
        select(StationRefLink.feed_key, func.count())
        .where(*clauses)
        .group_by(StationRefLink.feed_key)
    ).all()
    return split_feed_counts((row[0], int(row[1])) for row in rows)


# ──────────────────────────────── routes ────────────────────────────────


@router.get("/summary")
def links_summary(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """How many stops are in each list, and the values each filter offers."""
    every_unmatched = unmatched_clauses(labels=[ALL_LABELS])
    contradicting = contradiction_clauses()
    labels = _facet(db, StationRefLink.label, every_unmatched)
    countries = db.execute(
        select(StationRef.iso2, func.count())
        .join(StationRefLink, StationRefLink.station_id == StationRef.id)
        .where(*contradicting)
        .group_by(StationRef.iso2)
        .order_by(StationRef.iso2)
    ).all()
    return {
        "default_labels": list(DEFAULT_LABELS),
        "links": db.execute(select(func.count()).select_from(StationRefLink)).scalar_one(),
        "unmatched": {
            "total": sum(item["count"] for item in labels),
            "labels": labels,
            "feeds": _feed_facet(db, every_unmatched),
            "countries": _facet(db, StationRefLink.iso2, every_unmatched),
        },
        "contradictions": {
            "total": db.execute(
                select(func.count()).select_from(StationRefLink).where(*contradicting)
            ).scalar_one(),
            "feeds": _feed_facet(db, contradicting),
            "countries": [{"value": row[0], "count": int(row[1])} for row in countries],
        },
    }


@router.get("/unmatched")
def list_unmatched(
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    label: Annotated[
        list[str] | None,
        Query(description="Repeatable. Default: Rail and Multimodal. `all`: every label"),
    ] = None,
    feed: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=2)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Stop name")] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Stops no reference row could be matched to, the nearest first.

    Each row carries the nearest PLC and its distance, the hint an operator
    works from, and the reference row(s) of that PLC to link to.
    `X-Total-Count` is the number of stops the filters match.
    """
    clauses = unmatched_clauses(labels=label, feed=feed, country=country, q=q)
    order = (
        StationRefLink.nearest_distance_m.asc().nulls_last(),
        StationRefLink.stop_name,
        StationRefLink.id,
    )
    rows = _page(db, response, _UNMATCHED_COLUMNS, clauses, order, page, size)
    plcs = {row["nearest_plc"] for row in rows if row["nearest_plc"]}
    nearest: dict[str, list[dict[str, Any]]] = {}
    if plcs:
        for station in _stations(db, StationRef.plc.in_(plcs)):
            nearest.setdefault(station["plc"], []).append(station)
    for row in rows:
        row["nearest"] = nearest.get(row["nearest_plc"], [])
    return rows


@router.get("/contradictions")
def list_contradictions(
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    feed: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=2, description="Of the reference row")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Stop name, key or code")] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Stops whose code contradicts the reference: links that were not
    asserted although the stop carries a code. Each row carries the reference
    row the code pointed at. `X-Total-Count` is the number of rows matched.
    """
    clauses = contradiction_clauses(feed=feed, country=country, q=q)
    order = (StationRefLink.feed_key, StationRefLink.stop_name, StationRefLink.id)
    rows = _page(db, response, _CONTRADICTION_COLUMNS, clauses, order, page, size)
    ids = {row["station_id"] for row in rows}
    stations = {s["id"]: s for s in _stations(db, StationRef.id.in_(ids))} if ids else {}
    for row in rows:
        row["station"] = stations.get(row["station_id"])
    return rows
