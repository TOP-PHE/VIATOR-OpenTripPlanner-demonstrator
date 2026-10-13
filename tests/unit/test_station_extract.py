"""The stop extract for the station module (app/station_extract.py).

Every fixture is invented: stop ids with `ZZ`, numbers made of 9s, made-up
names. The archives are built here, in the test, never read from a feed.

`_msmm_check` re-states MSMM's acceptance rules (MSMM repository,
`app/master/feed_intake.py::check_extract`, docs/service-design.md 22.2
point 6) so that every extract written in these tests is shown to be one
MSMM takes. The real MSMM function is run on a generated extract by
`scripts/check_extract_with_msmm.py`, by hand, outside CI.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import re
import tracemalloc
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from app import provider_catalogue, station_extract
from app.station_extract import (
    EXTRACTOR_VERSION,
    ExtractError,
    build_extract,
    feed_key_for,
    warm_session_extracts,
)
from app.station_extract_gtfs import rail_mode
from app.station_extract_netex import _lambert93_to_wgs84

SHA_A = "a" * 64
SHA_B = "b" * 64
WHEN = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
NOW_TEXT = "2026-10-13T09:30:00+00:00"
DATA = Path(station_extract.__file__).parent / "data"


# ── MSMM's rules, re-stated for the tests ─────────────────────────────────

_COLUMNS = [
    "format_version",
    "feed_key",
    "feed_version",
    "extracted_at",
    "extractor_version",
    "stop_key",
    "stop_name",
    "lat",
    "lon",
    "iso2_point",
    "modes",
    "n_calls",
    "operators",
    "codes",
    "provider_id",
    "provider_label",
    "provider_country_iso",
    "provider_provenance",
]
_SAME = (
    "feed_version",
    "extracted_at",
    "extractor_version",
    "provider_id",
    "provider_label",
    "provider_country_iso",
    "provider_provenance",
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NUMBER = re.compile(r"-?\d{1,3}(?:\.\d{1,15})?", re.ASCII)
_CODE_FIELD = re.compile(r"[A-Za-z@][A-Za-z0-9_.:@/-]{0,63}", re.ASCII)
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", re.ASCII
)
_FEED_KEY = re.compile(station_extract.FEED_KEY_PATTERN, re.ASCII)


def _text_ok(value: str, *, empty: bool = False) -> bool:
    return (empty or bool(value)) and len(value) <= 200 and not _CONTROL.search(value)


def _items_ok(value: str) -> list[str]:
    items = value.split("|")
    assert len(items) <= 500, "more than 500 items"
    assert all(_text_ok(item) for item in items), "a bad item"
    assert len(set(items)) == len(items), "an item twice"
    return items


def _msmm_check(path: Path, feed_key: str) -> list[dict[str, str]]:
    """MSMM's `check_extract`, re-stated: returns the rows, fails on a fault."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    assert lines[-1] == "", "the file ends with a newline"
    lines = lines[:-1]
    assert not lines[0].startswith("﻿"), "no byte order mark"
    assert next(csv.reader([lines[0]])) == _COLUMNS, "the header"
    rows: list[dict[str, str]] = []
    keys: set[str] = set()
    for line in lines[1:]:
        assert len(line) <= 1024 * 1024, "a line past 1 MiB"
        record = next(csv.reader([line], strict=True))
        assert len(record) == 18, "18 fields"
        assert all(len(cell) <= 131_072 for cell in record), "a cell past 131072"
        row = dict(zip(_COLUMNS, record, strict=True))
        assert row["format_version"] == "1"
        assert row["feed_key"] == feed_key
        assert _FEED_KEY.fullmatch(feed_key)
        assert re.fullmatch(r"[0-9a-f]{64}", row["feed_version"])
        assert _TIMESTAMP.fullmatch(row["extracted_at"])
        datetime.fromisoformat(row["extracted_at"])
        for name in ("extractor_version", "provider_id", "stop_key", "stop_name"):
            assert _text_ok(row[name]), name
        assert _text_ok(row["provider_label"], empty=True)
        for name in ("iso2_point", "provider_country_iso"):
            assert row[name] == "" or re.fullmatch(r"[A-Z]{2}", row[name]), name
        assert row["provider_provenance"] in ("", "nap", "official", "community")
        for name, bound in (("lat", 90.0), ("lon", 180.0)):
            assert _NUMBER.fullmatch(row[name]), name
            assert math.isfinite(float(row[name]))
            assert abs(float(row[name])) <= bound
        assert set(_items_ok(row["modes"])) <= {"RAIL_MAIN", "RAIL_SUBURBAN"}
        assert re.fullmatch(r"\d{1,10}", row["n_calls"])
        assert 1 <= int(row["n_calls"]) <= 10**9
        _items_ok(row["operators"])
        for code in _items_ok(row["codes"]):
            field, sep, value = code.partition("=")
            assert sep, "a code item"
            assert _CODE_FIELD.fullmatch(field), "a code field"
            assert value, "a code value"
        if rows:
            assert all(row[name] == rows[0][name] for name in _SAME), "same on every row"
        assert row["stop_key"] not in keys, "a stop given twice"
        keys.add(row["stop_key"])
        rows.append(row)
    assert rows, "at least one row"
    assert len(rows) <= 200_000
    return rows


def _by_key(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["stop_key"]: row for row in rows}


# ── Invented archives ─────────────────────────────────────────────────────


def _zip(path: Path, files: Mapping[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


AGENCY = "agency_id,agency_name\nZZR,ZZ Rail\nZZB,ZZ Bus\n"
STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station,platform_code\n"
    'ZZ:S1,,"Gare, ZZ Nord",48.8000000,2.3000000,1,,\n'
    "ZZ:S1:P1,9901,ZZ Nord quai 1,48.80001,2.30001,0,ZZ:S1,1\n"
    "ZZ:S1:P2,,ZZ Nord quai 2,48.80002,2.30002,0,ZZ:S1,2\n"
    "ZZ:S2,,ZZ Sud,-0.00005,9.9,0,,\n"
    "ZZ:B1,,ZZ Arret bus,48.7,2.2,0,,\n"
    "ZZ:S3,,ZZ Ville,48.6,2.1,0,,\n"
)
ROUTES = (
    "route_id,agency_id,route_short_name,route_type\n"
    "R1,ZZR,IC,2\nR2,ZZB,B,3\nR3,ZZR,S,400\nR4,ZZR,SEV,714\n"
)
TRIPS = "route_id,service_id,trip_id\nR1,W,T1\nR1,W,T2\nR2,W,T3\nR3,W,T4\nR4,W,T5\n"
STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
    "T1,08:00:00,08:00:00,ZZ:S1:P1,1,,\n"
    "T1,09:00:00,09:00:00,ZZ:S2,2,,\n"
    "T2,10:00:00,10:00:00,ZZ:S1:P2,1,0,0\n"
    "T2,10:30:00,10:30:00,ZZ:S3,2,1,1\n"
    "T2,11:00:00,11:00:00,ZZ:S2,3,,\n"
    "T3,08:00:00,08:00:00,ZZ:B1,1,,\n"
    "T3,08:10:00,08:10:00,ZZ:S1:P1,2,,\n"
    "T4,07:00:00,07:00:00,ZZ:S3,1,,\n"
    "T4,07:20:00,07:20:00,ZZ:S1:P1,2,,\n"
    "T5,06:00:00,06:00:00,ZZ:B1,1,,\n"
)


