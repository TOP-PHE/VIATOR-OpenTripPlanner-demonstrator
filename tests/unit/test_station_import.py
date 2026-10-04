"""The station importer's plan, the correction rule, and the worker's `kind` branch.

The writes run against Postgres in tests/integration/test_station_panel.py.
Here: what the build decides to write (`plan_reference`, no database), how a
hand correction survives a rebuild, which inputs a build needs, and that a
`station_build` job never reaches the graph builders. The build stage itself
runs here too, against an in-memory stand-in for the station tables, and its
bulk inserts against SQLite: what a rebuild leaves behind, and in how many
statements, is checked without Postgres.

Every row is invented; the PLC prefix `ZZ` does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app import ingestion, worker
from app.master import station_files as sf
from app.master import station_import as si
from app.master import station_overrides as so
from app.master import station_parse as sp
from tests.station_fixtures import (
    crd_location_rows,
    link_rows,
    master_rows,
    telref_rows,
    unmapped_rows,
    write_station_files,
)

TODAY = date(2026, 10, 3)
BUILD = 7


def full(fmt: str, row: dict[str, str]) -> dict[str, str]:
    return {column: row.get(column, "") for column in sf.FILE_SHAPES[fmt]}


def parsed(master: list[dict[str, str]] | None = None) -> si.ParsedInputs:
    links = [sp.parse_link_row(full(sf.LINKS, row)) for row in link_rows()]
    unmapped = [sp.parse_unmapped_row(full(sf.UNMAPPED, row)) for row in unmapped_rows()]
    return si.ParsedInputs(
        master=sp.parse_master(full(sf.MASTER, row) for row in master or master_rows()),
        links=[*links, *unmapped],
        crd=sp.parse_crd_locations(full(sf.CRD_LOCATIONS, row) for row in crd_location_rows()),
        telref=sp.parse_telref(full(sf.ERA_TELREF, row) for row in telref_rows()),
    )


def as_existing(plan: si._Plan) -> dict[sp.Key, dict[str, Any]]:
    """What the database holds after `plan` was written as a first build."""
    out = {}
    for station_id, row in enumerate(plan.new_rows, start=1):
        fields = {k: v for k, v in row.items() if not k.endswith("_build_id")}
        key = (fields.pop("plc"), fields.pop("era_uopid"))
        out[key] = {"id": station_id, **fields}
    return out


def override(station_id: int, field: str, value: str | None, at_set: str | None, **kw: Any) -> Any:
    return SimpleNamespace(
        station_id=station_id,
        field_name=field,
        value=value,
        computed_value_at_set=at_set,
        computed_value_latest=at_set,
        reason=kw.get("reason", "checked on site"),
    )


# ── small pure helpers ─────────────────────────────────────────────────


def test_field_text() -> None:
    assert si.field_text(None) is None
    assert si.field_text(True) == "true"
    assert si.field_text(False) == "false"
    assert si.field_text(["ZZ", "YY"]) == "ZZ|YY"
    assert si.field_text(date(2021, 6, 30)) == "2021-06-30"
    assert si.field_text(50.5) == "50.5"
    assert si.field_text("Sampleton") == "Sampleton"


def test_diff_fields_reports_only_what_differs() -> None:
    old = {"name": "Sampleton", "lat": 50.1, "iso2_all": ["ZZ"]}
    new = {"name": "Sampleton Hbf", "lat": 50.1, "iso2_all": ["ZZ"]}
    assert si.diff_fields(old, new) == [("name", "Sampleton", "Sampleton Hbf")]
    assert si.diff_fields(new, new) == []


def test_chunks() -> None:
    assert [list(c) for c in si.chunks([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    assert list(si.chunks([], 2)) == []


def test_a_flag_payload_resolves_to_one_station_deterministically() -> None:
    by_plc = {
        "ZZ00001": [("ZZ00001", 11)],
        # Several operational points: the one named like the PLC is preferred...
        "ZZ00002": [("ZZOP02B", 22), ("ZZ00002", 21), ("ZZOP02A", 23)],
        # ...and failing that, the first in byte order.
        "ZZ00003": [("ZZOP03B", 32), ("ZZOP03A", 31)],
    }
    assert si.related_station("ZZ00001", by_plc) == 11
    assert si.related_station("ZZ00002", by_plc) == 21
    assert si.related_station("ZZ00003", by_plc) == 31
    assert si.related_station("ZZ00009", by_plc) is None  # not a PLC of this file
    assert si.related_station("with:colons", by_plc) is None


def test_a_flag_payload_listing_plcs_becomes_one_target_per_plc() -> None:
    by_plc = {
        "ZZ00001": [("ZZ00001", 11)],
        "ZZ00002": [("ZZ00002", 21)],
        "ZZ00003": [("ZZOP03B", 32), ("ZZOP03A", 31)],
    }
    # Every part is a PLC of the file: each station it names is linked.
    assert si.flag_targets("ZZ00002|ZZ00003", by_plc) == [("ZZ00002", 21), ("ZZ00003", 31)]
    assert si.flag_targets("ZZ00003|ZZ00001|ZZ00003", by_plc) == [("ZZ00003", 31), ("ZZ00001", 11)]
    # One PLC, as before.
    assert si.flag_targets("ZZ00002", by_plc) == [("ZZ00002", 21)]
    # Anything else is kept whole: a part that is not a PLC of the file, a
    # payload whose pipe separates something other than PLCs, no payload.
    assert si.flag_targets("ZZ00002|ZZ00099", by_plc) == [("ZZ00002|ZZ00099", None)]
    assert si.flag_targets("ZZ_RAIL=9900001|9900002", by_plc) == [("ZZ_RAIL=9900001|9900002", None)]
    assert si.flag_targets("with:colons", by_plc) == [("with:colons", None)]
    assert si.flag_targets("", by_plc) == [("", None)]


# ── computed fields ────────────────────────────────────────────────────


def test_computed_fields_join_the_spine_and_decide_is_current() -> None:
    data = parsed()
    rows = {row.key: row for row in data.master}

    yard = si.computed_fields(
        rows[("ZZ00003", "ZZOP03B")], data.crd.extras[("ZZ00003", "ZZOP03B")], TODAY
    )
    assert yard["plc_op_max_sep_m"] == 140
    assert yard["is_passenger_src"] == "CRD_derived"
    assert yard["crd_end"] == date(2021, 6, 30)
    assert yard["is_current"] is False  # retired: crd_end is past

    central = si.computed_fields(
        rows[("ZZ00001", "ZZ00001")], data.crd.extras[("ZZ00001", "ZZ00001")], TODAY
    )
    assert central["crd_start"] == date(2019, 12, 15)
    assert central["crd_end"] is None
    assert central["is_current"] is True


def test_is_current_is_evaluated_against_the_build_date() -> None:
    data = parsed()
    row = next(r for r in data.master if r.key == ("ZZ00003", "ZZOP03A"))
    extras = data.crd.extras[row.key]
    assert si.computed_fields(row, extras, date(2021, 6, 30))["is_current"] is True  # last day
    assert si.computed_fields(row, extras, date(2021, 7, 1))["is_current"] is False


def test_a_row_the_spine_does_not_know_gets_no_extras() -> None:
    row = parsed().master[0]
    fields = si.computed_fields(row, None, TODAY)
    assert fields["plc_op_max_sep_m"] is None
    assert fields["crd_start"] is None
    assert fields["is_current"] is True
    assert fields["n_op_with_plc"] == 1  # from the master itself


def test_built_columns_are_all_station_ref_columns() -> None:
    from app.models import StationRef

    columns = si._built_columns()
    assert len(columns) == len(set(columns))
    for name in columns:
        assert hasattr(StationRef, name), name
    # The build never touches identity, grouping or lineage.
    assert not {"id", "plc", "era_uopid", "complex_id", "complex_role"} & set(columns)
    assert set(si.computed_fields(parsed().master[0], None, TODAY)) == set(columns)


# ── the plan: build #1 and what a rebuild changes ──────────────────────


def test_a_first_build_creates_every_row() -> None:
    plan = si.plan_reference(parsed(), {}, {}, BUILD, TODAY)
    assert len(plan.new_rows) == 5
    assert plan.updates == []
    assert plan.history == []
    assert plan.unchanged == 0
    assert plan.without_spine == 0
    first = plan.new_rows[0]
    assert (first["plc"], first["era_uopid"]) == ("ZZ00001", "ZZ00001")
    assert first["first_seen_build_id"] == BUILD
    assert first["last_built_build_id"] == BUILD
    # Every row of a bulk insert has the same keys.
    assert len({tuple(sorted(row)) for row in plan.new_rows}) == 1


def test_rebuilding_from_the_same_files_changes_nothing() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    plan = si.plan_reference(parsed(), existing, {}, BUILD + 1, TODAY)
    assert plan.new_rows == []
    assert plan.updates == []
    assert plan.history == []
    assert plan.unchanged == 5


def test_a_changed_field_is_updated_and_written_to_history() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf", "lat": "50.123"}
    plan = si.plan_reference(parsed(rows), existing, {}, BUILD + 1, TODAY)

    assert plan.unchanged == 4
    (update,) = plan.updates
    station_id = existing[("ZZ00002", "ZZ00002")]["id"]
    assert update["id"] == station_id
    assert update["name"] == "Sampleton Hbf"
    assert update["last_changed_build_id"] == BUILD + 1
    assert "first_seen_build_id" not in update
    assert sorted((h["field_name"], h["old_value"], h["new_value"]) for h in plan.history) == [
        ("lat", "50.1", "50.123"),
        ("name", "Sampleton", "Sampleton Hbf"),
    ]
    assert all(h["station_id"] == station_id and h["build_id"] == BUILD + 1 for h in plan.history)
    assert dict(plan.fields_changed) == {"name": 1, "lat": 1}


def test_a_row_the_spine_lacks_is_counted() -> None:
    data = parsed()
    del data.crd.extras[("ZZ00004", "ZZ00004")]
    assert si.plan_reference(data, {}, {}, BUILD, TODAY).without_spine == 1


# ── override re-application ────────────────────────────────────────────


def test_apply_overrides_keeps_the_correction_and_remembers_the_computed_value() -> None:
    computed = {"name": "Sampleton", "lat": 50.1, "is_passenger": True}
    effective, outcomes = so.apply_overrides(
        computed,
        [
            so.ActiveOverride("name", "Sampleton Central", "Sampleton"),
            so.ActiveOverride("lat", "50.2", "49.9"),
            so.ActiveOverride("is_passenger", "true", "false"),
        ],
    )
    assert effective == {"name": "Sampleton Central", "lat": 50.2, "is_passenger": True}
    name, lat, passenger = outcomes
    assert (name.applied, name.computed, name.drifted, name.redundant) == (
        True,
        "Sampleton",
        False,
        False,
    )
    # The build's own value moved since the correction was made.
    assert (lat.computed, lat.drifted) == ("50.1", True)
    # The build now computes what the correction says: the correction is moot.
    assert (passenger.computed, passenger.redundant) == ("true", True)


def test_a_correction_to_no_value_blanks_the_field() -> None:
    effective, (outcome,) = so.apply_overrides(
        {"rl100": "ZEXC"}, [so.ActiveOverride("rl100", None, "ZEXC")]
    )
    assert effective == {"rl100": None}
    assert outcome.applied


def test_a_correction_that_can_no_longer_be_cast_is_reported_not_applied() -> None:
    effective, outcomes = so.apply_overrides(
        {"lat": 50.1, "plc": "ZZ00001"},
        [
            so.ActiveOverride("lat", "north", "50.1"),
            so.ActiveOverride("plc", "ZZ00002", "ZZ00001"),  # identity is not correctable
        ],
    )
    assert effective == {"lat": 50.1, "plc": "ZZ00001"}
    assert [o.applied for o in outcomes] == [False, False]


@pytest.mark.parametrize(
    ("field", "text", "value"),
    [
        ("name", "Sampleton", "Sampleton"),
        ("lat", "50.5", 50.5),
        ("lon", "-4.25", -4.25),
        ("iso2", "zz", "ZZ"),
        ("is_passenger", "no", False),
        ("is_passenger", "TRUE", True),
        ("uic_merits", "9900002", "9900002"),
        ("eva", "", None),
        ("name", None, None),
    ],
)
def test_from_text_casts_to_the_column_type(field: str, text: str | None, value: Any) -> None:
    assert so.from_text(field, text) == value


@pytest.mark.parametrize(
    ("field", "text", "fragment"),
    [
        ("lat", "95", "lat: must be between -90 and 90"),
        ("lon", "200", "lon: must be between -180 and 180"),
        ("lat", "north", "lat:"),
        ("iso2", "ZZZ", "iso2: a country is two letters"),
        ("is_passenger", "maybe", "is_passenger: must be true or false"),
        ("plc", "ZZ00002", "cannot be corrected by hand"),
        ("complex_id", "3", "cannot be corrected by hand"),
    ],
)
def test_from_text_refuses_what_does_not_fit(field: str, text: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        so.from_text(field, text)


def test_to_text_round_trips_through_from_text() -> None:
    for field, value in (("lat", 50.123456), ("is_passenger", False), ("name", "Sampleton")):
        assert so.from_text(field, so.to_text(value)) == value
    assert so.to_text(None) is None


def test_a_rebuild_reapplies_a_correction_and_lets_other_fields_improve() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    key = ("ZZ00002", "ZZ00002")
    station_id = existing[key]["id"]
    # A content manager corrected the name by hand after build #1.
    existing[key]["name"] = "Sampleton Central"
    correction = override(station_id, "name", "Sampleton Central", "Sampleton")

    # The next issue of the master renames the station AND moves it.
    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf", "lat": "50.123"}
    plan = si.plan_reference(parsed(rows), existing, {station_id: [correction]}, BUILD + 1, TODAY)

    (update,) = plan.updates
    assert update["name"] == "Sampleton Central"  # the correction holds
    assert update["lat"] == 50.123  # the improvement of another field goes through
    assert [h["field_name"] for h in plan.history] == ["lat"]  # the name did not change
    # What a release would restore is what the build computes NOW.
    assert correction.computed_value_latest == "Sampleton Hbf"
    assert dict(plan.overrides) == {"applied": 1, "drifted": 1, "redundant": 0}


def test_a_merits_correction_changes_the_chosen_candidate_not_the_candidates() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    key = ("ZZ00002", "ZZ00002")
    station_id = existing[key]["id"]
    correction = override(station_id, "uic_merits", "9900777", "9900002", reason="per the operator")
    plan = si.plan_reference(parsed(), existing, {station_id: [correction]}, BUILD + 1, TODAY)

    candidates = {c["code"]: c for c in plan.merits[key]}
    # Nothing computed is withdrawn; a Manual candidate is added and chosen.
    assert list(candidates) == ["9900002", "9900012", "9900022", "9900032", "9900777"]
    assert [c["is_chosen"] for c in candidates.values()] == [False, False, False, False, True]
    assert candidates["9900777"]["origin"] == so.ORIGIN_MANUAL
    assert candidates["9900777"]["rule"] == "per the operator"

    (update,) = plan.updates
    assert update["uic_merits"] == "9900777"
    assert update["uic_merits_origin"] == so.ORIGIN_MANUAL
    assert update["uic_merits_confidence"] is None
    # The rule stays the build's own: the reason is on the Manual candidate.
    assert update["uic_merits_rule"] == "Trainline preferred over the calculation"
    assert correction.computed_value_latest == "9900002"


def test_a_merits_correction_to_a_conflict_value_picks_that_candidate() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    key = ("ZZ00002", "ZZ00002")
    station_id = existing[key]["id"]
    # 9900022 is in the file as `9900022=ZZ_Rail`: the operator settles the
    # conflict in its favour.
    correction = override(station_id, "uic_merits", "9900022", "9900002")
    plan = si.plan_reference(parsed(), existing, {station_id: [correction]}, BUILD + 1, TODAY)

    # The candidate is found by its code: no Manual twin is added beside it.
    assert [(c["code"], c["is_chosen"]) for c in plan.merits[key]] == [
        ("9900002", False),
        ("9900012", False),
        ("9900022", True),
        ("9900032", False),
    ]
    picked = plan.merits[key][2]
    assert (picked["origin"], picked["sources"]) == (sp.ORIGIN_CONFLICT, ["ZZ_Rail"])
    (update,) = plan.updates
    assert (update["uic_merits"], update["uic_merits_origin"]) == ("9900022", sp.ORIGIN_CONFLICT)


def test_merits_candidates_under_a_correction_and_after_its_release() -> None:
    computed = [
        {
            "code": "9900002",
            "origin": "Trainline",
            "rule": "r",
            "confidence": "high",
            "is_chosen": True,
        },
        {
            "code": "9900012",
            "origin": "Calculated",
            "rule": None,
            "confidence": None,
            "is_chosen": False,
        },
    ]
    # Correcting to a code that is already a candidate adds nothing.
    picked = so.merits_with_override(computed, "9900012", "reason")
    assert [(c["code"], c["is_chosen"]) for c in picked] == [("9900002", False), ("9900012", True)]
    assert so.merits_mirror(picked)["uic_merits_origin"] == "Calculated"

    # Correcting to "no MERITS code" leaves every candidate, none chosen.
    none = so.merits_with_override(computed, None, "reason")
    assert [c["is_chosen"] for c in none] == [False, False]
    assert so.merits_mirror(none) == {
        "uic_merits": None,
        "uic_merits_origin": None,
        "uic_merits_confidence": None,
    }

    # Releasing drops the Manual candidate and restores the computed choice.
    manual = so.merits_with_override(computed, "9900777", "reason")
    released = so.merits_without_override(manual, "9900002")
    assert [(c["code"], c["is_chosen"]) for c in released] == [
        ("9900002", True),
        ("9900012", False),
    ]
    assert so.merits_mirror(released) == {
        "uic_merits": "9900002",
        "uic_merits_origin": "Trainline",
        "uic_merits_confidence": "high",
    }


def test_a_correction_that_cannot_be_reapplied_refuses_the_build() -> None:
    existing = as_existing(si.plan_reference(parsed(), {}, {}, BUILD, TODAY))
    station_id = existing[("ZZ00001", "ZZ00001")]["id"]
    broken = override(station_id, "lat", "north", "50.0")
    data, corrections = parsed(), {station_id: [broken]}
    with pytest.raises(sf.StationFileError, match="can no longer be applied"):
        si.plan_reference(data, existing, corrections, BUILD + 1, TODAY)


# ── child rows ─────────────────────────────────────────────────────────


def _ids(data: si.ParsedInputs) -> dict[sp.Key, int]:
    return {row.key: index for index, row in enumerate(data.master, start=1)}


def _flags_by_key(
    data: si.ParsedInputs, ids: dict[sp.Key, int]
) -> dict[tuple[int, str, str], int | None]:
    """(station, token, payload) -> related station, for every flag row written."""
    rows = si._flag_rows(data, ids)
    keys = [(f["station_id"], f["token"], f["payload"]) for f in rows]
    assert len(keys) == len(set(keys))  # UNIQUE (station_id, token, payload)
    assert len({tuple(sorted(row)) for row in rows}) == 1  # homogeneous for a bulk insert
    return {key: f["related_station_id"] for key, f in zip(keys, rows, strict=True)}


def test_flag_rows_link_to_the_station_their_payload_names() -> None:
    data = parsed()
    ids = _ids(data)
    flags = _flags_by_key(data, ids)
    central, sampleton, yard_a = ids[("ZZ00001", "ZZ00001")], ids[("ZZ00002", "ZZ00002")], 3

    assert flags[(central, "candidate_displaced_to", "ZZ00002")] == sampleton
    assert flags[(sampleton, "swap_partner", "ZZ00001")] == central
    # '' rather than NULL for a bare token, and no station.
    assert flags[(central, "plc_kind_national", "")] is None
    # A PLC with two operational points resolves to one of them, always the same.
    assert flags[(yard_a, "shares_plc_with", "ZZ00003")] == yard_a
    # A payload that is not a PLC of the file links to nothing.
    assert flags[(5, "token_of_tomorrow", "with:colons")] is None


def test_a_flag_listing_several_plcs_links_every_station_it_names() -> None:
    data = parsed()
    ids = _ids(data)
    flags = _flags_by_key(data, ids)
    central, sampleton, yard_a = ids[("ZZ00001", "ZZ00001")], ids[("ZZ00002", "ZZ00002")], 3

    # `candidate_displaced_to:ZZ00001|ZZ00003` on Sampleton: one row per PLC.
    token = (sampleton, "candidate_displaced_to")
    displaced = {key[2]: related for key, related in flags.items() if key[:2] == token}
    assert displaced == {"ZZ00001": central, "ZZ00003": yard_a}


def test_a_piped_payload_that_is_not_a_list_of_plcs_stays_one_flag() -> None:
    rows = master_rows()
    rows[0] = {
        **rows[0],
        "flags": "same_source_multiple_values:ZZ_RAIL=9900001|9900002"
        ";candidate_displaced_to:ZZ00002|ZZ00099",
    }
    data = parsed(rows)
    ids = _ids(data)
    central = ids[("ZZ00001", "ZZ00001")]
    flags = {key: rel for key, rel in _flags_by_key(data, ids).items() if key[0] == central}
    assert flags == {
        (central, "same_source_multiple_values", "ZZ_RAIL=9900001|9900002"): None,
        # ZZ00099 is not a PLC of the file: the payload is not split, and not linked.
        (central, "candidate_displaced_to", "ZZ00002|ZZ00099"): None,
    }


def test_a_plc_named_twice_under_one_token_is_one_flag_row() -> None:
    # The same station named by a list and on its own: the unique key
    # (station, token, payload) would refuse the second row.
    rows = master_rows()
    rows[0] = {
        **rows[0],
        "flags": "candidate_displaced_to:ZZ00002|ZZ00003;candidate_displaced_to:ZZ00002",
    }
    data = parsed(rows)
    ids = _ids(data)
    central = ids[("ZZ00001", "ZZ00001")]
    written = [key for key in _flags_by_key(data, ids) if key[0] == central]
    assert written == [
        (central, "candidate_displaced_to", "ZZ00002"),
        (central, "candidate_displaced_to", "ZZ00003"),
    ]


def test_an_old_plc_resolves_to_one_station() -> None:
    data = parsed()
    counts: dict[str, int] = {}
    aliases = si._alias_rows(data, _ids(data), BUILD, counts)
    assert aliases == [
        {"station_id": 2, "alias_plc": "ZZ00009", "reason": "previous_plc", "build_id": BUILD}
    ]
    assert counts == {"alias_collisions": 0}

    # Two rows naming the same old PLC: one alias, the first in key order, counted.
    rows = master_rows()
    rows[0] = {**rows[0], "previous_plc": "ZZ00009"}
    data = parsed(rows)
    aliases = si._alias_rows(data, _ids(data), BUILD, counts)
    assert [(a["alias_plc"], a["station_id"]) for a in aliases] == [("ZZ00009", 1)]
    assert counts == {"alias_collisions": 1}


def test_link_rows_keep_unmatched_stops_and_drop_orphans() -> None:
    data = parsed()
    inputs = {fmt: SimpleNamespace(version_id=uuid.uuid4()) for fmt in (sf.LINKS, sf.UNMAPPED)}
    counts: dict[str, int] = {}
    rows = si._link_rows(data, inputs, _ids(data), counts)  # type: ignore[arg-type]

    assert counts == {"links_orphaned": 1, "links_matched": 4, "links_unmatched": 4}
    assert len({tuple(sorted(row)) for row in rows}) == 1  # homogeneous for a bulk insert
    matched = [r for r in rows if r["station_id"] is not None]
    unmatched = [r for r in rows if r["station_id"] is None]
    assert {r["source_version_id"] for r in matched} == {inputs[sf.LINKS].version_id}
    assert {r["source_version_id"] for r in unmatched} == {inputs[sf.UNMAPPED].version_id}
    assert {r["label"] for r in unmatched} == {"Urban", "Rail", "Multimodal", "unknown"}
    assert unmatched[0]["nearest_plc"] == "ZZ00001"
    assert "NAPST0099" not in {r["offline_station_id"] for r in rows}  # the orphan


# ── what a build changed on a station's codes, MERITS candidates, flags ──


def test_child_sets_group_rows_by_station_and_read_a_list_as_a_tuple() -> None:
    # As read from the database (a text[] comes back as a list) or as about to
    # be written: the two must compare equal.
    rows = [
        (1, "9900002", ["ZZ_Rail", "ZZ_Timetable"], True),
        (1, "9900012", None, False),
        (2, "9900004", [], False),  # an empty array says what NULL says: no source
    ]
    assert si.child_sets(rows) == {
        1: {("9900002", ("ZZ_Rail", "ZZ_Timetable"), True), ("9900012", None, False)},
        2: {("9900004", None, False)},
    }
    assert si.child_sets([]) == {}


def test_child_text_names_every_value_a_row_carries() -> None:
    columns = ("code", "is_chosen", "origin", "sources")
    rows = {
        ("9900012", False, "Calculated", ("CALC",)),
        ("9900002", True, None, ("ZZ_Rail", "ZZ_Timetable")),
    }
    # A stable order, a true flag by its name alone, nothing for an empty value.
    assert si.child_text(columns, rows) == (
        "code=9900002 is_chosen sources=ZZ_Rail|ZZ_Timetable; "
        "code=9900012 origin=Calculated sources=CALC"
    )
    flag = ("token", "payload", "related_station_id")
    assert si.child_text(flag, {("bare_token", "", None)}) == "token=bare_token"
    assert si.child_text(flag, {("swap_partner", "ZZ00001", 11)}) == (
        "token=swap_partner payload=ZZ00001 related_station_id=11"
    )
    assert si.child_text(columns, set()) is None


def test_child_changes_report_the_rows_that_went_and_the_rows_that_came() -> None:
    columns = ("token", "payload")
    before = {1: {("swap_partner", "ZZ00002"), ("bare_token", "")}, 2: {("bare_token", "")}}
    after = {
        1: {("swap_partner", "ZZ00003"), ("bare_token", "")},
        2: {("bare_token", "")},
        3: set(),
    }
    # Station 2 kept its rows, station 3 never had any: neither is a change.
    assert si.child_changes("flags", columns, before, after, [1, 2, 3], BUILD) == [
        {
            "station_id": 1,
            "build_id": BUILD,
            "field_name": "flags",
            "old_value": "token=swap_partner payload=ZZ00002",
            "new_value": "token=swap_partner payload=ZZ00003",
        }
    ]
    # Only the stations asked about: one created by this build is not a change.
    assert si.child_changes("flags", columns, {}, {9: {("bare_token", "")}}, [], BUILD) == []
    # Every row gone: nothing came.
    (gone,) = si.child_changes("flags", columns, before, {}, [2], BUILD)
    assert (gone["old_value"], gone["new_value"]) == ("token=bare_token", None)


# ── the build stage, against an in-memory stand-in for the station tables ──


class _Result:
    """What `Session.execute` hands back, as far as the build stage reads it."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.rows)

    def all(self) -> list[tuple[Any, ...]]:
        return self.rows

    def scalars(self) -> list[Any]:
        return [row[0] for row in self.rows]


