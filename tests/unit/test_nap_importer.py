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
                {
                    "format": "NeTEx",
                    "url": "https://x/netex.zip",
                    "updated": "2026-04-30T12:00:00Z",
                },
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
                {"format": "GTFS", "url": "https://x/old.zip", "updated": "2025-01-01T00:00:00Z"},
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
                {
                    "format": "NeTEx",
                    "url": "https://x/netex.zip",
                    "updated": "2026-04-30T12:00:00Z",
                },
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
                {
                    "format": "gtfs-rt",
                    "url": "https://x/rt-alerts",
                    "updated": "2026-04-30T12:00:00Z",
                },
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
            {"format": "GTFS", "url": "https://x/timetable.zip"},  # ignored — not RT
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
                {
                    "format": "GTFS",
                    "url": "https://x/sncf-gtfs.zip",
                    "updated": "2026-04-30T12:00:00Z",
                },
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
                {
                    "format": "NeTEx",
                    "url": "https://x/netex-fr.zip",
                    "updated": "2026-04-30T12:00:00Z",
                },
            ],
        }
        assert make_provider_from_dataset(ds) is None

    def test_dataset_without_resources_returns_none(self):
        from app.master.nap_importer import make_provider_from_dataset

        assert (
            make_provider_from_dataset({"title": "X", "publisher": {"name": "X"}, "resources": []})
            is None
        )

    def test_country_falls_back_to_default(self):
        from app.master.nap_importer import make_provider_from_dataset

        ds = {
            "title": "Provider X",
            "publisher": {"name": "PROV"},
            "covered_area": [],  # empty, so country can't be determined from data
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
                {
                    "format": "NeTEx",
                    "url": "https://x/trenitalia-netex.zip",
                    "updated": "2026-04-30T12:00:00Z",
                },
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

    async def fake_fetch(_url, *, nap_auth=None):
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

    async def fake_fetch(_url, *, nap_auth=None):
        return [
            {
                "id": "horaires-sncf",
                "title": "Réseau SNCF TGV, Intercités et TER",
                "publisher": {"name": "SNCF Voyageurs"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {
                        "format": "GTFS",
                        "url": "https://x/sncf.zip",
                        "updated": "2026-04-30T12:00:00Z",
                    },
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


# ─── v0.1.12 picker: dataset_id pass-through + include_dataset_ids filter ───


def test_make_provider_attaches_nap_dataset_id():
    """Picker UI needs the upstream dataset id to key its checkboxes by."""
    from app.master.nap_importer import make_provider_from_dataset

    ds = {
        "id": "ds-abc-123",
        "title": "Réseau X",
        "publisher": {"name": "X"},
        "covered_area": [{"insee": "FR", "nom": "France"}],
        "resources": [
            {"format": "GTFS", "url": "https://x/y.zip", "updated": "2026-04-30T12:00:00Z"},
        ],
    }
    provider = make_provider_from_dataset(ds)
    assert provider is not None
    assert provider["_nap_dataset_id"] == "ds-abc-123"


@pytest.mark.asyncio
async def test_import_from_nap_include_dataset_ids_filters_to_picked(monkeypatch):
    """When the operator's picker sends include_dataset_ids, only matching
    datasets get imported. Non-matching are silently skipped (not in
    `skipped` either — picker UX would be cluttered with the noise)."""
    from app.master import nap_importer

    async def fake_fetch(_url, *, nap_auth=None):
        return [
            {
                "id": "ds-1",
                "title": "Réseau A",
                "publisher": {"name": "A"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {"format": "GTFS", "url": "https://x/a.zip", "updated": "2026-04-30T12:00:00Z"},
                ],
            },
            {
                "id": "ds-2",
                "title": "Réseau B",
                "publisher": {"name": "B"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {"format": "GTFS", "url": "https://x/b.zip", "updated": "2026-04-30T12:00:00Z"},
                ],
            },
            {
                "id": "ds-3",
                "title": "Réseau C",
                "publisher": {"name": "C"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {"format": "GTFS", "url": "https://x/c.zip", "updated": "2026-04-30T12:00:00Z"},
                ],
            },
        ]

    monkeypatch.setattr(nap_importer, "fetch_datasets", fake_fetch)

    # Operator picks ds-1 and ds-3 (skipping the middle B).
    result = await nap_importer.import_from_nap(
        existing_providers=[],
        country="FR",
        include_dataset_ids=["ds-1", "ds-3"],
    )
    ids = sorted(p["_nap_dataset_id"] for p in result["providers"])
    assert ids == ["ds-1", "ds-3"]
    # ds-2 was filtered silently — not in skipped (it wasn't a "couldn't
    # use" reason, just operator's choice).
    assert not any(s.get("dataset", "").startswith("Réseau B") for s in result["skipped"])


@pytest.mark.asyncio
async def test_import_from_nap_passes_auth_to_fetch(monkeypatch):
    """v0.1.12: catalogues with credentials must thread auth through to
    fetch_datasets so authenticated NAPs (mobilithek.info etc.) work."""
    from app.master import nap_importer

    captured: dict = {}

    async def fake_fetch(_url, *, nap_auth=None):
        captured["nap_auth"] = nap_auth
        return []

    monkeypatch.setattr(nap_importer, "fetch_datasets", fake_fetch)

    auth = ("bearer", "secret-token-123", None)
    await nap_importer.import_from_nap(
        existing_providers=[],
        nap_url="https://x/y",
        nap_auth=auth,
    )
    assert captured["nap_auth"] == auth


@pytest.mark.asyncio
async def test_import_from_nap_empty_include_list_keeps_all(monkeypatch):
    """Empty/None include_dataset_ids = no filter, keep all matching datasets.
    Mirrors the `exclude_dataset_ids` semantics."""
    from app.master import nap_importer

    async def fake_fetch(_url, *, nap_auth=None):
        return [
            {
                "id": "ds-1",
                "title": "Réseau Test",
                # 2+ uppercase chars — slug_to_provider_id requires that.
                "publisher": {"name": "ACME"},
                "covered_area": [{"insee": "FR", "nom": "France"}],
                "resources": [
                    {"format": "GTFS", "url": "https://x/x.zip", "updated": "2026-04-30T12:00:00Z"},
                ],
            },
        ]

    monkeypatch.setattr(nap_importer, "fetch_datasets", fake_fetch)

    for include in (None, []):
        result = await nap_importer.import_from_nap(
            existing_providers=[],
            country="FR",
            include_dataset_ids=include,
        )
        assert len(result["providers"]) == 1, f"include={include!r} should not filter"


# ─────────────────── URL safety + log sanitisation (audit 2026-05) ───────────────────


class TestValidateSafeHttpUrl:
    """SSRF defence — `_validate_safe_http_url` rejects URLs whose hostname
    resolves to private/loopback/link-local IP space, or whose scheme isn't
    http(s). Closes the SonarCloud finding at app/master/nap_importer.py."""

    def test_public_https_url_passes_and_returns_url(self):
        from app.master.nap_importer import _validate_safe_http_url

        # transport.data.gouv.fr is the canonical NAP URL — must keep working.
        # The function returns the URL unchanged (acts as inline sanitiser),
        # so callers can write `safe = _validate_safe_http_url(x)`.
        url = "https://transport.data.gouv.fr/api/datasets"
        assert _validate_safe_http_url(url) == url

    def test_non_http_scheme_rejected(self):
        from app.master.nap_importer import _validate_safe_http_url

        for bad in ("file:///etc/passwd", "gopher://x", "ldap://x", "ftp://x"):
            with pytest.raises(ValueError, match="scheme must be http"):
                _validate_safe_http_url(bad)

    def test_missing_hostname_rejected(self):
        from app.master.nap_importer import _validate_safe_http_url

        with pytest.raises(ValueError, match="no hostname"):
            _validate_safe_http_url("https:///path-only")

    def test_localhost_rejected(self, monkeypatch):
        # Resolve `localhost` to 127.0.0.1 deterministically. socket.getaddrinfo
        # returns 5-tuples (family, socktype, proto, canonname, sockaddr).
        from app.master import nap_importer

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(nap_importer.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ValueError, match="non-public address"):
            nap_importer._validate_safe_http_url("https://attacker.example.com/redirect")

    @pytest.mark.parametrize(
        "private_ip",
        [
            "10.0.0.1",  # RFC1918
            "192.168.1.1",  # RFC1918
            "172.16.0.1",  # RFC1918
            "169.254.169.254",  # AWS/GCP/Azure metadata
            "127.0.0.1",  # loopback
            "::1",  # IPv6 loopback
            "fe80::1",  # IPv6 link-local
            "fc00::1",  # IPv6 ULA (matches is_private)
        ],
    )
    def test_private_ip_ranges_rejected(self, monkeypatch, private_ip):
        from app.master import nap_importer

        family = 30 if ":" in private_ip else 2  # AF_INET6 vs AF_INET (loose)

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(family, 1, 6, "", (private_ip, 0))]

        monkeypatch.setattr(nap_importer.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ValueError, match="non-public address"):
            nap_importer._validate_safe_http_url("https://attacker.example.com/x")

    def test_unresolvable_hostname_rejected(self, monkeypatch):
        from app.master import nap_importer

        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise nap_importer.socket.gaierror("Name or service not known")

        monkeypatch.setattr(nap_importer.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ValueError, match="Cannot resolve"):
            nap_importer._validate_safe_http_url("https://does-not-exist.invalid/x")


class TestSanitizeForLog:
    """Log-injection defence — `_sanitize_for_log` strips control characters
    so an operator-supplied URL containing CR/LF can't split a log record."""

    def test_printable_ascii_passes_through(self):
        from app.master.nap_importer import _sanitize_for_log

        assert _sanitize_for_log("https://example.com/foo") == "https://example.com/foo"

    def test_crlf_escaped(self):
        from app.master.nap_importer import _sanitize_for_log

        # \r\n CRLF is the classic log-injection vector.
        out = _sanitize_for_log("https://x.com/\r\nFAKE: log line")
        assert "\r" not in out and "\n" not in out
        assert "\\x0d" in out and "\\x0a" in out

    def test_null_byte_escaped(self):
        from app.master.nap_importer import _sanitize_for_log

        out = _sanitize_for_log("https://x.com/\x00")
        assert "\x00" not in out
        assert "\\x00" in out

    def test_long_value_truncated(self):
        from app.master.nap_importer import _sanitize_for_log

        long_url = "https://x.com/" + ("a" * 500)
        out = _sanitize_for_log(long_url, max_len=100)
        assert len(out) <= 100 + len("...(truncated)")
        assert out.endswith("...(truncated)")

    def test_unicode_letters_kept(self):
        from app.master.nap_importer import _sanitize_for_log

        # Accented characters are printable; the function must not strip them.
        assert _sanitize_for_log("Île-de-France") == "Île-de-France"