def _gtfs(tmp_path: Path, name: str = "zz.zip", **overrides: str) -> Path:
    files = {
        "agency.txt": AGENCY,
        "stops.txt": STOPS,
        "routes.txt": ROUTES,
        "trips.txt": TRIPS,
        "stop_times.txt": STOP_TIMES,
    }
    files.update({f"{key}.txt": value for key, value in overrides.items()})
    return _zip(tmp_path / name, files)


def _provider(fmt: str = "gtfs", **timetable: Any) -> dict[str, Any]:
    tt = {"format": fmt, "source": "url", "url": "https://zz.example.invalid/feeds/zz.zip"}
    tt.update(timetable)
    return {"id": "ZZRAIL", "label": "ZZ Rail", "country_iso": "ZZ", "timetable": tt}


def _build(
    provider: dict[str, Any], archive: Path, tmp_path: Path, sha: str = SHA_A
) -> station_extract.ExtractResult:
    return build_extract(provider, archive, sha, extracted_at=WHEN, cache_dir=tmp_path / "x")


# ── Feed keys (MSMM 22.2, point 6) ────────────────────────────────────────

# A copy of MSMM's reference rule (MSMM repository, app/master/feed_intake.py,
# `resolver_identity`, `url_identity` and `RESOLVER_IDENTITY_FIELDS`, as of
# MSMM main after pull request 3a), never imported across repositories: the
# test below shows VIATOR's derivation gives the same key on the same configs.
_MSMM_FIELDS = {
    "udata": ("api", "dataset_id", "title_regex"),
    "permalink": ("url",),
    "dated": ("url",),
    "json_api": ("url", "items", "match", "sort", "download", "url_field"),
}


def _msmm_sixteen_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _msmm_resolver_identity(resolver: dict[str, object]) -> str:
    kind = str(resolver["type"])
    if kind == "tdg":
        return f"nap:tdg:{int(str(resolver['resource_id']))}"
    fields = {name: resolver[name] for name in _MSMM_FIELDS[kind] if name in resolver}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"nap:{kind}:{_msmm_sixteen_hex(canonical)}"


def _msmm_url_identity(url: str) -> str:
    parts = urlsplit(url)
    pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
    query = f"?{urlencode(pairs)}" if pairs else ""
    return f"url:{_msmm_sixteen_hex((parts.hostname or '') + parts.path + query)}"


ZZ_RESOLVERS: list[dict[str, Any]] = [
    {"type": "tdg", "dataset_id": "9" * 24, "resource_id": 99999},
    {
        "type": "udata",
        "api": "https://zz.example.invalid/api/1",
        "dataset_id": "zz-999",
        "title_regex": r"^zz-\d{8}\.zip$",
    },
    {"type": "permalink", "url": "https://zz.example.invalid/zz_{timetable_year}/permalink"},
    {"type": "dated", "url": "https://zz.example.invalid/{date}_zz.zip", "max_days_back": 9},
    {
        "type": "json_api",
        "url": "https://zz.example.invalid/api/list",
        "items": "data.files",
        "match": {"name": "^zz"},
        "sort": "date",
        "download": "https://zz.example.invalid/api/file/{id}",
    },
    {
        "type": "json_api",
        "url": "https://zz.example.invalid/api/list",
        "items": "",
        "match": {"name": "^zz"},
        "url_field": "href",
    },
]


@pytest.mark.parametrize("resolver", ZZ_RESOLVERS, ids=lambda r: str(r["type"]))
def test_nap_key_equals_msmm_rule(resolver: dict[str, Any]) -> None:
    from app.feed_resolvers import validate_resolver

    key = feed_key_for(_provider(source="nap", resolver=resolver))
    assert key == _msmm_resolver_identity(validate_resolver(resolver, "r"))
    assert key is not None
    assert _FEED_KEY.fullmatch(key)


@pytest.mark.parametrize(
    "url",
    [
        "https://ZZ.Example.invalid:8443/feeds/zz.zip#part",
        "https://zz.example.invalid/download?file=zz-b.zip&lang=zz",
        "https://zz.example.invalid/download?lang=zz&file=zz-b.zip",
        "http://zz.example.invalid/feeds/zz.zip?",
    ],
)
def test_url_key_equals_msmm_rule(url: str) -> None:
    assert feed_key_for(_provider(url=url)) == _msmm_url_identity(url)


def test_url_key_rules() -> None:
    plain = feed_key_for(_provider(url="https://zz.example.invalid/feeds/zz.zip"))
    # host case, scheme, port and fragment do not count; query order does not count
    assert plain == feed_key_for(_provider(url="http://ZZ.EXAMPLE.invalid:99/feeds/zz.zip#x"))
    one = feed_key_for(_provider(url="https://zz.example.invalid/d?file=a.zip&v=9"))
    assert one == feed_key_for(_provider(url="https://zz.example.invalid/d?v=9&file=a.zip"))
    # the query names the file: two files, two keys
    assert one != feed_key_for(_provider(url="https://zz.example.invalid/d?file=b.zip&v=9"))
    # the path is kept as given
    assert plain != feed_key_for(_provider(url="https://zz.example.invalid/Feeds/zz.zip"))


def test_pinned_keys() -> None:
    """Literal keys, so that a change of the rule in both copies is seen."""
    tdg = {"type": "tdg", "dataset_id": "9" * 24, "resource_id": "99999"}
    assert feed_key_for(_provider(source="nap", resolver=tdg)) == "nap:tdg:99999"
    assert (
        feed_key_for(_provider(url="https://zz.example.invalid/feeds/zz.zip"))
        == f"url:{_msmm_sixteen_hex('zz.example.invalid/feeds/zz.zip')}"
    )
    permalink = {"type": "permalink", "url": "https://zz.example.invalid/{timetable_year}"}
    expected = _msmm_sixteen_hex('{"url":"https://zz.example.invalid/{timetable_year}"}')
    assert feed_key_for(_provider(source="nap", resolver=permalink)) == f"nap:permalink:{expected}"


def test_dated_key_ignores_max_days_back() -> None:
    base = {"type": "dated", "url": "https://zz.example.invalid/{date}_zz.zip"}
    one = feed_key_for(_provider(source="nap", resolver={**base, "max_days_back": 9}))
    two = feed_key_for(_provider(source="nap", resolver={**base, "max_days_back": 19}))
    assert one == two


def test_keys_of_other_sources() -> None:
    upload = {"format": "gtfs", "source": "upload"}
    provider = {"id": "ZZUP", "timetable": upload}
    assert feed_key_for(provider) is None
    assert feed_key_for(provider, upload_sha256=SHA_B) == f"upload:{SHA_B}"
    assert feed_key_for(provider, upload_sha256="not-a-sha") is None
    derived = {
        "format": "gtfs",
        "source": "cross_border_filter",
        "derived_from": {"session_id": "zz", "provider_id": "ZZNAT"},
    }
    assert feed_key_for({"id": "ZZX", "timetable": derived}, upload_sha256=SHA_B) is None
    assert feed_key_for(_provider(source="nap", resolver={"type": "zz"})) is None
    assert feed_key_for({"id": "ZZ", "timetable": "nope"}) is None
    assert feed_key_for(_provider(url="")) is None


