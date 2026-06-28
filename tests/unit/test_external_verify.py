"""Tests for `app.network_coverage.external_verify` — the HAFAS adapter
that powers the click-to-verify button on `no_route` cells.

Two layers:

  1. Pure-function bits (`_coord_to_micro`, `_parse_hafas_duration`,
     `_build_trip_search_body`, `_parse_hafas_response`,
     `_summarise_connections`) — no network, deterministic.
  2. `verify_via_db_hafas` itself, exercised through httpx's
     `MockTransport` so we can simulate HAFAS responses without actually
     hitting db.hafas.de from CI.

We deliberately exercise the three observable verdict states the UI
distinguishes:
  - `ok=True` (external found connections — likely our gap)
  - `ok=False`, `error=None` (external also found 0 — likely real gap)
  - `ok=False`, `error=...` (couldn't reach external — unknown)
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from app.network_coverage import external_verify

# ─────────────────────── pure helpers ───────────────────────


@pytest.mark.parametrize(
    ("lat_or_lon", "expected_micro"),
    [
        (47.5876, 47_587_600),
        (7.5571, 7_557_100),
        (-0.5000, -500_000),
        (0.0, 0),
        (90.0, 90_000_000),
    ],
)
def test_coord_to_micro_rounds_correctly(lat_or_lon: float, expected_micro: int) -> None:
    """HAFAS expects integer micro-degrees; floats get round-half-to-even.
    Pinning a few real coords from the eu11 session hub list catches a
    silent unit error (degrees vs micro-degrees) before it ships."""
    assert external_verify._coord_to_micro(lat_or_lon) == expected_micro


@pytest.mark.parametrize(
    ("hafas_dur", "expected_seconds"),
    [
        ("040000", 4 * 3600),  # 4h
        ("000130", 90),  # 1m30s
        ("01020300", 24 * 3600 + 2 * 3600 + 3 * 60),  # 1d 02:03:00 (DDHHMMSS)
        ("000000", 0),
        ("999999", 99 * 3600 + 99 * 60 + 99),  # malformed but parses
    ],
)
def test_parse_hafas_duration_extracts_seconds(hafas_dur: str, expected_seconds: int) -> None:
    """Duration strings are HHMMSS or DDDHHMMSS — both must parse to the
    right total. The 99h99m99s case is intentionally lenient: HAFAS
    sometimes returns out-of-range fields during fare engine glitches
    and we shouldn't crash the modal on them."""
    assert external_verify._parse_hafas_duration(hafas_dur) == expected_seconds


@pytest.mark.parametrize("bad", [None, "", "abc", "xy00"])
def test_parse_hafas_duration_returns_none_on_garbage(bad) -> None:
    """The result feeds `best_duration_seconds` which is `int | None` —
    None is the right "we don't know" value, NOT 0 (0 would be a real
    zero-duration connection, very different signal)."""
    assert external_verify._parse_hafas_duration(bad) is None


def test_build_trip_search_body_uses_x_lon_y_lat_convention() -> None:
    """HAFAS swaps the lat/lon order most transport tools use:
    `crd: {x: longitude, y: latitude}`. Getting this wrong silently
    returns "no connections" for every pair (HAFAS thinks you queried
    a point in the ocean). Pin the swap explicitly."""
    body = external_verify._build_trip_search_body(
        from_lat=47.5876,
        from_lon=7.5571,
        to_lat=46.2044,
        to_lon=6.1432,
        depart_at=datetime(2026, 6, 28, 8, 0, 0),
    )
    dep_crd = body["svcReqL"][0]["req"]["depLocL"][0]["crd"]
    arr_crd = body["svcReqL"][0]["req"]["arrLocL"][0]["crd"]
    # x = lon, y = lat
    assert dep_crd["x"] == 7_557_100, "x must be longitude"
    assert dep_crd["y"] == 47_587_600, "y must be latitude"
    assert arr_crd["x"] == 6_143_200
    assert arr_crd["y"] == 46_204_400


def test_build_trip_search_body_formats_date_and_time() -> None:
    """HAFAS dates are YYYYMMDD, times HHMMSS (no separators)."""
    body = external_verify._build_trip_search_body(
        from_lat=0.0,
        from_lon=0.0,
        to_lat=0.0,
        to_lon=0.0,
        depart_at=datetime(2026, 6, 28, 8, 30, 15),
    )
    req = body["svcReqL"][0]["req"]
    assert req["outDate"] == "20260628"
    assert req["outTime"] == "083015"


def test_build_trip_search_body_carries_db_navigator_credentials() -> None:
    """The body MUST identify as DB Navigator — that's the credential
    HAFAS accepts. A typo in the aid silently turns every request into
    a 401-equivalent and the operator gets `error` verdicts for every
    cell."""
    body = external_verify._build_trip_search_body(
        from_lat=0.0,
        from_lon=0.0,
        to_lat=0.0,
        to_lon=0.0,
        depart_at=datetime(2026, 6, 28, 8, 0, 0),
    )
    assert body["auth"] == {"type": "AID", "aid": "n91dB8Z77MLdoR0K"}
    assert body["client"]["id"] == "DB"
    assert body["client"]["name"] == "DB Navigator"
    assert body["ver"] == "1.45"
    assert body["svcReqL"][0]["meth"] == "TripSearch"


# ─────────────────────── response parsing ───────────────────────


