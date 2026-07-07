"""Unit tests for the per-cell trips drilldown endpoint
(feat/coverage-modal-inline-trips).

The endpoint is `GET /api/admin/network-coverage/runs/{run_id}/cells/
{origin_id}/{dest_id}/trips`. It powers the live admin modal's inline
trip rendering — previously the modal only showed the summary + a
"re-run live" link, which made coverage drill-downs guess-work.

Layers under test:

  1. Direction handling — runs created with direction='single' return
     `return=None` so the JS hides the return section entirely, vs
     direction='both' returning both sides.
  2. Missing rows — a cell that exists in one direction but not the
     reverse returns `return=None` (data-gap case, distinct from the
     direction='single' case).
  3. Trip materialisation — uses the same `_fetch_trips_by_search` join
     chain as the HTML export, so trips appear under whichever direction
     they were recorded for.
  4. 404 — unknown run id surfaces as a clean 404 rather than a 500.

The endpoint is called directly (bypassing FastAPI DI) so we can mock
the DB and the trips fetcher in one place.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

# ─────────────────────── DB stubs ───────────────────────


def _result_row(origin, dest, status="ok", journey_search_id=None, **extra):
    """A NetworkCoverageResult stand-in. journey_search_id defaults to a
    fresh uuid so the trip-fetch path is exercised."""
    r = MagicMock()
    r.origin_hub_id = origin
    r.dest_hub_id = dest
    r.status = status
    r.response_ms = extra.get("response_ms", 775)
    r.num_itineraries = extra.get("num_itineraries", 3)
    r.best_duration_seconds = extra.get("best_duration_seconds", 22920)
    r.best_num_transfers = extra.get("best_num_transfers", 1)
    r.best_operators = extra.get("best_operators", "SNCF")
    r.error_message = extra.get("error_message")
    r.journey_search_id = journey_search_id if journey_search_id is not None else uuid.uuid4()
    # PR-E — explicit None for the external-verify verdict columns.
    # Without this MagicMock auto-creates child mocks on attribute access,
    # which then trip CellTripsDirection's `str | None` validators with
    # "Input should be a valid string" — pre-PR-E rows naturally have
    # NULL across the board, so None is the canonical fixture value.
    r.external_ok = extra.get("external_ok")
    r.external_num_connections = extra.get("external_num_connections")
    r.external_best_duration_seconds = extra.get("external_best_duration_seconds")
    r.external_best_transfers = extra.get("external_best_transfers")
    r.external_source = extra.get("external_source")
    r.external_error = extra.get("external_error")
    r.external_verified_at = extra.get("external_verified_at")
    # PR-196a — same NULL-defaults rule for the alignment heatmap columns.
    # Pydantic validators on CellTripsDirection reject the auto-mocked
    # child MagicMock; explicit None matches the legacy / un-swept row
    # shape.
    r.external_itineraries = extra.get("external_itineraries")
    r.external_alignment_score = extra.get("external_alignment_score")
    r.external_alignment_tier = extra.get("external_alignment_tier")
    return r


def _run_row(direction="both"):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.direction = direction
    return r


def _db_returning(*, run, rows):
    """A db MagicMock where db.get returns `run` and db.execute().scalars().all()
    returns the supplied result rows."""
    db = MagicMock()
    db.get.return_value = run
    scalars = MagicMock()
    scalars.all.return_value = rows
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db.execute.return_value = exec_result
    return db


def _fake_actor():
    a = MagicMock()
    a.id = uuid.uuid4()
    return a


# ─────────────────────── endpoint tests ───────────────────────


def test_get_cell_trips_returns_both_directions_for_direction_both(monkeypatch):
    """The default flow — a direction='both' run with both A→B and B→A
    rows present. Both sides are populated and the JS will render two
    collapsible sections."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="both")
    out_row = _result_row("bxl-mid", "gva-c", status="ok", num_itineraries=3)
    ret_row = _result_row("gva-c", "bxl-mid", status="ok", num_itineraries=2)
    db = _db_returning(run=run, rows=[out_row, ret_row])

    # Stub the trips fetcher — we're testing the endpoint's row-split
    # logic, not the JOIN it delegates to.
    monkeypatch.setattr(
        api,
        "_fetch_trips_by_search",
        lambda _db, ids, _depart_at=None: {
            str(out_row.journey_search_id): [{"rank": 0, "duration_seconds": 22920}],
            str(ret_row.journey_search_id): [{"rank": 0, "duration_seconds": 24000}],
        },
    )

    resp = api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert resp.direction == "both"
    assert resp.outbound is not None
    assert resp.outbound.origin_hub_id == "bxl-mid"
    assert resp.outbound.dest_hub_id == "gva-c"
    assert len(resp.outbound.trips) == 1
    assert resp.return_ is not None
    assert resp.return_.origin_hub_id == "gva-c"
    assert resp.return_.dest_hub_id == "bxl-mid"
    assert len(resp.return_.trips) == 1