def _catalogue_providers() -> list[dict[str, Any]]:
    providers = [provider_catalogue.session_provider(e) for e in provider_catalogue.load().values()]
    sources = json.loads((DATA / "eu19_nap_sources.json").read_text(encoding="utf-8"))
    providers += [
        {"id": pid, "timetable": tt} for pid, tt in sources.items() if not pid.startswith("_")
    ]
    return providers


def test_catalogue_keys_match_msmm_and_are_distinct() -> None:
    providers = _catalogue_providers()
    keys = [feed_key_for(p) for p in providers]
    assert all(key is not None and _FEED_KEY.fullmatch(key) for key in keys)
    assert len(set(keys)) == len(keys)
    kinds = {str(key).split(":")[0] + ":" + str(key).split(":")[1] for key in keys}
    # every resolver type and the url source occur in the shipped catalogues
    assert {"nap:tdg", "nap:udata", "nap:permalink", "nap:dated", "nap:json_api"} <= kinds
    assert any(str(key).startswith("url:") for key in keys)


# ── Rail and station level ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route_type", "mode"),
    [
        ("2", "RAIL_MAIN"),
        ("100", "RAIL_MAIN"),
        ("108", "RAIL_MAIN"),
        ("109", "RAIL_SUBURBAN"),
        ("110", "RAIL_MAIN"),
        ("117", "RAIL_MAIN"),
        ("300", "RAIL_SUBURBAN"),
        ("400", "RAIL_SUBURBAN"),
        ("403", "RAIL_SUBURBAN"),
        ("404", "RAIL_SUBURBAN"),
        ("99", None),
        ("118", None),
        ("401", None),
        ("714", None),
        ("3", None),
        ("", None),
        ("rail", None),
    ],
)
def test_rail_route_types(route_type: str, mode: str | None) -> None:
    assert rail_mode(route_type) == mode


def test_gtfs_extract(tmp_path: Path) -> None:
    result = _build(_provider(), _gtfs(tmp_path), tmp_path)
    key = feed_key_for(_provider())
    assert key is not None
    rows = _by_key(_msmm_check(result.path_gz, key))
    assert set(rows) == {"ZZ:S1", "ZZ:S2", "ZZ:S3"}  # the bus stop is not sent
    north = rows["ZZ:S1"]
    assert north["stop_name"] == "Gare, ZZ Nord"  # a quoted field
    assert north["modes"] == "RAIL_MAIN|RAIL_SUBURBAN"
    assert north["n_calls"] == "3"  # T1, T2, T4; not the bus T3
    assert north["operators"] == "ZZ Rail"
    assert north["codes"].split("|") == [
        "stop_id=ZZ:S1",
        "stop_id=ZZ:S1:P1",
        "stop_code=9901",
        "platform_code=1",
        "stop_id=ZZ:S1:P2",
        "platform_code=2",
    ]
    assert (north["lat"], north["lon"]) == ("48.8000000", "2.3000000")
    assert north["iso2_point"] == "FR"
    south = rows["ZZ:S2"]
    assert (south["lat"], south["lon"]) == ("-0.0000500", "9.9000000")  # never 5e-05
    assert south["iso2_point"] == ""
    assert rows["ZZ:S3"]["modes"] == "RAIL_SUBURBAN"
    assert rows["ZZ:S3"]["n_calls"] == "1"  # T2 only passes (pickup and drop-off 1)
    assert north["feed_version"] == SHA_A
    assert north["extracted_at"] == NOW_TEXT
    assert north["extractor_version"] == EXTRACTOR_VERSION
    assert (north["provider_id"], north["provider_label"]) == ("ZZRAIL", "ZZ Rail")
    assert (north["provider_country_iso"], north["provider_provenance"]) == ("ZZ", "")
    assert result.rows == 3
    assert result.feed_key == key
    assert result.feed_version == SHA_A
    body = result.path_gz.read_bytes()
    assert result.bytes == len(body)
    assert result.body_sha256 == hashlib.sha256(body).hexdigest()
    assert result.extractor_version == EXTRACTOR_VERSION


def test_quoting_round_trips(tmp_path: Path) -> None:
    stops = STOPS.replace('"Gare, ZZ Nord"', '"Gare ""ZZ"", Nord"')
    result = _build(_provider(), _gtfs(tmp_path, stops=stops), tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(_provider()))))
    assert rows["ZZ:S1"]["stop_name"] == 'Gare "ZZ", Nord'


def test_gtfs_without_agency_and_with_padding(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "pad.zip",
        {
            "zz/stops.txt": "﻿stop_id ,stop_name,stop_lat,stop_lon\n ZZ:9 ,ZZ Neuf,45,9\n",
            "zz/routes.txt": "route_id,route_type\nR, 2 \n",
            "zz/trips.txt": "route_id,trip_id\nR,T\n",
            "zz/stop_times.txt": "trip_id,stop_id\nT,ZZ:9\n",
        },
    )
    result = _build(_provider(), archive, tmp_path)
    rows = _msmm_check(result.path_gz, str(feed_key_for(_provider())))
    assert rows[0]["stop_key"] == "ZZ:9"
    assert rows[0]["operators"] == "VIATOR:provider:ZZRAIL"  # no agency at all: the marker


def test_single_agency_route_without_agency_id(tmp_path: Path) -> None:
    archive = _gtfs(
        tmp_path,
        agency="agency_id,agency_name\nZZS,ZZ Seule\n",
        routes="route_id,agency_id,route_type\nR1,,2\nR3,,400\n",
    )
    rows = _msmm_check(
        _build(_provider(), archive, tmp_path).path_gz, str(feed_key_for(_provider()))
    )
    assert {row["operators"] for row in rows} == {"ZZ Seule"}


# ── Cleaning (MSMM 22.2, point 6: what VIATOR cleans or drops) ────────────


def test_cleaning_rules(tmp_path: Path) -> None:
    long_name = "ZZ " + "n" * 250
    stops = (
        "stop_id,stop_code,stop_name,stop_lat,stop_lon,parent_station,platform_code\n"
        f"ZZ:S1,99|9,{long_name},48.8,2.3,,\n"
        "ZZ:S1:P1,9901,quai,48.8,2.3,ZZ:S1,1\n"
        "ZZ:S1:P2,9901,quai,48.8,2.3,ZZ:S1,1\n"
        f"ZZ:S1:P3,{'9' * 300},quai,48.8,2.3,ZZ:S1,\n"
        'ZZ:S2,,"ZZ\x01Sud",45,9,,\n'
        "ZZ:S3,,,45,9,,\n"
        "ZZ:S4,,ZZ sans position,,,,\n"
    )
    agency = "agency_id,agency_name\nZZR,ZZ|Rail\nZZQ,ZZ Q\n"
    routes = "route_id,agency_id,route_type\nR1,ZZR,2\nR2,ZZQ,2\n"
    trips = "route_id,trip_id\nR1,T1\nR2,T2\n"
    stop_times = (
        "trip_id,stop_id\nT1,ZZ:S1:P1\nT2,ZZ:S1:P2\nT2,ZZ:S2\nT2,ZZ:S3\nT2,ZZ:S4\nT1,ZZ:S4\n"
    )
    archive = _gtfs(
        tmp_path, stops=stops, agency=agency, routes=routes, trips=trips, stop_times=stop_times
    )
    result = _build(_provider(), archive, tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(_provider()))))
    assert set(rows) == {"ZZ:S1", "ZZ:S2"}
    north = rows["ZZ:S1"]
    assert len(north["stop_name"]) == 200
    assert north["stop_name"].startswith("ZZ n")
    codes = north["codes"].split("|")
    assert codes == [
        "stop_id=ZZ:S1",
        "stop_id=ZZ:S1:P1",
        "stop_code=9901",
        "platform_code=1",
        "stop_id=ZZ:S1:P2",
        "stop_id=ZZ:S1:P3",
    ]
    assert north["operators"] == "ZZ Q"  # the agency named with a | is dropped
    assert rows["ZZ:S2"]["stop_name"] == "ZZ Sud"  # a control character becomes a space
    assert result.dropped == {
        "label_cut": 1,
        "code_bar": 1,
        "code_long": 1,
        "operator_bar": 1,
        "no_name": 1,
        "no_position": 1,
    }


