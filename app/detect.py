"""File-format detection. Verifies a user's declared standard matches reality.

Detection is intentionally conservative: it returns one of the known kinds, or raises.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

KNOWN_KINDS = {
    "GTFS",
    "NeTEx-FR-Horaires",
    "NeTEx-FR-Arrets",
    "NeTEx-Nordic",
    "NeTEx-EPIP",
    "SNCF-MCT",
    "SNCF-Stations",
    "OSM-PBF",
}

GTFS_REQUIRED = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "agency.txt"}


def detect(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".pbf":
        with path.open("rb") as f:
            head = f.read(4)
        if head == b"\x00\x00\x00\x0d":
            return "OSM-PBF"
        raise ValueError("File has .pbf extension but invalid header")

    if suffix == ".zip":
        return _detect_zip(path)

    if suffix == ".csv":
        return _detect_csv(path)

    raise ValueError(f"Unsupported extension: {suffix}")


def _detect_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())

        # GTFS = flat zip with the canonical text files.
        basenames = {n.split("/")[-1] for n in names}
        if GTFS_REQUIRED.issubset(basenames):
            return "GTFS"

        # NeTEx = zip of XMLs in the NeTEx namespace.
        xml_files = sorted(n for n in names if n.lower().endswith(".xml"))
        if xml_files:
            with z.open(xml_files[0]) as x:
                head = x.read(8192).decode("utf-8", errors="ignore")
            if "netex" not in head.lower():
                raise ValueError("Zip contains XML but namespace is not NeTEx")
            return _classify_netex(head, xml_files)

        # CSV bundles: SNCF MCT or stations.
        csv_files = [n.upper() for n in names if n.lower().endswith(".csv")]
        if any("CONNECTION_TIMES" in n for n in csv_files):
            return "SNCF-MCT"
        if any("STATION" in n or "GARE" in n for n in csv_files):
            return "SNCF-Stations"

    raise ValueError("Zip does not match any known schema")


def _classify_netex(xml_head: str, files: list[str]) -> str:
    """Map a NeTEx XML head to a known profile string.

    Strategy: look for explicit codespace / namespace-prefix markers rather
    than free-text substring matches. The previous `"ent:" in xml_head`
    heuristic matched any XML containing element names ending in `ent:`
    (Component, Document, Element, ...) which is essentially every non-
    trivial NeTEx file, so AT / BE / DE / LU national feeds were all
    false-flagged as NeTEx-Nordic. The fix is to require the codespace
    prefix to appear as a real xmlns declaration or in a Codespace /
    ParticipantRef attribute.

    Unrecognised national NeTEx (CH `ch:1:`, AT `at:obb:`, DE `DE::`,
    LU `LU::`, etc.) falls through to NeTEx-EPIP — the EU-wide passenger-
    info profile that OTP can read. Most national NAP feeds are EPIP-
    derived even when they don't advertise the profile string in the head;
    if a feed genuinely deviates, OTP's build surfaces a clearer downstream
    error than the previous "profile could not be identified" rejection at
    detection time.
    """
    head_lower = xml_head.lower()
    # NeTEx-FR — French national profile. The xmlns prefix declaration
    # `xmlns:fr="…"` (or an explicit codespace attribute) is the real
    # marker; a bare `"fr:"` substring would also match `<FreeText>` etc.
    # Some FR NAP feeds (e.g. ZOU "FR-NAP_gtfs_Region-Sud_ZOU.zip") omit
    # the xmlns:fr declaration and only carry the codespace inside `FR:…`
    # IDs and a version string like `1.09:FR-NETEX_ARRET-2.1-1.0`. The
    # `fr-netex` literal in that version string is itself a clear signal
    # — match it as a fallback so those files aren't mis-classified as
    # EPIP and then archived to the wrong slot.
    if (
        re.search(r"xmlns:fr\s*=", xml_head)
        or 'codespace="fr"' in head_lower
        or 'participantref="fr' in head_lower
        or "fr-netex" in head_lower
    ):
        if any("arrets" in f.lower() or "stops" in f.lower() for f in files):
            return "NeTEx-FR-Arrets"
        return "NeTEx-FR-Horaires"
    # NeTEx-Nordic — Norwegian/Swedish/Finnish national stop register
    # codespace `nsr:`. Look for the xmlns declaration or an explicit
    # codespace attribute; the bare-substring check for `ent:` that lived
    # here previously produced false positives on every national NeTEx
    # (the substring appears in commonplace element names).
    if re.search(r"xmlns:nsr\s*=", xml_head) or 'codespace="nsr"' in head_lower:
        return "NeTEx-Nordic"
    # NeTEx-EPIP — explicit profile marker in the head. Some Italian and
    # Belgian feeds advertise it via a comment, a ProfileRef or a
    # Codespace name.
    if "epip" in head_lower:
        return "NeTEx-EPIP"
    # Unrecognised national profile (CH `ch:1:`, AT `at:obb:`, DE `DE::`,
    # LU `LU::`, etc.). Treat as EPIP — the EU-wide passenger-info profile.
    # Most national NAP feeds conform closely enough that OTP can build a
    # graph from them; a true incompatibility surfaces at build time with
    # a useful error message instead of being rejected here on a heuristic.
    return "NeTEx-EPIP"


def _detect_csv(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        header = f.readline().lower()
    if "uic" in header and ("trigramme" in header or "gare" in header or "code_uic" in header):
        return "SNCF-Stations"
    if "correspondance" in header or "connection" in header or "transfer" in header:
        return "SNCF-MCT"
    raise ValueError("CSV header does not match SNCF stations or MCT")
