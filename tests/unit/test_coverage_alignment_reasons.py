"""#304, step 3b — why a coverage cell scored what it did, on the pages.

The lines under a cell's tier (`alignment.explain_cell`, from the detail the check kept, M6),
the summary sentence of each reason, the trips after ÖBB's last connection marked (M9), the
tolerance the run was scored with, and the cell answer, report and templates carrying them. No
database: stand-in rows.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.api.admin import network_coverage as api
from app.journey.matching import reasons as r
from app.network_coverage import runner
from app.network_coverage.alignment import explain_cell
from tests.unit.matching_journeys import journey, ride
from tests.unit.test_coverage_cell_trips_endpoint import (
    _db_returning,
    _fake_actor,
    _result_row,
    _run_row,
)

TEMPLATES = Path(__file__).parents[2] / "app" / "templates" / "admin"

DETAIL = {
    "engine_version": 1,
    "profile": "coverage_alignment",
    "counts": {
        "same_service": 1,
        "probably_same": 0,
        "viator_only": 2,
        "oebb_only": 1,
        "uncomparable": 0,
        "outside_reference_window": 3,
    },
    "reasons": {
        "label_spacing_only": 1,
        "shorter_transfer": 1,
        "splits_after_shared_ride": 1,
        "line_label_differs": 2,
        "outside_reference_window": 3,
        "paired_elsewhere": 1,
    },
}


# ── The lines ─────────────────────────────────────────────────────────────────────────────


def test_a_cell_s_lines_say_the_pairs_then_each_reason_most_telling_first() -> None:
    assert explain_cell(DETAIL) == [
        "VIATOR vs ÖBB: 1 same service, 0 probably the same, 2 VIATOR only, 1 ÖBB only",
        "VIATOR journeys after ÖBB's last connection, not compared (3 times)",
        "the same train first, then different trains",
        "after a shared train, one side changes much faster",
        "line labels differ (2 times)",
    ]


@pytest.mark.parametrize("detail", [None, "zz", {}, {"counts": "zz", "reasons": ["zz"]}])
def test_a_cell_without_a_readable_detail_has_no_reasons(detail: Any) -> None:
    lines = explain_cell(detail)

    assert lines in (
        [],
        ["VIATOR vs ÖBB: 0 same service, 0 probably the same, 0 VIATOR only, 0 ÖBB only"],
    )
    assert explain_cell(None) == []


def test_a_summary_sentence_counts_and_hides() -> None:
    assert r.summary_sentence(r.LINE_LABEL_DIFFERS, 1, "VIATOR", "ÖBB") == "line labels differ"
    assert (
        r.summary_sentence(r.LINE_LABEL_DIFFERS, 3, "VIATOR", "ÖBB")
        == "line labels differ (3 times)"
    )
    assert r.summary_sentence(r.LABEL_SPACING_ONLY, 4, "VIATOR", "ÖBB") == ""
    assert r.summary_sentence(r.LINE_LABEL_DIFFERS, 0, "VIATOR", "ÖBB") == ""
    assert r.summary_sentence(r.OUTSIDE_REFERENCE_WINDOW, 1, "A", "B") == (
        "A journeys after B's last connection, not compared"
    )
    # Every summary is a code of the vocabulary.
    assert set(r.SUMMARIES) <= set(r.SENTENCES)


# ── The trips after ÖBB's last connection ─────────────────────────────────────────────────


def _trip(departure: str, arrival: str) -> dict[str, Any]:
    found = journey(
        f"motis:{departure}", ride("Wien Hbf", "Linz Hbf", departure, arrival, published="RJ 540")
    )
    return {"rank": 0, "journey": found.model_dump(mode="json")}


def _oebb(departure: str, arrival: str) -> dict[str, Any]:
    found = journey(
        "hafas:0",
        ride("Wien Hbf", "Linz Hbf", departure, arrival, published="RJ 540"),
        provider="hafas",
    )
    return found.model_dump(mode="json")


def test_the_trips_after_oebb_s_last_connection_are_marked() -> None:
    trips = [_trip("08:00", "09:15"), _trip("10:00", "11:15"), {"rank": 2}]

    marked = api._with_window_marks(trips, [_oebb("08:00", "09:15")], 120)

    assert [trip.get("outside_reference_window", False) for trip in marked] == [False, True, False]
    # The input is not changed: a new dict for a marked trip.
    assert "outside_reference_window" not in trips[1]


@pytest.mark.parametrize("external", [None, [], [{"zz": 1}]])
def test_without_a_readable_oebb_answer_nothing_is_marked(external: Any) -> None:
    trips = [_trip("10:00", "11:15")]

    assert api._with_window_marks(trips, external, 120) == trips


def test_the_window_is_the_one_the_run_was_scored_with() -> None:
    frozen = asdict(runner.CoverageConfig(match_departure_tolerance_s=300))

    assert api._run_match_tolerance(SimpleNamespace(execution_snapshot={"config": frozen})) == 300
    assert api._run_match_tolerance(SimpleNamespace(execution_snapshot=None)) == 120
    assert api._run_match_tolerance(SimpleNamespace()) == 120


# ── The cell answer, the report, the templates ────────────────────────────────────────────


def test_the_cell_answer_carries_the_lines_and_the_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_row(direction="single")
    run.execution_snapshot = None
    row = _result_row(
        "wien",
        "linz",
        external_journeys=[_oebb("08:00", "09:15")],
        external_verified_at=None,
    )
    row.external_alignment_detail = DETAIL
    db = _db_returning(run=run, rows=[row])
    monkeypatch.setattr(
        api,
        "_fetch_trips_by_search",
        lambda _db, _ids, _depart_at=None: {
            str(row.journey_search_id): [_trip("08:00", "09:15"), _trip("10:00", "11:15")]
        },
    )

    resp = api.get_cell_trips(
        run_id=run.id, origin_id="wien", dest_id="linz", db=db, _=_fake_actor()
    )

    assert resp.outbound is not None
    assert resp.outbound.alignment_lines == explain_cell(DETAIL)
    assert [t.get("outside_reference_window", False) for t in resp.outbound.trips] == [False, True]


def test_the_report_s_cells_carry_the_lines() -> None:
    row = MagicMock()
    row.origin_hub_id, row.dest_hub_id, row.journey_search_id = "wien", "linz", None
    row.external_alignment_detail = DETAIL
    run = MagicMock()
    run.id, run.execution_snapshot = uuid.uuid4(), None

    context = api._build_export_context(run=run, results=[row], hubs=[], trips_by_search={})

    assert context["cells"]["wien:linz"]["alignment_lines"] == explain_cell(DETAIL)


@pytest.mark.parametrize("template", ["network_coverage.html", "network_coverage_export.html"])
def test_both_pages_print_the_lines_escaped_and_mark_late_trips(template: str) -> None:
    text = (TEMPLATES / template).read_text(encoding="utf-8")

    assert "alignment_lines" in text
    assert "outside_reference_window" in text
    # Every line goes through the page's escaping helper, never as markup.
    assert ("escHTML(line)" in text) or ("escapeHTML(line)" in text)
    assert "after ÖBB\N{RIGHT SINGLE QUOTATION MARK}s last connection: not compared" in text
