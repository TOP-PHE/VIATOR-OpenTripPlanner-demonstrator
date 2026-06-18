"""P1 MOTIS — dispatch tests.

`planner_dispatch.get_planner(session)` and `planner_for_engine(engine)`
must return the right module for each engine value, default to OTP for
the legacy/no-engine case (so test fixtures predating P1 still work),
and raise on typos rather than silently degrading to OTP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.journey import motis_client, otp_client, planner_dispatch


def test_get_planner_routes_otp_engine_to_otp_client():
    s = SimpleNamespace(engine="otp")
    assert planner_dispatch.get_planner(s) is otp_client


def test_get_planner_routes_motis_engine_to_motis_client():
    s = SimpleNamespace(engine="motis")
    assert planner_dispatch.get_planner(s) is motis_client


def test_get_planner_defaults_to_otp_when_engine_attribute_missing():
    # Legacy fixtures that build SessionRow without the engine attribute
    # (tests written pre-P1) must continue to route through OTP.
    s = SimpleNamespace()  # no `engine` attr at all
    assert planner_dispatch.get_planner(s) is otp_client


def test_get_planner_defaults_to_otp_when_engine_is_none_or_empty():
    # Defensive: an existing DB row whose `engine` column somehow ended up
    # NULL or empty falls back to OTP rather than crashing the live
    # journey UI. Column is NOT NULL at the DB layer so this is belt-and-
    # braces, but cheap insurance.
    assert planner_dispatch.get_planner(SimpleNamespace(engine=None)) is otp_client
    assert planner_dispatch.get_planner(SimpleNamespace(engine="")) is otp_client


def test_planner_for_engine_string_dispatch():
    assert planner_dispatch.planner_for_engine("otp") is otp_client
    assert planner_dispatch.planner_for_engine("motis") is motis_client


def test_planner_for_engine_raises_on_typo():
    # A typo should be loud — silently routing to OTP would mask a config
    # mistake until somebody noticed query results coming from the wrong
    # planner. The coverage runner and journey route both rely on this
    # contract.
    with pytest.raises(ValueError, match="Unknown session engine"):
        planner_dispatch.planner_for_engine("ojp")
    with pytest.raises(ValueError, match="Unknown session engine"):
        planner_dispatch.planner_for_engine("OTP")  # case-sensitive on purpose


def test_get_planner_raises_on_unknown_engine_value():
    with pytest.raises(ValueError, match="Unknown session engine"):
        planner_dispatch.get_planner(SimpleNamespace(engine="motis-experimental"))
