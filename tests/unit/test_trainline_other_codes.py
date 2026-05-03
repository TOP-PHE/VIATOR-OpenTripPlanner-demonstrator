"""Trainline parser populates `other_codes` JSONB for non-headline operators.

Five dedicated columns (SNCF, DB, Trenitalia, Renfe, ATOC) keep their
existing top-level mapping. Everything else (OBB, SBB, NTV, Trenord,
Cercanías, Entur, Westbahn, Flixbus, Benerail, Busbud, Distribusion,
IATA) goes into `other_codes` so the UI can render them dynamically
without a schema migration per operator.
"""

from __future__ import annotations

CSV_HEADER = (
    "id;name;slug;uic;uic8_sncf;latitude;longitude;parent_station_id;hub_id;"
    "country;time_zone;is_city;is_main_station;is_airport;is_suggestable;"
    "country_hint;main_station_hint;sncf_id;sncf_tvs_id;sncf_is_enabled;"
    "entur_id;entur_is_enabled;db_id;db_is_enabled;busbud_id;busbud_is_enabled;"
    "distribusion_id;distribusion_is_enabled;flixbus_id;flixbus_is_enabled;"
    "cff_id;cff_is_enabled;obb_id;obb_is_enabled;trenitalia_id;trenitalia_is_enabled;"
    "trenitalia_rtvt_id;trenord_id;ntv_rtiv_id;ntv_id;ntv_is_enabled;renfe_id;"
    "renfe_is_enabled;cercanias_id;cercanias_hub_id;cercanias_is_enabled;atoc_id;"
    "atoc_is_enabled;benerail_id;benerail_is_enabled;westbahn_id;westbahn_is_enabled;"
    "smt_id;smt_is_enabled;sncf_self_service_machine;same_as;"
    "info:de;info:en;info:es;info:fr;info:it;info:nb;info:nl;info:cs;info:da;"
    "info:hu;info:ja;info:ko;info:pl;info:pt;info:ru;info:sv;info:tr;info:zh;"
    "normalised_code;iata_airport_code"
)


def _row(uic="8011160", **overrides):
    """Build one CSV row with sane defaults; overrides keyed by column name."""
    cols = [c.strip() for c in CSV_HEADER.split(";")]
    values = {c: "" for c in cols}
    values.update(
        {
            "id": "1",
            "name": "Berlin Hbf",
            "uic": uic,
            "country": "DE",
            "is_main_station": "t",
            "is_suggestable": "t",
        }
    )
    values.update(overrides)
    return ";".join(values[c] for c in cols)


def _parse_one(**overrides):
    """Helper: return the parsed dict for a single row with overrides applied."""
    from app.master.trainline import parse_csv

    csv = CSV_HEADER + "\n" + _row(**overrides) + "\n"
    rows, _ = parse_csv(csv)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    return rows[0]


# ─────────────────────── existing dedicated columns ───────────────────────


def test_dedicated_columns_still_populated():
    row = _parse_one(
        sncf_id="FRPNO",
        db_id="8011160",
        trenitalia_id="S00219",
        renfe_id="60000",
        atoc_id="KGX",
    )
    assert row["trigramme_sncf"] == "FRPNO"
    assert row["db_code"] == "8011160"
    assert row["trenitalia_code"] == "S00219"
    assert row["renfe_code"] == "60000"
    assert row["atoc_code"] == "KGX"


# ─────────────────────── other_codes population ───────────────────────


def test_obb_id_lands_in_other_codes():
    row = _parse_one(obb_id="1190100")
    assert row.get("other_codes", {}).get("obb") == "1190100"


def test_cff_id_maps_to_sbb_key():
    """Trainline calls it cff_id, we expose it as 'sbb' for operator clarity."""
    row = _parse_one(cff_id="8503000")
    assert row.get("other_codes", {}).get("sbb") == "8503000"


def test_multiple_operator_ids_collated():
    row = _parse_one(
        obb_id="1190100",
        cff_id="8503000",
        ntv_id="9540",
        cercanias_id="60003",
        flixbus_id="44032",
    )
    other = row["other_codes"]
    assert other == {
        "obb": "1190100",
        "sbb": "8503000",
        "ntv": "9540",
        "cercanias": "60003",
        "flixbus": "44032",
    }


def test_no_other_codes_means_no_key():
    """Stations with only dedicated codes shouldn't carry an empty other_codes
    key — the DB column has a server_default of '{}' that we let take over."""
    row = _parse_one(sncf_id="FRPNO")
    assert (
        "other_codes" not in row
    ), "row should not include an empty other_codes dict — let the DB default fire"


def test_iata_airport_code_when_present():
    row = _parse_one(iata_airport_code="CDG")
    assert row.get("other_codes", {}).get("iata") == "CDG"


# ─────────────────────── _diff_fields — drift granularity ───────────────────────


class _StubExisting:
    """Lightweight stand-in for a MasterStation ORM row, just for _diff_fields."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        for k in (
            "source",
            "trigramme_sncf",
            "db_code",
            "trenitalia_code",
            "renfe_code",
            "atoc_code",
            "name",
            "country_iso",
            "latitude",
            "longitude",
            "is_main_station",
            "is_suggestable",
            "uic",
            "uic8_sncf",
            "slug",
        ):
            if not hasattr(self, k):
                setattr(self, k, None)
        if not hasattr(self, "other_codes"):
            self.other_codes = {}


def test_diff_decomposes_other_codes_per_key():
    from app.master.trainline import _diff_fields

    existing = _StubExisting(
        name="Berlin Hbf",
        other_codes={"obb": "1190100", "sbb": "8503000"},
    )
    incoming = {
        "name": "Berlin Hbf",
        "other_codes": {
            "obb": "1190100",  # unchanged
            "sbb": "8503001",  # changed
            "trenord": "S99999",  # added
        },
    }
    diff = _diff_fields(existing, incoming)
    # Granular: per-key entries within other_codes.
    assert "other_codes.sbb" in diff, "changed sbb should appear"
    assert "other_codes.trenord" in diff, "newly-added trenord should appear"
    assert "other_codes.obb" not in diff, "unchanged obb shouldn't drift"
    # No bare `other_codes` entry — we always decompose.
    assert "other_codes" not in diff


def test_diff_ignores_source_field():
    from app.master.trainline import _diff_fields

    existing = _StubExisting(name="Berlin Hbf", source="manual")
    incoming = {"name": "Berlin Hbf", "source": "trainline"}
    diff = _diff_fields(existing, incoming)
    assert diff == [], "source flag changes shouldn't count as drift"
