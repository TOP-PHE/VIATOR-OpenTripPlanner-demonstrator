"""NAP importer (v0.1.8).

Pure unit tests for the dataset → provider transformation logic. No
network, no DB. The orchestrator `import_from_nap()` is tested with
`fetch_datasets()` monkey-patched so we control the input deterministically.
"""

from __future__ import annotations

import pytest


# ─────────────────── classify_modes ───────────────────


class TestClassifyModes:
    def test_sncf_title_classifies_as_rail(self):
        from app.master.nap_importer import classify_modes

        ds = {"title": "Réseau SNCF TGV, Intercités et TER", "tags": ["rail"]}
        assert "rail" in classify_modes(ds)

    def test_idfm_title_classifies_as_urban(self):
        from app.master.nap_importer import classify_modes

        ds = {"title": "Réseaux urbains et interurbains d'Île-de-France Mobilités", "tags": []}
        modes = classify_modes(ds)
        # IDFM dataset is broad — matches both urban (Île-de-France Mobilités,
        # réseau urbain) and bus (interurbain).
        assert "urban" in modes
        assert "bus" in modes

    def test_trenitalia_title_classifies_as_rail(self):
        from app.master.nap_importer import classify_modes

        ds = {"title": "Réseau national Trenitalia France", "tags": []}
        assert "rail" in classify_modes(ds)

    def test_carsharing_doesnt_match_rail(self):
        from app.master.nap_importer import classify_modes

        ds = {"title": "Base nationale consolidée des lieux de covoiturage", "tags": []}
        modes = classify_modes(ds)
        assert "rail" not in modes
        # Carpooling isn't in any of our 4 mode sets — should be empty.
        assert modes == set()

    def test_eurostar_title_classifies_as_rail(self):
        from app.master.nap_importer import classify_modes

        ds = {"title": "Réseau européen Eurostar", "tags": []}
        assert "rail" in classify_modes(ds)


# ─────────────────── select_resource ───────────────────


class TestSelectResource:
    def test_picks_gtfs_over_netex(self):
        from app.master.nap_importer import select_resource

        ds = {
            "resources": [
                {"format": "NeTEx", "url": "https://x/netex.zip", "updated": "2026-04-30T12:00:00Z"},
                {"format": "GTFS", "url": "https://x/gtfs.zip", "updated": "2026-04-30T12:00:00Z"},
            ]
        }
        resource, fmt = select_resource(ds)
        assert fmt == "gtfs"
        assert resource["url"] == "https://x/gtfs.zip"

    def test_picks_most_recent_gtfs(self):
        from app.master.nap_importer import select_resource

        ds = {
            "resources": [
                {"format": "GTFS", "url": "https://x/old.zip",   "updated": "2025-01-01T00:00:00Z"},
                {"format": "GTFS", "url": "https://x/fresh.zip", "updated": "2026-04-30T12:00:00Z"},
            ]
        }
        resource, fmt = select_resource(ds)
        assert resource["url"] == "https://x/fresh.zip"

    def test_falls_back_to_netex_fr_when_no_gtfs(self):
        """OTP can't read NeTEx-FR, but the importer still surfaces it as
        netex_fr so the orchestrator can warn rather than silently drop it."""
        from app.master.nap_importer import select_resource

        ds = {
            "resources": [
                {"format": "NeTEx", "url": "https://x/netex.zip", "updated": "2026-04-30T12:00:00Z"},
            ]
        }
        resource, fmt = select_resource(ds)
        assert fmt == "netex_fr"
        assert resource["url"] == "https://x/netex.zip"

    def test_distinguishes_nordic_profile_via_schema_name(self):
        from app.master.nap_importer import select_resource

        ds = {
            "resources": [
                {
                    "format": "NeTEx",
                    "url": "https://x/nordic.zip",
                    "updated": "2026-04-30T12:00:00Z",
                    "schema_name": "entur/nordic-netex-2024",
                },
            ]
        }
        _, fmt = select_resource(ds)
        assert fmt == "netex_nordic"

    def test_skips_gtfs_rt_resources(self):
        """GTFS-RT is real-time, not a timetable. Selector ignores it as
        a routing resource (caller wires it via gtfs_rt fields instead)."""
        from app.master.nap_importer import select_resource

        ds = {
            "resources": [
                {"format": "gtfs-rt", "url": "https://x/rt-alerts", "updated": "2026-04-30T12:00:00Z"},
            ]
        }
        resource, fmt = select_resource(ds)
        assert resource is None
        assert fmt is None

    def test_no_resources_returns_none(self):
        from app.master.nap_importer import select_resource

        assert select_resource({"resources": []}) == (None, None)
        assert select_resource({}) == (None, None)


