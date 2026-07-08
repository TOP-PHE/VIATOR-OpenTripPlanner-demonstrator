"""Tests for `app.network_coverage.external_verify` — the HAFAS adapter
that powers the click-to-verify button on `no_route` cells.

Three layers:

  1. Pure-function bits (`_coord_to_micro`, `_parse_hafas_duration`,
     `_translate_hafas_error`, `_build_locgeopos_body`,
     `_extract_lids_from_locgeopos`, `_build_trip_search_body`,
     `_parse_hafas_response`, `_decode_response_body`) — no network,
     deterministic.
  2. The `verify_via_oebb_hafas` two-step flow (LocGeoPos → TripSearch)
     exercised through httpx's `MockTransport`.
  3. The three observable verdict states the UI distinguishes:
     - `ok=True` (external found connections — likely our gap)
     - `ok=False`, `error=None` (external also found 0 — likely real gap)
     - `ok=False`, `error=...` (couldn't reach external — unknown)
"""

from __future__ import annotations

import json
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


# ─────────────────────── HAFAS error code translation ───────────────────────


@pytest.mark.parametrize(
    ("code", "expected_fragment"),
    [
        ("H9220", "no station found near the supplied coordinates"),
        ("H9230", "internal error"),
        ("H9240", "timeout"),
        ("H9250", "combination of train products"),
        ("H9300", "address search"),
    ],
)
def test_translate_hafas_error_known_codes(code: str, expected_fragment: str) -> None:
    """Known operator-facing codes get human-readable translations.
    Catches the case where a translation lookup quietly returns the raw
    code instead of the friendly message (regression hook for the
    table)."""
    assert expected_fragment in external_verify._translate_hafas_error(code)


def test_translate_hafas_error_unknown_falls_through() -> None:
    """Unknown codes fall through to `hafas svc: <code>` — escape hatch
    so a new HAFAS code we haven't catalogued still surfaces with the
    raw identifier rather than being swallowed."""
    msg = external_verify._translate_hafas_error("H9999")
    assert "H9999" in msg


@pytest.mark.parametrize(
    ("cat", "expected_mode"),
    [
        ("RJ", "RAIL"),
        ("BUS", "BUS"),
        ("Tram", "TRAM"),
        (None, "TRANSIT"),
        # HAFAS occasionally sends a numeric product code instead of a
        # string abbreviation — this was crashing every verify-sweep
        # call for the affected products with AttributeError: 'int'
        # object has no attribute 'upper', silently caught per-cell as
        # external_error='sweep_exception'. str(cat or "") must never
        # raise regardless of the input type.
        (7, "RAIL"),  # falls through to the big rail bucket, same as any unrecognised string
        (0, "TRANSIT"),  # falsy int must not crash
    ],
)
def test_hafas_cat_to_mode_handles_non_string_categories(cat, expected_mode: str) -> None:
    assert external_verify._hafas_cat_to_mode(cat) == expected_mode


# ─────────────────────── LocGeoPos body shape ───────────────────────


def test_build_locgeopos_body_uses_ring_with_5km_radius() -> None:
    """LocGeoPos uses `ring.cCrd` (centre coord) + `maxDist` (radius in
    metres). 5 km is generous: our hubs are typically within 25 m of
    the real station but very-rural origins might be hundreds of metres
    off. 5 km still safely picks the *intended* station in metropolitan
    areas (no two mainline stations are that close)."""
    body = external_verify._build_locgeopos_body([(50.9430, 6.9590)])  # Köln Hbf
    assert len(body["svcReqL"]) == 1
    req = body["svcReqL"][0]
    assert req["meth"] == "LocGeoPos"
    ring = req["req"]["ring"]
    # x = lon, y = lat (HAFAS convention swaps the usual order)
    assert ring["cCrd"]["x"] == 6_959_000, "x must be longitude in micro-degrees"
    assert ring["cCrd"]["y"] == 50_943_000, "y must be latitude in micro-degrees"
    assert ring["maxDist"] == 5000
    assert req["req"]["maxLoc"] == 1
    assert req["req"]["getStops"] is True
    assert req["req"]["getPOIs"] is False, "POIs would inject non-railway noise"


def test_build_locgeopos_body_two_coords_share_one_request() -> None:
    """Origin + destination resolution batches into a single POST with
    two svcReqL entries — one round-trip rather than two."""
    body = external_verify._build_locgeopos_body(
        [(50.9430, 6.9590), (50.1071, 8.6638)]  # Köln + Frankfurt
    )
    assert len(body["svcReqL"]) == 2
    assert all(r["meth"] == "LocGeoPos" for r in body["svcReqL"])
    coords = [r["req"]["ring"]["cCrd"] for r in body["svcReqL"]]
    assert coords[0]["y"] == 50_943_000
    assert coords[1]["y"] == 50_107_100