def test_get_cell_trips_hides_return_when_direction_single(monkeypatch):
    """The whole reason direction='single' exists — half the work, no
    B→A queried. The endpoint must return `return=None` so the JS hides
    the section instead of rendering "0 itineraries" (which would look
    like a data quality issue)."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="single")
    out_row = _result_row("bxl-mid", "gva-c", status="ok")
    # The reverse row genuinely doesn't exist for direction='single' runs.
    db = _db_returning(run=run, rows=[out_row])

    monkeypatch.setattr(
        api,
        "_fetch_trips_by_search",
        lambda _db, ids, _depart_at=None: {str(out_row.journey_search_id): []},
    )

    resp = api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert resp.direction == "single"
    assert resp.outbound is not None
    assert resp.return_ is None  # ← the key assertion


def test_get_cell_trips_return_none_for_missing_reverse_row(monkeypatch):
    """A direction='both' run where the reverse-direction row is missing
    (e.g. partial run, or a coverage row whose recorder write crashed).
    The JS distinguishes this from direction='single' by rendering "no
    coverage row recorded" instead of hiding the section."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="both")
    out_row = _result_row("bxl-mid", "gva-c")
    # Only the outbound row in the DB — reverse missing.
    db = _db_returning(run=run, rows=[out_row])

    monkeypatch.setattr(api, "_fetch_trips_by_search", lambda _db, ids, _depart_at=None: {})

    resp = api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert resp.direction == "both"
    assert resp.outbound is not None
    # Direction is 'both' but the row was missing — distinct case from
    # direction='single' (where we WANT None on principle).
    assert resp.return_ is None


def test_get_cell_trips_unknown_run_404():
    """db.get(NetworkCoverageRun, run_id) returns None → 404 with a
    clear message. Catches operator-typed run ids and stale URLs."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    db.get.return_value = None  # run not found

    with pytest.raises(HTTPException) as exc:
        api.get_cell_trips(
            run_id=uuid.uuid4(),
            origin_id="bxl-mid",
            dest_id="gva-c",
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 404
    assert "run" in str(exc.value.detail).lower()


def test_get_cell_trips_row_without_journey_search_id_returns_empty_trips(monkeypatch):
    """A coverage row whose `journey_search_id` is NULL (recorder failed
    mid-run) must not crash — the endpoint returns the row's summary
    fields with `trips=[]` so the modal shows "no itineraries" rather
    than 500-ing on a NULL lookup."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="both")
    out_row = _result_row("bxl-mid", "gva-c", journey_search_id=None)
    out_row.journey_search_id = None  # _result_row defaults to a uuid; force None
    db = _db_returning(run=run, rows=[out_row])

    # Trips fetcher should never be called with None ids, but stub it anyway.
    monkeypatch.setattr(api, "_fetch_trips_by_search", lambda _db, ids, _depart_at=None: {})

    resp = api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert resp.outbound is not None
    assert resp.outbound.trips == []