def test_station_with_only_dropped_operators_takes_provider(tmp_path: Path) -> None:
    agency = "agency_id,agency_name\nZZR,ZZ|Rail\nZZB,ZZ Bus\n"
    result = _build(_provider(), _gtfs(tmp_path, agency=agency), tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(_provider()))))
    assert rows["ZZ:S1"]["operators"] == "VIATOR:provider:ZZRAIL"
    assert result.dropped["operator_from_provider"] == 3


def test_more_cleaning(tmp_path: Path) -> None:
    stops = STOPS.replace("ZZ:S1:P1,9901,", "ZZ:S1:P1,99\x0201,").replace(
        "ZZ:S3,,ZZ Ville,48.6,", "ZZ:S3,,ZZ Ville,95.0,"
    )
    agency = "agency_id,agency_name\nZZR,ZZ Rail\nZZQ,ZZ  Rail\nZZB,ZZ Bus\n"
    routes = ROUTES + "R5,ZZQ,IC,2\n"
    trips = TRIPS + "R5,W,T6\n"
    stop_times = STOP_TIMES + "T6,12:00:00,12:00:00,ZZ:S1:P1,1,,\n"
    archive = _gtfs(
        tmp_path, stops=stops, agency=agency, routes=routes, trips=trips, stop_times=stop_times
    )
    result = _build(_provider(), archive, tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(_provider()))))
    assert set(rows) == {"ZZ:S1", "ZZ:S2"}  # a latitude of 95 is no position
    assert rows["ZZ:S1"]["operators"] == "ZZ Rail"  # two spellings, one item
    assert "stop_code=99\x0201" not in rows["ZZ:S1"]["codes"]
    assert result.dropped["code_control"] == 1
    assert result.dropped["duplicate_item"] == 1
    assert result.dropped["no_position"] == 1


def test_netex_non_rail_journeys_dropped_early(tmp_path: Path) -> None:
    from app.station_extract_netex import _Archive, _read_journeys

    archive = _zip(
        tmp_path / "epip.zip", {"zz.xml": _delivery(EPIP_SITE + EPIP_SERVICE + EPIP_TIMETABLE)}
    )
    with zipfile.ZipFile(archive) as zf:
        reader = _Archive(zf)
        by_pattern = _read_journeys(reader).by_pattern
    # the bus journey (own mode) and none of the line-mode ones that are not rail
    assert set(by_pattern) == {"ZZ:SJP:R", "ZZ:SJP:X"}
    bus_line = EPIP_TIMETABLE.replace(
        '<ServiceJourney id="ZZ:SJ:3"><TransportMode>bus</TransportMode>',
        '<ServiceJourney id="ZZ:SJ:3"><LineRef ref="ZZ:Line:B"/>',
    )
    archive = _zip(tmp_path / "b.zip", {"zz.xml": _delivery(EPIP_SITE + EPIP_SERVICE + bus_line)})
    with zipfile.ZipFile(archive) as zf:
        reader = _Archive(zf)
        by_pattern = _read_journeys(reader).by_pattern
    assert "ZZ:SJP:B" not in by_pattern  # the Line's bus mode drops it before the patterns


def test_codes_capped_at_500(tmp_path: Path) -> None:
    children = "".join(f"ZZ:S1:Q{n},,quai,48.8,2.3,1,ZZ:S1,\n" for n in range(600))
    stops = STOPS + children
    result = _build(_provider(), _gtfs(tmp_path, stops=stops), tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(_provider()))))
    codes = rows["ZZ:S1"]["codes"].split("|")
    assert len(codes) == 500
    assert codes[0] == "stop_id=ZZ:S1"
    assert result.dropped["codes_over_cap"] == 606 - 500


def test_bad_code_fields_dropped() -> None:
    from collections import Counter

    dropped: Counter[str] = Counter()
    assert station_extract._code_item("bad key", "9", dropped) is None
    assert station_extract._code_item("PrivateCode:tipo-é", "9", dropped) is None
    assert station_extract._code_item("1zz", "9", dropped) is None
    assert station_extract._code_item("z" * 65, "9", dropped) is None
    assert station_extract._code_item("@id", "ZZ:9", dropped) == "@id=ZZ:9"
    assert station_extract._code_item("Quay/PrivateCode:zz.9", "9", dropped) is not None
    assert dropped == {"code_field": 4}


def test_decimal_format() -> None:
    assert station_extract._decimal(-5e-05) == "-0.0000500"
    assert station_extract._decimal(1e-9) == "0.0000000"
    assert station_extract._decimal(179.123456789) == "179.1234568"


# ── NeTEx ─────────────────────────────────────────────────────────────────

NS = 'xmlns="http://www.netex.org.uk/netex" xmlns:gml="http://www.opengis.net/gml/3.2"'


def _delivery(frames: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><PublicationDelivery {NS} version="1.1">'
        f'<dataObjects><CompositeFrame id="ZZ:CF"><frames>{frames}</frames>'
        "</CompositeFrame></dataObjects></PublicationDelivery>"
    )


def _psa(n: int, ssp: str, place: str = "", quay: str = "") -> str:
    out = f'<PassengerStopAssignment id="ZZ:PSA:{n}" order="1">'
    out += f'<ScheduledStopPointRef ref="{ssp}"/>'
    if place:
        out += f'<StopPlaceRef ref="{place}"/>'
    if quay:
        out += f'<QuayRef ref="{quay}"/>'
    return out + "</PassengerStopAssignment>"


def _point(n: int, ssp: str, served: bool = True) -> str:
    flags = "" if served else "<ForAlighting>false</ForAlighting><ForBoarding>false</ForBoarding>"
    return (
        f'<StopPointInJourneyPattern id="ZZ:SPJP:{n}" order="{n}">'
        f'<ScheduledStopPointRef ref="{ssp}"/>{flags}</StopPointInJourneyPattern>'
    )


