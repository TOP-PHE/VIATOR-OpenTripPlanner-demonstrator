"""Tests for `GET /api/admin/network-coverage/runs/{run_id}/cells/
{origin_id}/{dest_id}/verify-external`.

The endpoint orchestrates four lookups (run, cell, origin hub, dest
hub) and dispatches to the HAFAS adapter — tests stub the adapter so
this layer is focused on the validation gates.

Coverage:
  - 404 on unknown run id
  - 404 on (run_id, origin, dest) tuple that doesn't exist in this run
  - 404 when one of the hubs was soft-deleted between run and verify
  - Happy path forwards (origin.lat/lon, dest.lat/lon, run.depart_at)
    to the adapter
  - Adapter result is returned verbatim (no shape mutation)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest


def _run_row(direction="both"):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.direction = direction
    r.depart_at = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
    return r


def _cell_row(origin, dest, status="no_route"):
    r = MagicMock()
    r.origin_hub_id = origin
    r.dest_hub_id = dest
    r.status = status
    return r


def _hub_row(slug, lat, lon):
    h = MagicMock()
    h.id = slug
    h.lat = lat
    h.lon = lon
    return h


def _db_for(*, run, cell, origin_hub, dest_hub):
    """A db MagicMock that returns `run` from db.get(NetworkCoverageRun, _),
    the hubs from db.get(NetworkCoverageHub, slug), and `cell` from the
    .scalars().first() of the existence query.

    Order of db.get calls in the endpoint:
      1. db.get(NetworkCoverageRun, run_id)  → run
      2. db.get(NetworkCoverageHub, origin_id) → origin_hub
      3. db.get(NetworkCoverageHub, dest_id)   → dest_hub
    A side_effect list pops in order; .first() on the execute() chain
    returns the cell.
    """
    db = MagicMock()
    db.get.side_effect = [run, origin_hub, dest_hub]
    scalars = MagicMock()
    scalars.first.return_value = cell
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db.execute.return_value = exec_result
    return db


def _fake_actor():
    a = MagicMock()
    a.id = uuid.uuid4()
    return a


# ─────────────────────── 404 paths ───────────────────────


@pytest.mark.asyncio
async def test_verify_external_404_on_unknown_run():
    """No run row → 404. Don't even start the cell / hub lookups."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    db = MagicMock()
    db.get.return_value = None  # run lookup returns None

    with pytest.raises(HTTPException) as exc:
        await api.verify_cell_external(
            run_id=uuid.uuid4(),
            origin_id="bxl-mid",
            dest_id="gva-c",
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 404
    assert "run" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_verify_external_404_on_unknown_cell():
    """Run exists but the (origin, dest) pair has no result row →
    404. Prevents the endpoint from being used as a "verify any pair"
    oracle that bypasses the matrix."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    run = _run_row()
    db = MagicMock()
    db.get.return_value = run
    scalars = MagicMock()
    scalars.first.return_value = None  # cell lookup returns None
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db.execute.return_value = exec_result

    with pytest.raises(HTTPException) as exc:
        await api.verify_cell_external(
            run_id=run.id,
            origin_id="bxl-mid",
            dest_id="gva-c",
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 404
    assert "cell" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_verify_external_404_on_soft_deleted_hub():
    """Cell exists but one hub was soft-deleted between run and verify
    → 404 (we'd lose the coords needed for HAFAS). Operator restores
    the hub to make the cell verifiable again."""
    from fastapi import HTTPException

    from app.api.admin import network_coverage as api

    run = _run_row()
    cell = _cell_row("bxl-mid", "gva-c")
    db = _db_for(
        run=run,
        cell=cell,
        origin_hub=None,  # ← soft-deleted
        dest_hub=_hub_row("gva-c", 46.2044, 6.1432),
    )

    with pytest.raises(HTTPException) as exc:
        await api.verify_cell_external(
            run_id=run.id,
            origin_id="bxl-mid",
            dest_id="gva-c",
            db=db,
            _=_fake_actor(),
        )
    assert exc.value.status_code == 404
    assert "hub" in str(exc.value.detail).lower()


# ─────────────────────── happy path ───────────────────────


@pytest.mark.asyncio
async def test_verify_external_forwards_coords_and_depart_at_to_adapter(monkeypatch):
    """The adapter MUST receive the hubs' coords + the run's depart_at,
    NOT the cell's slug or anything else. A regression where we
    accidentally pass the slug instead of lat/lon would silently turn
    HAFAS queries into "from string 'bxl-mid' to string 'gva-c'" — and
    HAFAS would return empty for every pair without erroring."""
    from app.api.admin import network_coverage as api
    from app.network_coverage import external_verify

    run = _run_row()
    cell = _cell_row("bxl-mid", "gva-c")
    origin = _hub_row("bxl-mid", 50.8358, 4.3361)
    dest = _hub_row("gva-c", 46.2104, 6.1424)
    db = _db_for(run=run, cell=cell, origin_hub=origin, dest_hub=dest)

    captured: dict = {}

    async def fake_verify(**kwargs):
        captured.update(kwargs)
        return external_verify.VerifyResult(
            source="db.hafas.de",
            ok=True,
            num_connections=3,
            best_duration_seconds=4 * 3600 + 15 * 60,
            best_transfers=1,
        )

    monkeypatch.setattr(external_verify, "verify_via_db_hafas", fake_verify)

    result = await api.verify_cell_external(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert captured["from_lat"] == 50.8358
    assert captured["from_lon"] == 4.3361
    assert captured["to_lat"] == 46.2104
    assert captured["to_lon"] == 6.1424
    assert captured["depart_at"] == run.depart_at
    assert result.source == "db.hafas.de"
    assert result.ok is True
    assert result.num_connections == 3


@pytest.mark.asyncio
async def test_verify_external_returns_adapter_result_verbatim(monkeypatch):
    """No mutation between adapter and endpoint — including the
    "external also found 0" (ok=False, error=None) verdict that the UI
    distinguishes from the "couldn't answer" (error set) case."""
    from app.api.admin import network_coverage as api
    from app.network_coverage import external_verify

    run = _run_row()
    cell = _cell_row("bxl-mid", "gva-c")
    db = _db_for(
        run=run,
        cell=cell,
        origin_hub=_hub_row("bxl-mid", 50.0, 4.0),
        dest_hub=_hub_row("gva-c", 46.0, 6.0),
    )

    expected = external_verify.VerifyResult(
        source="db.hafas.de",
        ok=False,
        num_connections=0,
        error=None,
    )

    async def fake_verify(**_kwargs):
        return expected

    monkeypatch.setattr(external_verify, "verify_via_db_hafas", fake_verify)

    result = await api.verify_cell_external(
        run_id=run.id,
        origin_id="bxl-mid",
        dest_id="gva-c",
        db=db,
        _=_fake_actor(),
    )

    assert result == expected