def test_build_locgeopos_body_carries_oebb_credentials() -> None:
    """The resolve POST must identify as ÖBB Scotty — same credentials
    as TripSearch, since both methods share the same mgate endpoint."""
    body = external_verify._build_locgeopos_body([(0.0, 0.0)])
    assert body["auth"] == {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"}
    assert body["client"]["id"] == "OEBB"
    assert body["ver"] == "1.42"


# ─────────────────────── LocGeoPos response extraction ───────────────────────


def test_extract_lids_from_locgeopos_happy_path() -> None:
    """Two svcResL entries, each with a stop → both lids returned in
    request order. Order matters because the caller maps slot 0 → from,
    slot 1 → to."""
    payload = {
        "err": "OK",
        "svcResL": [
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": "A=1@L=8000207@", "name": "Köln Hbf"}]},
            },
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": "A=1@L=8000105@", "name": "Frankfurt Hbf"}]},
            },
        ],
    }
    lids = external_verify._extract_lids_from_locgeopos(payload, count=2)
    assert lids == ["A=1@L=8000207@", "A=1@L=8000105@"]


def test_extract_lids_from_locgeopos_empty_locl_returns_none() -> None:
    """svcResL entry with err=OK but empty locL → None for that slot.
    Means LocGeoPos found no stop within the 5 km radius (Bergen-NO
    style coverage gap — coord is fine but ÖBB doesn't have it)."""
    payload = {
        "err": "OK",
        "svcResL": [
            {"meth": "LocGeoPos", "err": "OK", "res": {"locL": []}},
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": "A=1@L=8000105@"}]},
            },
        ],
    }
    lids = external_verify._extract_lids_from_locgeopos(payload, count=2)
    assert lids == [None, "A=1@L=8000105@"]


def test_extract_lids_from_locgeopos_service_error_returns_none() -> None:
    """svcResL entry with err != OK → None. Other slots can still
    return valid lids — partial success is preserved, the caller
    decides per-slot what to do."""
    payload = {
        "err": "OK",
        "svcResL": [
            {"meth": "LocGeoPos", "err": "H9230"},
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": "A=1@L=8000105@"}]},
            },
        ],
    }
    lids = external_verify._extract_lids_from_locgeopos(payload, count=2)
    assert lids == [None, "A=1@L=8000105@"]


def test_extract_lids_from_locgeopos_fewer_results_pads_with_none() -> None:
    """Defensive: if HAFAS returns fewer svcResL than requested, the
    missing slots come back as None. Shouldn't happen in practice but
    a malformed response shouldn't crash the modal."""
    payload = {
        "err": "OK",
        "svcResL": [
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": "A=1@L=8000207@"}]},
            }
        ],
    }
    lids = external_verify._extract_lids_from_locgeopos(payload, count=2)
    assert lids == ["A=1@L=8000207@", None]


# ─────────────────────── TripSearch body shape ───────────────────────


def test_build_trip_search_body_uses_station_lookup() -> None:
    """TripSearch now uses `type:"S"` with pre-resolved lids (not
    `type:"C"` with raw coords). This is the workaround for ÖBB's
    coord-snap rejecting valid hub coords with H9220 even for canonical
    hubs like Köln Hbf / Frankfurt (Main) Hbf."""
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=8000207@",
        to_lid="A=1@L=8000105@",
        depart_at=datetime(2026, 6, 28, 8, 0, 0),
    )
    req = body["svcReqL"][0]["req"]
    assert req["depLocL"] == [{"type": "S", "lid": "A=1@L=8000207@"}]
    assert req["arrLocL"] == [{"type": "S", "lid": "A=1@L=8000105@"}]


def test_build_trip_search_body_formats_date_and_time() -> None:
    """HAFAS dates are YYYYMMDD, times HHMMSS (no separators)."""
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=X@",
        to_lid="A=1@L=Y@",
        depart_at=datetime(2026, 6, 28, 8, 30, 15),
    )
    req = body["svcReqL"][0]["req"]
    assert req["outDate"] == "20260628"
    assert req["outTime"] == "083015"


