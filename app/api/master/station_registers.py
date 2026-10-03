"""The infrastructure registers (screen B): CRD and ERA.

    GET /api/master/station-registers/{register}/versions   the versions, and the source's licence
    GET /api/master/station-registers/{register}            its rows, for one version
    GET /api/master/station-registers/{register}/delta      what changed between two versions

`{register}` is `crd` or `era`. Authorization: content_manager or
platform_admin, declared on every route. Read-only: a register changes by
uploading a new version of its source on the Sources screen.

Each list reads one `source_version_id`, by default the latest one whose rows
are loaded. Rows are loaded by the importer's extract stage, not by the upload
request, so a version that was uploaded and not yet imported is listed and
marked as not loaded.

See docs/station-panel-design.md section 4B.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session as DbSession

from ...db import get_db
from ...master import station_delta as sd
from ...master import station_files as sf
from ...models import (
    CrdLocation,
    CrdSubsidiary,
    EraOperationalPoint,
    StationSource,
    StationSourceVersion,
)
from ...security import CurrentUser, require_content_manager
from .station_ref import like_pattern

router = APIRouter(prefix="/api/master/station-registers", tags=["master", "station-registers"])

Register = Literal["crd", "era"]

# How many rows of each kind a delta response carries; the counts are complete.
DELTA_LIMIT = 200
_NO_VERSION = "No version of this register is loaded yet"
_UNKNOWN_VERSION = "That version of this register is not loaded"

_CRD_COLUMNS = (
    "plc",
    "name",
    "lat",
    "lon",
    "country",
    "location_code",
    "start_validity",
    "end_validity",
    "passenger_flag",
    "freight_flag",
    "responsible_im",
    "nuts",
)
_ERA_COLUMNS = ("plc", "uopid", "name", "op_type", "iso2", "lat", "lon", "rl100")


@dataclass(frozen=True)
class _RegisterSpec:
    """What backs a register."""

    fmt: str  # the file shape of its source
    table: Any
    columns: tuple[str, ...]
    exact: str  # the column a search matches by equality
    country: str  # the column holding the country
    order: tuple[str, ...]


_REGISTERS: dict[str, _RegisterSpec] = {
    "crd": _RegisterSpec(
        sf.CRD_LOCATIONS,
        CrdLocation,
        _CRD_COLUMNS,
        "location_code",
        "country",
        ("plc", "start_validity"),
    ),
    "era": _RegisterSpec(
        sf.ERA_TELREF, EraOperationalPoint, _ERA_COLUMNS, "uopid", "iso2", ("plc", "uopid")
    ),
}


# ────────────────────────────── pure helpers ──────────────────────────────


def crd_rows(
    rows: Iterable[tuple[str, str | None, float | None, float | None, str | None]],
) -> list[sd.RegisterRow]:
    """CRD locations as delta rows, one per PLC.

    CRD's key is (country, code, validity), so one PLC can have several rows,
    one per validity period. The delta is about the location, not its history:
    the row with the latest start of validity stands for the PLC.
    """
    latest: dict[str, tuple[str, sd.RegisterRow]] = {}
    for plc, name, lat, lon, start in rows:
        stamp = start or ""
        if plc not in latest or stamp >= latest[plc][0]:
            latest[plc] = (stamp, sd.RegisterRow(plc, name, lat, lon))
    return [row for _, row in latest.values()]


def era_rows(
    rows: Iterable[tuple[str, str, str | None, float | None, float | None]],
) -> list[sd.RegisterRow]:
    """ERA operational points as delta rows, keyed on PLC and operational point."""
    return [
        sd.RegisterRow(f"{plc} / {uopid}", name, lat, lon) for plc, uopid, name, lat, lon in rows
    ]


def _row(row: sd.RegisterRow) -> dict[str, Any]:
    return {"key": row.key, "name": row.name, "lat": row.lat, "lon": row.lon}


def _pair(old: sd.RegisterRow, new: sd.RegisterRow) -> dict[str, Any]:
    return {"old": _row(old), "new": _row(new)}


def delta_payload(delta: sd.Delta, limit: int = DELTA_LIMIT) -> dict[str, Any]:
    """A delta as JSON: complete counts, and the first `limit` rows of each kind."""
    counts = delta.counts()
    return {
        "counts": counts,
        "limit": limit,
        "truncated": sorted(k for k, n in counts.items() if k != "unchanged" and n > limit),
        "created": [_row(r) for r in delta.created[:limit]],
        "removed": [_row(r) for r in delta.removed[:limit]],
        "renamed": [_pair(old, new) for old, new in delta.renamed[:limit]],
        "moved": [
            {**_pair(old, new), "distance_m": round(apart)}
            for old, new, apart in delta.moved[:limit]
        ],
        "renumbered": [_pair(old, new) for old, new in delta.renumbered[:limit]],
    }


# ────────────────────────────── versions ──────────────────────────────


def _loaded_ids(db: DbSession, register: str) -> set[uuid.UUID]:
    table = _REGISTERS[register].table
    return set(db.execute(select(table.source_version_id).distinct()).scalars())


def _versions(db: DbSession, register: str) -> list[tuple[StationSourceVersion, StationSource]]:
    """Every version of the register's source(s), newest first."""
    rows = db.execute(
        select(StationSourceVersion, StationSource)
        .join(StationSource, StationSource.id == StationSourceVersion.source_id)
        .where(StationSource.format == _REGISTERS[register].fmt)
        .order_by(StationSourceVersion.acquired_at.desc())
    ).all()
    return [(row[0], row[1]) for row in rows]


