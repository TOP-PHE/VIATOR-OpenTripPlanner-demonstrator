"""Parsing of the five offline station files (app/master/station_parse.py).

Pure: no database. Every row is invented; the PLC prefix `ZZ` does not exist.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.master import station_files as sf
from app.master import station_parse as sp
from tests.station_fixtures import (
    LICENCE_TAG,
    crd_location_rows,
    csv_bytes,
    link_rows,
    master_rows,
    telref_rows,
    unmapped_rows,
    write_station_files,
)


def full(fmt: str, row: dict[str, str]) -> dict[str, str]:
    """A row as `read_rows` yields it: every column present, empty when unnamed."""
    return {column: row.get(column, "") for column in sf.FILE_SHAPES[fmt]}


def master(**overrides: str) -> dict[str, str]:
    return full(sf.MASTER, {**master_rows()[0], **overrides})


def read_all(path: Path, fmt: str) -> list[dict[str, str]]:
    """`read_rows` is a generator: nothing is read, or refused, until it is consumed."""
    return list(sp.read_rows(path, fmt))


def parsed_master() -> dict[sp.Key, sp.MasterRow]:
    rows = sp.parse_master(full(sf.MASTER, row) for row in master_rows())
    return {row.key: row for row in rows}


# ── cells ──────────────────────────────────────────────────────────────


def test_split_cell_drops_blanks_and_repeats_and_keeps_order() -> None:
    assert sp.split_cell("9900001|9900001:0:1", "|") == ["9900001", "9900001:0:1"]
    assert sp.split_cell("b||a|b", "|") == ["b", "a"]
    assert sp.split_cell("", "|") == []
    assert sp.split_cell(None, ";") == []
    assert sp.split_cell("station;junction", ";") == ["station", "junction"]


def test_an_empty_cell_is_no_value_never_zero() -> None:
    assert sp.text_or_none("") is None
    assert sp.text_or_none("0") == "0"
    assert sp.parse_float("") is None
    assert sp.parse_int("") is None
    assert sp.parse_flag("") is None
    assert sp.parse_date("") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50.5", 50.5),
        (" 4.0 ", 4.0),
        ("-0.25", -0.25),
        ("north", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_parse_float(raw: str, expected: float | None) -> None:
    assert sp.parse_float(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("3", 3), ("3.0", 3), ("0", 0), ("3.5", None), ("three", None)]
)
def test_parse_int(raw: str, expected: int | None) -> None:
    assert sp.parse_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yes", True), ("no", False), ("1", True), ("0", False), ("YES", True), ("perhaps", None)],
)
def test_parse_flag_does_not_turn_an_unknown_word_into_false(
    raw: str, expected: bool | None
) -> None:
    assert sp.parse_flag(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "2024-12-15",
        "2024-12-15T00:00:00",
        "2024-12-15 00:00:00+01:00",
        "20241215",
        "15/12/2024",
        "15.12.2024",
    ],
)
def test_parse_date_accepts_the_forms_an_extract_writes(raw: str) -> None:
    assert sp.parse_date(raw) == date(2024, 12, 15)


def test_parse_date_gives_none_for_what_it_cannot_read() -> None:
    assert sp.parse_date("mid-December") is None
    assert sp.parse_date("2024-13-45") is None


def test_a_flag_splits_on_its_first_colon_only() -> None:
    assert sp.split_flag("candidate_displaced_to:ZZ00002") == ("candidate_displaced_to", "ZZ00002")
    assert sp.split_flag("plc_kind_national") == ("plc_kind_national", "")
    assert sp.split_flag("token:with:colons") == ("token", "with:colons")
    assert sp.split_flag(":odd") == (":odd", "")


# ── reading a file ─────────────────────────────────────────────────────


def test_read_rows_yields_every_cell_as_text(tmp_path: Path) -> None:
    paths = write_station_files(tmp_path)
    rows = list(sp.read_rows(paths[sf.MASTER], sf.MASTER))
    assert len(rows) == 5
    assert set(rows[0]) == set(sf.FILE_SHAPES[sf.MASTER])
    assert rows[0]["plc"] == "ZZ00001"  # the BOM did not end up in the first column name
    assert rows[0]["lat"] == "50.000000"  # text, not a number
    assert rows[4]["lat"] == ""


def test_read_rows_keeps_a_leading_zero_and_a_quoted_comma(tmp_path: Path) -> None:
    path = tmp_path / "telref.csv"
    path.write_bytes(
        csv_bytes(
            sf.FILE_SHAPES[sf.ERA_TELREF],
            {"plc": "ZZ00001", "uopid": "0012345", "name": "Exampleville, Central"},
        )
    )
    (row,) = sp.read_rows(path, sf.ERA_TELREF)
    assert row["uopid"] == "0012345"
    assert row["name"] == "Exampleville, Central"


def test_read_rows_refuses_a_file_of_another_shape(tmp_path: Path) -> None:
    paths = write_station_files(tmp_path)
    with pytest.raises(sf.StationFileError, match="missing columns"):
        read_all(paths[sf.LINKS], sf.MASTER)


def test_read_rows_refuses_a_row_with_more_cells_than_columns(tmp_path: Path) -> None:
    path = tmp_path / "telref.csv"
    header = ",".join(sf.FILE_SHAPES[sf.ERA_TELREF])
    too_long = ",".join(["x"] * 37)  # an unquoted comma shifted everything right
    path.write_text(f"{header}\n{too_long}\n", encoding="utf-8-sig")
    with pytest.raises(sf.StationFileError, match="line 2: more cells than columns"):
        read_all(path, sf.ERA_TELREF)


# ── the master: grain and refusals ─────────────────────────────────────


def test_the_grain_is_plc_and_operational_point() -> None:
    rows = parsed_master()
    assert len(rows) == 5
    # Two operational points share one PLC and stay two rows.
    assert ("ZZ00003", "ZZOP03A") in rows
    assert ("ZZ00003", "ZZOP03B") in rows
    assert len({plc for plc, _ in rows}) == 4


def test_a_repeated_pair_is_refused_and_named() -> None:
    rows = [full(sf.MASTER, row) for row in master_rows()]
    rows.append(dict(rows[2]))  # ZZ00003 / ZZOP03A again
    with pytest.raises(sf.StationFileError) as exc:
        sp.parse_master(rows)
    message = str(exc.value)
    assert "the pair (plc, era_uopid) repeats" in message
    assert "line 7 ('ZZ00003', 'ZZOP03A')" in message


def test_the_same_plc_with_another_operational_point_is_not_a_repeat() -> None:
    rows = [full(sf.MASTER, row) for row in master_rows()]
    rows.append({**rows[0], "era_uopid": "ZZOP01X"})
    assert len(sp.parse_master(rows)) == 6


def test_a_plc_that_is_not_seven_characters_is_refused() -> None:
    rows = [master(plc="ZZ1", era_uopid="ZZ1")]
    with pytest.raises(sf.StationFileError, match=r"a PLC must be 7 characters: line 2 \('ZZ1'"):
        sp.parse_master(rows)


def test_a_plc_is_validated_on_length_only() -> None:
    # Three kinds are not "two letters and five digits"; one contains a space.
    for plc in ("EU00123", "5512345", "ATNs G "):
        (row,) = sp.parse_master([master(plc=plc, era_uopid=plc)])
        assert row.plc == plc


def test_a_crd_derived_row_without_its_licence_tag_is_refused() -> None:
    untagged = [master(crd_source_tag="")]
    with pytest.raises(sf.StationFileError, match="carries no licence tag"):
        sp.parse_master(untagged)
    # An ERA-only row carries none, and that is not an error.
    (row,) = sp.parse_master([master(spine_source="ERA_only", crd_source_tag="")])
    assert row.fields["crd_source_tag"] is None


def test_error_messages_name_ten_rows_at_most() -> None:
    rows = [master(plc=f"Z{i}", era_uopid=f"Z{i}") for i in range(14)]
    with pytest.raises(sf.StationFileError, match=r"\(and 4 more\)"):
        sp.parse_master(rows)


def test_an_empty_operational_point_falls_back_to_the_plc() -> None:
    (row,) = sp.parse_master([master(era_uopid="")])
    assert row.key == ("ZZ00001", "ZZ00001")


def test_unknown_vocabulary_is_stored_not_refused() -> None:
    halt = parsed_master()[("ZZ00004", "ZZ00004")]
    assert halt.fields["best_tier"] == "T9_tier_of_tomorrow"
    assert halt.fields["nat_code_series"] == "ZZ_series_of_tomorrow"
    assert halt.flags == [("token_of_tomorrow", "with:colons"), ("bare_token", "")]


# ── the master: fields ─────────────────────────────────────────────────


def test_master_fields() -> None:
    fields = parsed_master()[("ZZ00001", "ZZ00001")].fields
    assert fields["name"] == "Exampleville Central"
    assert fields["alt_name"] == ["Exampleville", "Exampleville Hbf"]  # joined by `;` in the file
    assert fields["alt_name_text"] == "Exampleville; Exampleville Hbf"
    assert fields["op_type_all"] == ["station", "junction"]
    assert fields["iso2_all"] == ["ZZ"]
    assert fields["eva_all"] == ["9900001", "9900002"]
    assert fields["is_passenger"] is True
    assert fields["lat"] == 50.0
    assert fields["n_nap_feeds"] == 2
    assert fields["uic_merits"] == "9900001"
    assert fields["crd_source_tag"] == LICENCE_TAG
    assert fields["previous_plc"] is None


def test_a_row_without_a_position_has_none_not_zero() -> None:
    fields = parsed_master()[("ZZ00004", "ZZ00004")].fields
    assert fields["lat"] is None
    assert fields["lon"] is None
    assert fields["alt_name"] is None
    assert fields["alt_name_text"] is None
    assert fields["pos_src"] == "none"


def test_a_pipe_inside_an_alternative_name_is_part_of_the_name() -> None:
    # One bilingual name, as the register publishes it: not two names.
    fields = parsed_master()[("ZZ00002", "ZZ00002")].fields
    assert fields["alt_name"] == ["Sampleton-Midi | Sampelstad-Zuid"]
    # Searchable exactly as published: no doubled space around the pipe.
    assert fields["alt_name_text"] == "Sampleton-Midi | Sampelstad-Zuid"


def test_alternative_names_are_split_on_the_semicolon_only() -> None:
    both = sp.master_fields(master(era_alt_name="Exampleville-Midi | Voorbeeldstad-Zuid;Old Town"))
    assert both["alt_name"] == ["Exampleville-Midi | Voorbeeldstad-Zuid", "Old Town"]
    assert both["alt_name_text"] == "Exampleville-Midi | Voorbeeldstad-Zuid; Old Town"
    repeated = sp.master_fields(master(era_alt_name="Old Town;;Old Town"))
    assert repeated["alt_name"] == ["Old Town"]


# ── the master: MERITS candidates from one row ─────────────────────────


def test_merits_chosen_equals_calculated_is_one_candidate() -> None:
    (candidate,) = parsed_master()[("ZZ00001", "ZZ00001")].merits
    assert candidate == sp.MeritsCandidate(
        code="9900001",
        origin="Trainline = calculated",
        rule="Trainline and the calculation agree",
        confidence="high",
        sources=("TRAINLINE", "CALC"),
        check_digit="5",
        is_chosen=True,
    )


def test_merits_calculated_differs_and_conflict_values_are_kept() -> None:
    candidates = {c.code: c for c in parsed_master()[("ZZ00002", "ZZ00002")].merits}
    assert list(candidates) == ["9900002", "9900012", "9900022", "9900032"]
    assert [c.is_chosen for c in candidates.values()] == [True, False, False, False]

    chosen = candidates["9900002"]
    assert chosen.origin == "Trainline (calculated differs)"
    # `uic_merits_sources` and the check digit describe the calculation, not
    # this code: its one source is the label of the conflict value naming it.
    assert chosen.sources == ("Trainline_via_EVA",)
    assert chosen.check_digit is None

    calculated = candidates["9900012"]  # never withdrawn
    assert calculated.origin == sp.ORIGIN_CALCULATED
    assert calculated.sources == ("CALC",)
    assert calculated.check_digit == "7"

    # A conflict value is `code=labels`: the code alone is the candidate.
    assert candidates["9900022"].origin == sp.ORIGIN_CONFLICT
    assert candidates["9900022"].sources == ("ZZ_Rail",)
    assert candidates["9900032"].sources == ("ZZ_Rail", "ZZ_Timetable")
    assert all(c.code.isdigit() for c in candidates.values())


def test_merits_calculated_but_nothing_chosen() -> None:
    (candidate,) = parsed_master()[("ZZ00004", "ZZ00004")].merits
    assert candidate.code == "9900004"
    assert candidate.is_chosen is False
    assert candidate.origin == sp.ORIGIN_CALCULATED
    assert candidate.confidence == "low"


def test_merits_none_at_all() -> None:
    assert parsed_master()[("ZZ00003", "ZZOP03A")].merits == []


def test_a_conflict_value_equal_to_the_chosen_code_is_not_a_second_candidate() -> None:
    # The shape of the real cells: one item, `code=label`, the code being the
    # chosen one again.
    row = master(
        uic_merits="9900002",
        uic_merits_candidate="9900012",
        uic_merits_conflict_values="9900002=Trainline_via_EVA",
    )
    candidates = sp.merits_candidates(row)
    assert [(c.code, c.is_chosen) for c in candidates] == [("9900002", True), ("9900012", False)]
    assert candidates[0].sources == ("Trainline_via_EVA",)  # the label is not lost

    # A code that is already a candidate keeps the sources it has and gains the label.
    row = master(uic_merits_conflict_values="9900001=ZZ_Rail|9900077=ZZ_Rail")
    first, second = sp.merits_candidates(row)
    assert (first.code, first.sources) == ("9900001", ("TRAINLINE", "CALC", "ZZ_Rail"))
    assert (second.code, second.origin) == ("9900077", sp.ORIGIN_CONFLICT)


def test_a_conflict_value_splits_on_its_first_equals_sign_only() -> None:
    assert sp.split_conflict_value("9900022=ZZ_Rail") == ("9900022", ("ZZ_Rail",))
    assert sp.split_conflict_value("9900032=ZZ_Rail+ZZ_Timetable") == (
        "9900032",
        ("ZZ_Rail", "ZZ_Timetable"),
    )
    assert sp.split_conflict_value("9900042=a=b") == ("9900042", ("a=b",))
    # Never refused: a bare code has no label, and an item with no code is kept whole.
    assert sp.split_conflict_value("9900052") == ("9900052", ())
    assert sp.split_conflict_value("9900062=") == ("9900062", ())
    assert sp.split_conflict_value("=ZZ_Rail") == ("=ZZ_Rail", ())


# ── the master: codes and flags ────────────────────────────────────────


def test_a_multi_value_cell_gives_one_code_per_value() -> None:
    codes = parsed_master()[("ZZ00001", "ZZ00001")].codes
    assert codes == [
        sp.CodeValue("nap_CH_SBB", "9900001", True),
        sp.CodeValue("nap_CH_SBB", "9900001:0:1", False),
        # A feed#key value of an aggregate is kept whole.
        sp.CodeValue("nap_ES_regional", "ZZ_FEED#MC", True),
    ]


def test_nap_station_ids_is_not_a_provider_column() -> None:
    codes = parsed_master()[("ZZ00001", "ZZ00001")].codes
    assert "nap_station_ids" not in {c.source_key for c in codes}


def test_a_value_repeated_inside_a_cell_is_one_code() -> None:
    row = master(nap_CH_SBB="9900001|9900001")
    assert sp.provider_codes(row, ["nap_CH_SBB"]) == [sp.CodeValue("nap_CH_SBB", "9900001", True)]


def test_flags_split_into_token_and_payload() -> None:
    assert parsed_master()[("ZZ00001", "ZZ00001")].flags == [
        ("candidate_displaced_to", "ZZ00002"),
        ("plc_kind_national", ""),
    ]
    assert sp.row_flags(master(flags="a:1;a:1;b")) == [("a", "1"), ("b", "")]
    assert sp.row_flags(master(flags="")) == []
    # A payload listing several PLCs stays whole here: only the importer knows
    # which PLCs the file has, and writes one flag row per PLC.
    assert parsed_master()[("ZZ00002", "ZZ00002")].flags == [
        ("swap_partner", "ZZ00001"),
        ("candidate_displaced_to", "ZZ00001|ZZ00003"),
    ]


# ── links ──────────────────────────────────────────────────────────────


def test_a_link_row_resolves_by_plc_and_operational_point() -> None:
    link = sp.parse_link_row(full(sf.LINKS, link_rows()[0]))
    assert link.key == ("ZZ00001", "ZZ00001")
    assert link.fields["offline_station_id"] == "NAPST0001"  # the offline id, not a database id
    assert link.fields["feed_key"] == "CH_SBB"
    assert link.fields["asserted"] is True
    assert link.fields["distance_m"] == 12.5
    assert link.fields["code_series"] == "CH_service_point_number"
    assert link.fields["label"] == "Rail"


def test_a_non_asserted_link_keeps_its_free_text_method_and_tier() -> None:
    link = sp.parse_link_row(full(sf.LINKS, link_rows()[2]))
    assert link.fields["asserted"] is False
    assert link.fields["match_method"] == "code, refused on distance"
    assert link.fields["tier"] == "T2_name_distance"
    assert link.fields["note"] == "code points here, name does not"


def test_an_unmapped_stop_has_no_station_and_keeps_its_nearest_hint() -> None:
    stop = sp.parse_unmapped_row(full(sf.UNMAPPED, unmapped_rows()[0]))
    assert stop.key is None
    assert stop.fields["label"] == "Urban"
    assert stop.fields["nearest_plc"] == "ZZ00001"
    assert stop.fields["nearest_distance_m"] == 812.4
    assert stop.fields["asserted"] is False
    assert stop.fields["reason"] == "no_reference_within_300m"


def test_series_comes_from_the_links_only_when_they_state_exactly_one() -> None:
    links = [sp.parse_link_row(full(sf.LINKS, row)) for row in link_rows()]
    index = sp.link_series_index(links)
    station = index[("ZZ00001", "ZZ00001")]
    assert sp.series_for("9900001", station) == "CH_service_point_number"
    assert sp.series_for("9900001:0:1", station) is None  # the links do not name it
    assert sp.series_for("9900001", None) is None  # a station without links

    # Two series for one code value: ambiguous, so unknown rather than guessed.
    station["9900001"].add("SNCF_8digit")
    assert sp.series_for("9900001", station) is None


# ── registers ──────────────────────────────────────────────────────────


def parsed_crd() -> sp.CrdParse:
    return sp.parse_crd_locations(full(sf.CRD_LOCATIONS, row) for row in crd_location_rows())


def test_crd_locations_yield_the_columns_the_master_lacks() -> None:
    crd = parsed_crd()
    assert crd.rows == 6
    assert crd.extras[("ZZ00003", "ZZOP03B")] == {
        "plc_op_max_sep_m": 140,
        "is_passenger_src": "CRD_derived",
        "n_op_with_plc": 2,
        "crd_start": date(2019, 12, 15),
        "crd_end": date(2021, 6, 30),
    }
    assert crd.extras[("ZZ00001", "ZZ00001")]["crd_end"] is None


def test_one_crd_location_per_crd_key_not_per_operational_point() -> None:
    crd = parsed_crd()
    assert [loc["plc"] for loc in crd.locations] == ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00008"]
    assert crd.duplicates_dropped == 1  # the second operational point of ZZ00003
    yard = crd.locations[2]
    assert (yard["country"], yard["location_code"]) == ("ZZ", "00003")
    assert yard["start_validity"] == "2019-12-15"  # kept as published
    assert yard["end_validity"] == "2021-06-30"
    assert yard["freight_flag"] == "true"


def test_a_crd_location_takes_its_name_and_position_from_crd_only() -> None:
    central, _, _, retired = parsed_crd().locations
    # CRD's own name and position.
    assert (central["name"], central["lat"], central["lon"]) == ("Exampleville Central", 50.0, 4.0)
    # Retired in CRD: the spine fell back on ERA's name and position, and the
    # file carries no CRD value for them. The CRD register shows none.
    assert (retired["name"], retired["lat"], retired["lon"]) == (None, None, None)
    # Everything that is CRD's own stays.
    assert (retired["country"], retired["location_code"]) == ("ZZ", "00008")
    assert (retired["start_validity"], retired["end_validity"]) == ("2019-12-15", "2022-12-10")
    assert retired["responsible_im"] == "ZZ Infra"


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ({"name_src": "CRD", "pos_src": "CRD"}, ("Exampleville Central", 50.0, 4.0)),
        ({"name_src": "ERA", "pos_src": "CRD"}, (None, 50.0, 4.0)),
        ({"name_src": "CRD", "pos_src": "ERA"}, ("Exampleville Central", None, None)),
        ({"name_src": "CRD", "pos_src": "none"}, ("Exampleville Central", None, None)),
        # A source the file does not state is not assumed to be CRD.
        ({"name_src": "", "pos_src": ""}, (None, None, None)),
    ],
)
def test_the_name_and_the_position_of_a_crd_location_are_decided_separately(
    sources: dict[str, str], expected: tuple[str | None, float | None, float | None]
) -> None:
    rows = [full(sf.CRD_LOCATIONS, {**crd_location_rows()[0], **sources})]
    (location,) = sp.parse_crd_locations(rows).locations
    assert (location["name"], location["lat"], location["lon"]) == expected


def test_a_retired_location_is_the_same_whichever_operational_point_comes_first() -> None:
    # Several ERA operational points of one retired CRD location, each with its
    # own ERA name and position: the register row must not depend on file order.
    retired = crd_location_rows()[5]
    north = {**retired, "uopid": "ZZOP08A", "name": "Formerton Sidings North", "lat": "50.80"}
    south = {**retired, "uopid": "ZZOP08B", "name": "Formerton Sidings South", "lat": "50.81"}

    def locations(*rows: dict[str, str]) -> list[dict[str, object]]:
        return sp.parse_crd_locations(full(sf.CRD_LOCATIONS, row) for row in rows).locations

    assert locations(north, south) == locations(south, north)
    assert len(locations(north, south)) == 1


def test_an_era_only_row_yields_no_crd_location() -> None:
    assert "ZZ00004" not in {loc["plc"] for loc in parsed_crd().locations}
    assert ("ZZ00004", "ZZ00004") in parsed_crd().extras


def test_subsidiary_codes_are_one_row_per_value() -> None:
    assert parsed_crd().subsidiaries == [
        {"plc": "ZZ00001", "subsidiary_type": "crd_rl100", "code": "ZEXC"},
        {"plc": "ZZ00001", "subsidiary_type": "crd_sncf_codes", "code": "99001"},
        {"plc": "ZZ00001", "subsidiary_type": "crd_sncf_codes", "code": "99002"},
        {"plc": "ZZ00001", "subsidiary_type": "crd_dium_codes", "code": "990001"},
    ]


def test_an_unreadable_validity_date_is_counted_not_fatal() -> None:
    rows = [full(sf.CRD_LOCATIONS, {**crd_location_rows()[0], "crd_end": "mid-December"})]
    crd = sp.parse_crd_locations(rows)
    assert crd.dates_unparsed == 1
    assert crd.extras[("ZZ00001", "ZZ00001")]["crd_end"] is None
    assert crd.locations[0]["end_validity"] == "mid-December"  # the register keeps the text


def test_telref_is_one_point_per_plc_and_operational_point() -> None:
    rows = [full(sf.ERA_TELREF, row) for row in telref_rows()]
    rows.append(dict(rows[0]))  # repeated
    rows.append(full(sf.ERA_TELREF, {"plc": "ZZ00005", "name": "No uopid"}))
    rows.append(full(sf.ERA_TELREF, {"name": "No PLC"}))
    telref = sp.parse_telref(rows)
    assert telref.rows == 8
    assert len(telref.points) == 6
    assert telref.duplicates_dropped == 1
    assert telref.rows_without_plc == 1
    assert telref.points[-1]["uopid"] == "ZZ00005"  # the PLC is the sentinel
    assert telref.points[4]["lat"] is None