class _Tables:
    """Enough of a database for `write_reference`: the rows of each table, as dicts.

    It understands the statements the build stage issues and nothing else: a
    bulk INSERT, with or without RETURNING; a bulk UPDATE by primary key; an
    UPDATE or a DELETE of every row, or of the rows whose column is IN a
    list; a SELECT of columns of one table, every row. Postgres runs the same
    build in tests/integration/test_station_panel.py.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.serial = 0
        self.selects: list[str] = []  # the table of every SELECT, in order
        # The lock against the hand edits: how many SELECTs had been issued
        # each time it was taken, and what an edit in flight commits while
        # the build waits for it.
        self.locks: list[int] = []
        self.while_waiting: Callable[[], None] | None = None
        self.overrides: list[Any] = []
        # The aggregate provider column of the fixture: its codes are evidence.
        self.unresolved = ["nap_ES_regional"]

    def table(self, name: str) -> list[dict[str, Any]]:
        return self.rows.setdefault(name, [])

    def get(self, _model: object, _key: object) -> None:
        return None  # no source version row to mark as imported

    def execute(self, statement: Any, params: list[dict[str, Any]] | None = None) -> _Result:
        if statement.is_select:
            return self._select(statement)
        table = self.table(statement.table.name)
        if statement.is_insert:
            return self._insert(statement, table, params or [])
        if params is not None:  # a bulk UPDATE, by primary key
            by_id = {row["id"]: row for row in table}
            for values in params:
                by_id[values["id"]].update(values)
            return _Result([])
        chosen = self._chosen(statement, table)
        if statement.is_delete:
            gone = {row["id"] for row in chosen}
            table[:] = [row for row in table if row["id"] not in gone]
            return _Result([])
        bound = statement.compile().params
        values = {name: value for name, value in bound.items() if name in statement.table.c}
        for row in chosen:
            row.update(values)
        return _Result([])

    def _insert(
        self, statement: Any, table: list[dict[str, Any]], params: list[dict[str, Any]]
    ) -> _Result:
        # Every row of a batch carries the same keys, None included: without
        # that a bulk INSERT cannot go out as one statement.
        assert len({frozenset(row) for row in params}) == 1, statement.table.name
        # And none gives None to a column that has a server default: the bulk
        # INSERT writes a None as NULL, where the default was meant.
        defaulted = {c.name for c in statement.table.c if c.server_default is not None}
        for row in params:
            assert not [name for name in defaulted & set(row) if row[name] is None], row
        returned = [column["name"] for column in statement.returning_column_descriptions]
        out = []
        for values in params:
            self.serial += 1
            row = {"id": self.serial, **values}
            table.append(row)
            out.append(tuple(row[name] for name in returned))
        return _Result(out)

    @staticmethod
    def _chosen(statement: Any, table: list[dict[str, Any]]) -> list[dict[str, Any]]:
        where = statement.whereclause
        if where is None:
            return list(table)
        wanted = set(where.right.value)
        return [row for row in table if row[where.left.name] in wanted]

    def _lock(self) -> _Result:
        """The build's lock: granted once the edits in flight have committed."""
        if self.while_waiting is not None:
            self.while_waiting()
            self.while_waiting = None
        self.locks.append(len(self.selects))
        return _Result([])

    def _select(self, statement: Any) -> _Result:
        columns = list(statement.selected_columns)
        if getattr(columns[0], "name", None) == "pg_advisory_xact_lock":
            return self._lock()
        name = columns[0].table.name
        self.selects.append(name)
        if name == "station_ref_override":
            return _Result([(correction,) for correction in self.overrides])
        if name == "station_source":
            return _Result([(key,) for key in self.unresolved])
        return _Result([tuple(row.get(c.name) for c in columns) for row in self.table(name)])


