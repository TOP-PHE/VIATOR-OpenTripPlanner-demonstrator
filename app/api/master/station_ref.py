"""The VIATOR station reference (screen D).

    GET    /api/master/station-ref                          the reference, paginated
    GET    /api/master/station-ref/summary                  the build it comes from, and the filters
    GET    /api/master/station-ref/{id}                     one station, with everything known about it
    POST   /api/master/station-ref/{id}/overrides           correct one field by hand
    DELETE /api/master/station-ref/{id}/overrides/{oid}     release a correction
    POST   /api/master/station-ref/complexes                group stations into a complex
    DELETE /api/master/station-ref/complexes/{cid}          ungroup a complex

Authorization: content_manager or platform_admin, declared on every route.

**The pivot has one affordable shape.** "One column per provider" is stored
long, in `station_ref_code`. The list paginates `station_ref` FIRST, then
fetches the codes of that page's stations in one query and pivots them here,
in the application. It never joins first: a join would multiply the reference
rows by their codes before the page is cut.

A PLC can carry several operational points, most of them identical in every
displayed column. By default the list shows one row per PLC (`collapse=true`),
with how many operational points it carries; `collapse=false` shows them all.

See docs/station-panel-design.md section 4D.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Select, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session as DbSession

from ... import audit
from ...db import get_db
from ...master import station_overrides as so
from ...models import (
    StationBuild,
    StationComplex,
    StationRef,
    StationRefAlias,
    StationRefCode,
    StationRefFlag,
    StationRefHistory,
    StationRefLink,
    StationRefMerits,
    StationRefOverride,
    StationSource,
)
from ...security import CurrentUser, client_ip, require_content_manager

router = APIRouter(prefix="/api/master/station-ref", tags=["master", "station-ref"])

# The four structurally different groupings (design section 3.4).
COMPLEX_KINDS: frozenset[str] = frozenset(
    {"one_site", "shared_operational_point", "parallel_register", "adjacent_treated_as_one"}
)
COMPLEX_ROLES = ("principal", "member")
NO_CONFIDENCE = "none"
_NOT_FOUND = "Station not found"
_PROVIDER_FORMAT = "offline_master_column"
_FACET_LIMIT = 200
_HISTORY_LIMIT = 100
_MERITS_COLUMNS = ("code", "origin", "rule", "confidence", "sources", "check_digit", "is_chosen")

# The scalar station_ref columns the detail view returns as they are.
_DETAIL_FIELDS = (
    "id",
    "plc",
    "era_uopid",
    "previous_plc",
    "name",
    "name_src",
    "alt_name",
    "lat",
    "lon",
    "pos_src",
    "link_pos_src",
    "position_flag",
    "iso2",
    "iso2_all",
    "op_type_all",
    "op_type_src",
    "is_passenger",
    "is_passenger_src",
    "plc_kind",
    "n_op_with_plc",
    "plc_op_max_sep_m",
    "spine_source",
    "is_current",
    "crd_source_tag",
    "uic_merits",
    "uic_merits_origin",
    "uic_merits_rule",
    "uic_merits_confidence",
    "rl100",
    "nat_code",
    "nat_code_series",
    "nat_code_src",
    "ifopt_dhid",
    "ifopt_dhid_src",
    "eva",
    "eva_src",
    "eva_all",
    "complex_id",
    "complex_role",
    "warning_level",
    "best_tier",
    "n_nap_feeds",
    "first_seen_build_id",
    "last_built_build_id",
    "last_changed_build_id",
)


# ──────────────────────────── pydantic models ────────────────────────────


class ReferenceRow(BaseModel):
    id: int
    plc: str
    era_uopid: str
    name: str | None
    iso2: str | None
    is_passenger: bool | None
    is_current: bool
    complex_id: int | None
    complex_label: str | None
    complex_role: str | None
    uic_merits: str | None
    uic_merits_origin: str | None
    uic_merits_confidence: str | None
    # One entry per provider: the wide view of station_ref_code.
    codes: dict[str, list[str]]
    flags: list[str]
    warning_level: str | None
    best_tier: str | None
    n_nap_feeds: int | None
    n_op_with_plc: int | None
    plc_op_max_sep_m: int | None
    # "PLC carries 7 operational points, 0 m apart"; None for a PLC with one.
    op_badge: str | None
    has_override: bool
    in_latest_build: bool


class OverrideBody(BaseModel):
    field_name: str
    # None or '' corrects the field to "no value".
    value: str | None = None
    reason: str = Field(min_length=3, max_length=500)


class ComplexBody(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    kind: str
    station_ids: list[int] = Field(min_length=2, max_length=50)
    # The member passengers name the complex after; the others are plain members.
    principal_id: int | None = None
    rule: str | None = Field(default=None, max_length=500)
    requires_physical_separation: bool = False
    separation_reason: str | None = Field(default=None, max_length=500)


# ────────────────────────────── pure helpers ──────────────────────────────


def op_badge(n_op_with_plc: int | None, max_sep_m: int | None) -> str | None:
    """What tells an operator that a PLC is more than one operational point."""
    if n_op_with_plc is None or n_op_with_plc <= 1:
        return None
    apart = "" if max_sep_m is None else f", {max_sep_m} m apart"
    return f"PLC carries {n_op_with_plc} operational points{apart}"


def pivot_codes(rows: Iterable[tuple[int, str, str]]) -> dict[int, dict[str, list[str]]]:
    """Long to wide: (station id, source key, code) rows -> per station, per
    provider, its codes. Done here, after the page was cut, never in SQL."""
    out: dict[int, dict[str, list[str]]] = {}
    for station_id, source_key, code in rows:
        out.setdefault(station_id, {}).setdefault(source_key, []).append(code)
    return out


def like_pattern(term: str) -> str:
    """A LIKE pattern matching `term` anywhere, its own wildcards escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_clause(term: str) -> ColumnElement[bool]:
    """Name, PLC, or any code in any series.

    The code lookup is a subquery on `station_ref_code (code)`, not a join.
    """
    like = like_pattern(term)
    by_code = select(StationRefCode.station_id).where(StationRefCode.code == term)
    return or_(
        StationRef.name.ilike(like, escape="\\"),
        StationRef.alt_name_text.ilike(like, escape="\\"),
        StationRef.plc.ilike(like, escape="\\"),
        StationRef.era_uopid == term,
        StationRef.previous_plc == term,
        StationRef.uic_merits == term,
        StationRef.eva == term,
        StationRef.rl100 == term,
        StationRef.id.in_(by_code),
    )


