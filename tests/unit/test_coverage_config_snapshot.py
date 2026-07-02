"""PR-2 — `CoverageConfig` dataclass + `_load_coverage_config` helper.

The runner snapshots seven `COVERAGE_*` platform_config keys into a
frozen `CoverageConfig` at execute_run start; the snapshot is then
threaded through every per-pair helper for the rest of the run. These
tests pin:

  1. Dataclass defaults match the schema defaults (= the prior hardcoded
     module constants) — flipping a default here without matching the
     schema would silently drift behaviour from /admin/config.
  2. `_load_coverage_config` reads the cfg dict returned by
     `config_service.get_all` and surfaces each key on the dataclass
     with the right type.
  3. The dataclass is frozen — accidental in-flight mutation raises.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest

from app.network_coverage import runner


def test_coverage_config_defaults_match_schema():
    """Default-constructed CoverageConfig must equal the schema defaults
    for every key. Otherwise an operator who never touched /admin/config
    would see different behaviour from the runner than the schema
    advertises."""
    from app.config_schema import default_for

    cfg = runner.CoverageConfig()
    assert cfg.num_itineraries == default_for("COVERAGE_NUM_ITINERARIES")
    assert cfg.search_window_seconds == default_for("COVERAGE_SEARCH_WINDOW_SECONDS")
    assert cfg.pair_timeout_ms == default_for("COVERAGE_PAIR_TIMEOUT_MS")
    assert cfg.pair_parallelism == default_for("COVERAGE_PAIR_PARALLELISM")
    assert cfg.verify_parallelism == default_for("COVERAGE_VERIFY_PARALLELISM")
    assert cfg.verify_timeout_s == default_for("COVERAGE_VERIFY_TIMEOUT_S")
    assert cfg.verify_sleep_ms == default_for("COVERAGE_VERIFY_SLEEP_MS")


def test_coverage_config_is_frozen():
    """Frozen so a per-pair helper that accidentally mutates the
    snapshot doesn't poison the rest of the run."""
    cfg = runner.CoverageConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.num_itineraries = 1  # type: ignore[misc]


def test_load_coverage_config_reads_platform_config():
    """`_load_coverage_config` resolves every COVERAGE_* key from
    config_service.get_all and lands the values on the dataclass.
    This is the load-time contract — if config_service returns a value,
    we must use it (not fall back to the dataclass default)."""
    fake_cfg = {
        "COVERAGE_NUM_ITINERARIES": 100,
        "COVERAGE_SEARCH_WINDOW_SECONDS": 7200,
        "COVERAGE_PAIR_TIMEOUT_MS": 30_000,
        "COVERAGE_PAIR_PARALLELISM": 10,
        "COVERAGE_VERIFY_PARALLELISM": 1,
        "COVERAGE_VERIFY_TIMEOUT_S": 60,
        "COVERAGE_VERIFY_SLEEP_MS": 1_000,
    }
    with patch.object(runner.config_service, "get_all", return_value=fake_cfg):
        cfg = runner._load_coverage_config(db=None)  # type: ignore[arg-type]

    assert cfg.num_itineraries == 100
    assert cfg.search_window_seconds == 7_200
    assert cfg.pair_timeout_ms == 30_000
    assert cfg.pair_parallelism == 10
    assert cfg.verify_parallelism == 1
    assert cfg.verify_timeout_s == 60.0
    assert cfg.verify_sleep_ms == 1_000


def test_load_coverage_config_falls_back_to_defaults_for_missing_keys():
    """Defence in depth — if config_service ever returns a dict missing
    one of our keys (mid-migration, schema gap, …), each missing key
    falls back to the dataclass default instead of KeyError-ing the
    runner mid-loop."""
    with patch.object(runner.config_service, "get_all", return_value={}):
        cfg = runner._load_coverage_config(db=None)  # type: ignore[arg-type]

    # All fields equal the dataclass defaults.
    assert cfg == runner.CoverageConfig()
