"""The shapes of the five offline station files (app/master/station_files.py).

The headers in the module are a transcription of
docs/station-offline-file-shapes.md. The files themselves are never in the
repository, so that document is the only ground truth available here: the
first test reads it and fails if the transcription drifts.

Every row in these tests is invented. The PLC prefix `ZZ` does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.master import station_files as sf
from tests.station_fixtures import csv_bytes

SHAPES_DOC = Path(__file__).resolve().parents[2] / "docs" / "station-offline-file-shapes.md"

# Section number of each shape in the document.
DOC_SECTION = {
    sf.MASTER: 1,
    sf.LINKS: 2,
    sf.UNMAPPED: 3,
    sf.CRD_LOCATIONS: 4,
    sf.ERA_TELREF: 5,
}


def _doc_header(section: int) -> list[str]:
    """The first fenced block of a numbered section: the file's header line."""
    text = SHAPES_DOC.read_text(encoding="utf-8")
    match = re.search(rf"^## {section}\. .*?^```\r?\n(.*?)\r?\n```", text, re.DOTALL | re.MULTILINE)
    assert match, f"section {section} has no fenced header"
    return match.group(1).strip().split(",")


def write_csv(path: Path, header: list[str] | tuple[str, ...], *rows: dict[str, str]) -> Path:
    path.write_bytes(csv_bytes(header, *rows))
    return path


# ── the transcription ──────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", sorted(DOC_SECTION))
def test_headers_are_the_ones_the_shapes_document_gives(fmt: str) -> None:
    assert list(sf.FILE_SHAPES[fmt]) == _doc_header(DOC_SECTION[fmt])


@pytest.mark.parametrize(
    ("fmt", "columns"),
    [
        (sf.MASTER, 73),
        (sf.LINKS, 24),
        (sf.UNMAPPED, 23),
        (sf.CRD_LOCATIONS, 65),
        (sf.ERA_TELREF, 36),
    ],
)
def test_column_counts(fmt: str, columns: int) -> None:
    assert len(sf.FILE_SHAPES[fmt]) == columns
    assert len(set(sf.FILE_SHAPES[fmt])) == columns


def test_crd_locations_starts_with_the_telref_columns() -> None:
    assert sf.FILE_SHAPES[sf.CRD_LOCATIONS][:36] == sf.FILE_SHAPES[sf.ERA_TELREF]


def test_a_build_needs_all_five_shapes() -> None:
    assert set(sf.REQUIRED_FORMATS) == set(DOC_SECTION)
    assert set(sf.FORMAT_LABELS) == set(DOC_SECTION)


def test_the_master_has_sixteen_provider_columns() -> None:
    columns = sf.provider_columns()
    assert len(columns) == 16
    assert "nap_station_ids" not in columns
    assert {"nap_FR_regional", "nap_ES_regional", "nap_CH_SBB_non_rail_members"} <= set(columns)
    # Taken from the header given, not from a fixed list.
    assert sf.provider_columns(("plc", "nap_XX_NEW", "nap_station_ids")) == ("nap_XX_NEW",)


# ── header check ───────────────────────────────────────────────────────


def test_a_matching_header_passes_in_any_column_order() -> None:
    header = list(reversed(sf.FILE_SHAPES[sf.LINKS]))
    check = sf.check_header(sf.LINKS, header)
    assert check.ok
    assert check.describe() == "header matches"


def test_missing_unexpected_and_repeated_columns_are_named() -> None:
    header = [c for c in sf.FILE_SHAPES[sf.UNMAPPED] if c not in ("label", "reason")]
    header += ["surprise", "iso2"]
    check = sf.check_header(sf.UNMAPPED, header)
    assert not check.ok
    assert check.missing == ("label", "reason")
    assert check.unexpected == ("surprise",)
    assert check.duplicated == ("iso2",)
    assert check.describe() == (
        "missing columns: label, reason; unexpected columns: surprise; repeated columns: iso2"
    )


def test_a_file_of_another_shape_is_not_guessed_at() -> None:
    # A links file offered to the master source: reported, never reinterpreted.
    check = sf.check_header(sf.MASTER, list(sf.FILE_SHAPES[sf.LINKS]))
    assert not check.ok
    assert "era_name" in check.missing
    assert "station_id" in check.unexpected


def test_an_unknown_format_is_an_error() -> None:
    with pytest.raises(sf.StationFileError, match="not one of the station file shapes"):
        sf.check_header("gtfs", ["a"])


# ── reading a header off disk ──────────────────────────────────────────


def test_read_header_drops_the_bom(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "telref.csv", sf.FILE_SHAPES[sf.ERA_TELREF])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    header = sf.read_header(path)
    assert header[0] == "plc"
    assert header == list(sf.FILE_SHAPES[sf.ERA_TELREF])


def test_read_header_without_a_bom_works_too(tmp_path: Path) -> None:
    path = tmp_path / "plain.csv"
    path.write_text("plc, era_uopid \nZZ00001,ZZ00001\n", encoding="utf-8")
    assert sf.read_header(path) == ["plc", "era_uopid"]


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(sf.StationFileError, match="empty"):
        sf.read_header(path)


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "latin1.csv"
    path.write_bytes("plc,nom\nZZ00001,Gare d\xe9part\n".encode("latin-1"))
    with pytest.raises(sf.StationFileError, match="not UTF-8"):
        sf.read_header(path)


def test_require_shape_returns_the_header_or_says_what_is_wrong(tmp_path: Path) -> None:
    good = write_csv(tmp_path / "links.csv", sf.FILE_SHAPES[sf.LINKS])
    assert sf.require_shape(sf.LINKS, good) == list(sf.FILE_SHAPES[sf.LINKS])

    bad = write_csv(tmp_path / "bad.csv", [*sf.FILE_SHAPES[sf.LINKS][:-1], "extra"])
    with pytest.raises(sf.StationFileError) as exc:
        sf.require_shape(sf.LINKS, bad)
    message = str(exc.value)
    assert "station links" in message
    assert "missing columns: crd_source_tag" in message
    assert "unexpected columns: extra" in message