def filter_clauses(
    *,
    q: str | None = None,
    country: str | None = None,
    confidence: str | None = None,
    flag: str | None = None,
    has_code: bool | None = None,
    plc: str | None = None,
) -> list[ColumnElement[bool]]:
    """The WHERE clauses of the list, none of which joins."""
    clauses: list[ColumnElement[bool]] = []
    if q:
        clauses.append(search_clause(q))
    if country:
        clauses.append(StationRef.iso2 == country.upper())
    if confidence == NO_CONFIDENCE:
        clauses.append(StationRef.uic_merits_confidence.is_(None))
    elif confidence:
        clauses.append(StationRef.uic_merits_confidence == confidence)
    if flag:
        flagged = select(StationRefFlag.station_id).where(StationRefFlag.token == flag)
        clauses.append(StationRef.id.in_(flagged))
    if has_code is not None:
        coded = exists().where(StationRefCode.station_id == StationRef.id)
        clauses.append(coded if has_code else ~coded)
    if plc:
        clauses.append(StationRef.plc == plc)
    return clauses


def page_query(clauses: Sequence[ColumnElement[bool]], collapse: bool) -> Select[StationRef]:
    """The reference rows of the list, before the page is cut.

    Collapsed, one row per PLC stands for all its operational points: the one
    whose operational-point id is the PLC itself when there is one, else the
    first in byte order.
    """
    if not collapse:
        return select(StationRef).where(*clauses).order_by(StationRef.plc, StationRef.era_uopid)
    rank = func.row_number().over(
        partition_by=StationRef.plc,
        order_by=((StationRef.era_uopid == StationRef.plc).desc(), StationRef.era_uopid),
    )
    ranked = select(StationRef.id.label("id"), rank.label("rank")).where(*clauses).subquery()
    return (
        select(StationRef)
        .join(ranked, ranked.c.id == StationRef.id)
        .where(ranked.c.rank == 1)
        .order_by(StationRef.plc)
    )


