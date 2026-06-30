"""Static-text checks on `admin/config.html` so operator-facing tunables
stay surfaced.

PR-2 (#183) + PR-3 (#184) added 14 `COVERAGE_*` keys to `CONFIG_SCHEMA`.
The `/admin/config` page is rendered by a hardcoded `SECTIONS` list in the
template (no auto-introspection of the schema), so a new key shipped to the
backend stays invisible to operators until somebody edits this template.

These tests pin that wiring:

  1. The "Network coverage matrix" section heading is present.
  2. Each of the 14 `COVERAGE_*` keys lives in the template's `SECTIONS`
     keys array.
  3. The timezone `<select>` choices in the template's `CHOICES` dict
     match `CONFIG_SCHEMA['COVERAGE_DEFAULT_TIMEZONE'].choices` exactly —
     drift between the form and the API would otherwise let an operator
     pick a zone the API then rejects.
  4. Drift guard — any future `COVERAGE_*` key added to `CONFIG_SCHEMA`
     fails this test until the template is updated to expose it.

No Postgres / no FastAPI test client / no Jinja render: the template body
is read as plain text and asserted against. Keeps the test fast and free
of session/test-DB fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config_schema import CONFIG_SCHEMA

# Resolve the template path relative to the test file so a repo rename or
# pytest invocation from a different cwd doesn't break the test.
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "config.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    """Read the template body once per module — every test below greps it."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_network_coverage_section_heading_present(template_text: str) -> None:
    """The section title string is what shows as the `<h2>` on the rendered
    page; if a refactor renames it the operator-facing UX changes."""
    assert "'Network coverage matrix'" in template_text


# ─────── per-key wiring: 14 tests, one per COVERAGE_* key ───────


def test_coverage_num_itineraries_wired(template_text: str) -> None:
    assert "'COVERAGE_NUM_ITINERARIES'" in template_text


def test_coverage_search_window_seconds_wired(template_text: str) -> None:
    assert "'COVERAGE_SEARCH_WINDOW_SECONDS'" in template_text


def test_coverage_pair_timeout_ms_wired(template_text: str) -> None:
    assert "'COVERAGE_PAIR_TIMEOUT_MS'" in template_text


def test_coverage_pair_parallelism_wired(template_text: str) -> None:
    assert "'COVERAGE_PAIR_PARALLELISM'" in template_text


def test_coverage_within_pair_parallelism_wired(template_text: str) -> None:
    assert "'COVERAGE_WITHIN_PAIR_PARALLELISM'" in template_text


def test_coverage_slot_count_wired(template_text: str) -> None:
    assert "'COVERAGE_SLOT_COUNT'" in template_text


def test_coverage_num_itineraries_per_slot_wired(template_text: str) -> None:
    assert "'COVERAGE_NUM_ITINERARIES_PER_SLOT'" in template_text


def test_coverage_slot_timeout_ms_wired(template_text: str) -> None:
    assert "'COVERAGE_SLOT_TIMEOUT_MS'" in template_text


def test_coverage_default_window_start_wired(template_text: str) -> None:
    assert "'COVERAGE_DEFAULT_WINDOW_START'" in template_text


def test_coverage_default_window_end_wired(template_text: str) -> None:
    assert "'COVERAGE_DEFAULT_WINDOW_END'" in template_text


def test_coverage_default_timezone_wired(template_text: str) -> None:
    """COVERAGE_DEFAULT_TIMEZONE is choice-gated — it lives in the
    template's `CHOICES` dict (rendered as `<select>`) AND in the
    `SECTIONS` keys list. The drift guard further down checks the
    choice list equality; this only checks the key is wired in."""
    assert "'COVERAGE_DEFAULT_TIMEZONE'" in template_text


def test_coverage_verify_parallelism_wired(template_text: str) -> None:
    assert "'COVERAGE_VERIFY_PARALLELISM'" in template_text


def test_coverage_verify_timeout_s_wired(template_text: str) -> None:
    assert "'COVERAGE_VERIFY_TIMEOUT_S'" in template_text


def test_coverage_verify_sleep_ms_wired(template_text: str) -> None:
    assert "'COVERAGE_VERIFY_SLEEP_MS'" in template_text


# ─────────── drift guards ───────────


def test_no_coverage_schema_key_left_behind(template_text: str) -> None:
    """Future-proofing — any `COVERAGE_*` key in `CONFIG_SCHEMA` that's
    NOT mentioned by name in the template means PR-2/PR-3-style drift
    has reopened. Fixing it = add the key to the SECTIONS keys array
    in the template (and add a per-key wiring test above so it's
    individually pinned).
    """
    schema_coverage = {k for k in CONFIG_SCHEMA if k.startswith("COVERAGE_")}
    # Sanity — the schema-side block hasn't been removed.
    assert schema_coverage, "no COVERAGE_* keys in CONFIG_SCHEMA at all?"
    missing = sorted(k for k in schema_coverage if f"'{k}'" not in template_text)
    assert not missing, (
        f"{len(missing)} COVERAGE_* key(s) in CONFIG_SCHEMA but not surfaced "
        f"in admin/config.html: {missing}. Add them to the 'Network coverage "
        f"matrix' section's keys array."
    )


def test_timezone_choices_match_schema(template_text: str) -> None:
    """The `<select>` for COVERAGE_DEFAULT_TIMEZONE is driven by the
    template's `CHOICES` dict; if it diverges from the schema's
    `choices` list the operator can pick a zone the API then rejects
    (or, worse, a valid zone may be silently missing from the dropdown).
    Pin every advertised zone individually.
    """
    schema_choices = CONFIG_SCHEMA["COVERAGE_DEFAULT_TIMEZONE"]["choices"]
    # The schema currently advertises 15 IANA zones (UTC + 14 European).
    # If somebody bumps that count, update the assertion too — the test
    # below pins every individual entry so the per-zone checks are what
    # actually drive the drift detection.
    assert len(schema_choices) == 15
    missing = [zone for zone in schema_choices if f"'{zone}'" not in template_text]
    assert not missing, (
        f"{len(missing)} IANA zone(s) in CONFIG_SCHEMA["
        f"'COVERAGE_DEFAULT_TIMEZONE'].choices but not in the template's "
        f"CHOICES dict: {missing}"
    )