def _resolve_version(db: DbSession, register: str, version_id: uuid.UUID | None) -> uuid.UUID:
    """The version a list reads: the one asked for, else the latest loaded."""
    loaded = _loaded_ids(db, register)
    if version_id is not None:
        if version_id not in loaded:
            raise HTTPException(404, _UNKNOWN_VERSION)
        return version_id
    for version, _source in _versions(db, register):
        if version.id in loaded:
            return version.id
    raise HTTPException(404, _NO_VERSION)


def _version_dict(
    version: StationSourceVersion, source: StationSource, loaded: bool
) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "source_key": source.key,
        "acquired_at": version.acquired_at.isoformat() if version.acquired_at else None,
        "as_of": version.as_of.isoformat() if version.as_of else None,
        "filename": version.filename,
        "bytes": version.bytes,
        "sha256": version.sha256,
        "status": version.status,
        "stats": version.stats,
        "loaded": loaded,
    }


@router.get("/{register}/versions")
def list_versions(
    register: Register,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
) -> dict[str, Any]:
    """The versions of a register, newest first, and the licence of its source.

    `current` is the version the lists read by default: the latest loaded one.
    """
    loaded = _loaded_ids(db, register)
    versions = _versions(db, register)
    current = next((v for v, _ in versions if v.id in loaded), None)
    sources = {s.key: s for _, s in versions} or {
        s.key: s
        for s in db.execute(
            select(StationSource).where(StationSource.format == _REGISTERS[register].fmt)
        ).scalars()
    }
    return {
        "register": register,
        "current": str(current.id) if current else None,
        "versions": [_version_dict(v, s, v.id in loaded) for v, s in versions],
        "sources": [
            {"key": s.key, "label": s.label, "licence": s.licence, "licence_url": s.licence_url}
            for s in sources.values()
        ],
    }


# ────────────────────────────── lists ──────────────────────────────


def _subsidiaries(
    db: DbSession, version_id: uuid.UUID, plcs: Sequence[str]
) -> dict[str, list[dict[str, str]]]:
    """The subsidiary codes of one page's PLCs: fetched after the page is cut."""
    out: dict[str, list[dict[str, str]]] = {}
    if not plcs:
        return out
    rows = db.execute(
        select(CrdSubsidiary.plc, CrdSubsidiary.subsidiary_type, CrdSubsidiary.code)
        .where(CrdSubsidiary.source_version_id == version_id, CrdSubsidiary.plc.in_(plcs))
        .order_by(CrdSubsidiary.subsidiary_type, CrdSubsidiary.code)
    ).all()
    for plc, kind, code in rows:
        out.setdefault(plc, []).append({"type": kind, "code": code})
    return out


def list_clauses(
    register: str, version_id: uuid.UUID, q: str | None, country: str | None
) -> list[ColumnElement[bool]]:
    """The WHERE clauses of a register list: one version, and the filters.

    The search is by substring on the name and the PLC, and by equality on the
    register's own code (the CRD location code, the ERA operational point id).
    """
    spec = _REGISTERS[register]
    table = spec.table
    clauses: list[ColumnElement[bool]] = [table.source_version_id == version_id]
    if q:
        like = like_pattern(q)
        clauses.append(
            or_(
                table.name.ilike(like, escape="\\"),
                table.plc.ilike(like, escape="\\"),
                getattr(table, spec.exact) == q,
            )
        )
    if country:
        clauses.append(getattr(table, spec.country) == country.upper())
    return clauses


