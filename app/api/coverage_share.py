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
(coverage timing/alignment figures — nothing confidential), not a
login-gated share.

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
from ..network_coverage import runner
from ..rate_limit import limiter
from ..templating import templates
from .admin.network_coverage import (
    _RUN_NOT_FOUND,
    _build_export_context,
    _fetch_trips_by_search,
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
    """Render the same self-contained report as the admin export endpoint,
    inline (no Content-Disposition) and with no auth dependency — the
    URL itself, carrying the unguessable run id, is the access control.

    Reuses `_build_export_context` verbatim (same helper the authenticated
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
    search_ids = [r.journey_search_id for r in results if r.journey_search_id is not None]
    context = _build_export_context(
        run=run,
        results=results,
        hubs=_resolve_hubs(db),
        trips_by_search=_fetch_trips_by_search(db, search_ids),
    )
    return templates.TemplateResponse(request, "admin/network_coverage_export.html", context)
