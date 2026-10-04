"""The step 1 station importer: five offline files in, build #N of the reference out.

It runs in the worker, as a `rebuild_jobs` row of kind `station_build`
(`app/worker.py::tick` branches on the kind before it looks up an engine).

Three stages, in this order:

  1. **Parse**, with no database: every file is read and checked first. A
     header that is not the declared shape, a repeated `(plc, era_uopid)`, a
     PLC that is not seven characters or a missing input refuses the build
     before a single row is written.
  2. **Extract**: the CRD locations and the ERA operational points go into
     the register tables, once per source version, and are committed. The
     delta between two versions is a set difference on these tables, so it is
     available whether or not the build that follows succeeds.
  3. **Build**, in one transaction: `station_ref` is upserted on its natural
     key, hand corrections are re-applied, and the derived children (codes,
     MERITS candidates, flags, aliases, links) are replaced. A build that
     fails is recorded and discarded. The transaction opens by taking the
     lock of `station_lock`, so no hand correction is set or released
     between what the build reads and what it writes.

`station_ref` rows are current state. A row present in an earlier build and
absent from this one is kept, untouched, and is recognisable by its
`last_built_build_id`: deleting it would delete its hand corrections with it.

What a build changed is written to `station_ref_history`: one row per column
of `station_ref` that moved, and one row per station whose codes, MERITS
candidates or flags are no longer the rows it had, under the field name
`codes`, `merits` or `flags`. Either makes the station "changed".

Parsing lives in `station_parse.py` and the correction rule in
`station_overrides.py`; both are pure. What is here is the plan (pure) and the
writes.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Insert, delete, insert, select, update
from sqlalchemy.orm import Session as DbSession

from .. import ingestion
from ..db import SessionLocal
from ..logging_config import one_line
from ..models import (
    CrdLocation,
    CrdSubsidiary,
    EraOperationalPoint,
    StationBuild,
    StationCodeSeries,
    StationRef,
    StationRefAlias,
    StationRefCode,
    StationRefFlag,
    StationRefHistory,
    StationRefLink,
    StationRefMerits,
    StationRefOverride,
    StationSource,
    StationSourceVersion,
)
from . import station_files as sf
from . import station_lock, station_store
from . import station_overrides as so
from . import station_parse as sp
from .station_files import StationFileError

log = logging.getLogger(__name__)

BUILDER_VERSION = "offline-import/1"
STATION_BUILD_KIND = "station_build"
_BUILD_LOG_DIR = "_builds"
_CHUNK = 5000
# Bulk statements touch rows no ORM object of this session mirrors: there is
# nothing to synchronise, and looking for it would cost a query per statement.
_NO_SYNC = {"synchronize_session": False}

# station_ref columns the build computes, beyond the ones the master carries.
_SPINE_FIELDS = ("plc_op_max_sep_m", "is_passenger_src", "crd_start", "crd_end")
# The link columns, so every row of a bulk insert has the same keys.
_LINK_COLUMNS = (
    "offline_station_id",
    "feed_key",
    "stop_key",
    "stop_name",
    "iso2",
    "lat",
    "lon",
    "label",
    "code_value",
    "code_series",
    "match_method",
    "tier",
    "asserted",
    "distance_m",
    "name_sim",
    "reason",
    "nearest_plc",
    "nearest_distance_m",
    "note",
)
# The children a build derives whole from its files and that the diff of the
# station_ref columns cannot see: the name a change of them is written under
# in station_ref_history, their table, and the columns the build writes.
_CHILD_SETS: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
    ("codes", StationRefCode, ("source_key", "code", "series", "is_primary", "evidence_only")),
    (
        "merits",
        StationRefMerits,
        ("code", "is_chosen", "origin", "rule", "confidence", "sources", "check_digit"),
    ),
    ("flags", StationRefFlag, ("token", "payload", "related_station_id")),
)

# One child row as the values of its columns, and those rows per station id.
ChildRow = tuple[Any, ...]
ChildSets = dict[int, set[ChildRow]]


# ───────────────────────────── the plan (pure) ─────────────────────────────


def field_text(value: Any) -> str | None:
    """How a field's value is written to `station_ref_history`."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "|".join(str(item) for item in value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def computed_fields(
    row: sp.MasterRow, extras: Mapping[str, Any] | None, today: date
) -> dict[str, Any]:
    """Every station_ref column the build computes for one master row.

    `extras` are the columns that are only in the CRD locations file, joined
    on (plc, operational point); a row the spine does not know gets none.
    """
    extras = extras or {}
    fields = dict(row.fields)
    for name in _SPINE_FIELDS:
        fields[name] = extras.get(name)
    if fields["n_op_with_plc"] is None:
        fields["n_op_with_plc"] = extras.get("n_op_with_plc")
    crd_end = fields["crd_end"]
    # Not a generated column: CURRENT_DATE is not immutable. Re-evaluated per build.
    fields["is_current"] = crd_end is None or crd_end >= today
    return fields