def test_get_cell_trips_passes_only_non_null_search_ids_to_fetcher(monkeypatch):
    """Optimisation check — when one direction's row has no
    journey_search_id, we shouldn't query trips for it (would be a
    no-op anyway, but cleaner to filter at source)."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="both")
    out_row = _result_row("bxl-mid", "gva-c")  # has journey_search_id
    ret_row = _result_row("gva-c", "bxl-mid", journey_search_id=None)
    ret_row.journey_search_id = None
    db = _db_returning(run=run, rows=[out_row, ret_row])

    captured_ids: list = []

    def fake_fetcher(_db, ids, _depart_at=None):
        captured_ids.extend(ids)
        return {}

    monkeypatch.setattr(api, "_fetch_trips_by_search", fake_fetcher)

    api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    # Only the outbound row's search id should reach the fetcher.
    assert captured_ids == [out_row.journey_search_id]


def test_get_cell_trips_round_trips_alignment_fields_into_response(monkeypatch):
    """PR-196a — when the sweep has populated alignment fields on the
    row, the cell-trips response must surface them verbatim so the
    matrix heatmap and the future side-by-side modal don't have to
    refetch. Pinning so a refactor that drops one of the three fields
    from `_row_to_direction` doesn't silently degrade the heatmap to
    grey for cells that have a tier."""
    from app.api.admin import network_coverage as api

    run = _run_row(direction="single")
    fake_itinerary = {
        "legs": [
            {
                "mode": "RAIL",
                "from_uic": "UIC:8014441",
                "to_uic": "UIC:8503000",
                "dep_utc": "2026-06-28T08:00",
                "arr_utc": "2026-06-28T12:30",
                "route_name": "ICE 24",
            }
        ],
        "departure_at": "2026-06-28T08:00",
        "arrival_at": "2026-06-28T12:30",
        "duration_seconds": 4 * 3600 + 30 * 60,
        "num_transfers": 0,
    }
    out_row = _result_row(
        "bxl-mid",
        "gva-c",
        status="ok",
        external_itineraries=[fake_itinerary],
        external_alignment_score=0.7,
        external_alignment_tier="mostly_agree",
    )
    db = _db_returning(run=run, rows=[out_row])

    monkeypatch.setattr(api, "_fetch_trips_by_search", lambda _db, ids, _depart_at=None: {})

    resp = api.get_cell_trips(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert resp.outbound is not None
    assert resp.outbound.external_alignment_score == 0.7
    assert resp.outbound.external_alignment_tier == "mostly_agree"
    assert resp.outbound.external_itineraries == [fake_itinerary]


def test_get_cell_trips_rejects_unknown_alignment_tier(monkeypatch):
    """PR-196a — the Pydantic `AlignmentTier` Literal on CellTripsDirection
    must reject a tier label the JS palette doesn't know about. Catches
    scorer-vs-UI drift at the API boundary instead of silently shipping
    a CSS-unmapped tier that renders as a transparent cell."""
    from pydantic import ValidationError

    from app.api.admin import network_coverage as api

    run = _run_row(direction="single")
    # Deliberately corrupt tier value — what a buggy future scorer
    # might emit if a new tier was added without coordinating with the
    # API Literal + CSS palette.
    out_row = _result_row(
        "bxl-mid",
        "gva-c",
        status="ok",
        external_alignment_tier="some_new_tier_we_forgot_to_register",
    )
    db = _db_returning(run=run, rows=[out_row])

    monkeypatch.setattr(api, "_fetch_trips_by_search", lambda _db, ids, _depart_at=None: {})

    with pytest.raises(ValidationError):
        api.get_cell_trips(
            run_id=run.id,
            origin_id="bxl-mid",
            dest_id="gva-c",
            db=db,
            _=_fake_actor(),
        )


def test_get_cell_trips_response_serialises_return_under_return_key():
    """Pydantic alias check — the Python attribute is `return_` (keyword
    workaround) but the JSON key MUST be `return` so the JS doesn't
    need to know about the workaround. A drift here would silently break
    the modal's "Return" section."""
    from app.api.admin.network_coverage import (
        CellTripsDirection,
        CellTripsResponse,
    )

    resp = CellTripsResponse(
        direction="both",
        outbound=CellTripsDirection(origin_hub_id="A", dest_hub_id="B", status="ok"),
        return_=CellTripsDirection(origin_hub_id="B", dest_hub_id="A", status="ok"),
    )
    payload = resp.model_dump(by_alias=True)
    assert "return" in payload
    assert payload["return"]["origin_hub_id"] == "B"
    # And the Python attribute name is NOT leaked to the wire.
    assert "return_" not in payload
