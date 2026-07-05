"""Public, unauthenticated share link for a coverage run's HTML report.

`/api/admin/network-coverage/runs/{id}/export.html` (network_coverage.py)
requires platform_admin and forces a download — built for an operator to
grab a file and email it. This module covers the opposite case: handing a
stakeholder a URL they can just open in a browser, no login, no 9MB
attachment to route through email.

Security model (a deliberate product choice, not an oversight): the run
id IS the capability token. `NetworkCoverageRun.id` is a Postgres
`gen_random_uuid()` — 128 bits of cryptographic randomness — so knowing a
run's id is equivalent to holding an unguessable bearer token, and there
is no listing/enumeration endpoint here to discover one. This is
"unlisted, not secret," matching the actual sensitivity of the data
(coverage timing/alignment figures and the ÖBB verify itineraries used
for the side-by-side comparison — nothing confidential), not a
login-gated share. The per-cell trips endpoint below shares the same
model: everything it returns is rendered by the page at `/{run_id}`,
so knowing the run id already grants it.

Kept on its own router rather than as one dependency-less route bolted
onto the admin router, so its lack of auth is a property of *which
router it's registered on* — structurally impossible to inherit
`require_platform_admin` by accident, and equally impossible for a
future admin-wide auth change to silently start blocking it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import NetworkCoverageRun
from ..network_coverage import runner
from ..rate_limit import limiter
from ..templating import templates
from .admin.network_coverage import (
    _RUN_NOT_FOUND,
    CellTripsResponse,
    _build_cell_trips_response,
    _build_export_context,
    _resolve_hubs,
)

router = APIRouter(prefix="/share/coverage", tags=["public"])


@router.get(
    "/{run_id}",
    response_class=HTMLResponse,
    responses={404: {"description": _RUN_NOT_FOUND}},
)
@limiter.limit("60/minute")
def view_shared_run(
    request: Request,
    run_id: uuid.UUID,
    db: Annotated[DbSession, Depends(get_db)],
) -> HTMLResponse:
    """Render the run report inline (no Content-Disposition) and with no
    auth dependency — the URL itself, carrying the unguessable run id,
    is the access control.

    Unlike the downloaded export, this page embeds NO trip detail
    (`lazy_trips=True`): the modal fetches each clicked cell's
    itineraries from the sibling endpoint below, exactly like the live
    admin matrix does. That keeps the page a constant few MB regardless
    of run size — a 94-hub / 8742-pair run embedded ~150MB of leg JSON
    under the old embed-everything approach, which no browser could
    open — while preserving FULL leg detail on click for any run.

    Reuses `_build_export_context` (same helper the authenticated
    download endpoint calls) so the shared view and the downloaded file
    can never drift apart into two different renderings of the same run.

    The 60/minute rate limit is defense-in-depth against scraping/DoS on
    this specific route, not a defence against guessing — the id space
    (2^128) already makes brute-forcing infeasible regardless of any
    rate limit.
    """
    run, results = runner.get_run_with_results(db, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    context = _build_export_context(
        run=run,
        results=results,
        hubs=_resolve_hubs(db),
        trips_by_search={},
        lazy_trips=True,
    )
    return templates.TemplateResponse(request, "admin/network_coverage_export.html", context)


@router.get(
    "/{run_id}/cells/{origin_id}/{dest_id}/trips",
    responses={404: {"description": _RUN_NOT_FOUND}},
)
@limiter.limit("120/minute")
def shared_cell_trips(
    request: Request,
    run_id: uuid.UUID,
    origin_id: str,
    dest_id: str,
    db: Annotated[DbSession, Depends(get_db)],
) -> CellTripsResponse:
    """One cell's trip breakdown for the share page's click modal —
    the public twin of the admin `GET /runs/{id}/cells/{o}/{d}/trips`,
    sharing its query/marshalling helper so the two can't drift.

    No auth by design: the run id in the path is the same capability
    that already unlocks the full report page. The higher 120/minute
    budget (vs 60 for the page) is because a reader exploring a matrix
    legitimately clicks many cells in quick succession.

    `external_itineraries` (the ÖBB itineraries captured by the verify
    sweep) IS included — a deliberate product decision (2026-07-05):
    the share page renders the same VIATOR-vs-ÖBB side-by-side the
    admin matrix modal shows, because the comparison is the point of
    sharing a verified coverage report with a stakeholder. This means a
    share URL grants read access to the run's persisted ÖBB verify data,
    not only VIATOR's own results — accepted: the VerifyItinerary shape
    is deliberately narrow (times, UIC codes, route names; no fares or
    personal data), the same low sensitivity as the coverage figures.
    An earlier revision stripped the field; the strip was removed when
    the side-by-side landed on the share page.
    """
    run = db.get(NetworkCoverageRun, run_id)
    if run is None:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return _build_cell_trips_response(db, run, origin_id, dest_id)