def diff_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
    """The fields of `new` whose value differs from `old`: (name, old, new)."""
    return [(name, old.get(name), value) for name, value in new.items() if old.get(name) != value]


def child_sets(rows: Iterable[Sequence[Any]]) -> ChildSets:
    """Rows of (station id, value, ...) as one set of value tuples per station.

    A list (the sources of a MERITS candidate) is read as a tuple, and an
    empty one as no value, so that a row as the database returns it and the
    same row as the build is about to write it compare equal.
    """
    out: ChildSets = {}
    for station_id, *values in rows:
        row = tuple((tuple(v) or None) if isinstance(v, list) else v for v in values)
        out.setdefault(station_id, set()).add(row)
    return out


def child_text(columns: Sequence[str], rows: Iterable[ChildRow]) -> str | None:
    """How child rows are written to `station_ref_history`.

    Per row, `column=value` for every column that has a value and the bare
    column name for a true flag; the rows in a stable order, joined by `; `.
    No row is no value.
    """
    lines = sorted(
        " ".join(
            name if value is True else f"{name}={field_text(value)}"
            for name, value in zip(columns, row, strict=True)
            if value is not None and value is not False and value != ""
        )
        for row in rows
    )
    return "; ".join(lines) or None


def child_changes(
    name: str,
    columns: Sequence[str],
    before: Mapping[int, set[ChildRow]],
    after: Mapping[int, set[ChildRow]],
    station_ids: Iterable[int],
    build_id: int,
) -> list[dict[str, Any]]:
    """One history row per station whose child rows are not the ones it had.

    `old_value` lists the rows that went and `new_value` the rows that came;
    a row with one value changed is in both. Only `station_ids` are compared:
    a station this build creates has no earlier state to differ from.
    """
    nothing: set[ChildRow] = set()
    history = []
    for station_id in station_ids:
        old, new = before.get(station_id, nothing), after.get(station_id, nothing)
        if old != new:
            history.append(
                {
                    "station_id": station_id,
                    "build_id": build_id,
                    "field_name": name,
                    "old_value": child_text(columns, old - new),
                    "new_value": child_text(columns, new - old),
                }
            )
    return history


def related_station(plc: str, by_plc: Mapping[str, list[tuple[str, int]]]) -> int | None:
    """The station a flag's payload names, when the payload is a PLC of the file.

    A PLC can carry several operational points. The one whose operational
    point id is the PLC itself is preferred; failing that, the first in
    byte order. Deterministic, so a rebuild does not shuffle the links.
    """
    candidates = by_plc.get(plc)
    if not candidates:
        return None
    for uopid, station_id in candidates:
        if uopid == plc:
            return station_id
    return min(candidates)[1]


def flag_targets(
    payload: str, by_plc: Mapping[str, list[tuple[str, int]]]
) -> list[tuple[str, int | None]]:
    """The (payload, related station) pairs one flag is written as.

    A payload usually names one thing. Some list several PLCs joined by `|`
    (`candidate_displaced_to:ZZ00002|ZZ00003`): when every part is a PLC of
    the file, the flag becomes one row per PLC, so that each station it names
    is linked. A flag row has one `related_station_id`, and its unique key
    (station, token, payload) allows the rows. Any other payload is kept
    whole, whatever it contains (`same_source_multiple_values:ZZ_RAIL=1|2`).
    """
    parts = sp.split_cell(payload, "|")
    if len(parts) > 1 and all(part in by_plc for part in parts):
        return [(part, related_station(part, by_plc)) for part in parts]
    return [(payload, related_station(payload, by_plc) if payload else None)]


def chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ───────────────────────────── inputs ─────────────────────────────


