"""Cross-border GTFS filter (Phase 0.5 of cross-NAP federation).

Covers `app.gtfs_cross_border_filter` — the data-driven filter that keeps
only routes whose stops span 2+ UIC country prefixes, so a "corridors"
session can ingest a small GTFS containing just the international rail
services bundled inside a big national feed.

The synthetic feed below has four routes, one per case the filter must
get right:

  R_FR  domestic France      stops all 87…   → DROP
  R_CH  domestic Switzerland stops all 85…   → DROP
  R_LYRIA  Paris→Zürich Lyria 87…→85…        → KEEP (endpoint crossing)
  R_CENTO  Brig→Iselle→Domodossola→Locarno   → KEEP (mid-journey crossing,
           85→83→83→85 — the Domodossola case Patrick flagged)
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from app.gtfs_cross_border_filter import (
    country_prefix,
    filter_to_cross_border,
)

# ─────────────────── country_prefix ───────────────────


class TestCountryPrefix:
    def test_seven_digit_swiss(self):
        assert country_prefix("8503000") == "85"

    def test_eight_digit_french_with_check_digit(self):
        # SNCF publishes 8-digit UICs (7-digit UIC + trailing check digit).
        assert country_prefix("87286005") == "87"

    def test_embedded_in_sncf_stop_id(self):
        assert country_prefix("StopPoint:OCETrain-87271007") == "87"

    def test_prefixed_swiss(self):
        assert country_prefix("Parent8503000") == "85"

    def test_platform_suffix(self):
        assert country_prefix("8503000:0:5") == "85"

    def test_italian(self):
        assert country_prefix("8300010") == "83"

    def test_no_uic_returns_none(self):
        assert country_prefix("IDFM:monomodalStopPlace:43098") is None
        assert country_prefix("StopArea:abc") is None

    def test_none_and_empty(self):
        assert country_prefix(None) is None
        assert country_prefix("") is None

    def test_does_not_match_sub_run_of_longer_number(self):
        # A 10-digit blob shouldn't yield a bogus 2-digit prefix.
        assert country_prefix("1234567890") is None


# ─────────────────── synthetic GTFS builder ───────────────────


def _csv(fieldnames: list[str], rows: list[dict]) -> str:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _build_synthetic_gtfs(path: Path) -> None:
    """Write a minimal but valid 4-route GTFS to `path`."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # agency
        zf.writestr(
            "agency.txt",
            _csv(
                ["agency_id", "agency_name", "agency_url", "agency_timezone"],
                [
                    {
                        "agency_id": "SNCF",
                        "agency_name": "SNCF",
                        "agency_url": "https://sncf.com",
                        "agency_timezone": "Europe/Paris",
                    },
                    {
                        "agency_id": "SBB",
                        "agency_name": "SBB",
                        "agency_url": "https://sbb.ch",
                        "agency_timezone": "Europe/Zurich",
                    },
                ],
            ),
        )
        # stops — UICs encode the country in the first 2 digits
        stops = [
            # France (87)
            {
                "stop_id": "87271007",
                "stop_name": "Paris Est",
                "stop_lat": "48.876",
                "stop_lon": "2.359",
                "parent_station": "",
            },
            {
                "stop_id": "87286005",
                "stop_name": "Lille Flandres",
                "stop_lat": "50.637",
                "stop_lon": "3.071",
                "parent_station": "",
            },
            {
                "stop_id": "8768603",
                "stop_name": "Paris Gare de Lyon",
                "stop_lat": "48.844",
                "stop_lon": "2.374",
                "parent_station": "",
            },
            # Switzerland (85)
            {
                "stop_id": "8503000",
                "stop_name": "Zürich HB",
                "stop_lat": "47.378",
                "stop_lon": "8.540",
                "parent_station": "",
            },
            {
                "stop_id": "8507000",
                "stop_name": "Bern",
                "stop_lat": "46.949",
                "stop_lon": "7.439",
                "parent_station": "",
            },
            {
                "stop_id": "8500064",
                "stop_name": "Brig",
                "stop_lat": "46.319",
                "stop_lon": "7.988",
                "parent_station": "",
            },
            {
                "stop_id": "8505026",
                "stop_name": "Locarno",
                "stop_lat": "46.172",
                "stop_lon": "8.797",
                "parent_station": "",
            },
            # Italy (83) — the Domodossola / Centovalli case
            {
                "stop_id": "8300013",
                "stop_name": "Iselle di Trasquera",
                "stop_lat": "46.231",
                "stop_lon": "8.143",
                "parent_station": "",
            },
            {
                "stop_id": "8300010",
                "stop_name": "Domodossola",
                "stop_lat": "46.116",
                "stop_lon": "8.292",
                "parent_station": "",
            },
        ]
        zf.writestr(
            "stops.txt",
            _csv(["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"], stops),
        )
        # routes
        routes = [
            {
                "route_id": "R_FR",
                "agency_id": "SNCF",
                "route_short_name": "TER-HDF",
                "route_type": "2",
            },
            {"route_id": "R_CH", "agency_id": "SBB", "route_short_name": "IC1", "route_type": "2"},
            {
                "route_id": "R_LYRIA",
                "agency_id": "SNCF",
                "route_short_name": "TGV-LYRIA",
                "route_type": "2",
            },
            {
                "route_id": "R_CENTO",
                "agency_id": "SBB",
                "route_short_name": "CENTOVALLI",
                "route_type": "2",
            },
        ]
        zf.writestr(
            "routes.txt",
            _csv(["route_id", "agency_id", "route_short_name", "route_type"], routes),
        )
        # trips — one per route
        trips = [
            {"route_id": "R_FR", "service_id": "WD", "trip_id": "T_FR", "shape_id": ""},
            {"route_id": "R_CH", "service_id": "WD", "trip_id": "T_CH", "shape_id": ""},
            {"route_id": "R_LYRIA", "service_id": "WD", "trip_id": "T_LYRIA", "shape_id": ""},
            {"route_id": "R_CENTO", "service_id": "DAILY", "trip_id": "T_CENTO", "shape_id": ""},
        ]
        zf.writestr(
            "trips.txt",
            _csv(["route_id", "service_id", "trip_id", "shape_id"], trips),
        )
        # stop_times
        st: list[dict] = []

        def _leg(trip, seq, stop, t):
            st.append(
                {
                    "trip_id": trip,
                    "stop_sequence": str(seq),
                    "stop_id": stop,
                    "arrival_time": t,
                    "departure_time": t,
                }
            )

        # R_FR: Paris Est → Lille (both 87) — domestic France
        _leg("T_FR", 1, "87271007", "08:00:00")
        _leg("T_FR", 2, "87286005", "09:00:00")
        # R_CH: Zürich → Bern (both 85) — domestic Switzerland
        _leg("T_CH", 1, "8503000", "08:00:00")
        _leg("T_CH", 2, "8507000", "09:00:00")
        # R_LYRIA: Paris Gare de Lyon (87) → Zürich HB (85) — cross-border
        _leg("T_LYRIA", 1, "8768603", "07:00:00")
        _leg("T_LYRIA", 2, "8503000", "11:00:00")
        # R_CENTO: Brig (85) → Iselle (83) → Domodossola (83) → Locarno (85)
        _leg("T_CENTO", 1, "8500064", "10:00:00")
        _leg("T_CENTO", 2, "8300013", "10:20:00")
        _leg("T_CENTO", 3, "8300010", "10:35:00")
        _leg("T_CENTO", 4, "8505026", "12:00:00")
        zf.writestr(
            "stop_times.txt",
            _csv(["trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time"], st),
        )
        # calendar
        zf.writestr(
            "calendar.txt",
            _csv(
                [
                    "service_id",
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                    "start_date",
                    "end_date",
                ],
                [
                    {
                        "service_id": "WD",
                        "monday": "1",
                        "tuesday": "1",
                        "wednesday": "1",
                        "thursday": "1",
                        "friday": "1",
                        "saturday": "0",
                        "sunday": "0",
                        "start_date": "20260518",
                        "end_date": "20260816",
                    },
                    {
                        "service_id": "DAILY",
                        "monday": "1",
                        "tuesday": "1",
                        "wednesday": "1",
                        "thursday": "1",
                        "friday": "1",
                        "saturday": "1",
                        "sunday": "1",
                        "start_date": "20260518",
                        "end_date": "20260816",
                    },
                ],
            ),
        )
        # feed_info — should be copied verbatim
        zf.writestr(
            "feed_info.txt",
            _csv(
                ["feed_publisher_name", "feed_publisher_url", "feed_lang"],
                [
                    {
                        "feed_publisher_name": "TEST",
                        "feed_publisher_url": "https://x",
                        "feed_lang": "fr",
                    }
                ],
            ),
        )