def _hafas_ok_response(connections: list[dict]) -> dict:
    """Build a HAFAS-shaped response with the supplied connection list."""
    return {
        "err": "OK",
        "svcResL": [
            {
                "meth": "TripSearch",
                "err": "OK",
                "res": {"outConL": connections},
            }
        ],
    }


def test_parse_hafas_response_summarises_connections() -> None:
    """Three connections returned → ok=True, num_connections=3, best
    duration is the minimum of the three."""
    resp = _hafas_ok_response(
        [
            {"dur": "060000", "chg": 2},  # 6h, 2 transfers
            {"dur": "041500", "chg": 1},  # 4h15, 1 transfer ← best
            {"dur": "053000", "chg": 0},  # 5h30, direct
        ]
    )
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is True
    assert result.num_connections == 3
    assert result.best_duration_seconds == 4 * 3600 + 15 * 60
    # Transfers come from the same row that won "best duration".
    assert result.best_transfers == 1


def test_parse_hafas_response_empty_conl_is_ok_false_no_error() -> None:
    """Empty `outConL` with `err: OK` is a clean "no service" answer —
    distinct from a transport error. UI renders this as the blue
    "external also found 0" verdict, NOT the yellow "couldn't answer"
    one."""
    result = external_verify._parse_hafas_response(_hafas_ok_response([]))
    assert result.ok is False
    assert result.num_connections == 0
    assert result.error is None


def test_parse_hafas_response_h890_is_no_route_not_error() -> None:
    """HAFAS error code `H890` means "no connections found" — semantically
    identical to an empty outConL. Treat as the negative answer, not as
    an unknown."""
    resp = {
        "err": "OK",
        "svcResL": [{"meth": "TripSearch", "err": "H890"}],
    }
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is False
    assert result.error is None  # ← key: not flagged as transport error
    assert result.num_connections == 0


def test_parse_hafas_response_envelope_error_propagates() -> None:
    """A non-OK `err` at the envelope level (e.g. auth failure) must
    surface as `error` set so the UI yellow-warns rather than reading
    the meaningless empty `svcResL`."""
    resp = {"err": "ERROR", "errTxt": "AID invalid"}
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is False
    assert result.error is not None
    assert "ERROR" in result.error


def test_parse_hafas_response_service_error_propagates() -> None:
    """Per-service `err` other than OK / H890 → transport error."""
    resp = {
        "err": "OK",
        "svcResL": [{"meth": "TripSearch", "err": "SERVER_ERROR"}],
    }
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is False
    assert result.error is not None
    assert "SERVER_ERROR" in result.error


def test_parse_hafas_response_missing_svc_res_is_error() -> None:
    """A response with no `svcResL` array is malformed — return error,
    not a silent ok=False/empty (which would look like a real "no
    service" answer)."""
    result = external_verify._parse_hafas_response({"err": "OK"})
    assert result.ok is False
    assert result.error is not None


# ─────────────────────── verify_via_db_hafas (end-to-end with mocked HTTP) ───────────────────────


@pytest.mark.asyncio
async def test_verify_via_db_hafas_success_round_trip() -> None:
    """Inject a mocked transport that returns a 3-connection response;
    assert the VerifyResult mirrors the summary correctly."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Sanity check the request is well-formed before the response
        # round-trip — if a future refactor breaks the body shape the
        # MockTransport assertion catches it.
        assert request.method == "POST"
        assert request.url.path == "/bin/mgate.exe"
        return httpx.Response(
            200,
            json=_hafas_ok_response(
                [
                    {"dur": "060000", "chg": 1},
                    {"dur": "041500", "chg": 1},
                ]
            ),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_db_hafas(
            from_lat=47.5876,
            from_lon=7.5571,
            to_lat=46.2044,
            to_lon=6.1432,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is True
    assert result.num_connections == 2
    assert result.best_duration_seconds == 4 * 3600 + 15 * 60


@pytest.mark.asyncio
async def test_verify_via_db_hafas_http_500_is_error_not_ok_false() -> None:
    """HTTP 500 from HAFAS → `error` set so the UI yellow-warns. Critical
    that we DON'T silently return ok=False (which would look like
    "external confirmed no service")."""
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_db_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is not None
    assert "500" in result.error


@pytest.mark.asyncio
async def test_verify_via_db_hafas_connection_error_does_not_raise() -> None:
    """Network errors (DNS, refused, timeout) must surface as
    VerifyResult.error rather than propagating to the FastAPI endpoint.
    The endpoint is operator-facing — a 500 from a third-party
    backend should NOT trip our own 500."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_db_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is not None
    assert "backend down" in result.error or "http" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_via_db_hafas_non_json_response_is_error() -> None:
    """HAFAS occasionally returns an HTML interstitial when behind a
    maintenance page. The parser must catch the JSON decode failure
    rather than crashing the endpoint."""
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, text="<html>maintenance</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_db_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is not None
    assert "json" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_via_db_hafas_h890_returns_real_no_route() -> None:
    """End-to-end check that the H890 branch flows through the full
    function and lands as ok=False with no error (the 'external also
    found 0' verdict). Pinning the user-visible behaviour, not just
    the parser unit."""
    resp = {
        "err": "OK",
        "svcResL": [{"meth": "TripSearch", "err": "H890"}],
    }
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=resp))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_db_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is None
    assert result.num_connections == 0
