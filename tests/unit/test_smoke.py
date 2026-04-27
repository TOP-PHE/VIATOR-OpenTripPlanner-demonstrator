"""Import-chain smoke tests.

Purpose: ensure the project's modules can be imported without errors.
This is the cheapest possible regression guard against:
  - syntax errors
  - circular imports
  - missing dependencies in requirements.txt
  - broken module reorganisations

If a real bug ever sneaks past these, that's a signal we need a focused test
in the right module — not that this file should grow.
"""

from __future__ import annotations

import importlib

import pytest

EXPECTED_MODULES = [
    "app",
    "app.settings",
    "app.db",
    "app.detect",
    "app.ingestion",
    "app.main",
    "app.worker",
]


@pytest.mark.parametrize("module_name", EXPECTED_MODULES)
def test_module_imports_cleanly(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_detect_known_kinds_is_non_empty() -> None:
    """Tiny invariant: the detector must declare at least the eight kinds the spec lists."""
    from app.detect import KNOWN_KINDS

    assert len(KNOWN_KINDS) >= 8
    assert "GTFS" in KNOWN_KINDS
    assert "OSM-PBF" in KNOWN_KINDS
    assert "SNCF-MCT" in KNOWN_KINDS


def test_settings_has_safe_defaults() -> None:
    """Settings() must construct without raising even with minimal env."""
    from app.settings import Settings

    s = Settings()
    assert s.max_upload_mb > 0
    assert s.debounce_seconds > 0