def test_build_trip_search_body_carries_oebb_credentials() -> None:
    """The body MUST identify as ÖBB Scotty — that's the credential
    ÖBB's mgate.exe accepts. A typo in the aid silently turns every
    request into a 401-equivalent and the operator gets `error`
    verdicts for every cell."""
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=X@",
        to_lid="A=1@L=Y@",
        depart_at=datetime(2026, 6, 28, 8, 0, 0),
    )
    assert body["auth"] == {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"}
    assert body["client"]["id"] == "OEBB"
    assert body["client"]["name"] == "oebb"
    assert body["ver"] == "1.42"
    assert body["svcReqL"][0]["meth"] == "TripSearch"
    # ÖBB profile doesn't carry an `ext` field (DB did) — verify it's
    # absent so a future paste-in from a DB profile doesn't sneak it
    # back and break the auth handshake.
    assert "ext" not in body


# ─────────────────────── response parsing ───────────────────────


def _hafas_ok_response(connections: list[dict]) -> dict:
    """Build a HAFAS-shaped TripSearch response with the supplied
    connection list."""
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
    assert result.source == "fahrplan.oebb.at"
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


def test_parse_hafas_response_h9220_uses_friendly_translation() -> None:
    """H9220 (no stop near coords) should NOT surface as the raw code
    — the lookup table translates it to "no station found near the
    supplied coordinates". (This case shouldn't fire via the normal
    two-step flow since LocGeoPos resolves first, but a defensive
    parse path matters if ÖBB returns it on TripSearch too.)"""
    resp = {
        "err": "OK",
        "svcResL": [{"meth": "TripSearch", "err": "H9220"}],
    }
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is False
    assert result.error is not None
    assert "no station found near the supplied coordinates" in result.error
    # The friendly message replaces — operator never sees the raw code.
    assert "H9220" not in result.error


def test_parse_hafas_response_envelope_error_propagates() -> None:
    """A non-OK `err` at the envelope level (e.g. auth failure) must
    surface as `error` set so the UI yellow-warns rather than reading
    the meaningless empty `svcResL`."""
    resp = {"err": "ERROR", "errTxt": "AID invalid"}
    result = external_verify._parse_hafas_response(resp)
    assert result.ok is False
    assert result.error is not None
    assert "ERROR" in result.error


def test_parse_hafas_response_unknown_service_error_falls_through() -> None:
    """Per-service `err` that isn't in the translation table → raw code
    surfaces via the `hafas svc:` escape hatch. Ensures we don't
    swallow a new HAFAS code by translating it into a misleading
    friendly message."""
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


# ─────────────────────── body decoding (UTF-8 / Latin-1 fallback) ───────────────────────


def test_decode_response_body_utf8() -> None:
    """The common path: ÖBB's mgate returns UTF-8 JSON. Standard utf-8
    decode succeeds and we parse normally."""
    raw = '{"err": "OK", "svcResL": [{"name": "München Hbf"}]}'.encode()
    payload = external_verify._decode_response_body(raw)
    assert payload["err"] == "OK"
    assert payload["svcResL"][0]["name"] == "München Hbf"


def test_decode_response_body_latin1_fallback() -> None:
    """Sibling ÖBB endpoint (ajax-getstop) returns Latin-1; during
    outages we've seen Latin-1 leak into mgate responses too. Decoding
    bytes that fail UTF-8 (e.g. 0xfc which is `ü` in Latin-1, invalid
    as a UTF-8 lead byte) must fall back to Latin-1 rather than
    crashing the modal with a UnicodeDecodeError."""
    # 0xfc is `ü` in Latin-1; on its own it's NOT a valid UTF-8 byte.
    raw = b'{"err": "OK", "svcResL": [{"name": "M\xfcnchen Hbf"}]}'
    # Confirm the precondition: utf-8 decode fails on this byte stream.
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    # The adapter must handle it without raising.
    payload = external_verify._decode_response_body(raw)
    assert payload["err"] == "OK"
    assert payload["svcResL"][0]["name"] == "München Hbf"


def test_decode_response_body_invalid_json_raises() -> None:
    """Decode falls back to Latin-1 for byte issues but JSON parse
    errors still propagate (the caller catches and converts to a
    VerifyResult with `error` set)."""
    with pytest.raises(json.JSONDecodeError):
        external_verify._decode_response_body(b"<html>maintenance</html>")


# ─────────────────────── verify_via_oebb_hafas (two-step end-to-end) ───────────────────────


def _locgeopos_response(stops: list[tuple[str | None, str]]) -> dict:
    """Build a HAFAS-shaped LocGeoPos response.

    `stops` is a list of `(lid_or_None, name)` — one entry per
    endpoint in the request. A None lid simulates "no stop within
    radius" (the Bergen-NO style case)."""
    return {
        "err": "OK",
        "svcResL": [
            {
                "meth": "LocGeoPos",
                "err": "OK",
                "res": {"locL": [{"lid": lid, "name": name}] if lid else []},
            }
            for (lid, name) in stops
        ],
    }


def _two_step_handler(
    *,
    resolve_response: dict | None = None,
    trip_response: dict | None = None,
    resolve_status: int = 200,
    trip_status: int = 200,
):
    """Build a MockTransport handler that switches on the first
    svcReqL entry's `meth`. LocGeoPos posts get the resolve fixture;
    TripSearch posts get the trip fixture. Unknown methods → 400 (a
    test that hits this branch is a regression in svcReqL routing)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        meth = body["svcReqL"][0]["meth"]
        if meth == "LocGeoPos":
            return httpx.Response(resolve_status, json=resolve_response)
        if meth == "TripSearch":
            return httpx.Response(trip_status, json=trip_response)
        return httpx.Response(400, text=f"unexpected meth: {meth}")

    return handler


@pytest.mark.asyncio
async def test_verify_via_oebb_hafas_happy_two_step() -> None:
    """Standard flow: LocGeoPos resolves both endpoints to lids, then
    TripSearch returns 2 connections. Two POSTs to mgate, single
    consolidated VerifyResult back to the caller."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response(
            [
                ("A=1@L=8000207@", "Köln Hbf"),
                ("A=1@L=8000105@", "Frankfurt(Main)Hbf"),
            ]
        ),
        trip_response=_hafas_ok_response(
            [
                {"dur": "060000", "chg": 1},
                {"dur": "041500", "chg": 1},
            ]
        ),
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
            from_lat=50.9430,
            from_lon=6.9587,  # Köln Hbf
            to_lat=50.1071,
            to_lon=8.6638,  # Frankfurt Hbf
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.source == "fahrplan.oebb.at"
    assert result.ok is True
    assert result.num_connections == 2
    assert result.best_duration_seconds == 4 * 3600 + 15 * 60


