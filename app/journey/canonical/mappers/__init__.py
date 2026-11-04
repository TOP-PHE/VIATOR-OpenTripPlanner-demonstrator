"""CanonicalJourney → standard journey formats: OJP 2.0 (`ojp`) and OSDM (`osdm`).

Proof of concept for #303 AC5; the export layers themselves are #306 (OJP) and #307 (OSDM).
"""

from __future__ import annotations

from datetime import datetime


def iso_duration(seconds: int) -> str:
    """Whole seconds as an ISO-8601 duration (`PT1H9M`, `PT0S`), the form OJP and OSDM use."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{hours}H" if hours else "",
        f"{minutes}M" if minutes else "",
        f"{secs}S" if secs else "",
    ]
    return "PT" + ("".join(parts) or "0S")


def iso_instant(value: datetime) -> str:
    """A UTC instant as `YYYY-MM-DDTHH:MM:SSZ`."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