def _read_ids(zip_path: Path, member: str, id_field: str) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        if member not in zf.namelist():
            return set()
        with zf.open(member) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
            return {r[id_field] for r in reader}


# ─────────────────── filter_to_cross_border ───────────────────


class TestFilterToCrossBorder:
    def test_keeps_only_cross_border_routes(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        stats = filter_to_cross_border(src, out)

        kept_routes = _read_ids(out, "routes.txt", "route_id")
        # Lyria (endpoint crossing) + Centovalli (mid-journey crossing) kept.
        assert kept_routes == {"R_LYRIA", "R_CENTO"}
        # Domestic routes dropped.
        assert "R_FR" not in kept_routes
        assert "R_CH" not in kept_routes
        assert stats.routes_kept == 2
        assert stats.routes_total == 4

    def test_domodossola_midjourney_crossing_kept(self, tmp_path):
        """The case Patrick flagged: a Swiss-operated train dipping through
        Italy (Domodossola) mid-route. 85→83→83→85 = 2 countries = kept."""
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        kept_routes = _read_ids(out, "routes.txt", "route_id")
        assert "R_CENTO" in kept_routes

    def test_cascade_trips_and_stop_times(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        kept_trips = _read_ids(out, "trips.txt", "trip_id")
        assert kept_trips == {"T_LYRIA", "T_CENTO"}
        # stop_times only for kept trips
        with zipfile.ZipFile(out) as zf, zf.open("stop_times.txt") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))
        trip_ids_in_st = {r["trip_id"] for r in rows}
        assert trip_ids_in_st == {"T_LYRIA", "T_CENTO"}
        # Lyria has 2 stops, Centovalli has 4 → 6 stop_times total
        assert len(rows) == 6

    def test_cascade_stops_only_referenced_kept(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        kept_stops = _read_ids(out, "stops.txt", "stop_id")
        # Kept stops: Lyria's (8768603, 8503000) + Centovalli's
        # (8500064, 8300013, 8300010, 8505026). NOT the FR-domestic
        # Paris Est / Lille, NOT the CH-domestic Bern.
        assert kept_stops == {"8768603", "8503000", "8500064", "8300013", "8300010", "8505026"}
        assert "87271007" not in kept_stops  # Paris Est (FR domestic)
        assert "8507000" not in kept_stops  # Bern (CH domestic)

    def test_cascade_calendar_keeps_used_services(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        kept_services = _read_ids(out, "calendar.txt", "service_id")
        # WD used by Lyria, DAILY used by Centovalli — both kept.
        assert kept_services == {"WD", "DAILY"}

    def test_feed_info_copied_verbatim(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        with zipfile.ZipFile(out) as zf:
            assert "feed_info.txt" in zf.namelist()
            with zf.open("feed_info.txt") as f:
                rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))
        assert rows[0]["feed_publisher_name"] == "TEST"

    def test_country_combos_in_stats(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        stats = filter_to_cross_border(src, out)
        # Lyria = FR+CH, Centovalli = CH+IT
        assert stats.country_combos.get("CH+FR") == 1
        assert stats.country_combos.get("CH+IT") == 1

    def test_agency_filtered_to_referenced(self, tmp_path):
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        kept_agencies = _read_ids(out, "agency.txt", "agency_id")
        # Lyria → SNCF, Centovalli → SBB. Both kept (both used).
        assert kept_agencies == {"SNCF", "SBB"}

    def test_output_is_valid_loadable_gtfs(self, tmp_path):
        """Smoke: output has the mandatory GTFS files and they parse."""
        src = tmp_path / "national.zip"
        out = tmp_path / "corridors.zip"
        _build_synthetic_gtfs(src)

        filter_to_cross_border(src, out)
        with zipfile.ZipFile(out) as zf:
            members = set(zf.namelist())
        for mandatory in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            assert mandatory in members