_INPUTS = {
    fmt: SimpleNamespace(version_id=uuid.uuid4()) for fmt in (sf.MASTER, sf.LINKS, sf.UNMAPPED)
}


def _build(db: _Tables, build_id: int, data: si.ParsedInputs | None = None) -> dict[str, Any]:
    """Run the build stage as build `build_id`; returns its diff summary."""
    _, diff = si.write_reference(db, build_id, _INPUTS, data or parsed(), TODAY)  # type: ignore[arg-type]
    return diff


def _station(db: _Tables, plc: str, uopid: str | None = None) -> dict[str, Any]:
    key = (plc, uopid or plc)
    return next(r for r in db.table("station_ref") if (r["plc"], r["era_uopid"]) == key)


def _history(db: _Tables) -> list[tuple[Any, ...]]:
    return [
        (h["station_id"], h["build_id"], h["field_name"], h["old_value"], h["new_value"])
        for h in db.table("station_ref_history")
    ]


def _aliases(db: _Tables) -> list[tuple[str, int, int]]:
    return [(a["alias_plc"], a["station_id"], a["build_id"]) for a in db.table("station_ref_alias")]


def test_a_first_build_writes_the_reference_and_its_children() -> None:
    db = _Tables()
    diff = _build(db, 1)
    assert diff == {"created": 5, "changed": 0, "unchanged": 0, "absent": 0, "fields_changed": {}}
    assert {name: len(rows) for name, rows in db.rows.items() if rows} == {
        "station_ref": 5,
        "station_code_series": 2,
        "station_ref_code": 4,
        "station_ref_merits": 6,
        "station_ref_flag": 8,
        "station_ref_alias": 1,
        "station_ref_link": 8,
    }
    assert {row["last_changed_build_id"] for row in db.table("station_ref")} == {1}


