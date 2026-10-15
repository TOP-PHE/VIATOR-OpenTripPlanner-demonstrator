"""A station without a UIC code never enters stop-id routing or signatures.

The contract with the station module, phase 2 (its design 22.12): VIATOR
accepts a module station whose `uic` is null when it carries a VIATOR id
(`ZZ-99999` in form). VIATOR's planner and signatures stay keyed by the UIC
parsed from stop ids (app/journey/signature.py), so such a station falls
back to coordinates; and a VIATOR id is never read as a station code, nor
written where a UIC goes. Invented values only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.api.journey import Coord, _stop_id_for
from app.journey import signature
from app.models import StationXref


def test_a_viator_id_sent_as_a_station_code_is_read_as_none() -> None:
    coord = Coord(lat=50.0, lon=4.0, label="Zz Halt", uic="ZZ-99004")

    assert coord.uic is None


def test_a_station_code_and_no_code_are_kept_as_they_are() -> None:
    assert Coord(lat=50.0, lon=4.0, uic="9900001").uic == "9900001"
    assert Coord(lat=50.0, lon=4.0, uic=None).uic is None
    assert Coord(lat=50.0, lon=4.0).uic is None


def test_a_station_without_a_code_gets_no_stop_id() -> None:
    session = SimpleNamespace(config={"sources": {"providers": [{"id": "ZZFEED"}]}})
    coord = Coord(lat=50.0, lon=4.0, uic="ZZ-99004")

    assert _stop_id_for(session, coord.uic) is None  # type: ignore[arg-type]
    assert _stop_id_for(session, "9900001") == "ZZFEED:9900001"  # type: ignore[arg-type]


@pytest.mark.parametrize("stop_id", ["ZZ-99004", "zzfeed:ZZ-99004", "ZZFEED:ZZ-99004:0"])
def test_a_viator_id_in_a_stop_id_parses_as_no_uic(stop_id: str) -> None:
    assert signature._uic_from_stop_id(stop_id) is None
    token = signature._fingerprint_stop_token(stop_id, 50.0, 4.0)
    assert not token.startswith("UIC:")


class _NoXref:
    """A session whose `stations_xref` has no row for the stop."""

    def get(self, *_args: Any) -> None:
        return None


def test_a_stop_without_a_uic_row_signs_by_its_position() -> None:
    token = signature._stop_token(
        _NoXref(),  # type: ignore[arg-type]
        session_id="zz-session",
        stop_id="ZZ-99004",
        lat=50.1234,
        lon=4.54321,
    )

    assert token == "@50.1234,4.5432"


def test_stations_xref_keys_a_uic_of_master_stations_only() -> None:
    """The bridge to a VIATOR id is a later step, not through this table:
    its `uic` references `master_stations.uic` (22.12, last paragraph)."""
    (constraint,) = [
        c for c in StationXref.__table__.constraints if getattr(c, "name", None) == "fk_xref_uic"
    ]
    (element,) = constraint.elements  # type: ignore[attr-defined]
    assert element.target_fullname == "master_stations.uic"