EPIP_SITE = (
    '<SiteFrame id="ZZ:SF"><stopPlaces>'
    '<StopPlace id="ZZ:StopPlace:99001" version="1">'
    "<keyList><KeyValue><Key>uic</Key><Value>9900001</Value></KeyValue>"
    "<KeyValue><Key>bad key</Key><Value>9</Value></KeyValue></keyList>"
    "<Name>ZZ Centrale</Name>"
    "<Centroid><Location><Longitude>9.1</Longitude><Latitude>45.1</Latitude></Location></Centroid>"
    '<PrivateCode type="zzRef">9901</PrivateCode><PublicCode>ZZC</PublicCode>'
    "<TransportMode>rail</TransportMode><StopPlaceType>railStation</StopPlaceType>"
    '<quays><Quay id="ZZ:Quay:99001-1"><keyList><KeyValue><Key>uic</Key>'
    "<Value>9900009</Value></KeyValue></keyList><Name>1</Name></Quay></quays>"
    "</StopPlace>"
    '<StopPlace id="ZZ:StopPlace:99001-c"><ParentSiteRef ref="ZZ:StopPlace:99001"/>'
    '<Name>ZZ Centrale sotto</Name><PrivateCode type="tipo-é">5</PrivateCode>'
    '<quays><Quay id="ZZ:Quay:99001-c1"/></quays></StopPlace>'
    '<StopPlace id="ZZ:StopPlace:99002"><Name>ZZ Ovest</Name>'
    '<Centroid><Location><gml:pos srsName="EPSG:2154">700000 6600000</gml:pos></Location>'
    "</Centroid><StopPlaceType>railStation</StopPlaceType>"
    '<quays><Quay id="ZZ:Quay:99002-1"/></quays></StopPlace>'
    '<StopPlace id="ZZ:StopPlace:99003"><Name>ZZ Bus</Name>'
    "<Centroid><Location><Longitude>9.3</Longitude><Latitude>45.3</Latitude></Location>"
    "</Centroid><TransportMode>bus</TransportMode><StopPlaceType>onstreetBus</StopPlaceType>"
    "</StopPlace>"
    "</stopPlaces></SiteFrame>"
)
EPIP_SERVICE = (
    '<ServiceFrame id="ZZ:SVF"><lines>'
    '<Line id="ZZ:Line:R"><Name>R</Name><TransportMode>rail</TransportMode>'
    '<OperatorRef ref="ZZ:Operator:R"/></Line>'
    '<Line id="ZZ:Line:B"><Name>B</Name><TransportMode>bus</TransportMode>'
    '<OperatorRef ref="ZZ:Operator:B"/></Line>'
    "</lines><stopAssignments>"
    + _psa(1, "ZZ:SSP:1", "ZZ:StopPlace:99001", "ZZ:Quay:99001-1")
    + _psa(2, "ZZ:SSP:2", "ZZ:StopPlace:99001-c", "ZZ:Quay:99001-c1")
    + _psa(3, "ZZ:SSP:3", "ZZ:StopPlace:99002")
    + _psa(4, "ZZ:SSP:4", "ZZ:StopPlace:99003")
    + "</stopAssignments><journeyPatterns>"
    '<ServiceJourneyPattern id="ZZ:SJP:R"><RouteView><LineRef ref="ZZ:Line:R"/></RouteView>'
    "<pointsInSequence>"
    + _point(1, "ZZ:SSP:1")
    + _point(2, "ZZ:SSP:2")
    + _point(3, "ZZ:SSP:3", served=False)
    + "</pointsInSequence></ServiceJourneyPattern>"
    '<ServiceJourneyPattern id="ZZ:SJP:B"><RouteView><LineRef ref="ZZ:Line:B"/></RouteView>'
    "<pointsInSequence>"
    + _point(1, "ZZ:SSP:4")
    + _point(2, "ZZ:SSP:1")
    + "</pointsInSequence></ServiceJourneyPattern>"
    '<ServiceJourneyPattern id="ZZ:SJP:X"><pointsInSequence>'
    + _point(1, "ZZ:SSP:3")
    + _point(2, "ZZ:SSP:4")
    + "</pointsInSequence></ServiceJourneyPattern>"
    "</journeyPatterns></ServiceFrame>"
)
EPIP_TIMETABLE = (
    '<TimetableFrame id="ZZ:TF"><vehicleJourneys>'
    '<ServiceJourney id="ZZ:SJ:1"><ServiceJourneyPatternRef ref="ZZ:SJP:R"/>'
    '<LineRef ref="ZZ:Line:R"/></ServiceJourney>'
    '<ServiceJourney id="ZZ:SJ:2"><ServiceJourneyPatternRef ref="ZZ:SJP:R"/>'
    '<LineRef ref="ZZ:Line:R"/></ServiceJourney>'
    '<ServiceJourney id="ZZ:SJ:3"><TransportMode>bus</TransportMode>'
    '<ServiceJourneyPatternRef ref="ZZ:SJP:B"/></ServiceJourney>'
    '<ServiceJourney id="ZZ:SJ:4"><ServiceJourneyPatternRef ref="ZZ:SJP:X"/></ServiceJourney>'
    "</vehicleJourneys></TimetableFrame>"
)


def test_netex_epip_extract(tmp_path: Path) -> None:
    # the timetable before the lines and the sites: references point forwards
    archive = _zip(
        tmp_path / "epip.zip",
        {"zz_epip.xml": _delivery(EPIP_TIMETABLE + EPIP_SERVICE + EPIP_SITE)},
    )
    provider = _provider("netex_epip")
    result = _build(provider, archive, tmp_path)
    rows = _by_key(_msmm_check(result.path_gz, str(feed_key_for(provider))))
    assert set(rows) == {"ZZ:StopPlace:99001", "ZZ:StopPlace:99002"}
    central = rows["ZZ:StopPlace:99001"]
    assert central["stop_name"] == "ZZ Centrale"
    assert central["n_calls"] == "4"  # two rail journeys at the root and at its child
    assert central["modes"] == "RAIL_MAIN"
    assert central["operators"] == "ZZ:Operator:R"  # never the bus line's
    assert central["codes"].split("|") == [
        "@id=ZZ:StopPlace:99001",
        "uic=9900001",
        "PrivateCode:zzRef=9901",
        "PublicCode=ZZC",
        "Quay/@id=ZZ:Quay:99001-1",
        "Quay/uic=9900009",
        "StopPlace/@id=ZZ:StopPlace:99001-c",
        "Quay/@id=ZZ:Quay:99001-c1",
    ]
    assert (central["lat"], central["lon"]) == ("45.1000000", "9.1000000")
    west = rows["ZZ:StopPlace:99002"]
    # the modeless journey counts at a railStation only (the fallback)
    assert (west["n_calls"], west["modes"], west["operators"]) == (
        "1",
        "RAIL_MAIN",
        "VIATOR:provider:ZZRAIL",
    )
    assert (west["lat"], west["lon"]) == ("46.5000000", "3.0000000")  # Lambert-93 origin
    assert result.dropped["code_field"] == 2  # "bad key" and the non-ASCII type