def test_select_gtfs_rt_urls_partitions_by_keyword():
    from app.master.nap_importer import select_gtfs_rt_urls

    ds = {
        "resources": [
            {"format": "gtfs-rt", "url": "https://x/sncf-gtfs-rt-service-alerts"},
            {"format": "gtfs-rt", "url": "https://x/sncf-gtfs-rt-trip-updates"},
            {"format": "gtfs-rt", "url": "https://x/sncf-vehicle-positions"},
            {"format": "GTFS",    "url": "https://x/timetable.zip"},  # ignored — not RT
        ]
    }
    rt = select_gtfs_rt_urls(ds)
    assert rt["alerts_url"] == "https://x/sncf-gtfs-rt-service-alerts"
    assert rt["trip_updates_url"] == "https://x/sncf-gtfs-rt-trip-updates"
    assert rt["vehicle_positions_url"] == "https://x/sncf-vehicle-positions"


# ─────────────────── slug_to_provider_id ───────────────────


class TestSlugToProviderId:
    def test_picks_first_all_caps_token(self):
        from app.master.nap_importer import slug_to_provider_id

        assert slug_to_provider_id("SNCF Voyageurs") == "SNCF"
        assert slug_to_provider_id("RATP — Bus Île-de-France") == "RATP"

    def test_falls_back_to_full_uppercased_when_no_caps_token(self):
        from app.master.nap_importer import slug_to_provider_id

        # "trenitalia france" → "TRENITALIAFRANCE" (truncated to 16)
        out = slug_to_provider_id("trenitalia france")
        assert out == "TRENITALIAFRANCE"

    def test_dedupes_against_existing(self):
        from app.master.nap_importer import slug_to_provider_id

        out = slug_to_provider_id("SNCF Voyageurs", existing={"SNCF"})
        # "-2" appended; total ≤ 16 chars
        assert out == "SNCF-2"
        out2 = slug_to_provider_id("SNCF Voyageurs", existing={"SNCF", "SNCF-2"})
        assert out2 == "SNCF-3"

    def test_returns_empty_for_pathological_input(self):
        from app.master.nap_importer import slug_to_provider_id

        # All non-conformant chars
        assert slug_to_provider_id("###") == ""
        assert slug_to_provider_id("") == ""


# ─────────────────── make_provider_from_dataset ───────────────────


class TestMakeProviderFromDataset:
    def test_full_sncf_dataset_yields_complete_provider(self):
        from app.master.nap_importer import make_provider_from_dataset

        ds = {
            "id": "horaires-sncf",
            "title": "Réseau SNCF TGV, Intercités et TER",
            "publisher": {"name": "SNCF Voyageurs"},
            "covered_area": [{"insee": "FR", "nom": "France"}],
            "resources": [
                {"format": "GTFS",    "url": "https://x/sncf-gtfs.zip", "updated": "2026-04-30T12:00:00Z"},
                {"format": "gtfs-rt", "url": "https://x/sncf-gtfs-rt-service-alerts"},
                {"format": "gtfs-rt", "url": "https://x/sncf-gtfs-rt-trip-updates"},
            ],
        }
        provider = make_provider_from_dataset(ds, default_country="FR")
        assert provider is not None
        assert provider["id"] == "SNCF"
        assert provider["country_iso"] == "FR"
        assert provider["timetable"]["format"] == "gtfs"
        assert provider["timetable"]["url"] == "https://x/sncf-gtfs.zip"
        assert provider["gtfs_rt"]["alerts_url"] == "https://x/sncf-gtfs-rt-service-alerts"
        assert provider["gtfs_rt"]["trip_updates_url"] == "https://x/sncf-gtfs-rt-trip-updates"

    def test_netex_fr_only_dataset_returns_none(self):
        """Operator publishes only NeTEx-FR — OTP can't route it. The
        importer returns None so the orchestrator surfaces a warning
        rather than silently adding an unusable provider."""
        from app.master.nap_importer import make_provider_from_dataset

        ds = {
            "id": "x",
            "title": "Some operator",
            "publisher": {"name": "X"},
            "covered_area": [{"insee": "FR", "nom": "France"}],
            "resources": [
                {"format": "NeTEx", "url": "https://x/netex-fr.zip", "updated": "2026-04-30T12:00:00Z"},
            ],
        }
        assert make_provider_from_dataset(ds) is None

    def test_dataset_without_resources_returns_none(self):
        from app.master.nap_importer import make_provider_from_dataset

        assert make_provider_from_dataset({"title": "X", "publisher": {"name": "X"}, "resources": []}) is None

    def test_country_falls_back_to_default(self):
        from app.master.nap_importer import make_provider_from_dataset

        ds = {
            "title": "Provider X",
            "publisher": {"name": "PROV"},
            "covered_area": [],   # empty, so country can't be determined from data
            "resources": [
                {"format": "GTFS", "url": "https://x/y.zip", "updated": "2026-04-30T12:00:00Z"},
            ],
        }
        provider = make_provider_from_dataset(ds, default_country="FR")
        assert provider["country_iso"] == "FR"