@pytest.mark.asyncio
async def test_verify_via_oebb_hafas_resolve_empty_for_one_endpoint() -> None:
    """LocGeoPos returns empty locL for the dest coord (no ÖBB stop
    nearby — Bergen-NO style gap). The trip search MUST be skipped:
    return a "no station found" verdict with the friendly translation,
    not a raw H9220, and never fire the second POST."""
    captured_meths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_meths.append(body["svcReqL"][0]["meth"])
        return httpx.Response(
            200,
            json=_locgeopos_response(
                [
                    ("A=1@L=8000207@", "Köln Hbf"),
                    (None, ""),  # ← dest didn't snap to any ÖBB stop
                ]
            ),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
            from_lat=50.9430,
            from_lon=6.9587,
            to_lat=60.39,
            to_lon=5.32,  # Bergen NO — out of ÖBB pool
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is not None
    assert "no station found near the supplied coordinates" in result.error
    # Critical: trip search must NOT fire if resolve failed — saves a
    # round-trip and avoids a useless TripSearch with an empty lid.
    assert captured_meths == ["LocGeoPos"]


@pytest.mark.asyncio
async def test_verify_via_oebb_hafas_resolve_http_500_is_error() -> None:
    """HTTP 500 on LocGeoPos → error verdict, trip search not
    attempted."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(500, text="oops"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
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
async def test_verify_via_oebb_hafas_connection_error_does_not_raise() -> None:
    """Network errors (DNS, refused, timeout) must surface as
    VerifyResult.error rather than propagating to the FastAPI endpoint.
    The endpoint is operator-facing — a 500 from a third-party
    backend should NOT trip our own 500."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
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
async def test_verify_via_oebb_hafas_resolve_envelope_error_propagates() -> None:
    """Envelope-level err (e.g. auth failure) on the LocGeoPos POST →
    yellow error verdict. Trip search not attempted."""
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"err": "ERROR", "errTxt": "AID invalid"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is False
    assert result.error is not None
    assert "ERROR" in result.error


@pytest.mark.asyncio
async def test_verify_via_oebb_hafas_trip_h890_returns_real_no_route() -> None:
    """End-to-end: resolve OK, TripSearch returns H890 (real "no
    connections"). Verdict is blue ok=False/error=None — distinct from
    "couldn't answer"."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response(
            [
                ("A=1@L=8000207@", "Köln Hbf"),
                ("A=1@L=8000105@", "Frankfurt Hbf"),
            ]
        ),
        trip_response={
            "err": "OK",
            "svcResL": [{"meth": "TripSearch", "err": "H890"}],
        },
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
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


@pytest.mark.asyncio
async def test_verify_via_oebb_hafas_non_json_response_is_error() -> None:
    """HAFAS occasionally returns an HTML interstitial when behind a
    maintenance page. The parser must catch the JSON decode failure
    rather than crashing the endpoint."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>maintenance</html>"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
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
async def test_verify_via_oebb_hafas_latin1_body_does_not_crash() -> None:
    """ÖBB occasionally returns Latin-1-encoded bytes in mgate responses
    (especially in `locL` station names). The adapter must decode
    without raising — the modal can't display a UnicodeDecodeError
    traceback to the operator."""
    # Latin-1 LocGeoPos response with 0xfc (`ü` in Latin-1) in name.
    resolve_body = (
        b'{"err": "OK", "svcResL": ['
        b'{"meth": "LocGeoPos", "err": "OK", '
        b'"res": {"locL": [{"lid": "A=1@L=8000261@", "name": "M\xfcnchen Hbf"}]}}, '
        b'{"meth": "LocGeoPos", "err": "OK", '
        b'"res": {"locL": [{"lid": "A=1@L=8011160@", "name": "Berlin Hbf"}]}}'
        b"]}"
    )
    trip_body_json = _hafas_ok_response([{"dur": "041500", "chg": 1}])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        meth = body["svcReqL"][0]["meth"]
        if meth == "LocGeoPos":
            return httpx.Response(
                200, content=resolve_body, headers={"Content-Type": "application/json"}
            )
        return httpx.Response(200, json=trip_body_json)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await external_verify.verify_via_oebb_hafas(
            from_lat=48.1402,
            from_lon=11.5586,  # München Hbf
            to_lat=52.5251,
            to_lon=13.3692,  # Berlin Hbf
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=client,
        )
    assert result.ok is True
    assert result.num_connections == 1
    assert result.best_duration_seconds == 4 * 3600 + 15 * 60


# ─────────── depart_at is Vienna wall-clock on the wire ───────────
# `outDate`/`outTime` go on the wire as bare strftime strings with no
# offset, and HAFAS reads them in the operator's own zone. Any tz-aware
# `depart_at` must therefore be CONVERTED to Europe/Vienna before
# formatting. The coverage runner hands in `run.depart_at` straight from a
# timestamptz column (psycopg returns UTC), so without this a
# Europe/Brussels run's 06:40 (= 04:40Z) asked OeBB about 04:40 Vienna.


def test_build_trip_search_body_converts_aware_depart_at_to_vienna() -> None:
    from datetime import UTC

    # 04:40Z on 2026-07-20 == 06:40 Vienna (CEST, UTC+2)
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=X@",
        to_lid="A=1@L=Y@",
        depart_at=datetime(2026, 7, 20, 4, 40, 0, tzinfo=UTC),
    )
    req = body["svcReqL"][0]["req"]
    assert req["outTime"] == "064000"
    assert req["outDate"] == "20260720"


