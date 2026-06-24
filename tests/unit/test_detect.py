"""Format-detection tests, with extra coverage on the NeTEx profile
classifier — the previous heuristic false-flagged most national feeds
(AT, BE, DE, LU) as NeTEx-Nordic because of an `"ent:"` substring match,
and rejected unknown national profiles (CH `ch:1:`) outright.

These tests pin down the new behaviour so the regression can't sneak
back: the Nordic marker must be a real xmlns declaration / codespace,
and unrecognised national NeTEx defaults to EPIP rather than raising.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app import detect

# ─────────────────────── helpers ───────────────────────


def _zip_with_xml(tmp_path: Path, xml: str, filename: str = "shared.xml") -> Path:
    """Build a single-XML NeTEx zip at `tmp_path / netex.zip` and return it."""
    out = tmp_path / "netex.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(filename, xml)
    return out


# Real-world-shaped XML heads. Trimmed but preserve the namespace
# declarations and codespace prefixes the classifier looks at.
_AT_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex"
                     xmlns:gml="http://www.opengis.net/gml/3.2"
                     xmlns:siri="http://www.siri.org.uk/siri">
  <dataObjects>
    <CompositeFrame id="at:obb:CompositeFrame:01">
      <codespaces><Codespace><Xmlns>at:obb</Xmlns></Codespace></codespaces>
      <Operator id="at:obb:Operator:01"><Name>OEBB Personenverkehr AG</Name></Operator>
    </CompositeFrame>
  </dataObjects>"""

_BE_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
  <Operator id="FR:Operator:nmbssncb"><Name>NMBS/SNCB</Name></Operator>
  <Component>placeholder</Component>"""

_DE_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
  <Operator id="DE::Operator:7888::"><Name>Verkehrsgesellschaft Oberhessen mbH</Name></Operator>"""

_LU_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
  <Authority id="LU::Authority:16::"><Name>AVL</Name></Authority>
  <Authority id="LU::Authority:1::"><Name>CdT (Master)</Name></Authority>"""

_CH_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex"
                     xmlns:gml="http://www.opengis.net/gml/3.2"
                     xmlns:siri="http://www.siri.org.uk/siri">
  <Operator id="ch:1:Operator:11"><Name>Schweizerische Bundesbahnen SBB</Name></Operator>
  <Operator id="ch:1:Operator:101"><Name>Verkehrsbetriebe Biel</Name></Operator>"""

_NORDIC_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex"
                     xmlns:nsr="http://www.rutebanken.org/ns/nsr">
  <ScheduledStopPoint id="NSR:ScheduledStopPoint:100"/>"""

_FR_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex"
                     xmlns:fr="http://www.sncf.com/netex/fr">
  <Codespace><Xmlns>fr</Xmlns><XmlnsUrl>http://www.sncf.com/netex/fr</XmlnsUrl></Codespace>"""

_EPIP_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
  <!-- Conforms to NeTEx EPIP v1.1 profile -->
  <Operator id="IT::Operator:05403151003:TRENITALIA"><Name>TRENITALIA</Name></Operator>"""


# ─────────────────────── new behaviour (national → EPIP) ───────────────────────


@pytest.mark.parametrize(
    ("name", "head"),
    [
        ("AT-NAP / OBB-codespace", _AT_HEAD),
        ("BE-NAP / SNCB single-XML (false-Nordic regression)", _BE_HEAD),
        ("DE-NAP / gesamtdeutschland", _DE_HEAD),
        ("LU-NAP / AVL+CdT", _LU_HEAD),
        ("CH-NAP / 482-operator national bundle", _CH_HEAD),
    ],
)
def test_national_netex_defaults_to_epip(tmp_path: Path, name: str, head: str) -> None:
    """National-codespace NeTEx feeds without an explicit profile marker
    now default to NeTEx-EPIP instead of either:
      (a) being false-flagged as Nordic because the head contains 'ent:'
          inside element/attribute names (the AT/BE/DE/LU regression), or
      (b) being rejected with "profile could not be identified" (CH).

    OTP can read EPIP-shaped NeTEx; if a national feed genuinely deviates,
    the build surfaces a clearer error than detection would.
    """
    zip_path = _zip_with_xml(tmp_path, head)
    assert detect.detect(zip_path) == "NeTEx-EPIP", f"failed for {name}"


# ─────────────────────── Nordic still works on real markers ─────────────


def test_nordic_recognised_via_xmlns_nsr(tmp_path: Path) -> None:
    """A real Nordic feed declares `xmlns:nsr=…` in the head — that's the
    real marker, not the bare `"ent:"` substring used previously."""
    zip_path = _zip_with_xml(tmp_path, _NORDIC_HEAD)
    assert detect.detect(zip_path) == "NeTEx-Nordic"


def test_nordic_recognised_via_codespace_attribute(tmp_path: Path) -> None:
    """Some feeds put the codespace in an attribute rather than as an
    xmlns prefix. Accept that too. The XML still needs the NeTEx
    namespace declaration so the outer `_detect_zip` guard
    (`"netex" in head`) lets us through to `_classify_netex` in the
    first place — `codespace="nsr"` alone doesn't contain "netex"."""
    head = (
        '<?xml version="1.0"?>'
        '<PublicationDelivery xmlns="http://www.netex.org.uk/netex" codespace="nsr">'
        '<ScheduledStopPoint id="x"/></PublicationDelivery>'
    )
    zip_path = _zip_with_xml(tmp_path, head)
    assert detect.detect(zip_path) == "NeTEx-Nordic"