def test_a_rebuild_from_the_same_files_changes_nothing_and_piles_nothing_up() -> None:
    db = _Tables()
    _build(db, 1)
    before = {name: len(rows) for name, rows in db.rows.items()}
    diff = _build(db, 2)
    assert diff == {"created": 0, "changed": 0, "unchanged": 5, "absent": 0, "fields_changed": {}}
    assert {name: len(rows) for name, rows in db.rows.items()} == before
    assert _history(db) == []
    lineage = {
        (r["last_built_build_id"], r["last_changed_build_id"]) for r in db.rows["station_ref"]
    }
    assert lineage == {(2, 1)}
    # The children are compared in memory: each table is read once per build,
    # whatever the number of stations.
    assert db.selects[len(db.selects) // 2 :] == [
        "station_ref",
        "station_ref_override",
        "station_ref_code",
        "station_ref_merits",
        "station_ref_flag",
        "station_source",
        "station_code_series",
    ]


def test_a_build_leaves_exactly_the_current_aliases() -> None:
    db = _Tables()
    _build(db, 1)
    _build(db, 2)
    central, sampleton = _station(db, "ZZ00001")["id"], _station(db, "ZZ00002")["id"]
    # One alias per old PLC, not one per build: it resolves in one lookup.
    assert _aliases(db) == [("ZZ00009", sampleton, 2)]

    # The next issue of the master gives the old PLC to another station...
    rows = master_rows()
    rows[0] = {**rows[0], "previous_plc": "ZZ00009"}
    rows[1] = {**rows[1], "previous_plc": ""}
    _build(db, 3, parsed(rows))
    assert _aliases(db) == [("ZZ00009", central, 3)]  # no longer listed under Sampleton

    # ...and the one after withdraws it: nothing of an earlier build stays.
    rows[0] = {**rows[0], "previous_plc": ""}
    _build(db, 4, parsed(rows))
    assert _aliases(db) == []


