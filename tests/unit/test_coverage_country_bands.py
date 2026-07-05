"""Trip-wire tests for the coverage matrix's country + type header bands.

The live matrix (network_coverage.html) builds this client-side in JS;
the offline export (network_coverage_export.html) renders it server-side
via Jinja from `_build_export_context`'s pre-computed `band_color` /
`country_rowspan` / `country_col_runs` (covered separately in
test_coverage_export.py). This file pins the markup/JS contract each
template depends on so a refactor that drops a helper or a class name is
caught here rather than by an operator seeing an unstyled matrix.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.api.admin.network_coverage import HubCreate, HubUpdate, create_hub, update_hub

REPO = Path(__file__).resolve().parents[2]
LIVE_TEMPLATE = REPO / "app" / "templates" / "admin" / "network_coverage.html"
EXPORT_TEMPLATE = REPO / "app" / "templates" / "admin" / "network_coverage_export.html"


@pytest.fixture(scope="module")
def live_text() -> str:
    return LIVE_TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def export_text() -> str:
    return EXPORT_TEMPLATE.read_text(encoding="utf-8")


# ─────────────────────── live matrix (JS) ───────────────────────


def test_country_hues_helper_defined(live_text: str):
    assert re.search(r"function\s+countryColor\s*\(", live_text)
    assert "const COUNTRY_HUES" in live_text


def test_group_runs_helper_defined(live_text: str):
    assert re.search(r"function\s+groupRuns\s*\(", live_text)


def test_header_bands_use_country_and_type_classes(live_text: str):
    assert "cov-band-country" in live_text
    assert "cov-band-type" in live_text
    assert "cov-corner" in live_text


def test_type_band_falls_back_to_unclassified_marker(live_text: str):
    """A hub with no `modes` value must render `?`, not `undefined` or a
    blank cell an operator could mistake for a rendering bug."""
    assert "h.modes || '?'" in live_text or "orig.modes || '?'" in live_text


def test_old_region_based_header_colouring_is_removed(live_text: str):
    """The country band supersedes the old per-region `th` tint — leaving
    both active would silently break the new colouring for FR hubs via
    the old rule's `!important`."""
    assert "region-${" not in live_text
    assert ".region-paris" not in live_text


def test_legend_explains_the_type_codes(live_text: str):
    for code in ("Rail", "Tram", "Metro", "Bus", "Coach"):
        assert code in live_text


def test_matrix_uses_fixed_table_layout_with_colgroup(live_text: str):
    """Regression lock for a real bug report: scrolled data cells bled
    through the sticky country/type/name columns. Root cause was
    table-layout: auto silently shrinking a declared-width column
    (band-type's cells are all a single "?" glyph) below its CSS
    `width`, which desynced it from the next sticky column's
    calc()-based `left` offset. `fixed` layout only sizes columns
    correctly when paired with a <colgroup> declaring every column's
    width — colspan'd header cells can't do that on their own — and the
    table needs `width: max-content` or it proportionally shrinks the
    declared widths back down to fit the wrapper, reintroducing the
    exact same gap."""
    assert "table-layout: fixed" in live_text
    assert "width: max-content" in live_text
    assert re.search(r"const colgroup = ", live_text)
    assert "--cov-hub-label-w" in live_text and "--cov-data-col-w" in live_text


def test_matrix_tbody_sticky_cells_have_explicit_z_index(live_text: str):
    """The gap-closing fix alone wasn't sufficient — even with zero gap,
    a rowspan'd sticky <th> without an explicit z-index let plain <td>s
    from later rows paint over it during horizontal scroll (confirmed
    via elementFromPoint in a browser, not just geometry). Every tbody
    sticky th needs the same explicit value so none of them can lose
    that fight to a data cell."""
    assert re.search(r"\.cov-matrix tbody th \{[^}]*z-index:\s*1", live_text)


# ─────────────────────── offline export (Jinja) ───────────────────────


def test_export_thead_has_three_band_rows(export_text: str):
    assert "band-country" in export_text
    assert "band-type" in export_text
    assert "country_col_runs" in export_text


def test_export_type_band_falls_back_to_unclassified_marker(export_text: str):
    assert "h.modes or '?'" in export_text or "orig.modes or '?'" in export_text


def test_export_legend_explains_the_type_codes(export_text: str):
    for code in ("Rail", "Tram", "Metro", "Bus", "Coach"):
        assert code in export_text


def test_export_matrix_uses_fixed_table_layout_with_colgroup(export_text: str):
    """Same regression lock as the live matrix (see the sibling test):
    the offline export had the identical table-layout: auto + declared
    -width-column mismatch, worse in practice since its px-based guess
    (38px) was further from the "?" glyph's real rendered width than
    the live app's rem-based one."""
    assert "table-layout: fixed" in export_text
    assert "width: max-content" in export_text
    assert "<colgroup>" in export_text
    assert "--axis-row-w" in export_text and "--data-col-w" in export_text


def test_export_matrix_tbody_sticky_cells_have_explicit_z_index(export_text: str):
    """band-country and band-type had NO z-index at all (only axis-row
    did) — confirmed via elementFromPoint in a browser that scrolled
    data cells painted over them despite correct geometry. All three
    need it."""
    assert re.search(r"th\.band-country\s*\{[^}]*z-index:\s*1", export_text)
    assert re.search(r"th\.band-type\s*\{[^}]*z-index:\s*1", export_text)
    assert re.search(r"th\.axis-row\s*\{[^}]*z-index:\s*1", export_text)


# ─────────────────────── modes field round-trip (hub CRUD) ───────────────────────


def _fake_actor():
    a = MagicMock()
    a.id = uuid.uuid4()
    return a


def test_create_hub_persists_modes():
    db = MagicMock()
    body = HubCreate(
        id="test-hub", name="Test Hub", short="Test", country="AT", lat=48.0, lon=16.0, modes="R+M"
    )

    result = create_hub(body=body, db=db, _=_fake_actor())

    assert result.modes == "R+M"
    added_hub = db.add.call_args[0][0]
    assert added_hub.modes == "R+M"


def test_create_hub_defaults_modes_to_none_when_not_classified():
    db = MagicMock()
    body = HubCreate(id="test-hub", name="Test Hub", short="Test", country="AT", lat=48.0, lon=16.0)

    result = create_hub(body=body, db=db, _=_fake_actor())

    assert result.modes is None


def test_update_hub_can_set_modes():
    hub = MagicMock()
    hub.id = "test-hub"
    hub.name = "Test Hub"
    hub.short = "Test"
    hub.region = None
    hub.country = "AT"
    hub.tier = "main"
    hub.modes = None
    hub.lat = 48.0
    hub.lon = 16.0
    hub.is_active = True
    hub.sort_order = 100
    db = MagicMock()
    db.get.return_value = hub

    update_hub(hub_id="test-hub", body=HubUpdate(modes="R+T"), db=db, _=_fake_actor())

    assert hub.modes == "R+T"
