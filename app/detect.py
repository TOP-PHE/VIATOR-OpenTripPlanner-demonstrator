"""File-format detection. Verifies a user's declared standard matches reality.

Detection is intentionally conservative: it returns one of the known kinds, or raises.
"""

from __future__ import annotations

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
    head_lower = xml_head.lower()
    # Heuristic: codespace prefixes
    if "fr:" in xml_head or 'codespace="fr"' in head_lower or 'participantref="fr' in head_lower:
        if any("arrets" in f.lower() or "stops" in f.lower() for f in files):
            return "NeTEx-FR-Arrets"
        return "NeTEx-FR-Horaires"
    if "nsr:" in xml_head or "ent:" in xml_head:
        return "NeTEx-Nordic"
    if "epip" in head_lower:
        return "NeTEx-EPIP"
    # default to a clearly-labelled unknown so the dispatcher can refuse
    raise ValueError("NeTEx file detected, but profile (FR/Nordic/EPIP) could not be identified")


def _detect_csv(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        header = f.readline().lower()
    if "uic" in header and ("trigramme" in header or "gare" in header or "code_uic" in header):
        return "SNCF-Stations"
    if "correspondance" in header or "connection" in header or "transfer" in header:
        return "SNCF-MCT"
    raise ValueError("CSV header does not match SNCF stations or MCT")