def count_query(clauses: Sequence[ColumnElement[bool]], collapse: bool) -> Select[int]:
    counted = func.count(StationRef.plc.distinct()) if collapse else func.count()
    return select(counted).select_from(StationRef).where(*clauses)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ────────────────────────────── list ──────────────────────────────


def _latest_build_id(db: DbSession) -> int | None:
    latest: int | None = db.execute(
        select(func.max(StationBuild.id)).where(StationBuild.status == "done")
    ).scalar_one()
    return latest


def _page_extras(
    db: DbSession, stations: Sequence[StationRef]
) -> tuple[dict[int, dict[str, list[str]]], dict[int, list[str]], dict[int, str], set[int]]:
    """Codes, flags, complex labels and corrected stations, for one page only."""
    ids = [s.id for s in stations]
    if not ids:
        return {}, {}, {}, set()
    code_rows = db.execute(
        select(StationRefCode.station_id, StationRefCode.source_key, StationRefCode.code)
        .where(StationRefCode.station_id.in_(ids))
        .order_by(StationRefCode.is_primary.desc(), StationRefCode.code)
    ).all()
    flags: dict[int, list[str]] = {}
    for station_id, token in db.execute(
        select(StationRefFlag.station_id, StationRefFlag.token)
        .where(StationRefFlag.station_id.in_(ids))
        .order_by(StationRefFlag.token)
    ).all():
        flags.setdefault(station_id, []).append(token)
    complex_ids = {s.complex_id for s in stations if s.complex_id is not None}
    labels = (
        {
            row[0]: row[1]
            for row in db.execute(
                select(StationComplex.id, StationComplex.label).where(
                    StationComplex.id.in_(complex_ids)
                )
            ).all()
        }
        if complex_ids
        else {}
    )
    corrected = set(
        db.execute(
            select(StationRefOverride.station_id).where(
                StationRefOverride.station_id.in_(ids), StationRefOverride.released_at.is_(None)
            )
        ).scalars()
    )
    return pivot_codes((r[0], r[1], r[2]) for r in code_rows), flags, labels, corrected


def reference_row(
    station: StationRef,
    *,
    codes: dict[str, list[str]],
    flags: list[str],
    complex_label: str | None,
    has_override: bool,
    latest_build_id: int | None,
) -> ReferenceRow:
    return ReferenceRow(
        id=station.id,
        plc=station.plc,
        era_uopid=station.era_uopid,
        name=station.name,
        iso2=station.iso2,
        is_passenger=station.is_passenger,
        is_current=station.is_current,
        complex_id=station.complex_id,
        complex_label=complex_label,
        complex_role=station.complex_role,
        uic_merits=station.uic_merits,
        uic_merits_origin=station.uic_merits_origin,
        uic_merits_confidence=station.uic_merits_confidence,
        codes=codes,
        flags=flags,
        warning_level=station.warning_level,
        best_tier=station.best_tier,
        n_nap_feeds=station.n_nap_feeds,
        n_op_with_plc=station.n_op_with_plc,
        plc_op_max_sep_m=station.plc_op_max_sep_m,
        op_badge=op_badge(station.n_op_with_plc, station.plc_op_max_sep_m),
        has_override=has_override,
        in_latest_build=latest_build_id is not None
        and station.last_built_build_id == latest_build_id,
    )