def test_a_change_of_codes_merits_or_flags_alone_is_a_change() -> None:
    db = _Tables()
    _build(db, 1)
    central, sampleton = _station(db, "ZZ00001")["id"], _station(db, "ZZ00002")["id"]

    rows = master_rows()
    rows[0] = {
        **rows[0],
        # A stop key replaced inside a provider cell: same feed count, same tier.
        "nap_CH_SBB": "9900001|9900001:0:2",
        # One more conflict value: the chosen code and the confidence stay.
        "uic_merits_conflict_values": "9900041=ZZ_Rail",
        # One more flag, at the same warning level.
        "flags": rows[0]["flags"] + ";platform_count_differs:ZZ00002",
    }
    diff = _build(db, 2, parsed(rows))

    # No column of station_ref moved, and the station is still not "unchanged".
    assert (diff["created"], diff["changed"], diff["unchanged"]) == (0, 1, 4)
    assert diff["fields_changed"] == {"codes": 1, "merits": 1, "flags": 1}
    assert _history(db) == [
        (
            central,
            2,
            "codes",
            "source_key=nap_CH_SBB code=9900001:0:1",
            "source_key=nap_CH_SBB code=9900001:0:2",
        ),
        (central, 2, "merits", None, "code=9900041 origin=Conflict value sources=ZZ_Rail"),
        (
            central,
            2,
            "flags",
            None,
            f"token=platform_count_differs payload=ZZ00002 related_station_id={sampleton}",
        ),
    ]
    changed = {r["plc"]: r["last_changed_build_id"] for r in db.table("station_ref")}
    assert changed == {"ZZ00001": 2, "ZZ00002": 1, "ZZ00003": 1, "ZZ00004": 1}
    assert {r["last_built_build_id"] for r in db.table("station_ref")} == {2}


def test_a_series_the_links_file_changes_is_a_change_of_the_code() -> None:
    db = _Tables()
    _build(db, 1)
    data = parsed()
    for link in data.links:
        if link.fields.get("code_series") == "CH_service_point_number":
            link.fields["code_series"] = "ZZ_series_renamed"
    diff = _build(db, 2, data)

    assert (diff["changed"], diff["unchanged"]) == (1, 4)
    assert _history(db) == [
        (
            _station(db, "ZZ00001")["id"],
            2,
            "codes",
            "source_key=nap_CH_SBB code=9900001 series=CH_service_point_number is_primary",
            "source_key=nap_CH_SBB code=9900001 series=ZZ_series_renamed is_primary",
        )
    ]


