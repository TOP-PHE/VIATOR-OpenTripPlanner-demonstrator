"""Provider-bundle schema tests (v0.1.6).

Three input shapes converge on one canonical providers list:
  - v0.1.6 native — sources.providers = [{...}]
  - v0.1.4 multi-feed — sources.gtfs = [{id, url}, ...]
  - pre-v0.1.4 single-feed — sources.gtfs = "<url>"

The canonical shape is what gets written back on save and what the build-time
machinery reads.
"""

from __future__ import annotations

import pytest

# ─────────────────────── normalize_providers ───────────────────────


class TestNormalizeProviders:
    def test_empty_config_returns_empty_list(self):
        from app.ingestion import normalize_providers

        assert normalize_providers({}) == []
        assert normalize_providers({"sources": {}}) == []
        assert normalize_providers({"sources": {"gtfs": ""}}) == []
        assert normalize_providers({"sources": {"gtfs": None}}) == []

    def test_legacy_string_lifts_to_one_provider(self):
        """Pre-v0.1.4 single-string config opens to one provider with id=GTFS."""
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "gtfs": "https://example.com/sncf.zip",
                    "mct": "https://example.com/mct.csv",
                    "stations": "https://example.com/st.csv",
                }
            }
        )
        assert len(out) == 1
        p = out[0]
        assert p["id"] == "GTFS"
        assert p["label"] == "GTFS"
        assert p["country_iso"] is None
        assert p["timetable"] == {"format": "gtfs", "url": "https://example.com/sncf.zip"}
        # First provider inherits session-level mct/stations URLs
        assert p["mct_url"] == "https://example.com/mct.csv"
        assert p["stations_csv_url"] == "https://example.com/st.csv"
        assert p["gtfs_rt"] == {}

    def test_legacy_multi_feed_lifts_each_feed_to_provider(self):
        """v0.1.4 list lifts to N providers; first inherits session-level mct/stations."""
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "gtfs": [
                        {"id": "SNCF", "url": "https://a/sncf.zip"},
                        {"id": "IDFM", "url": "https://b/idfm.zip"},
                    ],
                    "mct": "https://c/mct.csv",
                }
            }
        )
        assert len(out) == 2
        assert out[0]["id"] == "SNCF"
        assert out[0]["mct_url"] == "https://c/mct.csv"
        assert out[1]["id"] == "IDFM"
        assert out[1]["mct_url"] is None  # only first inherits

    def test_v016_native_pass_through(self):
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "providers": [
                        {
                            "id": "SNCF",
                            "label": "SNCF Trains",
                            "country_iso": "FR",
                            "timetable": {"format": "gtfs", "url": "https://a/sncf.zip"},
                            "gtfs_rt": {"alerts_url": "https://x/alerts"},
                            "mct_url": "https://m/mct.csv",
                            "stations_csv_url": "https://s/st.csv",
                        }
                    ]
                }
            }
        )
        assert len(out) == 1
        assert out[0]["country_iso"] == "FR"
        assert out[0]["gtfs_rt"]["alerts_url"] == "https://x/alerts"

    def test_invalid_feed_id_rejected(self):
        from app.ingestion import normalize_providers

        with pytest.raises(ValueError, match="must match"):
            normalize_providers(
                {
                    "sources": {
                        "providers": [
                            {
                                "id": "sncf",
                                "timetable": {"format": "gtfs", "url": "https://a/x.zip"},
                            }
                        ]
                    }
                }
            )

    def test_invalid_country_rejected(self):
        from app.ingestion import normalize_providers

        with pytest.raises(ValueError, match="2-letter ISO"):
            normalize_providers(
                {
                    "sources": {
                        "providers": [
                            {
                                "id": "SNCF",
                                "country_iso": "FRA",  # 3 letters
                                "timetable": {"format": "gtfs", "url": "https://a/x.zip"},
                            }
                        ]
                    }
                }
            )

    def test_country_iso_normalised_to_uppercase(self):
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "providers": [
                        {
                            "id": "SNCF",
                            "country_iso": "fr",  # lowercase input
                            "timetable": {"format": "gtfs", "url": "https://a/x.zip"},
                        }
                    ]
                }
            }
        )
        assert out[0]["country_iso"] == "FR"

    def test_invalid_timetable_format_rejected(self):
        """OTP can't read NeTEx-FR; we reject that format explicitly."""
        from app.ingestion import normalize_providers

        with pytest.raises(ValueError, match="NeTEx-FR is intentionally excluded"):
            normalize_providers(
                {
                    "sources": {
                        "providers": [
                            {
                                "id": "SNCF",
                                "timetable": {"format": "netex_fr", "url": "https://a/x.zip"},
                            }
                        ]
                    }
                }
            )

    def test_duplicate_provider_id_rejected(self):
        from app.ingestion import normalize_providers

        with pytest.raises(ValueError, match="appears twice"):
            normalize_providers(
                {
                    "sources": {
                        "providers": [
                            {
                                "id": "SNCF",
                                "timetable": {"format": "gtfs", "url": "https://a/x.zip"},
                            },
                            {
                                "id": "SNCF",
                                "timetable": {"format": "gtfs", "url": "https://b/y.zip"},
                            },
                        ]
                    }
                }
            )

    def test_optional_fields_default_to_empty(self):
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "providers": [
                        {"id": "SNCF", "timetable": {"format": "gtfs", "url": "https://a/x.zip"}}
                    ]
                }
            }
        )
        assert out[0]["gtfs_rt"] == {}
        assert out[0]["mct_url"] is None
        assert out[0]["stations_csv_url"] is None

    def test_netex_nordic_format_accepted(self):
        from app.ingestion import normalize_providers

        out = normalize_providers(
            {
                "sources": {
                    "providers": [
                        {
                            "id": "ENTUR",
                            "country_iso": "NO",
                            "timetable": {"format": "netex_nordic", "url": "https://a/no.zip"},
                        }
                    ]
                }
            }
        )
        assert out[0]["timetable"]["format"] == "netex_nordic"

    def test_non_http_urls_rejected(self):
        from app.ingestion import normalize_providers

        with pytest.raises(ValueError, match="http"):
            normalize_providers(
                {
                    "sources": {
                        "providers": [
                            {"id": "SNCF", "timetable": {"format": "gtfs", "url": "ftp://a/x.zip"}}
                        ]
                    }
                }
            )


def test_staged_filename_for_format():
    from app.ingestion import staged_filename_for_format

    # Filename is the same regardless of format — both formats land as .zip.
    # The dispatch subdir (gtfs/ vs netex/) is what differs and that's
    # determined by the kind, not the filename.
    assert staged_filename_for_format("SNCF", "gtfs") == "sncf.zip"
    assert staged_filename_for_format("ENTUR", "netex_nordic") == "entur.zip"


def test_staged_filename_unknown_format_rejects():
    from app.ingestion import staged_filename_for_format

    with pytest.raises(ValueError, match="Unknown timetable format"):
        staged_filename_for_format("SNCF", "csv")
