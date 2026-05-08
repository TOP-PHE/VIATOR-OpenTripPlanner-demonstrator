"""RequestIdMiddleware — header echo, generation, sanitisation, contextvar binding."""

from __future__ import annotations

import re

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware


@pytest.fixture(autouse=True)
def _reset_contextvars() -> None:
    structlog.contextvars.clear_contextvars()


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    def echo() -> dict[str, str | None]:
        bound = structlog.contextvars.get_contextvars()
        return {"request_id": bound.get("request_id")}

    return app


def test_inbound_header_is_preserved() -> None:
    client = TestClient(_make_app())
    resp = client.get("/echo", headers={REQUEST_ID_HEADER: "client-supplied-42"})
    assert resp.headers[REQUEST_ID_HEADER] == "client-supplied-42"
    assert resp.json()["request_id"] == "client-supplied-42"


def test_missing_header_generates_uuid_hex() -> None:
    client = TestClient(_make_app())
    resp = client.get("/echo")
    rid = resp.headers[REQUEST_ID_HEADER]
    assert re.fullmatch(r"[0-9a-f]{32}", rid)
    assert resp.json()["request_id"] == rid


@pytest.mark.parametrize(
    "bad_value",
    [
        "",  # empty
        "x" * 65,  # too long
        "has spaces",  # space char
        "has\nnewline",  # newline (log-injection vector)
        'has"quote',  # quote (JSON-injection vector)
        # Non-ASCII headers can't reach the middleware via any RFC 7230-
        # compliant client (httpx blocks at encode time with
        # UnicodeEncodeError). The regex would reject them as defence-in-
        # depth if one ever did, but there's no clean way to inject one
        # through the test client to exercise that path.
    ],
)
def test_malformed_inbound_id_is_rejected(bad_value: str) -> None:
    client = TestClient(_make_app())
    resp = client.get("/echo", headers={REQUEST_ID_HEADER: bad_value})
    rid = resp.headers[REQUEST_ID_HEADER]
    assert rid != bad_value
    assert re.fullmatch(r"[0-9a-f]{32}", rid)


def test_contextvar_is_cleared_after_request() -> None:
    client = TestClient(_make_app())
    client.get("/echo", headers={REQUEST_ID_HEADER: "abc"})
    # Outside the request scope, no request_id should remain bound.
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test_each_request_gets_independent_id() -> None:
    client = TestClient(_make_app())
    a = client.get("/echo").headers[REQUEST_ID_HEADER]
    b = client.get("/echo").headers[REQUEST_ID_HEADER]
    assert a != b