def test_a_station_changed_in_its_columns_and_its_children_is_counted_once() -> None:
    db = _Tables()
    _build(db, 1)
    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf", "nap_DE_DELFI": "de:99:3"}
    diff = _build(db, 2, parsed(rows))

    assert (diff["changed"], diff["unchanged"]) == (1, 4)
    assert diff["fields_changed"] == {"name": 1, "codes": 1}
    sampleton = _station(db, "ZZ00002")
    assert (sampleton["name"], sampleton["last_changed_build_id"]) == ("Sampleton Hbf", 2)
    assert _history(db) == [
        (sampleton["id"], 2, "name", "Sampleton", "Sampleton Hbf"),
        (
            sampleton["id"],
            2,
            "codes",
            "source_key=nap_DE_DELFI code=de:99:2 is_primary",
            "source_key=nap_DE_DELFI code=de:99:3 is_primary",
        ),
    ]


def test_a_created_station_is_not_a_change_and_the_flag_that_now_links_to_it_is() -> None:
    rows = master_rows()
    # A flag naming a PLC the file does not have yet: stored, linked to nothing.
    rows[2] = {**rows[2], "flags": rows[2]["flags"] + ";renumbered_to:ZZ00005"}
    db = _Tables()
    _build(db, 1, parsed(rows))

    newcomer = {**rows[4], "plc": "ZZ00005", "era_uopid": "ZZ00005", "era_name": "Newville"}
    diff = _build(db, 2, parsed([*rows, newcomer]))

    assert (diff["created"], diff["changed"], diff["unchanged"]) == (1, 1, 4)
    yard, newville = _station(db, "ZZ00003", "ZZOP03A")["id"], _station(db, "ZZ00005")["id"]
    # The new station's own codes, candidates and flags are not a change of it.
    assert _history(db) == [
        (
            yard,
            2,
            "flags",
            "token=renumbered_to payload=ZZ00005",
            f"token=renumbered_to payload=ZZ00005 related_station_id={newville}",
        )
    ]


def test_a_correction_made_between_two_builds_is_not_a_change_of_the_next() -> None:
    db = _Tables()
    _build(db, 1)
    station = _station(db, "ZZ00002")
    # What the API does when a content manager corrects the MERITS code: the
    # candidates are rewritten and the chosen one is mirrored on the row.
    columns = ("code", "origin", "rule", "confidence", "sources", "check_digit", "is_chosen")
    merits = db.table("station_ref_merits")
    mine = [m for m in merits if m["station_id"] == station["id"]]
    corrected = so.merits_with_override(
        [{c: m[c] for c in columns} for m in mine], "9900777", "per the operator"
    )
    merits[:] = [m for m in merits if m["station_id"] != station["id"]]
    merits += [{"id": 900 + n, "station_id": station["id"], **c} for n, c in enumerate(corrected)]
    station.update(so.merits_mirror(corrected))
    db.overrides.append(
        override(station["id"], "uic_merits", "9900777", "9900002", reason="per the operator")
    )

    diff = _build(db, 2)
    # The build re-applies the correction and finds what is already there.
    assert (diff["changed"], diff["unchanged"]) == (0, 5)
    assert _history(db) == []
    assert _station(db, "ZZ00002")["uic_merits"] == "9900777"


# ── the build and the hand edits: one lock, and the rule of a MERITS code ──


def test_the_build_takes_the_lock_before_it_reads_the_reference_and_the_corrections() -> None:
    db = _Tables()
    _build(db, 1)
    # Once, and before any SELECT: what the build then reads, and plans from,
    # cannot be corrected under it until its transaction ends.
    assert db.locks == [0]
    assert db.selects[:2] == ["station_ref", "station_ref_override"]


def test_a_correction_committed_while_the_build_waits_is_reapplied_not_overwritten() -> None:
    db = _Tables()
    _build(db, 1)
    station = _station(db, "ZZ00002")

    def correct_the_name() -> None:
        # What the API commits for a correction of the name. Its request holds
        # the lock the build is waiting for, so it is in before the build reads.
        station["name"] = "Sampleton Central"
        db.overrides.append(override(station["id"], "name", "Sampleton Central", "Sampleton"))

    db.while_waiting = correct_the_name
    # The next issue of the master renames the station and moves it: the build
    # rewrites every built column of its row.
    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf", "lat": "50.123"}
    diff = _build(db, 2, parsed(rows))

    assert (station["name"], station["lat"]) == ("Sampleton Central", 50.123)
    (correction,) = db.overrides
    assert correction.computed_value_latest == "Sampleton Hbf"  # what a release restores
    assert [(h[2], h[4]) for h in _history(db)] == [("lat", "50.123")]
    assert (diff["changed"], diff["unchanged"]) == (1, 4)


# Invented, with the shape of the real ones: where the master gives a station
# no MERITS code, it says why in a sentence, and the station has no candidate.
NO_CODE_RULE = "No operator timetable names this station, and no calculation covers its country"
REWORDED_RULE = "No operator timetable names this station; its country has no calculation yet"
_MERITS_COLUMNS = ("code", "origin", "rule", "confidence", "sources", "check_digit", "is_chosen")