@router.get("")
def list_reference(
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    q: Annotated[str | None, Query(max_length=200, description="Name, PLC or any code")] = None,
    country: Annotated[str | None, Query(max_length=2)] = None,
    confidence: Annotated[str | None, Query(max_length=40)] = None,
    flag: Annotated[str | None, Query(max_length=200)] = None,
    has_code: Annotated[bool | None, Query(description="Has a timetable code")] = None,
    plc: Annotated[str | None, Query(max_length=7)] = None,
    collapse: Annotated[bool, Query(description="One row per PLC")] = True,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ReferenceRow]:
    """The reference, ordered by PLC.

    Headers: `X-Total-Count` is the number of rows the filters match (PLCs
    when collapsed, operational points otherwise).
    """
    clauses = filter_clauses(
        q=q, country=country, confidence=confidence, flag=flag, has_code=has_code, plc=plc
    )
    total = db.execute(count_query(clauses, collapse)).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    stations = (
        db.execute(page_query(clauses, collapse).offset(page * size).limit(size)).scalars().all()
    )
    codes, flags, labels, corrected = _page_extras(db, stations)
    latest = _latest_build_id(db)
    return [
        reference_row(
            s,
            codes=codes.get(s.id, {}),
            flags=flags.get(s.id, []),
            complex_label=labels.get(s.complex_id) if s.complex_id is not None else None,
            has_override=s.id in corrected,
            latest_build_id=latest,
        )
        for s in stations
    ]


# ────────────────────────────── summary ──────────────────────────────


def _facet(db: DbSession, column: Any) -> list[dict[str, Any]]:
    rows = db.execute(
        select(column, func.count())
        .group_by(column)
        .order_by(func.count().desc(), column)
        .limit(_FACET_LIMIT)
    ).all()
    return [{"value": row[0], "count": int(row[1])} for row in rows]


def _providers(db: DbSession) -> list[dict[str, Any]]:
    """The columns of the wide view: every provider that has a code, and every
    seeded provider column, in a stable order."""
    used = set(db.execute(select(StationRefCode.source_key).distinct()).scalars())
    seeded = {
        row[0]: (row[1], row[2])
        for row in db.execute(
            select(
                StationSource.key, StationSource.label, StationSource.source_key_unresolved
            ).where(StationSource.format == _PROVIDER_FORMAT)
        ).all()
    }
    return [
        {
            "key": key,
            "label": seeded.get(key, (key, False))[0],
            "unresolved": seeded.get(key, (key, False))[1],
            "has_codes": key in used,
        }
        for key in sorted(used | set(seeded))
    ]


@router.get("/summary")
def reference_summary(
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """The header of the screen: the build this state comes from, its input
    versions and its diff against the previous state; and the filter values."""
    build = (
        db.execute(
            select(StationBuild)
            .where(StationBuild.status == "done")
            .order_by(StationBuild.id.desc())
        )
        .scalars()
        .first()
    )
    return {
        "build": None
        if build is None
        else {
            "id": build.id,
            "finished_at": _iso(build.finished_at),
            "builder_version": build.builder_version,
            "inputs": build.inputs,
            "counts": build.counts,
            "diff_summary": build.diff_summary,
        },
        "stations": db.execute(select(func.count()).select_from(StationRef)).scalar_one(),
        "plcs": db.execute(select(func.count(StationRef.plc.distinct()))).scalar_one(),
        "providers": _providers(db),
        "countries": _facet(db, StationRef.iso2),
        "confidences": _facet(db, StationRef.uic_merits_confidence),
        "flags": _facet(db, StationRefFlag.token),
        "complex_kinds": sorted(COMPLEX_KINDS),
        "overridable_fields": sorted(so.OVERRIDABLE_FIELDS),
    }


# ────────────────────────────── detail ──────────────────────────────


def _station_or_404(db: DbSession, station_id: int) -> StationRef:
    station = db.get(StationRef, station_id)
    if station is None:
        raise HTTPException(404, _NOT_FOUND)
    return station


def _brief(db: DbSession, clause: ColumnElement[bool]) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            StationRef.id,
            StationRef.plc,
            StationRef.era_uopid,
            StationRef.name,
            StationRef.complex_role,
        )
        .where(clause)
        .order_by(StationRef.plc, StationRef.era_uopid)
    ).all()
    return [
        {"id": r[0], "plc": r[1], "era_uopid": r[2], "name": r[3], "complex_role": r[4]}
        for r in rows
    ]