# ─────────────────── import_from_nap orchestrator ───────────────────


@pytest.mark.asyncio
async def test_import_from_nap_filters_and_dedupes(monkeypatch):
    """End-to-end orchestrator test with mocked fetch_datasets."""
    from app.master import nap_importer

    # Pretend the NAP returned 4 datasets — 2 rail (1 SNCF, 1 carpooling
    # incorrectly tagged with rail keyword), 1 urban, 1 NeTEx-FR-only.
    fake_datasets = [
        {
            "id": "horaires-sncf",
            "title": "Réseau SNCF TGV, Intercités et TER",
            "publisher": {"name": "SNCF Voyageurs"},
            "covered_area": [{"insee": "FR", "nom": "France"}],
            "resources": [
                {"format": "GTFS", "url": "https://x/sncf.zip", "updated": "2026-04-30T12:00:00Z"},
            ],
        },
        {
            "id": "idfm-bus",
            "title": "Réseaux urbains et interurbains d'Île-de-France Mobilités",
            "publisher": {"name": "Île-de-France Mobilités"},
            "covered_area": [{"insee": "FR", "nom": "Île-de-France"}],
            "resources": [
                {"format": "GTFS", "url": "https://x/idfm.zip", "updated": "2026-04-30T12:00:00Z"},
            ],
        },
        {
            "id": "trenitalia",
            "title": "Réseau national Trenitalia France",
            "publisher": {"name": "Trenitalia"},
            "covered_area": [{"insee": "FR", "nom": "France"}],
            "resources": [
                # Only NeTEx, no GTFS — should be skipped + warn
                {"format": "NeTEx", "url": "https://x/trenitalia-netex.zip", "updated": "2026-04-30T12:00:00Z"},
            ],
        },
        {
            "id": "covoiturage",
            "title": "Base nationale consolidée des lieux de covoiturage",
            "publisher": {"name": "PAN"},
            "covered_area": [{"insee": "FR", "nom": "France"}],
            "resources": [
                {"format": "csv", "url": "https://x/c.csv", "updated": "2026-03-05T12:00:00Z"},
            ],
        },
    ]

    async def fake_fetch(_url):
        return fake_datasets

    monkeypatch.setattr(nap_importer, "fetch_datasets", fake_fetch)

    # Filter: rail only, France only, no existing providers.
    result = await nap_importer.import_from_nap(
        existing_providers=[],
        country="FR",
        modes=["rail"],
    )

    # SNCF kept (rail + GTFS). Trenitalia warned (rail + only NeTEx-FR).
    # IDFM filtered out (urban+bus, not rail). Carpooling filtered out
    # (no mode match, type=carpooling).
    provider_ids = [p["id"] for p in result["providers"]]
    assert "SNCF" in provider_ids
    assert len(provider_ids) == 1, f"unexpected extras: {provider_ids}"

    # Trenitalia should appear in skipped + warnings (NeTEx-FR only).
    trenitalia_skipped = [s for s in result["skipped"] if "Trenitalia" in s["dataset"]]
    assert len(trenitalia_skipped) == 1
    assert "NeTEx-FR" in trenitalia_skipped[0]["reason"]
    assert any("NeTEx-FR" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_import_from_nap_dedupes_by_url(monkeypatch):
    """A re-import shouldn't duplicate providers already in the session."""
    from app.master import nap_importer

    async def fake_fetch(_url):
        return [
            {
                "id": "horaires-sncf",
                "title": "Réseau SNCF TGV, Intercités et TER",
                "publisher": {"name": "SNCF Voyageurs"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {"format": "GTFS", "url": "https://x/sncf.zip", "updated": "2026-04-30T12:00:00Z"},
                ],
            },
        ]

    monkeypatch.setattr(nap_importer, "fetch_datasets", fake_fetch)

    existing = [{"id": "SNCF", "timetable": {"url": "https://x/sncf.zip"}}]
    result = await nap_importer.import_from_nap(
        existing_providers=existing,
        country="FR",
        modes=["rail"],
    )
    assert result["providers"] == []
    assert any(s["reason"] == "already in session (same URL)" for s in result["skipped"])
