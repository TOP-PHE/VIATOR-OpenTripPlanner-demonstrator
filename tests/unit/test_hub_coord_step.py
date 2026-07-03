"""Regression test for the Promote-to-Hub coordinate step-mismatch bug.

The lat/lon inputs on the Promote-to-Hub modal (journey.html) used to carry
`step="0.000001"`, a hard HTML5 constraint requiring the value to land
exactly on a 6-decimal grid. Coordinates pulled from a MOTIS/OTP trip
result routinely carry more (or fewer, via float round-trip noise)
decimal digits than that, so the browser's own step-mismatch validation
rejected perfectly valid values and displayed a confusing native error
suggesting a truncated coordinate the operator never typed — easily
mistaken for a VIATOR/backend precision limit, when none exists (the API
and DB layers accept any-precision floats; only range is validated).

Fix: `step="any"` disables the step grid entirely, matching the backend's
actual (rangeonly) validation contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "app" / "templates" / "journey.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_hub_lat_lon_inputs_accept_any_precision(template_text: str):
    assert 'id="hub-lat" required' in template_text
    assert 'id="hub-lon" required' in template_text
    assert 'step="any" id="hub-lat"' in template_text
    assert 'step="any" id="hub-lon"' in template_text


def test_hub_lat_lon_inputs_no_longer_use_fixed_step(template_text: str):
    """A fixed step (e.g. 0.000001) reintroduces the step-mismatch bug —
    any full-precision coordinate not landing exactly on that grid would
    fail the browser's native validation again."""
    assert 'step="0.000001" id="hub-lat"' not in template_text
    assert 'step="0.000001" id="hub-lon"' not in template_text