@router.get("/{register}", responses={404: {"description": _NO_VERSION}})
def list_register(
    register: Register,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    q: Annotated[str | None, Query(max_length=200, description="Name, PLC or code")] = None,
    country: Annotated[str | None, Query(max_length=2)] = None,
    version_id: Annotated[uuid.UUID | None, Query(description="Default: the latest loaded")] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """The rows of one version of a register, ordered by PLC.

    `/crd`: CRD primary locations, each with its subsidiary codes. `/era`: ERA
    operational points. `X-Total-Count` is the number of rows the filters
    match; `X-Version-Id` is the version that was read.
    """
    spec = _REGISTERS[register]
    table = spec.table
    version = _resolve_version(db, register, version_id)
    response.headers["X-Version-Id"] = str(version)
    clauses = list_clauses(register, version, q, country)
    total = db.execute(select(func.count()).select_from(table).where(*clauses)).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    found = db.execute(
        select(*[getattr(table, name) for name in spec.columns])
        .where(*clauses)
        .order_by(*[getattr(table, name) for name in spec.order])
        .offset(page * size)
        .limit(size)
    ).all()
    rows = [dict(zip(spec.columns, row, strict=True)) for row in found]
    if register == "crd":
        codes = _subsidiaries(db, version, [row["plc"] for row in rows])
        for row in rows:
            row["subsidiaries"] = codes.get(row["plc"], [])
    return rows


# ────────────────────────────── delta ──────────────────────────────


def _delta_rows(db: DbSession, register: str, version_id: uuid.UUID) -> list[sd.RegisterRow]:
    if register == "crd":
        rows = db.execute(
            select(
                CrdLocation.plc,
                CrdLocation.name,
                CrdLocation.lat,
                CrdLocation.lon,
                CrdLocation.start_validity,
            ).where(CrdLocation.source_version_id == version_id)
        ).all()
        return crd_rows((r[0], r[1], r[2], r[3], r[4]) for r in rows)
    points = db.execute(
        select(
            EraOperationalPoint.plc,
            EraOperationalPoint.uopid,
            EraOperationalPoint.name,
            EraOperationalPoint.lat,
            EraOperationalPoint.lon,
        ).where(EraOperationalPoint.source_version_id == version_id)
    ).all()
    return era_rows((r[0], r[1], r[2], r[3], r[4]) for r in points)


def _default_pair(db: DbSession, register: str) -> tuple[uuid.UUID, uuid.UUID]:
    """The two most recent loaded versions: (older, newer)."""
    loaded = _loaded_ids(db, register)
    recent = [v.id for v, _ in _versions(db, register) if v.id in loaded][:2]
    if len(recent) < 2:
        raise HTTPException(409, "A delta needs two loaded versions of this register")
    return recent[1], recent[0]


@router.get(
    "/{register}/delta",
    responses={
        404: {"description": _UNKNOWN_VERSION},
        409: {"description": "Fewer than two versions of this register are loaded"},
    },
)
def version_delta(
    register: Register,
    db: Annotated[DbSession, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(require_content_manager)],
    older: Annotated[
        uuid.UUID | None, Query(description="Default: the one before the latest")
    ] = None,
    newer: Annotated[uuid.UUID | None, Query(description="Default: the latest loaded")] = None,
    threshold_m: Annotated[float, Query(ge=0, le=100_000)] = sd.DEFAULT_THRESHOLD_M,
) -> dict[str, Any]:
    """What changed between two versions of a register: created, removed,
    renamed, moved further than `threshold_m`, and renumbered.

    A set difference between two `source_version_id`s. Counts are complete;
    each list carries its first rows only.
    """
    if older is None or newer is None:
        default_older, default_newer = _default_pair(db, register)
        older, newer = older or default_older, newer or default_newer
    loaded = _loaded_ids(db, register)
    if older not in loaded or newer not in loaded:
        raise HTTPException(404, _UNKNOWN_VERSION)
    delta = sd.register_delta(
        _delta_rows(db, register, older), _delta_rows(db, register, newer), threshold_m=threshold_m
    )
    return {
        "register": register,
        "older": str(older),
        "newer": str(newer),
        "threshold_m": threshold_m,
        **delta_payload(delta),
    }