def _records(
    db: DbSession, model: Any, columns: Sequence[str], clause: Any, order: Any
) -> list[dict[str, Any]]:
    """Rows of `model` as dicts of `columns`."""
    rows = db.execute(
        select(*[getattr(model, name) for name in columns]).where(clause).order_by(*order)
    ).all()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _flags_of(db: DbSession, station_id: int) -> list[dict[str, Any]]:
    """A station's flags, each with the station its payload names, if any."""
    flags = _records(
        db,
        StationRefFlag,
        ("token", "payload", "level", "warning_code", "related_station_id"),
        StationRefFlag.station_id == station_id,
        (StationRefFlag.token, StationRefFlag.payload),
    )
    related_ids = {f["related_station_id"] for f in flags if f["related_station_id"] is not None}
    related = (
        {s["id"]: s for s in _brief(db, StationRef.id.in_(related_ids))} if related_ids else {}
    )
    for item in flags:
        item["related"] = related.get(item["related_station_id"])
    return flags


def _complex_of(db: DbSession, station: StationRef) -> dict[str, Any] | None:
    if station.complex_id is None:
        return None
    group = db.get(StationComplex, station.complex_id)
    if group is None:  # pragma: no cover  the FK is SET NULL
        return None
    return {
        "id": group.id,
        "label": group.label,
        "kind": group.kind,
        "source": group.source,
        "rule": group.rule,
        "requires_physical_separation": group.requires_physical_separation,
        "separation_reason": group.separation_reason,
        "members": _brief(db, StationRef.complex_id == group.id),
    }