NORDIC_SHARED = _delivery(
    '<SiteFrame id="ZZ:SF"><stopPlaces>'
    '<StopPlace id="ZZ:StopPlace:9"><Name>ZZ Nordby</Name>'
    "<Centroid><Location><Longitude>10.5</Longitude><Latitude>59.9</Latitude></Location>"
    "</Centroid><TransportMode>rail</TransportMode>"
    '<quays><Quay id="ZZ:Quay:9-1"><PublicCode>1</PublicCode></Quay>'
    '<Quay id="ZZ:Quay:9-2"><PublicCode>2</PublicCode></Quay></quays></StopPlace>'
    '<StopPlace id="ZZ:StopPlace:99"><Name>ZZ Sorby</Name>'
    "<Centroid><Location><Longitude>10.6</Longitude><Latitude>59.8</Latitude></Location>"
    '</Centroid><quays><Quay id="ZZ:Quay:99-1"/></quays></StopPlace>'
    "</stopPlaces></SiteFrame>"
    '<ServiceFrame id="ZZ:SVF:shared"><stopAssignments>'
    + _psa(1, "ZZ:SSP:1", quay="ZZ:Quay:9-1")
    + _psa(2, "ZZ:SSP:2", quay="ZZ:Quay:99-1")
    + _psa(3, "ZZ:SSP:3", quay="ZZ:Quay:9-2")
    + "</stopAssignments></ServiceFrame>"
)
NORDIC_LINE = _delivery(
    '<ServiceFrame id="ZZ:SVF:L1"><lines><Line id="ZZ:Line:L1"><Name>L1</Name>'
    "<TransportMode>rail</TransportMode><TransportSubmode><RailSubmode>suburbanRailway"
    '</RailSubmode></TransportSubmode><AuthorityRef ref="ZZ:Authority:9"/></Line></lines>'
    '<routes><Route id="ZZ:Route:L1"><LineRef ref="ZZ:Line:L1"/></Route></routes>'
    '<journeyPatterns><JourneyPattern id="ZZ:JP:L1"><RouteRef ref="ZZ:Route:L1"/>'
    "<pointsInSequence>"
    + _point(1, "ZZ:SSP:1")
    + _point(2, "ZZ:SSP:2")
    + _point(3, "ZZ:SSP:3")
    + "</pointsInSequence></JourneyPattern></journeyPatterns></ServiceFrame>"
    '<TimetableFrame id="ZZ:TF:L1"><vehicleJourneys>'
    '<ServiceJourney id="ZZ:SJ:9"><JourneyPatternRef ref="ZZ:JP:L1"/>'
    '<OperatorRef ref="ZZ:Operator:9"/><passingTimes><TimetabledPassingTime>'
    '<StopPointInJourneyPatternRef ref="ZZ:SPJP:1"/></TimetabledPassingTime></passingTimes>'
    "</ServiceJourney></vehicleJourneys></TimetableFrame>"
)


def test_netex_nordic_extract(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "nordic.zip",
        {"_ZZ_shared_data.xml": NORDIC_SHARED, "ZZ_line_L1.xml": NORDIC_LINE},
    )
    provider = _provider("netex_nordic")
    rows = _by_key(
        _msmm_check(_build(provider, archive, tmp_path).path_gz, str(feed_key_for(provider)))
    )
    assert set(rows) == {"ZZ:StopPlace:9", "ZZ:StopPlace:99"}
    north = rows["ZZ:StopPlace:9"]
    assert north["modes"] == "RAIL_SUBURBAN"  # the line's submode, through route and pattern
    assert north["n_calls"] == "2"  # quays 1 and 3 of the pattern
    assert north["operators"] == "ZZ:Authority:9|ZZ:Operator:9"
    assert north["codes"].split("|") == [
        "@id=ZZ:StopPlace:9",
        "Quay/@id=ZZ:Quay:9-1",
        "Quay/PublicCode=1",
        "Quay/@id=ZZ:Quay:9-2",
        "Quay/PublicCode=2",
    ]
    assert (north["lat"], north["lon"]) == ("59.9000000", "10.5000000")


def test_netex_bus_only_is_no_extract(tmp_path: Path) -> None:
    line = NORDIC_LINE.replace(
        "<TransportMode>rail</TransportMode>", "<TransportMode>bus</TransportMode>"
    )
    archive = _zip(tmp_path / "bus.zip", {"a.xml": NORDIC_SHARED, "b.xml": line})
    provider = _provider("netex_nordic")
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    assert caught.value.reason == "no_rail_stop"
    assert not list((tmp_path / "x").glob("*.gz"))


def test_netex_rail_replacement_bus_is_not_rail(tmp_path: Path) -> None:
    line = NORDIC_LINE.replace(
        "<TransportMode>rail</TransportMode><TransportSubmode><RailSubmode>suburbanRailway"
        "</RailSubmode>",
        "<TransportMode>bus</TransportMode><TransportSubmode><BusSubmode>railReplacementBus"
        "</BusSubmode>",
    )
    archive = _zip(tmp_path / "sev.zip", {"a.xml": NORDIC_SHARED, "b.xml": line})
    provider = _provider("netex_nordic")
    with pytest.raises(ExtractError):
        _build(provider, archive, tmp_path)


def test_netex_positions() -> None:
    lat, lon = _lambert93_to_wgs84(700000.0, 6600000.0)
    assert abs(lat - 46.5) < 1e-9
    assert abs(lon - 3.0) < 1e-9
    from defusedxml.ElementTree import fromstring

    from app.station_extract_netex import _position

    def at(location: str) -> tuple[float | None, float | None]:
        return _position(
            fromstring(
                f"<StopPlace {NS}><Centroid><Location>{location}</Location></Centroid></StopPlace>"
            )
        )

    assert at('<gml:pos srsName="EPSG:4326">45.5 9.5</gml:pos>') == (45.5, 9.5)
    assert at('<gml:pos srsName="urn:ogc:def:crs:OGC:1.3:CRS84">9.5 45.5</gml:pos>') == (45.5, 9.5)
    assert at("<gml:pos>45.5 9.5</gml:pos>") == (45.5, 9.5)  # no system: WGS84, latitude first
    assert at("<gml:pos>700000 6600000</gml:pos>") == (None, None)  # not a latitude
    assert at('<gml:pos srsName="EPSG:4258">45.5 9.5</gml:pos>') == (45.5, 9.5)
    assert at('<gml:pos srsName="EPSG:32154">700000 6600000</gml:pos>') == (None, None)
    assert at('<gml:pos srsName="EPSG:21540">700000 6600000</gml:pos>') == (None, None)
    for name in ("urn:ogc:def:crs:EPSG::2154", "http://www.opengis.net/def/crs/EPSG/0/2154"):
        lat, lon = at(f'<gml:pos srsName="{name}">700000 6600000</gml:pos>')
        assert (round(lat or 0, 6), round(lon or 0, 6)) == (46.5, 3.0)
    assert at('<gml:pos srsName="EPSG:4326">45.5</gml:pos>') == (None, None)
    assert at("<Longitude>x</Longitude><Latitude>45</Latitude>") == (None, None)


def test_netex_not_well_formed(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "bad.zip", {"a.xml": "<PublicationDelivery><oops>"})
    provider = _provider("netex_epip")
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    assert caught.value.reason == "bad_archive"


def test_netex_entities_refused(tmp_path: Path) -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "zz">]><x>&a;</x>'
    archive = _zip(tmp_path / "ent.zip", {"a.xml": bomb})
    provider = _provider("netex_epip")
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    assert caught.value.reason == "bad_archive"


# ── What is not an extract ────────────────────────────────────────────────