def _rewrite_candidates(
    db: _Tables,
    station: dict[str, Any],
    change: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> None:
    """What the API does to a station's MERITS candidates when a correction is
    set or released: rewrite them, and mirror the chosen one on the row."""
    merits = db.table("station_ref_merits")
    mine = [m for m in merits if m["station_id"] == station["id"]]
    after = change([{c: m[c] for c in _MERITS_COLUMNS} for m in mine])
    merits[:] = [m for m in merits if m["station_id"] != station["id"]]
    merits += [{"id": 900 + n, "station_id": station["id"], **c} for n, c in enumerate(after)]
    station.update(so.merits_mirror(after))


def test_the_mirror_of_the_chosen_candidate_leaves_the_rule_alone() -> None:
    # Code, origin and confidence follow the chosen candidate. The rule does
    # not: with no code, the master's sentence is on the row and on no
    # candidate, and a mirror that wrote it could not put it back.
    assert so.MERITS_MIRROR == ("uic_merits", "uic_merits_origin", "uic_merits_confidence")
    manual = so.merits_with_override([], "9900555", "per the operator")
    assert so.merits_mirror(manual) == {
        "uic_merits": "9900555",
        "uic_merits_origin": so.ORIGIN_MANUAL,
        "uic_merits_confidence": None,
    }
    assert so.merits_mirror(so.merits_without_override(manual, None)) == dict.fromkeys(
        so.MERITS_MIRROR
    )


def test_a_released_merits_correction_leaves_the_rule_the_build_computes_now() -> None:
    def issue(rule: str) -> si.ParsedInputs:
        rows = master_rows()
        rows[3] = {**rows[3], "uic_merits_rule": rule}  # no code, no candidate: a sentence
        return parsed(rows)

    db = _Tables()
    _build(db, 1, issue(NO_CODE_RULE))
    station = _station(db, "ZZ00003", "ZZOP03B")
    assert (station["uic_merits"], station["uic_merits_rule"]) == (None, NO_CODE_RULE)
    assert not [m for m in db.table("station_ref_merits") if m["station_id"] == station["id"]]

    # A content manager gives the station a code by hand...
    _rewrite_candidates(
        db, station, lambda found: so.merits_with_override(found, "9900555", "per the operator")
    )
    db.overrides.append(
        override(station["id"], "uic_merits", "9900555", None, reason="per the operator")
    )
    assert station["uic_merits_rule"] == NO_CODE_RULE
    # ...the next issue of the master rewords its sentence...
    _build(db, 2, issue(REWORDED_RULE))
    assert (station["uic_merits"], station["uic_merits_origin"]) == ("9900555", so.ORIGIN_MANUAL)
    assert station["uic_merits_rule"] == REWORDED_RULE
    # ...and the correction is released.
    (correction,) = db.overrides
    _rewrite_candidates(
        db,
        station,
        lambda found: so.merits_without_override(found, correction.computed_value_latest),
    )
    db.overrides.clear()
    assert (station["uic_merits"], station["uic_merits_origin"]) == (None, None)
    assert station["uic_merits_rule"] == REWORDED_RULE  # what the build computes now, not NULL

    # The next build has nothing to put back: no change, no history row of its own.
    before = _history(db)
    assert [(h[1], h[2]) for h in before] == [(2, "uic_merits_rule")]
    diff = _build(db, 3, issue(REWORDED_RULE))
    assert (diff["changed"], diff["unchanged"]) == (0, 5)
    assert _history(db) == before


# ── bulk inserts: one statement per chunk, whatever cells are empty ────


class _ProbeBase(DeclarativeBase):
    pass


class _Probe(_ProbeBase):
    """Shaped like the station tables where it matters: nullable columns."""

    __tablename__ = "bulk_probe"

    id: Mapped[int] = mapped_column(primary_key=True)
    plc: Mapped[str]
    era_uopid: Mapped[str]
    name: Mapped[str | None]
    lat: Mapped[float | None]
    eva: Mapped[str | None]
    n_nap_feeds: Mapped[int | None]


def _probe_rows(count: int = 120) -> list[dict[str, Any]]:
    """Rows with the same keys, whose empty cells fall differently row after row."""
    return [
        {
            "plc": f"ZZ{n:05d}",
            "era_uopid": f"ZZ{n:05d}",
            "name": None if n % 2 else f"Station {n}",
            "lat": None if n % 3 else 50.0 + n / 1000,
            "eva": None if n % 5 else f"99{n:05d}",
            "n_nap_feeds": None if n % 7 else n,
        }
        for n in range(1, count + 1)
    ]


@pytest.fixture
def probe() -> Iterator[tuple[Session, list[str]]]:
    """A SQLite session on the probe table, and the INSERT statements it sends."""
    engine = create_engine("sqlite://")
    _ProbeBase.metadata.create_all(engine)
    inserts: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.startswith("INSERT"):
            inserts.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    with Session(engine) as db:
        yield db, inserts
    engine.dispose()


def _probe_stored(db: Session) -> list[dict[str, Any]]:
    columns = [c for c in _Probe.__table__.c if c.name != "id"]
    stored = db.execute(select(*columns).order_by(_Probe.id)).all()
    return [dict(row._mapping) for row in stored]


def test_a_bulk_insert_is_one_statement_whatever_cells_are_empty(
    probe: tuple[Session, list[str]],
) -> None:
    db, inserts = probe
    rows = _probe_rows()
    si._insert(db, _Probe, rows)
    # Left to itself the ORM drops a None-valued key and batches only
    # neighbours with the same keys left: these 120 rows were 120 statements.
    assert len(inserts) == 1
    # What is written is what was written before: an empty cell is NULL.
    assert _probe_stored(db) == rows


def test_new_stations_go_in_one_statement_and_their_ids_come_back(
    probe: tuple[Session, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, inserts = probe
    monkeypatch.setattr(si, "StationRef", _Probe)
    rows = _probe_rows()
    ids = si._write_stations(db, si._Plan(new_rows=rows))
    assert len(inserts) == 1
    assert set(ids) == {(row["plc"], row["era_uopid"]) for row in rows}
    assert sorted(ids.values()) == list(range(1, len(rows) + 1))
    assert _probe_stored(db) == rows


# ── the inputs a build needs ───────────────────────────────────────────


def _source(key: str, fmt: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), key=key, format=fmt, enabled=True)


def _version(path: Path | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sha256="ab" * 32,
        filename="station_master_crd_2026-09.csv",
        stored_path=str(path) if path else None,
        as_of=date(2026, 9, 1),
    )


def test_input_for_explains_what_is_in_the_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = tmp_path / "master.csv"
    stored.write_bytes(b"x")
    master = _source("OFFLINE_MASTER", sf.MASTER)
    versions: dict[str, Any] = {}
    monkeypatch.setattr(si, "_latest_version", lambda _db, source: versions.get(source.key))

    assert "no enabled source declares the station master" in si._input_for(None, sf.MASTER, [])  # type: ignore[arg-type, operator]
    twin = _source("MASTER_COPY", sf.MASTER)
    assert "several enabled sources" in si._input_for(None, sf.MASTER, [master, twin])  # type: ignore[arg-type, operator, list-item]
    assert "no file uploaded yet" in si._input_for(None, sf.MASTER, [master])  # type: ignore[arg-type, operator, list-item]

    versions["OFFLINE_MASTER"] = _version(tmp_path / "deleted.csv")
    assert "is gone" in si._input_for(None, sf.MASTER, [master])  # type: ignore[arg-type, operator, list-item]

    versions["OFFLINE_MASTER"] = _version(stored)
    found = si._input_for(None, sf.MASTER, [master])  # type: ignore[arg-type, list-item]
    assert isinstance(found, si.BuildInput)
    assert found.manifest() == {
        "format": sf.MASTER,
        "version_id": str(versions["OFFLINE_MASTER"].id),
        "sha256": "ab" * 32,
        "filename": "station_master_crd_2026-09.csv",
        "as_of": "2026-09-01",
    }


def test_parse_inputs_reads_all_five_files(tmp_path: Path) -> None:
    paths = write_station_files(tmp_path)
    inputs = {
        fmt: si.BuildInput(fmt, f"SRC_{i}", uuid.uuid4(), "ab" * 32, path.name, path, None)
        for i, (fmt, path) in enumerate(paths.items())
    }
    build_log = si.BuildLog()
    data = si.parse_inputs(inputs, build_log)
    assert len(data.master) == 5
    assert len(data.links) == 9  # five link rows and four unmatched stops
    assert len(data.crd.locations) == 4
    assert len(data.telref.points) == 5
    assert "master: 5 rows" in build_log.text()


def test_a_refused_file_is_named_by_its_source(tmp_path: Path) -> None:
    paths = write_station_files(tmp_path)
    paths[sf.MASTER].write_bytes(paths[sf.LINKS].read_bytes())  # the wrong file
    inputs = {
        fmt: si.BuildInput(fmt, f"SRC_{fmt}", uuid.uuid4(), "ab" * 32, path.name, path, None)
        for fmt, path in paths.items()
    }
    build_log = si.BuildLog()
    with pytest.raises(sf.StationFileError) as exc:
        si.parse_inputs(inputs, build_log)
    assert f"SRC_{sf.MASTER} (station_master_crd_2026-09.csv)" in str(exc.value)
    assert "missing columns" in str(exc.value)


# ── run_build: refused and crashed builds leave the reference alone ────


class _NoDb:
    def __enter__(self) -> _NoDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def _run(
    monkeypatch: pytest.MonkeyPatch, problems: list[str], **patches: Any
) -> tuple[str, bool, dict[str, Any]]:
    closed: dict[str, Any] = {}
    monkeypatch.setattr(si, "SessionLocal", _NoDb)
    monkeypatch.setattr(si, "find_inputs", lambda _db: ({}, problems))
    monkeypatch.setattr(si, "_open_build", lambda _inputs: 42)
    monkeypatch.setattr(si, "_close_build", lambda _id, _log, **kw: closed.update(kw))
    for name, value in patches.items():
        monkeypatch.setattr(si, name, value)
    output, success = si.run_build(TODAY)
    return output, success, closed


def test_a_build_without_all_five_inputs_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_write(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("a refused build must not write anything")

    output, success, closed = _run(
        monkeypatch,
        ["CRD: no file uploaded yet (CRD locations (crd_locations_*.csv))"],
        load_registers=must_not_write,
        write_reference=must_not_write,
    )
    assert success is False
    assert closed["status"] == "failed"
    assert closed["counts"]["error"].startswith("inputs missing: CRD: no file uploaded yet")
    assert "refused: inputs missing" in output
    assert "build #42" in output


def test_a_refused_file_fails_the_build_with_the_operator_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_inputs: Any, _log: Any) -> None:
        raise sf.StationFileError("OFFLINE_MASTER (x.csv): the pair (plc, era_uopid) repeats")

    _, success, closed = _run(monkeypatch, [], parse_inputs=refuse)
    assert success is False
    assert closed["counts"] == {
        "error": "OFFLINE_MASTER (x.csv): the pair (plc, era_uopid) repeats"
    }


def test_a_crash_is_recorded_without_leaking_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(_inputs: Any, _log: Any) -> None:
        raise RuntimeError("connection to /var/run/postgresql/.s.PGSQL.5432 failed")

    monkeypatch.setattr(si.log, "disabled", True)
    _, success, closed = _run(monkeypatch, [], parse_inputs=crash)
    assert success is False
    assert closed["counts"] == {"error": "internal error (RuntimeError): see the worker log"}


# ── coalescing on (status, session, kind) ──────────────────────────────


class _Query:
    def __init__(self, pending: Any) -> None:
        self.pending = pending
        self.filters: list[str] = []

    def filter(self, clause: Any) -> _Query:
        self.filters.append(str(clause))
        return self

    def first(self) -> Any:
        return self.pending


class _QueueDb:
    def __init__(self, pending: Any = None) -> None:
        self.q = _Query(pending)
        self.added: list[Any] = []
        self.committed = False

    def query(self, _model: object) -> _Query:
        return self.q

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True


def test_enqueue_coalesces_on_status_session_and_kind() -> None:
    db = _QueueDb()
    assert ingestion._enqueue_rebuild(db, session_id=None, reason="x", kind="station_build") is True  # type: ignore[arg-type]
    assert db.q.filters == [
        "rebuild_jobs.status = :status_1",
        "rebuild_jobs.session_id IS NULL",
        "rebuild_jobs.kind = :kind_1",
    ]
    (job,) = db.added
    assert (job.kind, job.session_id, job.status) == ("station_build", None, "pending")
    assert db.committed


def test_enqueue_defaults_to_a_graph_job() -> None:
    db = _QueueDb()
    ingestion._enqueue_rebuild(db, session_id="eu19", reason="new GTFS uploaded")  # type: ignore[arg-type]
    assert db.added[0].kind == "graph"
    assert db.added[0].session_id == "eu19"


def test_a_pending_job_of_the_same_kind_swallows_the_request() -> None:
    db = _QueueDb(pending=object())
    assert (
        ingestion._enqueue_rebuild(db, session_id=None, reason="x", kind="station_build") is False
    )  # type: ignore[arg-type]
    assert db.added == []
    assert not db.committed


def test_enqueue_build_queues_a_session_less_station_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake(_db: Any, **kw: Any) -> bool:
        calls.append(kw)
        return True

    monkeypatch.setattr(si.ingestion, "_enqueue_rebuild", fake)
    assert si.enqueue_build(None, "a new version of CRD") is True  # type: ignore[arg-type]
    assert calls == [
        {"session_id": None, "reason": "a new version of CRD", "kind": "station_build"}
    ]
    assert si.STATION_BUILD_KIND == worker._STATION_BUILD_KIND == "station_build"
    assert ingestion.GRAPH_JOB_KIND == worker._GRAPH_JOB_KIND == "graph"


# ── the worker's kind branch ───────────────────────────────────────────


class _Chain:
    def __init__(self, job: Any) -> None:
        self.job = job

    def filter(self, *_a: object) -> _Chain:
        return self

    def order_by(self, *_a: object) -> _Chain:
        return self

    def first(self) -> Any:
        return self.job


class _TickDb:
    def __init__(self, job: Any) -> None:
        self.job = job
        self.session_lookups = 0

    def __enter__(self) -> _TickDb:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def query(self, _model: object) -> _Chain:
        return _Chain(self.job)

    def execute(self, _stmt: object) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.job.id)

    def get(self, model: object, _key: object) -> Any:
        if model is worker.RebuildJob:
            return self.job
        self.session_lookups += 1
        return SimpleNamespace(engine="motis", state="populated")

    def commit(self) -> None:
        return None


def _job(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "session_id": None,
        "status": "pending",
        "kind": "station_build",
        "log": "queued\n",
        # Well in the past: tick() leaves a job alone until its debounce elapsed.
        "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "started_at": None,
        "finished_at": None,
        "graph_path": None,
        "max_memory": False,
        "cancel_requested_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _tick(monkeypatch: pytest.MonkeyPatch, job: Any, result: Any) -> tuple[_TickDb, list[str]]:
    reached: list[str] = []

    def graph_builder(name: str) -> Any:
        def build(**_kw: Any) -> tuple[str, bool, str]:
            reached.append(name)
            return "built", True, "/data/graphs/x"

        return build

    def station_build() -> tuple[str, bool]:
        reached.append("station_import.run_build")
        if isinstance(result, Exception):
            raise result
        return result

    db = _TickDb(job)
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)
    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(worker, "_cancel_requested", lambda _jid: False)
    monkeypatch.setattr(worker, "run_build", graph_builder("run_build"))
    monkeypatch.setattr(worker, "run_build_motis", graph_builder("run_build_motis"))
    monkeypatch.setattr(worker, "_watch_for_cancel", lambda *_a: reached.append("cancel watcher"))
    monkeypatch.setattr(
        worker.graph_snapshots, "record_snapshot", lambda *_a, **_k: reached.append("snapshot")
    )
    monkeypatch.setattr(si, "run_build", station_build)
    worker.tick()
    return db, reached


def test_a_station_job_runs_the_importer_and_nothing_of_a_graph_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    db, reached = _tick(monkeypatch, job, ("[  0.1s] done: 5 created\n", True))
    assert reached == ["station_import.run_build"]  # no engine builder, no cancel watcher
    assert db.session_lookups == 0  # the branch is before the engine lookup
    assert job.status == "done"
    assert job.finished_at is not None
    assert job.log == "queued\n[  0.1s] done: 5 created\n"
    assert job.graph_path is None


def test_a_refused_station_build_marks_the_job_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job()
    _tick(monkeypatch, job, ("refused: inputs missing: CRD\n", False))
    assert job.status == "failed"
    assert "inputs missing" in job.log


def test_a_crashing_importer_never_leaves_the_job_running(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job()
    monkeypatch.setattr(worker.log, "disabled", True)
    _tick(monkeypatch, job, RuntimeError("the database went away"))
    assert job.status == "failed"
    assert "the station build crashed" in job.log
    assert "database went away" not in job.log


@pytest.mark.parametrize("job", [_job(kind="graph", session_id="eu19"), _job(kind=None)])
def test_a_graph_job_still_reaches_its_engine_builder(
    monkeypatch: pytest.MonkeyPatch, job: Any
) -> None:
    # `kind=None`: a row object built before the column existed is a graph job.
    _, reached = _tick(monkeypatch, job, ("unused", True))
    assert "station_import.run_build" not in reached
    assert {"run_build", "run_build_motis"} & set(reached)
    assert "cancel watcher" in reached  # a graph build still gets its watcher
    assert job.status == "done"
