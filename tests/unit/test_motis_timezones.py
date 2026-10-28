"""A default time zone for each NeTEx feed of a MOTIS build, and MOTIS's
import errors in the rebuild log (app/motis_timezones.py).

ÖBB's NAP file declares no time zone in any of its 432 files, and MOTIS
drops a NeTEx file without one ("ERROR:  not found in timezone database"
in logs/tt.txt): on 2026-10-28 eu19 held no ÖBB stop at all.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import motis_timezones as mt

# The block `motis config` writes per dataset (MOTIS test/config_test.cc).
_BLOCK = (
    "      extend_calendar: false\n"
    "      default_bikes_allowed: false\n"
    "      default_cars_allowed: false\n"
    "      default_reservation_not_required: true\n"
    "      clasz_reservation_not_required:\n"
    "        AIR: false\n"
    "        COACH: false\n"
)


def _config(*datasets: tuple[str, str]) -> str:
    body = "".join(f"    {name}:\n      path: {path}\n{_BLOCK}" for name, path in datasets)
    return (
        "osm: /inbox/eu19/osm/osm.pbf\n"
        "timetable:\n"
        "  first_day: TODAY\n"
        "  num_days: 365\n"
        f"  datasets:\n{body}"
        "street_routing: true\n"
    )


def _provider(pid: str, fmt: str, country: str | None, **timetable: str) -> dict:
    return {"id": pid, "country_iso": country, "timetable": {"format": fmt, **timetable}}


# ── countries and providers ─────────────────────────────────────────


@pytest.mark.parametrize("country", sorted(mt.COUNTRY_TIMEZONES))
def test_every_country_zone_is_in_the_time_zone_database(country: str) -> None:
    ZoneInfo(mt.COUNTRY_TIMEZONES[country])


def test_a_country_code_is_read_in_any_case() -> None:
    assert mt.timezone_for_country("at") == "Europe/Vienna"
    assert mt.timezone_for_country(" AT ") == "Europe/Vienna"
    assert mt.timezone_for_country("ZZ") is None
    assert mt.timezone_for_country(None) is None
    assert mt.timezone_for_country("") is None


def test_only_netex_providers_with_a_known_country_get_a_zone() -> None:
    zones = mt.dataset_timezones(
        [
            _provider("OBB", "netex_epip", "AT"),
            _provider("ENTUR", "netex_nordic", "NO"),
            _provider("SNCF", "gtfs", "FR"),
            _provider("NMBS", "netex_epip", None),
            _provider("X", "netex_epip", "ZZ"),
            # A derived provider's file is GTFS whatever it declares.
            _provider("CB", "netex_epip", "DE", source="cross_border_filter"),
            {"id": "BAD", "country_iso": "AT", "timetable": {"format": "netex_fr"}},
            {"country_iso": "AT", "timetable": {"format": "netex_epip"}},
        ]
    )
    assert zones == {"obb.zip": "Europe/Vienna", "entur.zip": "Europe/Oslo"}


# ── config.yml ──────────────────────────────────────────────────────


def test_the_zone_goes_after_the_path_of_each_netex_dataset(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        _config(
            ("sncf", "/inbox/eu19/gtfs/sncf.zip"),
            ("obb", "/inbox/eu19/netex/obb.zip"),
            ("si-nap", "/graphs/motis/eu19/_netex/si-nap.zip"),
            ("nmbs", "/inbox/eu19/netex/nmbs.zip"),
        ),
        encoding="utf-8",
    )
    notes = mt.set_default_timezones(
        cfg, {"obb.zip": "Europe/Vienna", "si-nap.zip": "Europe/Ljubljana", "sncf.zip": "X/Y"}
    )
    expected = _config(
        ("sncf", "/inbox/eu19/gtfs/sncf.zip"),
        ("obb", "/inbox/eu19/netex/obb.zip\n      default_timezone: Europe/Vienna"),
        (
            "si-nap",
            "/graphs/motis/eu19/_netex/si-nap.zip\n      default_timezone: Europe/Ljubljana",
        ),
        ("nmbs", "/inbox/eu19/netex/nmbs.zip"),
    )
    assert cfg.read_text(encoding="utf-8") == expected
    assert notes == [
        "motis time zone: obb.zip → Europe/Vienna, for its files that declare none "
        "(from the provider's country)",
        "motis time zone: si-nap.zip → Europe/Ljubljana, for its files that declare none "
        "(from the provider's country)",
        "motis time zone: nmbs.zip gets no default (its provider declares no country): "
        "a file of it without a time zone of its own is dropped",
    ]


def test_a_zone_already_in_the_block_is_replaced_once(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    original = _config(("obb", "/inbox/eu19/netex/obb.zip"), ("db", "/inbox/eu19/netex/db.zip"))
    cfg.write_text(
        original.replace(
            "      path: /inbox/eu19/netex/obb.zip\n",
            "      path: /inbox/eu19/netex/obb.zip\n      default_timezone: Europe/Paris\n",
        ),
        encoding="utf-8",
    )
    zones = {"obb.zip": "Europe/Vienna", "db.zip": "Europe/Berlin"}
    mt.set_default_timezones(cfg, zones)
    first = cfg.read_text(encoding="utf-8")
    assert first.count("default_timezone:") == 2
    assert "Europe/Paris" not in first
    assert "      path: /inbox/eu19/netex/obb.zip\n      default_timezone: Europe/Vienna\n" in first
    # A second pass changes nothing: the key is never written twice.
    mt.set_default_timezones(cfg, zones)
    assert cfg.read_text(encoding="utf-8") == first


def test_a_config_without_netex_datasets_is_unchanged(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    original = _config(("sncf", "/inbox/eu19/gtfs/sncf.zip"))
    cfg.write_text(original, encoding="utf-8")
    assert mt.set_default_timezones(cfg, {"sncf.zip": "Europe/Paris"}) == []
    assert cfg.read_text(encoding="utf-8") == original


def test_a_quoted_path_is_read(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(_config(("obb", '"/inbox/eu19/netex/obb.zip"')), encoding="utf-8")
    mt.set_default_timezones(cfg, {"obb.zip": "Europe/Vienna"})
    assert '/obb.zip"\n      default_timezone: Europe/Vienna\n' in cfg.read_text(encoding="utf-8")


# ── logs/tt.txt ─────────────────────────────────────────────────────


def test_no_log_file_or_no_error_gives_nothing(tmp_path: Path) -> None:
    assert mt.import_error_lines(tmp_path / "missing.txt") == []
    log = tmp_path / "tt.txt"
    log.write_text("loading obb\nok\n", encoding="utf-8")
    assert mt.import_error_lines(log) == []


def test_errors_are_counted_even_when_two_share_a_line(tmp_path: Path) -> None:
    """eu19's tt.txt on 2026-10-28: 2 580 lines, some holding two messages
    written at once by parallel loaders."""
    log = tmp_path / "logs" / "tt.txt"
    log.parent.mkdir()
    log.write_text(
        "ERROR:  not found in timezone database\n" * 3
        + "ERROR:  not found in timezone databaseERROR:  not found in timezone database\n"
        + "ERROR: journey pattern required IN /inbox/eu19/netex/db.zip/a.xml\n"
        + "progress 47%\n",
        encoding="utf-8",
    )
    assert mt.import_error_lines(log) == [
        "motis import errors (logs/tt.txt): 6",
        "        5  not found in timezone database",
        "        1  journey pattern required IN /inbox/eu19/netex/db.zip/a.xml",
        "  a NeTEx file declares no time zone and its feed has no default: "
        "give its provider a country so the build sets one",
    ]


def test_many_messages_are_capped_and_cleaned(tmp_path: Path) -> None:
    log = tmp_path / "tt.txt"
    lines = [f"ERROR: message {i}\n" for i in range(12)]
    lines += ["ERROR: bad\x07 char " + "x" * 300 + "\n"] * 2  # the most frequent
    log.write_text("".join(lines), encoding="utf-8")
    out = mt.import_error_lines(log)
    assert out[0] == "motis import errors (logs/tt.txt): 14"
    assert len(out) == 1 + 10 + 1
    assert out[-1] == "  … and 3 other messages"
    long_line = next(line for line in out if "bad" in line)
    assert "\x07" not in long_line
    assert long_line.endswith("…")
    assert not any("no time zone" in line for line in out)
