"""Which columns of `network_coverage_results` the pages listing every cell of a run load.

On 8 Nov 2026 the share page of a 123-hub run (~7,500 cells) timed out (120 s, then 459 s
and a 502 from the proxy): `runner.get_run_with_results` loaded every column of every row,
ÖBB's journeys (`external_journeys`, canonical JSON, about five a cell) included, for pages
that never draw them. The admin run view polls the same loader every 5 s. Since then each
caller asks for the JSON columns it draws; the others are deferred with `raiseload`:

- the admin run view (`GET .../runs/{id}`): the summary columns only;
- the share page: the summary + why each cell scored so (`external_alignment_detail`);
- the download: the summary + the detail, + ÖBB's journeys only up to
  `_EXPORT_LEG_DETAIL_MAX_PAIRS` rows (tested in test_coverage_export.py).

These tests compile the real SELECT (no database) and pin its columns, so a new column makes
them fail until someone decides whether a page listing every cell needs it, and pin what each
endpoint asks for. `tests/integration/test_coverage_result_columns.py` runs the three pages on
PostgreSQL and checks they show the same data as a load of every column.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models import NetworkCoverageResult
from app.network_coverage import runner

RUN_ID = uuid.UUID("99999999-9999-4999-8999-999999999999")

# What a page listing every cell reads of each row: the matrix, its dots and its heatmap.
SUMMARY_COLUMNS = {
    "id",
    "run_id",
    "origin_hub_id",
    "dest_hub_id",
    "status",
    "response_ms",
    "num_itineraries",
    "best_duration_seconds",
    "best_num_transfers",
    "best_operators",
    "session_ids",
    "error_message",
    "journey_search_id",
    "created_at",
    "external_verified_at",
    "external_ok",
    "external_num_connections",
    "external_best_duration_seconds",
    "external_best_transfers",
    "external_source",
    "external_error",
    "external_alignment_score",
    "external_alignment_tier",
}
# Why the cell scored so: a few hundred bytes a cell, printed by the share page and the report.
DETAIL = "external_alignment_detail"
# ÖBB's journeys: most of a row's bytes, embedded by the small download only.
JOURNEYS = "external_journeys"


def _selected(**flags: bool) -> set[str]:
    """The columns the SELECT of `runner.results_query` names, as PostgreSQL gets it."""
    sql = str(runner.results_query(RUN_ID, **flags).compile(dialect=postgresql.dialect()))
    select_list = sql.split("\nFROM ", 1)[0].removeprefix("SELECT ")
    columns = {part.strip() for part in select_list.split(",")}
    prefix = f"{NetworkCoverageResult.__tablename__}."
    assert all(column.startswith(prefix) for column in columns), sql
    return {column.removeprefix(prefix) for column in columns}


def test_every_column_of_the_table_is_sorted_into_summary_detail_or_journeys() -> None:
    """A new column fails here: add it to SUMMARY_COLUMNS when the pages listing every cell
    draw it, or leave it out of their SELECT in `runner.results_query` (deferred) when only
    a cell's own detail reads it."""
    table = {column.name for column in NetworkCoverageResult.__table__.columns}
    assert table == SUMMARY_COLUMNS | {DETAIL, JOURNEYS}


def test_the_run_view_loads_the_summary_columns_only() -> None:
    assert _selected() == SUMMARY_COLUMNS


def test_the_share_page_also_loads_why_each_cell_scored_so() -> None:
    assert _selected(alignment_detail=True) == SUMMARY_COLUMNS | {DETAIL}


def test_the_small_download_loads_every_column() -> None:
    assert _selected(alignment_detail=True, external_journeys=True) == (
        SUMMARY_COLUMNS | {DETAIL, JOURNEYS}
    )


def test_journeys_alone_leave_the_detail_out() -> None:
    assert _selected(external_journeys=True) == SUMMARY_COLUMNS | {JOURNEYS}


def test_the_rows_are_those_of_the_run() -> None:
    sql = str(runner.results_query(RUN_ID).compile(dialect=postgresql.dialect()))
    assert sql.endswith("WHERE network_coverage_results.run_id = %(run_id_1)s::UUID")


def test_the_polled_run_view_asks_for_no_cell_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The share page's flags are pinned in test_coverage_share.py (through its app), the
    download's in test_coverage_export.py."""
    from app.api.admin import network_coverage as mod

    asked: dict = {}

    def load(_db, _run_id, **flags):
        asked.update(flags)
        return None, []

    monkeypatch.setattr(mod.runner, "get_run_with_results", load)
    with pytest.raises(HTTPException):
        mod.get_run(RUN_ID, db=None, _=None)
    assert asked == {}