# ─────────────────────── FR profile (no false-positive on bare 'fr:') ───


def test_fr_profile_recognised_via_xmlns(tmp_path: Path) -> None:
    """`xmlns:fr="…"` declaration must be matched by the classifier."""
    zip_path = _zip_with_xml(tmp_path, _FR_HEAD, filename="horaires_2026.xml")
    assert detect.detect(zip_path) == "NeTEx-FR-Horaires"


def test_fr_arrets_variant_recognised_by_filename_hint(tmp_path: Path) -> None:
    """When the FR codespace is present AND the zip contains an arrets/
    stops file, it's the stops variant."""
    out = tmp_path / "netex.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("arrets-2026.xml", _FR_HEAD)
    assert detect.detect(out) == "NeTEx-FR-Arrets"


def test_fr_netex_recognised_via_version_string(tmp_path: Path) -> None:
    """Some FR NAP feeds (notably ZOU / Region-Sud) omit the
    `xmlns:fr=…` declaration and carry the codespace only in IDs and the
    version string like `1.09:FR-NETEX_ARRET-2.1-1.0`. The `fr-netex`
    literal in the head must be enough to flag NeTEx-FR — otherwise the
    file falls through to EPIP and OTP fails opaquely at build time.
    """
    head = (
        '<?xml version="1.0"?>'
        '<PublicationDelivery version="1.09:FR-NETEX_ARRET-2.1-1.0" '
        '                     xmlns="http://www.netex.org.uk/netex">'
        "<ParticipantRef>ZOU</ParticipantRef>"
        '<Quay id="FR:Quay:SNC_VSP_FR__LMO_x:"/>'
    )
    # Filename hints at "arrets" → Arrets variant.
    zip_path = _zip_with_xml(tmp_path, head, filename="arrets.xml")
    assert detect.detect(zip_path) == "NeTEx-FR-Arrets"


def test_bare_fr_substring_does_not_misfire(tmp_path: Path) -> None:
    """A head with `<FreeText>` or `<TransferDuration>` contains the
    substring "fr:" only inside element / attribute markup — the old
    over-loose check would have classified this as NeTEx-FR. With the
    fix it falls through to EPIP."""
    head = (
        '<?xml version="1.0"?><PublicationDelivery xmlns="http://www.netex.org.uk/netex">'
        "<FreeText>From Genève to Lausanne</FreeText></PublicationDelivery>"
    )
    zip_path = _zip_with_xml(tmp_path, head)
    # No NeTEx-FR marker → falls through to EPIP (the EU-wide default).
    assert detect.detect(zip_path) == "NeTEx-EPIP"


# ─────────────────────── EPIP explicit marker ─────────────────────────


def test_epip_explicit_marker(tmp_path: Path) -> None:
    """A head containing 'EPIP' (case-insensitive) maps to NeTEx-EPIP
    before falling through to the EPIP default — same destination but the
    detection reason is the explicit marker, not the fallback."""
    zip_path = _zip_with_xml(tmp_path, _EPIP_HEAD)
    assert detect.detect(zip_path) == "NeTEx-EPIP"


# ─────────────────────── non-NeTEx, non-GTFS still rejects ─────────────


def test_iff_zip_rejected(tmp_path: Path) -> None:
    """The NL NAP "NeTEx" file is actually IFF (.dat files). detect.py
    has no IFF support — `_detect_zip` should raise rather than guess.
    Pinned here so a future change that adds .dat heuristics for some
    other reason can't accidentally make IFF look detectable as NeTEx.
    """
    out = tmp_path / "iff.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("stations.dat", b"placeholder")
        z.writestr("timetbls_new.dat", b"placeholder")
        z.writestr("company.dat", b"placeholder")
    with pytest.raises(ValueError, match="Zip does not match any known schema"):
        detect.detect(out)


def test_zip_with_only_non_netex_xml_rejected(tmp_path: Path) -> None:
    """A zip of XMLs that aren't NeTEx (e.g. SIRI alone, GTFS-RT protobuf
    metadata) is rejected — the old behaviour. Pin it so the EPIP fallback
    doesn't accidentally swallow non-NeTEx XML."""
    out = tmp_path / "siri.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "siri.xml",
            '<?xml version="1.0"?><Siri xmlns="http://www.siri.org.uk/siri"/>',
        )
    with pytest.raises(ValueError, match="namespace is not NeTEx"):
        detect.detect(out)


# ─────────────────────── GTFS classifier untouched ─────────────────────


def test_gtfs_zip_with_canonical_files(tmp_path: Path) -> None:
    """Sanity: the GTFS branch of detect() isn't affected by the NeTEx
    classifier changes."""
    out = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(out, "w") as z:
        for fn in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "agency.txt"):
            z.writestr(fn, "header\n")
    assert detect.detect(out) == "GTFS"
