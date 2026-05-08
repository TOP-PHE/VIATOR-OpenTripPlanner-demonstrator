"""Request-id middleware — bind a stable id to structlog contextvars per request.

Inbound `X-Request-ID` is preserved if it matches the safe character set;
otherwise a fresh UUID4 hex is minted. The id is bound to structlog
contextvars for the duration of the request — every log line emitted within
the request scope (including from background tasks dispatched by FastAPI)
carries it via `structlog.contextvars.merge_contextvars` (configured in
`app.logging_config`). The header is echoed back on the response so clients
can correlate end-to-end.

Inbound id is restricted to `[A-Za-z0-9_-]{1,64}` to prevent log-injection
via crafted headers (an attacker controlling the header could otherwise
inject newlines or quotes into JSON log lines).
"""

from __future__ import annotations

import re
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_request_id(raw: str | None) -> str:
    if raw and _VALID_REQUEST_ID.match(raw):
        return raw
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _safe_request_id(request.headers.get(REQUEST_ID_HEADER))
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