def test_refusals(tmp_path: Path) -> None:
    archive = _gtfs(tmp_path)
    provider = _provider(url="")
    with pytest.raises(ExtractError, match="no_feed_key"):
        _build(provider, archive, tmp_path)
    provider = _provider()
    with pytest.raises(ExtractError, match="bad_sha256"):
        _build(provider, archive, tmp_path, sha="A" * 64)
    provider = _provider("netex_fr")
    with pytest.raises(ExtractError, match="unsupported_format"):
        _build(provider, archive, tmp_path)
    provider = {**_provider(), "id": "zz lower"}
    with pytest.raises(ExtractError, match="bad_provider"):
        _build(provider, archive, tmp_path)
    provider = _provider()
    naive = datetime(2026, 1, 1)
    with pytest.raises(ExtractError, match="bad_time"):
        build_extract(provider, archive, SHA_A, extracted_at=naive)
    not_zip = tmp_path / "nz.zip"
    not_zip.write_bytes(b"not a zip")
    provider = _provider()
    with pytest.raises(ExtractError, match="bad_archive"):
        _build(provider, not_zip, tmp_path)
    missing = _zip(tmp_path / "m.zip", {"stops.txt": STOPS})
    provider = _provider()
    with pytest.raises(ExtractError, match="bad_archive"):
        _build(provider, missing, tmp_path)
    no_rail = _gtfs(tmp_path, name="b.zip", routes="route_id,agency_id,route_type\nR1,ZZR,3\n")
    provider = _provider()
    with pytest.raises(ExtractError, match="no_rail_stop"):
        _build(provider, no_rail, tmp_path)
    assert not list((tmp_path / "x").iterdir())  # nothing written, no temporary file left


# ── MSMM's limits: over one, no extract ───────────────────────────────────


@pytest.mark.parametrize(
    ("constant", "value", "reason"),
    [
        ("MAX_ROWS", 2, "too_many_rows"),
        ("UNPACKED_MAX_BYTES", 600, "too_large"),
        ("EXTRACT_MAX_BYTES", 100, "too_large"),
        ("LINE_MAX", 100, "line_too_long"),
        ("FIELD_MAX", 20, "cell_too_long"),
    ],
)
def test_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str, value: int, reason: str
) -> None:
    monkeypatch.setattr(station_extract, constant, value)
    provider = _provider()
    archive = _gtfs(tmp_path)
    with pytest.raises(ExtractError) as caught:
        _build(provider, archive, tmp_path)
    assert caught.value.reason == reason
    assert not list((tmp_path / "x").iterdir())


def test_limits_just_met(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_extract, "MAX_ROWS", 3)
    assert _build(_provider(), _gtfs(tmp_path), tmp_path).rows == 3


def test_limits_mirror_msmm() -> None:
    assert station_extract.MAX_ROWS == 200_000
    assert station_extract.EXTRACT_MAX_BYTES == 32 * 1024 * 1024
    assert station_extract.UNPACKED_MAX_BYTES == 256 * 1024 * 1024
    assert station_extract.LINE_MAX == 1024 * 1024
    assert station_extract.FIELD_MAX == 131_072
    assert station_extract.ITEMS_MAX == 500
    assert station_extract.TEXT_MAX == 200
    # 500 items of 200 characters stay under a cell, and three such cells under a line
    assert station_extract.FIELD_MAX >= 500 * 200 + 499
    assert 3 * 2 * station_extract.FIELD_MAX < station_extract.LINE_MAX


# ── feed_version and the cache ─────────────────────────────────────────────


def test_cache_by_archive_and_extractor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _gtfs(tmp_path)
    first = _build(_provider(), archive, tmp_path)
    archive.unlink()  # a cache hit never opens the archive
    again = _build(_provider(), archive, tmp_path)
    assert again.path_gz == first.path_gz
    assert again.body_sha256 == first.body_sha256
    assert again.rows == first.rows
    assert again.dropped == {}
    # another archive, or another extractor version, is another extract; one kept
    archive = _gtfs(tmp_path)
    other = _build(_provider(), archive, tmp_path, sha=SHA_B)
    assert other.path_gz != first.path_gz
    assert not first.path_gz.exists()
    assert _msmm_check(other.path_gz, other.feed_key)[0]["feed_version"] == SHA_B
    monkeypatch.setattr(station_extract, "EXTRACTOR_VERSION", "viator-stop-extract-99")
    newer = _build(_provider(), archive, tmp_path, sha=SHA_B)
    assert newer.path_gz != other.path_gz
    assert newer.extractor_version == "viator-stop-extract-99"
    assert [p.name for p in (tmp_path / "x").iterdir()] == [newer.path_gz.name]


def test_same_rows_same_bytes(tmp_path: Path) -> None:
    one = build_extract(
        _provider(), _gtfs(tmp_path), SHA_A, extracted_at=WHEN, cache_dir=tmp_path / "1"
    )
    two = build_extract(
        _provider(), _gtfs(tmp_path), SHA_A, extracted_at=WHEN, cache_dir=tmp_path / "2"
    )
    assert one.body_sha256 == two.body_sha256


def test_default_cache_is_the_session_extracts(tmp_path: Path) -> None:
    slot = tmp_path / "inbox" / "zz-session" / "gtfs"
    slot.mkdir(parents=True)
    archive = _gtfs(slot, name="zzrail.zip")
    result = build_extract(_provider(), archive, SHA_A, extracted_at=WHEN)
    assert result.path_gz.parent == tmp_path / "inbox" / "zz-session" / "_extracts"


def test_provenance(tmp_path: Path) -> None:
    archive = _gtfs(tmp_path)
    nap = _provider(source="nap", resolver=ZZ_RESOLVERS[0])
    rows = _msmm_check(_build(nap, archive, tmp_path).path_gz, str(feed_key_for(nap)))
    assert rows[0]["provider_provenance"] == "nap"
    community = {**_provider(), "provenance": "community"}
    rows = _msmm_check(_build(community, archive, tmp_path).path_gz, str(feed_key_for(community)))
    assert rows[0]["provider_provenance"] == "community"
    entry = next(e for e in provider_catalogue.load().values() if e["provenance"] == "official")
    listed = provider_catalogue.session_provider(entry)
    assert station_extract._provenance(listed, str(feed_key_for(listed))) == "official"
    moved = {**listed, "timetable": {**listed["timetable"], "url": "https://zz.example.invalid/x"}}
    assert station_extract._provenance(moved, str(feed_key_for(moved))) == ""


# ── At refresh time ───────────────────────────────────────────────────────


def test_warm_session_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import feed_fetch

    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    session = inbox / "zz-session"
    (session / "gtfs").mkdir(parents=True)
    _gtfs(session / "gtfs", name="zzrail.zip")
    _gtfs(session / "gtfs", name="zzup.zip")
    _gtfs(session / "gtfs", name="zzwait.zip")
    feed_fetch.save_state(
        session / "_fetch_state", "provider[ZZRAIL].timetable(gtfs)", {"sha256": SHA_A}
    )
    config = {
        "sources": {
            "providers": [
                _provider(),
                {"id": "ZZUP", "timetable": {"format": "gtfs", "source": "upload"}},
                {"id": "ZZWAIT", "timetable": {"format": "gtfs", "source": "upload"}},
                {
                    "id": "ZZNONE",
                    "timetable": {"format": "gtfs", "url": "https://zz.example.invalid/n"},
                },
                {
                    "id": "ZZX",
                    "timetable": {
                        "format": "gtfs",
                        "source": "cross_border_filter",
                        "derived_from": {"session_id": "zz", "provider_id": "ZZNAT"},
                    },
                },
            ]
        }
    }
    out = warm_session_extracts("zz-session", config, upload_sha256={"ZZUP": SHA_B})
    assert isinstance(out["ZZRAIL"], station_extract.ExtractResult)
    assert out["ZZRAIL"].feed_version == SHA_A  # the downloaded bytes', from the fetch state
    assert isinstance(out["ZZUP"], station_extract.ExtractResult)
    assert out["ZZUP"].feed_key == f"upload:{SHA_B}"
    assert out["ZZWAIT"] == out["ZZNONE"] == out["ZZX"] == "skipped"
    assert out["ZZRAIL"].path_gz.parent == session / "_extracts"
    only = warm_session_extracts("zz-session", config, only_provider="ZZRAIL")
    assert list(only) == ["ZZRAIL"]
    assert warm_session_extracts("zz-session", {"sources": {"providers": "bad"}}) == {}