@dataclass(frozen=True)
class BuildInput:
    fmt: str
    source_key: str
    version_id: uuid.UUID
    sha256: str
    filename: str
    path: Path
    as_of: date | None

    def manifest(self) -> dict[str, Any]:
        return {
            "format": self.fmt,
            "version_id": str(self.version_id),
            "sha256": self.sha256,
            "filename": self.filename,
            "as_of": self.as_of.isoformat() if self.as_of else None,
        }


def _latest_version(db: DbSession, source: StationSource) -> StationSourceVersion | None:
    return db.execute(
        select(StationSourceVersion)
        .where(StationSourceVersion.source_id == source.id)
        .order_by(StationSourceVersion.acquired_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _input_for(db: DbSession, fmt: str, sources: list[StationSource]) -> BuildInput | str:
    """The file a build reads for one shape, or the reason there is none."""
    label = sf.FORMAT_LABELS[fmt]
    if not sources:
        return f"no enabled source declares the {label}"
    if len(sources) > 1:
        keys = ", ".join(sorted(s.key for s in sources))
        return f"several enabled sources declare the {label} ({keys}): disable all but one"
    source = sources[0]
    version = _latest_version(db, source)
    if version is None:
        return f"{source.key}: no file uploaded yet ({label})"
    path = Path(version.stored_path) if version.stored_path else None
    if path is None or not path.is_file():
        return f"{source.key}: the stored file of its latest version is gone"
    return BuildInput(
        fmt, source.key, version.id, version.sha256, version.filename, path, version.as_of
    )


def find_inputs(db: DbSession) -> tuple[dict[str, BuildInput], list[str]]:
    """The five files a build would read now, by shape, and what is in the way.

    A build needs all five: a partial import leaves screens silently empty.
    """
    sources = (
        db.execute(
            select(StationSource).where(
                StationSource.enabled, StationSource.format.in_(sf.REQUIRED_FORMATS)
            )
        )
        .scalars()
        .all()
    )
    inputs: dict[str, BuildInput] = {}
    problems: list[str] = []
    for fmt in sf.REQUIRED_FORMATS:
        found = _input_for(db, fmt, [s for s in sources if s.format == fmt])
        if isinstance(found, str):
            problems.append(found)
        else:
            inputs[fmt] = found
    return inputs, problems


def enqueue_build(db: DbSession, reason: str) -> bool:
    """Queue a station build for the worker. False when one is already pending:
    like graph rebuilds, station builds coalesce, on (status, session, kind)."""
    return ingestion._enqueue_rebuild(db, session_id=None, reason=reason, kind=STATION_BUILD_KIND)


# ───────────────────────────── parse stage ─────────────────────────────


@dataclass
class ParsedInputs:
    master: list[sp.MasterRow]
    links: list[sp.LinkRow]
    crd: sp.CrdParse
    telref: sp.TelrefParse


class BuildLog:
    """The lines a build writes: to the job row, and to a file beside the sources."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._started = time.monotonic()

    def add(self, message: str) -> None:
        self.lines.append(f"[{time.monotonic() - self._started:7.1f}s] {message}")
        log.info("station build: %s", one_line(message))

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def parse_inputs(inputs: Mapping[str, BuildInput], build_log: BuildLog) -> ParsedInputs:
    """Read and check all five files. Raises StationFileError, naming the file."""

    def rows(fmt: str) -> Iterator[dict[str, str]]:
        return sp.read_rows(inputs[fmt].path, fmt)

    def parse(fmt: str, parser: Any) -> Any:
        try:
            return parser(rows(fmt))
        except StationFileError as exc:
            raise StationFileError(
                f"{inputs[fmt].source_key} ({inputs[fmt].filename}): {exc}"
            ) from exc

    master = parse(sf.MASTER, sp.parse_master)
    build_log.add(f"master: {len(master)} rows")
    links = parse(sf.LINKS, lambda r: [sp.parse_link_row(row) for row in r])
    unmapped = parse(sf.UNMAPPED, lambda r: [sp.parse_unmapped_row(row) for row in r])
    build_log.add(f"links: {len(links)} matched rows, {len(unmapped)} unmatched stops")
    crd = parse(sf.CRD_LOCATIONS, sp.parse_crd_locations)
    build_log.add(f"CRD locations: {crd.rows} rows, {len(crd.locations)} CRD locations")
    telref = parse(sf.ERA_TELREF, sp.parse_telref)
    build_log.add(f"ERA telref: {telref.rows} rows, {len(telref.points)} operational points")
    return ParsedInputs(master=master, links=[*links, *unmapped], crd=crd, telref=telref)


# ───────────────────────────── extract stage ─────────────────────────────


def _bulk_insert(model: Any) -> Insert:
    """The INSERT every bulk write of the importer goes through.

    Left to itself, an ORM bulk INSERT leaves a None-valued key out of the
    statement, so that a server default can apply, and then batches only the
    consecutive rows that have the same keys left. On station rows, where the
    empty cells fall differently from one row to the next, that is close to
    one statement per row. `render_nulls` writes the NULL instead: rows that
    carry the same keys go out as one executemany per chunk.

    It asks two things of the rows, and every list the importer builds meets
    both: every row of a batch carries the same keys, None included; and a
    column with a server default is either given a value on every row or left
    out of every row, since a None would now be written as NULL.
    """
    return insert(model).execution_options(render_nulls=True)


def _insert(db: DbSession, model: Any, rows: Sequence[dict[str, Any]]) -> None:
    for chunk in chunks(rows):
        db.execute(_bulk_insert(model), list(chunk))


def _has_rows(db: DbSession, model: Any, version_id: uuid.UUID) -> bool:
    return (
        db.execute(select(model.id).where(model.source_version_id == version_id).limit(1)).first()
        is not None
    )


def _mark_imported(db: DbSession, version_id: uuid.UUID, stats: dict[str, Any]) -> None:
    version = db.get(StationSourceVersion, version_id)
    if version is not None:
        version.status = "imported"
        version.error = None
        version.stats = {**(version.stats or {}), **stats}


def load_registers(
    db: DbSession, inputs: Mapping[str, BuildInput], parsed: ParsedInputs
) -> dict[str, int]:
    """Write the register rows of each source version, once per version."""
    crd_version = inputs[sf.CRD_LOCATIONS].version_id
    if not _has_rows(db, CrdLocation, crd_version):
        tag = {"source_version_id": crd_version}
        _insert(db, CrdLocation, [{**row, **tag} for row in parsed.crd.locations])
        _insert(db, CrdSubsidiary, [{**row, **tag} for row in parsed.crd.subsidiaries])
    crd_stats = {
        "rows": parsed.crd.rows,
        "crd_locations": len(parsed.crd.locations),
        "crd_subsidiaries": len(parsed.crd.subsidiaries),
        "duplicates_dropped": parsed.crd.duplicates_dropped,
        "dates_unparsed": parsed.crd.dates_unparsed,
    }
    _mark_imported(db, crd_version, crd_stats)

    era_version = inputs[sf.ERA_TELREF].version_id
    if not _has_rows(db, EraOperationalPoint, era_version):
        _insert(
            db,
            EraOperationalPoint,
            [{**row, "source_version_id": era_version} for row in parsed.telref.points],
        )
    era_stats = {
        "rows": parsed.telref.rows,
        "era_operational_points": len(parsed.telref.points),
        "duplicates_dropped": parsed.telref.duplicates_dropped,
        "rows_without_plc": parsed.telref.rows_without_plc,
    }
    _mark_imported(db, era_version, era_stats)
    return {
        "crd_locations": crd_stats["crd_locations"],
        "crd_subsidiaries": crd_stats["crd_subsidiaries"],
        "era_operational_points": era_stats["era_operational_points"],
        "crd_dates_unparsed": crd_stats["dates_unparsed"],
    }


# ───────────────────────────── build stage ─────────────────────────────


@dataclass
class _Plan:
    """What the build stage will write, assembled before any write."""

    new_rows: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    # Per station key: the MERITS candidates after any correction.
    merits: dict[sp.Key, list[dict[str, Any]]] = field(default_factory=dict)
    present: list[sp.Key] = field(default_factory=list)
    unchanged: int = 0
    without_spine: int = 0
    overrides: Counter[str] = field(default_factory=Counter)
    fields_changed: Counter[str] = field(default_factory=Counter)


def _built_columns() -> list[str]:
    template = sp.master_fields(dict.fromkeys(sf.FILE_SHAPES[sf.MASTER], ""))
    return [*template, *_SPINE_FIELDS, "is_current"]


def _load_existing(db: DbSession, columns: Sequence[str]) -> dict[sp.Key, dict[str, Any]]:
    rows = db.execute(
        select(
            StationRef.id,
            StationRef.plc,
            StationRef.era_uopid,
            *[getattr(StationRef, name) for name in columns],
        )
    ).all()
    return {
        (row[1], row[2]): {"id": row[0], **dict(zip(columns, row[3:], strict=True))} for row in rows
    }


def _load_overrides(db: DbSession) -> dict[int, list[StationRefOverride]]:
    out: dict[int, list[StationRefOverride]] = {}
    active = db.execute(
        select(StationRefOverride).where(StationRefOverride.released_at.is_(None))
    ).scalars()
    for override in active:
        out.setdefault(override.station_id, []).append(override)
    return out


def _candidate_dicts(row: sp.MasterRow) -> list[dict[str, Any]]:
    return [
        {
            "code": c.code,
            "origin": c.origin,
            "rule": c.rule,
            "confidence": c.confidence,
            "sources": list(c.sources) or None,
            "check_digit": c.check_digit,
            "is_chosen": c.is_chosen,
        }
        for c in row.merits
    ]


def _apply_corrections(
    computed: dict[str, Any],
    candidates: list[dict[str, Any]],
    overrides: list[StationRefOverride],
    plan: _Plan,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Re-apply a station's hand corrections; returns (effective fields, candidates)."""
    active = [so.ActiveOverride(o.field_name, o.value, o.computed_value_at_set) for o in overrides]
    effective, outcomes = so.apply_overrides(computed, active)
    for override, outcome in zip(overrides, outcomes, strict=True):
        if not outcome.applied:
            raise StationFileError(
                f"the correction of {override.field_name!r} on station {override.station_id} "
                "can no longer be applied; release it before rebuilding"
            )
        override.computed_value_latest = outcome.computed
        plan.overrides["applied"] += 1
        plan.overrides["drifted"] += outcome.drifted
        plan.overrides["redundant"] += outcome.redundant
        if override.field_name == so.MERITS_FIELD:
            candidates = so.merits_with_override(candidates, override.value, override.reason)
            effective.update(so.merits_mirror(candidates))
    return effective, candidates


def plan_reference(
    parsed: ParsedInputs,
    existing: Mapping[sp.Key, Mapping[str, Any]],
    overrides: Mapping[int, list[StationRefOverride]],
    build_id: int,
    today: date,
) -> _Plan:
    """Decide, row by row, what the build writes. No database access."""
    plan = _Plan()
    for row in parsed.master:
        extras = parsed.crd.extras.get(row.key)
        plan.without_spine += extras is None
        effective = computed_fields(row, extras, today)
        candidates = _candidate_dicts(row)
        current = existing.get(row.key)
        if current is not None and current["id"] in overrides:
            effective, candidates = _apply_corrections(
                effective, candidates, overrides[current["id"]], plan
            )
        plan.present.append(row.key)
        plan.merits[row.key] = candidates
        if current is None:
            plan.new_rows.append(
                {
                    "plc": row.plc,
                    "era_uopid": row.era_uopid,
                    **effective,
                    "first_seen_build_id": build_id,
                    "last_built_build_id": build_id,
                    "last_changed_build_id": build_id,
                }
            )
            continue
        changes = diff_fields(current, effective)
        if not changes:
            plan.unchanged += 1
            continue
        plan.updates.append({"id": current["id"], **effective, "last_changed_build_id": build_id})
        for name, old, new in changes:
            plan.fields_changed[name] += 1
            plan.history.append(
                {
                    "station_id": current["id"],
                    "build_id": build_id,
                    "field_name": name,
                    "old_value": field_text(old),
                    "new_value": field_text(new),
                }
            )
    return plan


def _write_stations(db: DbSession, plan: _Plan) -> dict[sp.Key, int]:
    """Insert the new rows, update the changed ones; returns key -> id for every
    row of this build."""
    ids: dict[sp.Key, int] = {}
    for chunk in chunks(plan.new_rows):
        returned = db.execute(
            _bulk_insert(StationRef).returning(StationRef.id, StationRef.plc, StationRef.era_uopid),
            list(chunk),
        )
        ids.update({(r[1], r[2]): r[0] for r in returned})
    for chunk in chunks(plan.updates):
        db.execute(update(StationRef), list(chunk))
    return ids


def _load_children(db: DbSession) -> dict[str, ChildSets]:
    """Every station's codes, MERITS candidates and flags as the last build
    left them: one query per table, so that the build can compare them in
    memory with the ones it writes. Read before they are replaced."""
    return {
        name: child_sets(
            db.execute(
                select(model.station_id, *[getattr(model, column) for column in columns])
            ).all()
        )
        for name, model, columns in _CHILD_SETS
    }


def _child_history(
    before: Mapping[str, ChildSets],
    written: Mapping[str, Sequence[Mapping[str, Any]]],
    station_ids: Sequence[int],
    build_id: int,
) -> list[dict[str, Any]]:
    """The history rows of the stations of `station_ids` whose codes, MERITS
    candidates or flags, as this build writes them, are not the ones in
    `before`."""
    history: list[dict[str, Any]] = []
    for name, _model, columns in _CHILD_SETS:
        after = child_sets(
            [row["station_id"], *(row[column] for column in columns)] for row in written[name]
        )
        history += child_changes(name, columns, before[name], after, station_ids, build_id)
    return history


def _mark_changed(db: DbSession, station_ids: Sequence[int], build_id: int) -> None:
    """Set `last_changed_build_id` on the stations only their children changed
    on: no column of theirs moved, so the update of the changed rows, which
    sets it for the others, left them alone."""
    for chunk in chunks(station_ids):
        db.execute(
            update(StationRef),
            [{"id": station_id, "last_changed_build_id": build_id} for station_id in chunk],
        )


def _replace_children(db: DbSession, station_ids: Sequence[int]) -> None:
    """Codes, MERITS candidates and flags are derived whole from the master:
    drop them for every station of this build before they are written again."""
    for chunk in chunks(station_ids):
        for model in (StationRefCode, StationRefMerits, StationRefFlag):
            db.execute(
                delete(model).where(model.station_id.in_(chunk)).execution_options(**_NO_SYNC)
            )


def _code_rows(
    db: DbSession, parsed: ParsedInputs, ids: Mapping[sp.Key, int], counts: dict[str, int]
) -> list[dict[str, Any]]:
    series_index = sp.link_series_index(parsed.links)
    unresolved = set(
        db.execute(select(StationSource.key).where(StationSource.source_key_unresolved)).scalars()
    )
    rows: list[dict[str, Any]] = []
    for master in parsed.master:
        by_code = series_index.get(master.key)
        for code in master.codes:
            rows.append(
                {
                    "station_id": ids[master.key],
                    "source_key": code.source_key,
                    "series": sp.series_for(code.code, by_code),
                    "code": code.code,
                    "is_primary": code.is_primary,
                    # A code of an aggregate column is `feed#key`: evidence, not a join key.
                    "evidence_only": code.source_key in unresolved,
                }
            )
    used = {row["series"] for row in rows if row["series"]}
    known = set(db.execute(select(StationCodeSeries.key)).scalars())
    added = sorted(used - known)
    # The vocabulary is data: a series the links file introduces is an INSERT.
    _insert(db, StationCodeSeries, [{"key": key, "label": key} for key in added])
    counts["codes_with_series"] = sum(1 for row in rows if row["series"])
    counts["series_added"] = len(added)
    return rows


def _flag_rows(parsed: ParsedInputs, ids: Mapping[sp.Key, int]) -> list[dict[str, Any]]:
    by_plc: dict[str, list[tuple[str, int]]] = {}
    for (plc, uopid), station_id in ids.items():
        by_plc.setdefault(plc, []).append((uopid, station_id))
    rows: list[dict[str, Any]] = []
    for master in parsed.master:
        # A dict: a PLC named by a list and again on its own is one row, which
        # is what the unique key (station, token, payload) requires.
        written = dict.fromkeys(
            (token, part, related)
            for token, payload in master.flags
            for part, related in flag_targets(payload, by_plc)
        )
        rows.extend(
            {
                "station_id": ids[master.key],
                "token": token,
                "payload": part,
                "related_station_id": related,
            }
            for token, part, related in written
        )
    return rows


def _alias_rows(
    parsed: ParsedInputs, ids: Mapping[sp.Key, int], build_id: int, counts: dict[str, int]
) -> list[dict[str, Any]]:
    """One alias per old PLC, so an old PLC resolves in one lookup. Where
    several rows name the same old PLC, the first in key order wins."""
    rows: dict[str, dict[str, Any]] = {}
    collisions = 0
    for master in sorted(parsed.master, key=lambda m: m.key):
        old = master.fields["previous_plc"]
        if not old:
            continue
        if old in rows:
            collisions += 1
            continue
        rows[old] = {
            "station_id": ids[master.key],
            "alias_plc": old,
            "reason": "previous_plc",
            "build_id": build_id,
        }
    counts["alias_collisions"] = collisions
    return list(rows.values())


def _link_rows(
    parsed: ParsedInputs,
    inputs: Mapping[str, BuildInput],
    ids: Mapping[sp.Key, int],
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    rows = []
    orphaned = 0
    for link in parsed.links:
        station_id = None
        if link.key is not None:
            station_id = ids.get(link.key)
            if station_id is None:
                # Names a reference row the master does not have: the files are
                # not from the same issue. Counted, not stored as "unmatched".
                orphaned += 1
                continue
        source = sf.UNMAPPED if link.key is None else sf.LINKS
        row: dict[str, Any] = dict.fromkeys(_LINK_COLUMNS)
        row.update(link.fields)
        row["station_id"] = station_id
        row["source_version_id"] = inputs[source].version_id
        rows.append(row)
    counts["links_orphaned"] = orphaned
    counts["links_matched"] = sum(1 for row in rows if row["station_id"] is not None)
    counts["links_unmatched"] = len(rows) - counts["links_matched"]
    return rows


def write_reference(
    db: DbSession,
    build_id: int,
    inputs: Mapping[str, BuildInput],
    parsed: ParsedInputs,
    today: date,
) -> tuple[dict[str, int], dict[str, Any]]:
    """The build stage. The caller owns the transaction.

    The lock comes first, and is held until that transaction ends: everything
    below is planned from one reading of the reference and of the active
    corrections, so no correction may be set or released between that reading
    and the commit. See `station_lock`.
    """
    station_lock.hold_for_build(db)
    columns = _built_columns()
    existing = _load_existing(db, columns)
    plan = plan_reference(parsed, existing, _load_overrides(db), build_id, today)

    present = set(plan.present)
    ids = {key: row["id"] for key, row in existing.items() if key in present}
    # The stations an earlier build wrote, and their children as it left them.
    known = sorted(ids.values())
    children_before = _load_children(db)
    ids.update(_write_stations(db, plan))
    station_ids = sorted(ids.values())
    _replace_children(db, station_ids)
    for chunk in chunks(station_ids):
        db.execute(
            update(StationRef)
            .where(StationRef.id.in_(chunk))
            .values(last_built_build_id=build_id)
            .execution_options(**_NO_SYNC)
        )

    counts: dict[str, int] = {}
    codes = _code_rows(db, parsed, ids, counts)
    merits = [
        {"station_id": ids[key], **candidate}
        for key, candidates in plan.merits.items()
        for candidate in candidates
    ]
    flags = _flag_rows(parsed, ids)
    aliases = _alias_rows(parsed, ids, build_id, counts)
    links = _link_rows(parsed, inputs, ids, counts)

    # The diff of the station_ref columns does not see the children: a station
    # whose codes, candidates or flags alone changed is a changed station too.
    child_history = _child_history(
        children_before, {"codes": codes, "merits": merits, "flags": flags}, known, build_id
    )
    children_only = sorted(
        {row["station_id"] for row in child_history} - {row["id"] for row in plan.updates}
    )
    _mark_changed(db, children_only, build_id)

    # Links and aliases are the whole set of this build: nothing of an earlier
    # one stays, so an old PLC resolves to one station, in one lookup.
    db.execute(delete(StationRefLink).execution_options(**_NO_SYNC))
    db.execute(delete(StationRefAlias).execution_options(**_NO_SYNC))
    for model, rows in (
        (StationRefCode, codes),
        (StationRefMerits, merits),
        (StationRefFlag, flags),
        (StationRefAlias, aliases),
        (StationRefLink, links),
        (StationRefHistory, [*plan.history, *child_history]),
    ):
        _insert(db, model, rows)
    for fmt in (sf.MASTER, sf.LINKS, sf.UNMAPPED):
        _mark_imported(db, inputs[fmt].version_id, {})

    absent = len(existing) - len(ids) + len(plan.new_rows)
    counts.update(
        station_ref=len(ids),
        station_ref_code=len(codes),
        station_ref_merits=len(merits),
        station_ref_flag=len(flags),
        flag_tokens=len({row["token"] for row in flags}),
        station_ref_alias=len(aliases),
        station_ref_link=len(links),
        master_rows_without_spine=plan.without_spine,
        overrides_applied=plan.overrides["applied"],
        overrides_drifted=plan.overrides["drifted"],
        overrides_redundant=plan.overrides["redundant"],
    )
    fields_changed = plan.fields_changed + Counter(row["field_name"] for row in child_history)
    diff_summary: dict[str, Any] = {
        "created": len(plan.new_rows),
        "changed": len(plan.updates) + len(children_only),
        "unchanged": plan.unchanged - len(children_only),
        "absent": absent,
        "fields_changed": dict(fields_changed.most_common()),
    }
    return counts, diff_summary


# ───────────────────────────── the job ─────────────────────────────


def _open_build(inputs: Mapping[str, BuildInput]) -> int:
    with SessionLocal() as db:
        build = StationBuild(
            status="running",
            builder_version=BUILDER_VERSION,
            inputs={i.source_key: i.manifest() for i in inputs.values()},
        )
        db.add(build)
        db.commit()
        return build.id


def _write_log_file(build_id: int, build_log: BuildLog) -> str | None:
    """Best effort: the log is also on the job row, so a full disk must not
    turn a finished build into a failed one."""
    try:
        folder = station_store.store_root() / _BUILD_LOG_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"build-{build_id}.log"
        path.write_text(build_log.text(), encoding="utf-8")
    except OSError:
        log.exception("station build %s: could not write its log file", build_id)
        return None
    return str(path)


def _close_build(
    build_id: int,
    build_log: BuildLog,
    *,
    status: str,
    counts: dict[str, Any],
    diff_summary: dict[str, Any] | None = None,
) -> None:
    log_path = _write_log_file(build_id, build_log)
    with SessionLocal() as db:
        build = db.get(StationBuild, build_id)
        if build is None:  # pragma: no cover  defensive
            return
        build.status = status
        build.finished_at = datetime.now(UTC)
        build.counts = counts
        build.diff_summary = diff_summary
        build.log_path = log_path
        db.commit()


def run_build(today: date | None = None) -> tuple[str, bool]:
    """Run one station build. Returns (log, success); never raises.

    A refusal (a missing input, a file of the wrong shape, a repeated key) and
    a crash both leave the reference exactly as it was.
    """
    build_log = BuildLog()
    with SessionLocal() as db:
        inputs, problems = find_inputs(db)
    build_id = _open_build(inputs)
    build_log.add(f"build #{build_id} ({BUILDER_VERSION})")
    if problems:
        return _refused(build_id, build_log, "inputs missing: " + "; ".join(problems))
    try:
        parsed = parse_inputs(inputs, build_log)
        with SessionLocal() as db:
            register_counts = load_registers(db, inputs, parsed)
            db.commit()
        build_log.add("registers loaded")
        with SessionLocal() as db:
            counts, diff_summary = write_reference(
                db, build_id, inputs, parsed, today or datetime.now(UTC).date()
            )
            db.commit()
    except StationFileError as exc:
        return _refused(build_id, build_log, str(exc))
    except Exception as exc:
        log.exception("station build %s crashed", build_id)
        return _refused(
            build_id, build_log, f"internal error ({type(exc).__name__}): see the worker log"
        )
    counts.update(register_counts)
    build_log.add(
        f"done: {diff_summary['created']} created, {diff_summary['changed']} changed, "
        f"{diff_summary['unchanged']} unchanged, {diff_summary['absent']} absent"
    )
    _close_build(build_id, build_log, status="done", counts=counts, diff_summary=diff_summary)
    return build_log.text(), True


def _refused(build_id: int, build_log: BuildLog, reason: str) -> tuple[str, bool]:
    build_log.add(f"refused: {reason}")
    _close_build(build_id, build_log, status="failed", counts={"error": reason})
    return build_log.text(), False


def mark_orphaned_builds() -> int:
    """Builds left `running` by a worker that died: nothing will finish them."""
    with SessionLocal() as db:
        orphans = list(
            db.execute(select(StationBuild).where(StationBuild.status == "running")).scalars()
        )
        for build in orphans:
            build.status = "failed"
            build.finished_at = datetime.now(UTC)
            build.counts = {"error": "the worker restarted while this build was running"}
        db.commit()
        return len(orphans)