def test_build_trip_search_body_conversion_can_roll_the_date() -> None:
    from datetime import UTC

    # 23:30Z on 2026-07-20 == 01:30 Vienna on the 21st.
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=X@",
        to_lid="A=1@L=Y@",
        depart_at=datetime(2026, 7, 20, 23, 30, 0, tzinfo=UTC),
    )
    req = body["svcReqL"][0]["req"]
    assert req["outDate"] == "20260721"
    assert req["outTime"] == "013000"


def test_build_trip_search_body_passes_naive_depart_at_through() -> None:
    """A naive depart_at is already the Vienna wall-clock the caller meant;
    converting it would be a guess. Preserves the pre-existing contract."""
    body = external_verify._build_trip_search_body(
        from_lid="A=1@L=X@",
        to_lid="A=1@L=Y@",
        depart_at=datetime(2026, 7, 20, 6, 40, 0),
    )
    assert body["svcReqL"][0]["req"]["outTime"] == "064000"


def test_to_oebb_local_is_idempotent_for_an_already_vienna_datetime() -> None:
    """`hafas_client._localise_when` already converts before calling into
    the two-step, so the journey path double-converts. Must be a no-op."""
    from zoneinfo import ZoneInfo

    vienna = datetime(2026, 7, 20, 6, 40, tzinfo=ZoneInfo("Europe/Vienna"))
    once = external_verify._to_oebb_local(vienna)
    twice = external_verify._to_oebb_local(once)
    assert once == twice == vienna