@router.get("/{station_id}", responses={404: {"description": _NOT_FOUND}})
def get_station(
    station_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """One station: every code with its series, source and rule; every MERITS
    candidate; the NAP stops matched to it; its complex; its flags, each with
    the station it names; its corrections; what the builds changed on it."""
    station = _station_or_404(db, station_id)
    detail: dict[str, Any] = {name: getattr(station, name) for name in _DETAIL_FIELDS}
    detail.update(
        crd_start=station.crd_start.isoformat() if station.crd_start else None,
        crd_end=station.crd_end.isoformat() if station.crd_end else None,
        op_badge=op_badge(station.n_op_with_plc, station.plc_op_max_sep_m),
        in_latest_build=station.last_built_build_id == _latest_build_id(db),
        # The other operational points of the same PLC.
        siblings=_brief(db, (StationRef.plc == station.plc) & (StationRef.id != station.id)),
        complex=_complex_of(db, station),
        codes=_records(
            db,
            StationRefCode,
            (
                "source_key",
                "series",
                "code",
                "code_raw",
                "normalisation_rule",
                "is_primary",
                "evidence_only",
                "confidence",
                "method",
            ),
            StationRefCode.station_id == station_id,
            (StationRefCode.source_key, StationRefCode.is_primary.desc(), StationRefCode.code),
        ),
        merits=_records(
            db,
            StationRefMerits,
            _MERITS_COLUMNS,
            StationRefMerits.station_id == station_id,
            (StationRefMerits.is_chosen.desc(), StationRefMerits.code),
        ),
        links=_records(
            db,
            StationRefLink,
            (
                "feed_key",
                "stop_key",
                "stop_name",
                "code_value",
                "code_series",
                "match_method",
                "tier",
                "asserted",
                "distance_m",
                "name_sim",
                "label",
                "note",
                "offline_station_id",
            ),
            StationRefLink.station_id == station_id,
            (StationRefLink.asserted.desc(), StationRefLink.feed_key, StationRefLink.stop_key),
        ),
        aliases=_records(
            db,
            StationRefAlias,
            ("alias_plc", "reason", "build_id"),
            StationRefAlias.station_id == station_id,
            (StationRefAlias.build_id.desc(),),
        ),
        flags=_flags_of(db, station_id),
        overrides=[_override_dict(o) for o in _overrides_of(db, station_id)],
        history=_records(
            db,
            StationRefHistory,
            ("build_id", "field_name", "old_value", "new_value"),
            StationRefHistory.station_id == station_id,
            (StationRefHistory.build_id.desc(), StationRefHistory.field_name),
        )[:_HISTORY_LIMIT],
    )
    return detail


# ────────────────────────────── overrides ──────────────────────────────


def _overrides_of(db: DbSession, station_id: int) -> list[StationRefOverride]:
    return list(
        db.execute(
            select(StationRefOverride)
            .where(StationRefOverride.station_id == station_id)
            .order_by(
                StationRefOverride.released_at.is_(None).desc(), StationRefOverride.set_at.desc()
            )
        ).scalars()
    )


def _override_dict(override: StationRefOverride) -> dict[str, Any]:
    return {
        "id": override.id,
        "field_name": override.field_name,
        "value": override.value,
        "reason": override.reason,
        "set_at": _iso(override.set_at),
        "computed_value_at_set": override.computed_value_at_set,
        "computed_value_latest": override.computed_value_latest,
        "released_at": _iso(override.released_at),
        "active": override.released_at is None,
        # The build's own value has moved since the correction was made.
        "drifted": override.computed_value_latest != override.computed_value_at_set,
    }


def _candidates(db: DbSession, station_id: int) -> list[dict[str, Any]]:
    return _records(
        db,
        StationRefMerits,
        _MERITS_COLUMNS,
        StationRefMerits.station_id == station_id,
        (StationRefMerits.id,),
    )


def _replace_candidates(
    db: DbSession, station: StationRef, candidates: list[dict[str, Any]]
) -> None:
    """Write a station's MERITS candidates and mirror the chosen one on the row."""
    db.execute(delete(StationRefMerits).where(StationRefMerits.station_id == station.id))
    db.flush()
    for candidate in candidates:
        db.add(StationRefMerits(station_id=station.id, **candidate))
    for name, value in so.merits_mirror(candidates).items():
        setattr(station, name, value)


def _apply(
    db: DbSession, station: StationRef, field_name: str, value: Any, reason: str | None
) -> None:
    """Put a corrected value on the station. A MERITS code changes which
    candidate is chosen; every other field is just the column."""
    if field_name == so.MERITS_FIELD:
        candidates = so.merits_with_override(_candidates(db, station.id), value, reason)
        _replace_candidates(db, station, candidates)
    else:
        setattr(station, field_name, value)


def _restore(db: DbSession, station: StationRef, field_name: str, computed: str | None) -> None:
    """Put back what the build computes for a field whose correction is released."""
    if field_name == so.MERITS_FIELD:
        candidates = so.merits_without_override(_candidates(db, station.id), computed)
        _replace_candidates(db, station, candidates)
    else:
        setattr(station, field_name, so.from_text(field_name, computed))


@router.post(
    "/{station_id}/overrides",
    status_code=201,
    responses={
        400: {"description": "A field that cannot be corrected, or a value that does not fit it"},
        404: {"description": _NOT_FOUND},
    },
)
def set_override(
    station_id: int,
    body: OverrideBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """Correct one field of a station by hand.

    The correction survives rebuilds: the build re-applies it and remembers
    underneath what it computes itself. Correcting a field that is already
    corrected replaces the earlier correction, which is kept as released.
    """
    station = _station_or_404(db, station_id)
    try:
        value = so.from_text(body.field_name, body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    now = datetime.now(UTC)
    previous = db.execute(
        select(StationRefOverride).where(
            StationRefOverride.station_id == station_id,
            StationRefOverride.field_name == body.field_name,
            StationRefOverride.released_at.is_(None),
        )
    ).scalar_one_or_none()
    if previous is not None:
        # What the build computes is under the earlier correction, not in the column.
        computed = previous.computed_value_latest
        previous.released_at = now
        db.flush()  # the partial unique index allows one active correction per field
    else:
        computed = so.to_text(getattr(station, body.field_name))

    override = StationRefOverride(
        station_id=station_id,
        field_name=body.field_name,
        value=so.to_text(value),
        reason=body.reason,
        set_by=actor.id,
        set_at=now,
        computed_value_at_set=computed,
        computed_value_latest=computed,
    )
    db.add(override)
    _apply(db, station, body.field_name, value, body.reason)
    db.flush()
    audit.record(
        db,
        action="station_ref.override.set",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_ref",
        target_id=str(station_id),
        metadata={"field": body.field_name, "plc": station.plc, "era_uopid": station.era_uopid},
    )
    db.commit()
    db.refresh(override)
    return _override_dict(override)


@router.delete(
    "/{station_id}/overrides/{override_id}",
    responses={
        404: {"description": "No such correction on this station"},
        409: {"description": "The correction was already released, or cannot be"},
    },
)
def release_override(
    station_id: int,
    override_id: int,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """Release a correction: the field goes back to what the build computes
    now, which is not necessarily what it computed when the correction was made."""
    station = _station_or_404(db, station_id)
    override = db.get(StationRefOverride, override_id)
    if override is None or override.station_id != station_id:
        raise HTTPException(404, "Correction not found")
    if override.released_at is not None:
        raise HTTPException(409, "This correction was already released")
    override.released_at = datetime.now(UTC)
    try:
        _restore(db, station, override.field_name, override.computed_value_latest)
    except ValueError as exc:
        raise HTTPException(409, f"This correction cannot be released: {exc}") from exc
    audit.record(
        db,
        action="station_ref.override.released",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_ref",
        target_id=str(station_id),
        metadata={"field": override.field_name, "plc": station.plc},
    )
    db.commit()
    db.refresh(override)
    return _override_dict(override)


# ────────────────────────────── complexes ──────────────────────────────


@router.post(
    "/complexes",
    status_code=201,
    responses={
        400: {"description": "Unknown kind, or a principal that is not one of the stations"},
        404: {"description": "One of the stations does not exist"},
        409: {"description": "One of the stations already belongs to a complex"},
    },
)
def create_complex(
    body: ComplexBody,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """Group stations that passengers treat as one into a complex.

    CRD carries no station-to-sub-station link, so every grouping is derived
    or manual; one made here is `manual`.
    """
    if body.kind not in COMPLEX_KINDS:
        raise HTTPException(400, f"unknown kind {body.kind!r}; valid: {sorted(COMPLEX_KINDS)}")
    ids = sorted(set(body.station_ids))
    if body.principal_id is not None and body.principal_id not in ids:
        raise HTTPException(400, "principal_id must be one of station_ids")
    stations = list(db.execute(select(StationRef).where(StationRef.id.in_(ids))).scalars())
    if len(stations) != len(ids):
        raise HTTPException(404, "One of the stations does not exist")
    taken = sorted(s.plc for s in stations if s.complex_id is not None)
    if taken:
        raise HTTPException(409, f"already in a complex: {', '.join(taken)}")

    group = StationComplex(
        label=body.label,
        kind=body.kind,
        source="manual",
        rule=body.rule,
        requires_physical_separation=body.requires_physical_separation,
        separation_reason=body.separation_reason,
    )
    db.add(group)
    db.flush()
    for station in stations:
        station.complex_id = group.id
        station.complex_role = (
            COMPLEX_ROLES[0] if station.id == body.principal_id else COMPLEX_ROLES[1]
        )
    audit.record(
        db,
        action="station_complex.created",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_complex",
        target_id=str(group.id),
        metadata={"label": group.label, "kind": group.kind, "station_ids": ids},
    )
    db.commit()
    return {"id": group.id, "label": group.label, "kind": group.kind, "station_ids": ids}


@router.delete(
    "/complexes/{complex_id}",
    status_code=204,
    responses={404: {"description": "Complex not found"}},
)
def delete_complex(
    complex_id: int,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    actor: Annotated[CurrentUser, Depends(require_content_manager)],
) -> None:
    """Ungroup a complex. Its stations stay; only the grouping goes."""
    group = db.get(StationComplex, complex_id)
    if group is None:
        raise HTTPException(404, "Complex not found")
    # The FK nulls complex_id on delete; the role must go with it.
    db.execute(
        update(StationRef)
        .where(StationRef.complex_id == complex_id)
        .values(complex_id=None, complex_role=None)
        .execution_options(synchronize_session=False)
    )
    audit.record(
        db,
        action="station_complex.deleted",
        actor_user_id=actor.id,
        actor_ip=client_ip(request),
        target_kind="station_complex",
        target_id=str(complex_id),
        metadata={"label": group.label},
    )
    db.delete(group)
    db.commit()