def test_warm_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import feed_fetch

    inbox = tmp_path / "inbox"
    monkeypatch.setattr(station_extract.settings, "inbox_dir", inbox)
    (inbox / "s" / "gtfs").mkdir(parents=True)
    (inbox / "s" / "gtfs" / "zzrail.zip").write_bytes(b"not a zip")
    feed_fetch.save_state(
        inbox / "s" / "_fetch_state", "provider[ZZRAIL].timetable(gtfs)", {"sha256": SHA_A}
    )
    config = {"sources": {"providers": [_provider()]}}
    assert warm_session_extracts("s", config) == {"ZZRAIL": "bad_archive"}

    def boom(*args: Any, **kwargs: Any) -> None:
        raise MemoryError

    monkeypatch.setattr(station_extract, "ensure_extract", boom)
    assert warm_session_extracts("s", config) == {"ZZRAIL": "error"}


# ── Memory stays bounded ──────────────────────────────────────────────────


def _large_gtfs(path: Path, trips: int, calls_per_trip: int) -> Path:
    """A synthetic feed: 500 rail stations, `trips` rail trips, one bus route
    with as many trips, every trip calling `calls_per_trip` times."""
    stops = io.StringIO()
    stops.write("stop_id,stop_name,stop_lat,stop_lon,parent_station\n")
    for n in range(500):
        stops.write(f"ZZ:S{n},ZZ Gare {n},45.{n:04d},9.{n:04d},\n")
        stops.write(f"ZZ:S{n}:P,ZZ Quai {n},45.{n:04d},9.{n:04d},ZZ:S{n}\n")
    trips_text = io.StringIO()
    trips_text.write("route_id,trip_id\n")
    times = io.StringIO()
    times.write("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n")
    for t in range(trips):
        for route in ("R", "B"):
            trips_text.write(f"{route},{route}{t}\n")
            for c in range(calls_per_trip):
                times.write(f"{route}{t},08:00:00,08:00:00,ZZ:S{(t + c) % 500}:P,{c}\n")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agency.txt", "agency_id,agency_name\nZZ,ZZ Rail\n")
        zf.writestr("routes.txt", "route_id,agency_id,route_type\nR,ZZ,2\nB,ZZ,3\n")
        zf.writestr("stops.txt", stops.getvalue())
        zf.writestr("trips.txt", trips_text.getvalue())
        zf.writestr("stop_times.txt", times.getvalue())
    return path


def _peak(provider: dict[str, Any], archive: Path, folder: Path) -> tuple[int, int]:
    tracemalloc.start()
    try:
        result = build_extract(provider, archive, SHA_A, extracted_at=WHEN, cache_dir=folder)
        return tracemalloc.get_traced_memory()[1], result.rows
    finally:
        tracemalloc.stop()


def test_memory_bounded_gtfs(tmp_path: Path) -> None:
    small = _large_gtfs(tmp_path / "small.zip", trips=1_000, calls_per_trip=20)
    large = _large_gtfs(tmp_path / "large.zip", trips=8_000, calls_per_trip=20)
    with zipfile.ZipFile(large) as zf:
        unpacked = sum(info.file_size for info in zf.infolist())
    assert unpacked > 10 * 1024 * 1024  # stop_times alone is some 13 MB
    _, rows_small = _peak(_provider(), small, tmp_path / "a")
    peak_large, rows_large = _peak(_provider(), large, tmp_path / "b")
    assert rows_small == rows_large == 500
    # bounded by the rail trips and stations, not by the rows of stop_times
    assert peak_large < 8 * 1024 * 1024, peak_large
    assert peak_large < unpacked / 2


def _large_netex(path: Path, journeys: int) -> Path:
    sites = "".join(
        f'<StopPlace id="ZZ:StopPlace:{n}"><Name>ZZ Gare {n}</Name><Centroid><Location>'
        f"<Longitude>9.{n:04d}</Longitude><Latitude>45.{n:04d}</Latitude></Location></Centroid>"
        f'<quays><Quay id="ZZ:Quay:{n}"/></quays></StopPlace>'
        for n in range(500)
    )
    assignments = "".join(_psa(n, f"ZZ:SSP:{n}", quay=f"ZZ:Quay:{n}") for n in range(500))
    patterns = "".join(
        f'<ServiceJourneyPattern id="ZZ:SJP:{p}"><pointsInSequence>'
        + "".join(_point(c, f"ZZ:SSP:{(p * 7 + c) % 500}") for c in range(20))
        + "</pointsInSequence></ServiceJourneyPattern>"
        for p in range(50)
    )
    passing = "".join(
        f'<TimetabledPassingTime><StopPointInJourneyPatternRef ref="ZZ:SPJP:{c}"/>'
        "<DepartureTime>08:00:00</DepartureTime></TimetabledPassingTime>"
        for c in range(20)
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "shared.xml",
            _delivery(
                f'<SiteFrame id="ZZ:SF"><stopPlaces>{sites}</stopPlaces></SiteFrame>'
                '<ServiceFrame id="ZZ:SVF"><lines><Line id="ZZ:Line:R">'
                "<TransportMode>rail</TransportMode></Line></lines>"
                f"<stopAssignments>{assignments}</stopAssignments>"
                f"<journeyPatterns>{patterns}</journeyPatterns></ServiceFrame>"
            ),
        )
        with zf.open("timetable.xml", "w", force_zip64=True) as out:
            out.write(
                f'<PublicationDelivery {NS}><dataObjects><TimetableFrame id="ZZ:TF">'.encode()
            )
            out.write(b"<vehicleJourneys>")
            for j in range(journeys):
                out.write(
                    (
                        f'<ServiceJourney id="ZZ:SJ:{j}"><LineRef ref="ZZ:Line:R"/>'
                        f'<ServiceJourneyPatternRef ref="ZZ:SJP:{j % 50}"/>'
                        f"<passingTimes>{passing}</passingTimes></ServiceJourney>"
                    ).encode()
                )
            out.write(b"</vehicleJourneys></TimetableFrame></dataObjects></PublicationDelivery>")
    return path


def test_memory_bounded_netex(tmp_path: Path) -> None:
    small = _large_netex(tmp_path / "small.zip", journeys=500)
    large = _large_netex(tmp_path / "large.zip", journeys=8_000)
    with zipfile.ZipFile(large) as zf:
        unpacked = sum(info.file_size for info in zf.infolist())
    assert unpacked > 20 * 1024 * 1024
    provider = _provider("netex_epip")
    peak_small, rows_small = _peak(provider, small, tmp_path / "a")
    peak_large, rows_large = _peak(provider, large, tmp_path / "b")
    assert rows_small == rows_large
    assert peak_large < 8 * 1024 * 1024, peak_large
    assert peak_large < 2 * peak_small + 1024 * 1024
