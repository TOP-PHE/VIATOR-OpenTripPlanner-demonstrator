"""Tests for `app.journey.hafas_client` — the journey-level adapter on
top of ÖBB's public HAFAS backend.

Three layers, mirroring `test_external_verify.py`:

  1. Pure normaliser helpers (`_index_locations`, `_index_products`,
     `_index_operators`, `_hafas_dt_to_utc_iso`, `_map_cat_to_mode`,
     `_section_duration_seconds`) — no network, deterministic.
  2. `_normalise_payload` end-to-end: a HAFAS-shaped TripSearch
     payload → canonical trip dicts (matches OTP / MOTIS / OJP shape,
     including the PR-3 `first_transit_leg_departure_utc` field).
  3. `fetch_plan` integration through `external_verify.fetch_oebb_two_step`
     with httpx's `MockTransport`, covering the three observable
     status states the comparison panel distinguishes: ok / no_route /
     error.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.journey import hafas_client
from app.network_coverage import external_verify

# ─────────────────────── pure helpers ───────────────────────


def test_index_locations_inverts_micro_degrees_to_decimals() -> None:
    """HAFAS `common.locL` coords are integer micro-degrees (lat * 1e6,
    lon * 1e6) with x=lon / y=lat. The normaliser must invert that
    convention so downstream code sees decimal-degree floats."""
    loc_l = [
        {
            "name": "Wien Hbf",
            "lid": "A=1@L=8100002@",
            "crd": {"x": 16_375_400, "y": 48_185_900},
        },
        {
            "name": "Salzburg Hbf",
            "lid": "A=1@L=8100173@",
            "crd": {"x": 13_046_700, "y": 47_812_700},
        },
    ]
    out = hafas_client._index_locations(loc_l)
    assert out[0]["name"] == "Wien Hbf"
    assert out[0]["lid"] == "A=1@L=8100002@"
    # lat = y / 1e6, lon = x / 1e6 — inverting LocGeoPos's swap.
    assert out[0]["lat"] == pytest.approx(48.185900)
    assert out[0]["lon"] == pytest.approx(16.375400)
    assert out[1]["lat"] == pytest.approx(47.812700)


def test_index_locations_handles_missing_coords() -> None:
    """Some HAFAS entries (e.g. category-only sentinels in the common
    pool) lack `crd`. The normaliser must default lat/lon to None so
    a card without coords renders rather than crashing."""
    out = hafas_client._index_locations([{"name": "no coords"}])
    assert out[0]["name"] == "no coords"
    assert out[0]["lat"] is None
    assert out[0]["lon"] is None


def test_index_products_pulls_name_line_cat_oprx() -> None:
    """Products carry the train identifier (name = "RJ 1141"), the
    line short-name (addName / nameS = "RJ"), the GTFS-ish category
    (`prodCtx.catOut`), and an index into `opL` for the operator."""
    prod_l = [
        {
            "name": "RJ 1141",
            "addName": "RJ",
            "prodCtx": {"catOut": "RAIL"},
            "oprX": 0,
        }
    ]
    out = hafas_client._index_products(prod_l)
    assert out[0]["name"] == "RJ 1141"
    assert out[0]["line"] == "RJ"
    assert out[0]["cat"] == "RAIL"
    assert out[0]["oprX"] == 0


def test_index_operators_extracts_name_id() -> None:
    op_l = [{"name": "ÖBB-Personenverkehr AG", "id": "OBB"}]
    out = hafas_client._index_operators(op_l)
    assert out[0]["name"] == "ÖBB-Personenverkehr AG"
    assert out[0]["id"] == "OBB"


@pytest.mark.parametrize(
    ("category", "expected_mode"),
    [
        ("RJ", "RAIL"),
        ("ICE", "RAIL"),
        ("EC", "RAIL"),
        ("TGV", "RAIL"),
        ("AVE", "RAIL"),
        ("BUS", "BUS"),
        ("Tram", "TRAM"),
        ("U-BAHN", "SUBWAY"),
        ("S-BAHN", "RAIL"),
        ("FERRY", "FERRY"),
        ("RAIL", "RAIL"),
        ("", "TRANSIT"),  # empty falls through to TRANSIT
        ("CUSTOM-MODE", "CUSTOM-MODE"),  # unrecognised pass-through (upper)
        (None, "TRANSIT"),  # missing category
        (7, "7"),  # HAFAS sometimes sends a numeric product code, not a string
        (0, "TRANSIT"),  # falsy int must not crash str(cat or "").upper()
    ],
)
def test_map_cat_to_mode(category: str | int | None, expected_mode: str) -> None:
    """Map various HAFAS product categories to VIATOR's mode
    vocabulary. The pass-through case is deliberate — a new category
    we haven't classified should still be visible in diagnostics
    rather than silently collapsed to a wrong bucket."""
    assert hafas_client._map_cat_to_mode(category) == expected_mode


def test_hafas_dt_to_utc_iso_localises_vienna_time_to_utc() -> None:
    """HAFAS expresses times in Europe/Vienna for ÖBB. The normaliser
    must convert to UTC for cross-engine consistency with the recorder
    (which expects `+00:00`-suffixed strings)."""
    # 2026-06-28 08:30:15 Europe/Vienna (CEST, UTC+2) → 06:30:15 UTC
    iso = hafas_client._hafas_dt_to_utc_iso("20260628", "083015")
    assert iso == "2026-06-28T06:30:15+00:00"


def test_hafas_dt_to_utc_iso_handles_day_roll_prefix() -> None:
    """Connections crossing midnight come back with a day-offset
    prefix on the time field (e.g. "01024500" = next-day 02:45:00).
    The normaliser must add the offset to the date — silently dropping
    the prefix would put a +1 day train on yesterday."""
    iso = hafas_client._hafas_dt_to_utc_iso("20260628", "01024500")
    # 2026-06-29 02:45:00 Europe/Vienna (CEST) → 00:45:00 UTC same day.
    assert iso == "2026-06-29T00:45:00+00:00"


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "20260"])
def test_hafas_dt_to_utc_iso_returns_none_on_bad_date(bad_date: str) -> None:
    """Malformed date must return None rather than crash the
    normaliser — one bad row shouldn't drop the whole panel."""
    assert hafas_client._hafas_dt_to_utc_iso(bad_date, "083015") is None


@pytest.mark.parametrize("bad_time", [None, "", "abcdef"])
def test_hafas_dt_to_utc_iso_returns_none_on_bad_time(bad_time) -> None:
    assert hafas_client._hafas_dt_to_utc_iso("20260628", bad_time) is None


def test_section_duration_seconds_prefers_gis_dur() -> None:
    """`gis.dur` is the walk-time HAFAS computed — prefer it over a
    derived dep/arr difference when both are present."""
    sec = {"gis": {"dur": "000300"}}  # 3 minutes
    out = hafas_client._section_duration_seconds(
        sec, "2026-06-28T08:00:00+00:00", "2026-06-28T08:15:00+00:00"
    )
    assert out == 180


def test_section_duration_seconds_falls_back_to_iso_diff() -> None:
    """No gis.dur → compute from dep/arr ISO strings. Useful when
    HAFAS omits the gis block on a transit section (where the
    duration comes from the train timetable, not a walk routing)."""
    out = hafas_client._section_duration_seconds(
        {}, "2026-06-28T08:00:00+00:00", "2026-06-28T10:30:00+00:00"
    )
    assert out == 2 * 3600 + 30 * 60


def test_section_duration_seconds_zero_when_unparseable() -> None:
    """Neither gis nor ISO inputs → 0, not None — keeps the canonical
    leg dict's `duration_seconds` type stable for downstream consumers."""
    assert hafas_client._section_duration_seconds({}, None, None) == 0


# ─────────────────────── _normalise_payload end-to-end ───────────────────────


def _hafas_payload(connections: list[dict], common: dict | None = None) -> dict:
    """Build a HAFAS-shaped TripSearch payload around the supplied
    connections + common-pool dictionary."""
    return {
        "err": "OK",
        "svcResL": [
            {
                "meth": "TripSearch",
                "err": "OK",
                "res": {
                    "common": common or {"locL": [], "prodL": [], "opL": []},
                    "outConL": connections,
                },
            }
        ],
    }


def _real_world_payload() -> dict:
    """A trimmed real-shape HAFAS TripSearch response: Wien Hbf →
    Salzburg Hbf, one RJ direct, ~2h45. Pinned shape matches what
    fahrplan.oebb.at returns in production; if HAFAS rotates field
    names this test breaks loudly with the diff."""
    common = {
        "locL": [
            {
                "name": "Wien Hbf",
                "lid": "A=1@L=8100002@",
                "crd": {"x": 16_375_400, "y": 48_185_900},
            },
            {
                "name": "Salzburg Hbf",
                "lid": "A=1@L=8100173@",
                "crd": {"x": 13_046_700, "y": 47_812_700},
            },
        ],
        "prodL": [
            {
                "name": "RJ 1141",
                "addName": "RJ",
                "prodCtx": {"catOut": "RJ"},
                "oprX": 0,
            }
        ],
        "opL": [{"name": "ÖBB-Personenverkehr AG", "id": "OBB"}],
    }
    connections = [
        {
            "date": "20260628",
            "dur": "024500",  # 2h45
            "chg": 0,
            "dep": {"locX": 0, "dTimeS": "080000"},
            "arr": {"locX": 1, "aTimeS": "104500"},
            "secL": [
                {
                    "type": "JNY",
                    "dep": {"locX": 0, "dTimeS": "080000"},
                    "arr": {"locX": 1, "aTimeS": "104500"},
                    "jny": {"prodX": 0, "jid": "1|123|0|81|28062026", "dirTxt": "Innsbruck Hbf"},
                }
            ],
        }
    ]
    return _hafas_payload(connections, common)


def test_normalise_payload_happy_path_canonical_trip_dict() -> None:
    """End-to-end: a real-shape HAFAS response normalises to a single
    canonical trip dict with all the fields the journey UI renders.
    Pinned against the OJP / OTP / MOTIS contract — every required
    key MUST be present even if its value is empty."""
    trips = hafas_client._normalise_payload(_real_world_payload())
    assert len(trips) == 1
    t = trips[0]
    # Top-level itinerary fields
    assert t["duration_seconds"] == 2 * 3600 + 45 * 60
    assert t["num_transfers"] == 0
    # CEST → UTC: 08:00 Vienna = 06:00 UTC, 10:45 Vienna = 08:45 UTC.
    assert t["departure_at"] == "2026-06-28T06:00:00+00:00"
    assert t["arrival_at"] == "2026-06-28T08:45:00+00:00"
    assert t["modes"] == "RAIL"
    # Per-leg fields
    assert len(t["legs"]) == 1
    leg = t["legs"][0]
    assert leg["mode"] == "RAIL"
    assert leg["from_name"] == "Wien Hbf"
    assert leg["to_name"] == "Salzburg Hbf"
    assert leg["from_stop_id"] == "A=1@L=8100002@"
    assert leg["to_stop_id"] == "A=1@L=8100173@"
    assert leg["from_lat"] == pytest.approx(48.185900)
    assert leg["to_lat"] == pytest.approx(47.812700)
    assert leg["route_short_name"] == "RJ"
    assert leg["route_long_name"] == "RJ 1141"
    assert leg["agency_name"] == "ÖBB-Personenverkehr AG"
    assert leg["agency_id"] == "OBB"
    assert leg["trip_id"] == "1|123|0|81|28062026"
    assert leg["trip_headsign"] == "Innsbruck Hbf"
    # Feed-id is the source label — drives the operator badge in the
    # journey UI so HAFAS-sourced legs render distinctly.
    assert leg["feed_id"] == "fahrplan.oebb.at"
    # PR-3 — first transit-leg boarding time MUST be present.
    assert t["first_transit_leg_departure_utc"] == "2026-06-28T06:00:00+00:00"


def test_normalise_payload_walking_section_marked_walk() -> None:
    """A walking / transfer section (`type:"WALK"` or `"TRSF"`) must
    surface as `mode="WALK"` so the journey UI's walk-leg colouring
    + the cross-engine `transit_fingerprint` (which strips walks) work
    on HAFAS trips identically to OTP/MOTIS/OJP ones."""
    common = {
        "locL": [
            {"name": "A", "lid": "A=1@L=1@", "crd": {"x": 0, "y": 0}},
            {"name": "B", "lid": "A=1@L=2@", "crd": {"x": 0, "y": 0}},
        ],
        "prodL": [],
        "opL": [],
    }
    connections = [
        {
            "date": "20260628",
            "dur": "000500",
            "chg": 0,
            "dep": {"locX": 0, "dTimeS": "080000"},
            "arr": {"locX": 1, "aTimeS": "080500"},
            "secL": [
                {
                    "type": "WALK",
                    "dep": {"locX": 0, "dTimeS": "080000"},
                    "arr": {"locX": 1, "aTimeS": "080500"},
                    "gis": {"dur": "000500", "dist": 350},
                }
            ],
        }
    ]
    trips = hafas_client._normalise_payload(_hafas_payload(connections, common))
    assert len(trips) == 1
    assert trips[0]["legs"][0]["mode"] == "WALK"
    assert trips[0]["legs"][0]["distance_meters"] == 350.0
    # Walk-only itinerary → no first transit leg → None per PR-3
    # contract (matches OTP / MOTIS / OJP).
    assert trips[0]["first_transit_leg_departure_utc"] is None


def test_normalise_payload_missing_secl_drops_connection() -> None:
    """A connection with neither `dep` nor `arr` populated isn't usable
    — better to drop the row than render a card with empty fields."""
    # date is required by `_normalise_connection`; missing → drop.
    trips = hafas_client._normalise_payload(_hafas_payload([{"dep": {}, "arr": {}, "secL": []}]))
    assert trips == []


def test_normalise_payload_empty_outconl_returns_empty_list() -> None:
    """No connections in the payload → empty trip list. Caller maps
    this to status="no_route" (HAFAS's clean negative answer)."""
    assert hafas_client._normalise_payload(_hafas_payload([])) == []


def test_normalise_payload_malformed_envelope_does_not_crash() -> None:
    """A payload missing svcResL / res / common still returns [] rather
    than raising — defensive against HAFAS error envelopes that the
    `external_verify._parse_hafas_response` path filters out earlier
    but might leak through if the contract changes."""
    assert hafas_client._normalise_payload({}) == []
    assert hafas_client._normalise_payload({"svcResL": []}) == []


# ─────────────────────── fetch_plan integration ───────────────────────


def _locgeopos_response(stops: list[tuple[str | None, str]]) -> dict:
    """LocGeoPos response shape (lifted from test_external_verify)."""
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


def _two_step_handler(*, resolve_response: dict, trip_response: dict):
    """MockTransport handler that routes LocGeoPos vs TripSearch by
    the first svcReqL's `meth`."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        meth = body["svcReqL"][0]["meth"]
        if meth == "LocGeoPos":
            return httpx.Response(200, json=resolve_response)
        if meth == "TripSearch":
            return httpx.Response(200, json=trip_response)
        return httpx.Response(400, text=f"unexpected meth: {meth}")

    return handler


def _patch_two_step_with_mock_transport(handler):
    """Helper: build a patch context manager that overrides
    `hafas_client`'s `external_verify` reference so its
    `fetch_oebb_two_step` calls route through an httpx MockTransport.

    Patching `external_verify.fetch_oebb_two_step` directly would
    create infinite recursion (the side-effect function calls the
    very symbol it's replacing), so we wrap the *real* function
    captured before patching and inject a MockTransport-backed
    client into it."""
    transport = httpx.MockTransport(handler)
    real_two_step = external_verify.fetch_oebb_two_step

    async def fake_two_step(**kwargs):
        # Strip any caller-supplied client (hafas_client never passes
        # one today, but be defensive against a future regression).
        kwargs.pop("client", None)
        async with httpx.AsyncClient(transport=transport) as c:
            return await real_two_step(client=c, **kwargs)

    return patch.object(external_verify, "fetch_oebb_two_step", side_effect=fake_two_step)


@pytest.mark.asyncio
async def test_fetch_plan_happy_returns_ok_and_trips() -> None:
    """End-to-end: LocGeoPos resolves both endpoints, TripSearch
    returns one RJ Wien → Salzburg. fetch_plan returns
    `(raw with status='ok', [trip])`. Patched two-step so the test
    runs without network."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response(
            [
                ("A=1@L=8100002@", "Wien Hbf"),
                ("A=1@L=8100173@", "Salzburg Hbf"),
            ]
        ),
        trip_response=_real_world_payload(),
    )
    with _patch_two_step_with_mock_transport(handler):
        raw, trips = await hafas_client.fetch_plan(
            from_lat=48.185900,
            from_lon=16.375400,
            to_lat=47.812700,
            to_lon=13.046700,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        )
    assert raw["status"] == "ok"
    assert raw["format"] == "hafas-mgate"
    assert raw["from_lid"] == "A=1@L=8100002@"
    assert raw["to_lid"] == "A=1@L=8100173@"
    assert len(trips) == 1
    assert trips[0]["modes"] == "RAIL"
    assert trips[0]["first_transit_leg_departure_utc"]


@pytest.mark.asyncio
async def test_fetch_plan_h890_returns_no_route_with_empty_trips() -> None:
    """Clean negative: LocGeoPos succeeds, TripSearch returns H890
    (HAFAS's "no connections"). fetch_plan must return status='no_route'
    with `[]` — the comparison panel renders this as a distinct
    "no itinerary found" state, not a transport error."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response([("A=1@L=1@", "A"), ("A=1@L=2@", "B")]),
        trip_response={
            "err": "OK",
            "svcResL": [{"meth": "TripSearch", "err": "H890"}],
        },
    )
    with _patch_two_step_with_mock_transport(handler):
        raw, trips = await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        )
    assert raw["status"] == "no_route"
    assert trips == []
    assert "error" not in raw  # H890 is not an error condition


@pytest.mark.asyncio
async def test_fetch_plan_resolve_failure_returns_error_with_message() -> None:
    """When LocGeoPos can't snap one endpoint (Bergen-NO style), the
    underlying verdict carries a friendly error string. fetch_plan
    must surface it as status='error' + `error` so the comparison
    panel renders the yellow "unavailable" state with the diagnostic."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response(
            [
                ("A=1@L=1@", "A"),
                (None, ""),  # dest didn't snap — Bergen-NO style
            ]
        ),
        trip_response={},  # never reached
    )
    with _patch_two_step_with_mock_transport(handler):
        raw, trips = await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=60.39,
            to_lon=5.32,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        )
    assert raw["status"] == "error"
    assert raw["error"] is not None
    assert "no station" in raw["error"].lower()
    assert trips == []


@pytest.mark.asyncio
async def test_fetch_plan_network_failure_surfaces_as_error() -> None:
    """A transport exception inside the two-step path is converted to
    `verdict.error` by `fetch_oebb_two_step` (it never raises), so
    fetch_plan sees `verdict.error` set and returns status='error'."""

    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend down")

    with _patch_two_step_with_mock_transport(handler):
        raw, trips = await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        )
    assert raw["status"] == "error"
    assert raw["error"]
    assert trips == []


@pytest.mark.asyncio
async def test_fetch_plan_records_response_ms() -> None:
    """Every response carries a `response_ms` timing field so the
    journey UI can render "Reference (ÖBB HAFAS) · Nms" in the
    panel header. Wall-time approximate, just assert it's a non-
    negative int."""
    fake = AsyncMock(
        return_value=external_verify.HafasTripPayload(
            verdict=external_verify.VerifyResult(
                source="fahrplan.oebb.at", ok=False, num_connections=0
            )
        )
    )
    with patch.object(external_verify, "fetch_oebb_two_step", fake):
        raw, _ = await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
        )
    assert isinstance(raw["response_ms"], int)
    assert raw["response_ms"] >= 0


@pytest.mark.asyncio
async def test_fetch_plan_naive_when_localises_to_vienna() -> None:
    """A naive `when` (the journey UI's datetime-local) must be
    localised to Europe/Vienna before being passed into the
    two-step — preserves the operator's "pick 12:51 → HAFAS searches
    12:51 Vienna-local" semantics. Captured via the `depart_at`
    kwarg the mock receives."""
    captured: dict = {}

    async def fake_two_step(**kwargs):
        captured.update(kwargs)
        return external_verify.HafasTripPayload(
            verdict=external_verify.VerifyResult(
                source="fahrplan.oebb.at", ok=False, num_connections=0
            )
        )

    with patch.object(external_verify, "fetch_oebb_two_step", side_effect=fake_two_step):
        await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=datetime(2026, 6, 28, 12, 51, 0),  # naive
        )
    assert "depart_at" in captured
    assert captured["depart_at"].tzinfo is not None
    # Wall time MUST be preserved — operator's 12:51 stays 12:51,
    # only the tzinfo is attached.
    assert captured["depart_at"].hour == 12
    assert captured["depart_at"].minute == 51


@pytest.mark.asyncio
async def test_fetch_plan_aware_when_converted_to_vienna_wall_clock() -> None:
    """A tz-aware `when` (e.g. `datetime.now(UTC)` from `_resolve_when`,
    or any pagination anchor, which `next_anchor_or_none` emits in UTC)
    must be CONVERTED to Europe/Vienna — not passed through.

    `external_verify._build_trip_search_body` serialises outDate/outTime
    with bare strftime, no offset, and HAFAS reads those fields as
    Vienna local. Passing a UTC-aware datetime through unchanged sent
    UTC wall-clock mislabelled as Vienna, searching 2 h earlier than
    asked (CEST). The *instant* must be preserved while the wall-clock
    fields become Vienna's."""
    captured: dict = {}

    async def fake_two_step(**kwargs):
        captured.update(kwargs)
        return external_verify.HafasTripPayload(
            verdict=external_verify.VerifyResult(
                source="fahrplan.oebb.at", ok=False, num_connections=0
            )
        )

    when = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)  # 2026-06-28 is CEST (UTC+2)
    with patch.object(external_verify, "fetch_oebb_two_step", side_effect=fake_two_step):
        await hafas_client.fetch_plan(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            when=when,
        )
    # Same instant...
    assert captured["depart_at"] == when
    # ...but the wall-clock HAFAS will read is Vienna's, not UTC's.
    assert captured["depart_at"].hour == 10
    assert captured["depart_at"].strftime("%H%M%S") == "100000"


# ─────────────────────── verify_via_oebb_hafas façade still works ───────────────────────


@pytest.mark.asyncio
async def test_external_verify_facade_unchanged_after_refactor() -> None:
    """`verify_via_oebb_hafas` is now a thin wrapper around
    `fetch_oebb_two_step` — must still return exactly a VerifyResult,
    same shape as before the refactor. Guards against journey-API or
    coverage-runner callers being broken by the refactor."""
    handler = _two_step_handler(
        resolve_response=_locgeopos_response([("A=1@L=1@", "A"), ("A=1@L=2@", "B")]),
        trip_response={
            "err": "OK",
            "svcResL": [
                {
                    "meth": "TripSearch",
                    "err": "OK",
                    "res": {"outConL": [{"dur": "020000", "chg": 1}]},
                }
            ],
        },
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as c:
        result = await external_verify.verify_via_oebb_hafas(
            from_lat=0.0,
            from_lon=0.0,
            to_lat=0.0,
            to_lon=0.0,
            depart_at=datetime(2026, 6, 28, 8, 0, 0),
            client=c,
        )
    assert isinstance(result, external_verify.VerifyResult)
    assert result.ok is True
    assert result.num_connections == 1
    assert result.best_duration_seconds == 2 * 3600


# ─────────────────────── fetch_plan_paginated ───────────────────────
#
# v0.1.45 — mirrors test_ojp_client.py's TestFetchReferencePaginated
# (same algorithm, shared via trip_normalize.dedup_batch_and_track_
# latest_dep / next_anchor_or_none). Monkeypatches `hafas_client.
# fetch_plan` directly rather than mocking HTTP transport, matching
# how the OJP suite tests fetch_reference_paginated against
# fetch_reference — the pagination loop's own logic is what's under
# test, not the wire format underneath a single page.


def _make_hafas_trip(*, dep_iso: str, route: str, from_uic: str, to_uic: str) -> dict:
    """A minimal trip dict whose transit_fingerprint is determined by
    (route, from_uic, to_uic, dep_minute) — same shape convention as
    test_ojp_client.py's _make_trip."""
    return {
        "duration_seconds": 1800,
        "num_transfers": 0,
        "departure_at": dep_iso,
        "arrival_at": dep_iso,
        "modes": "RAIL",
        "legs": [
            {
                "mode": "RAIL",
                "from_lat": 48.1859,
                "from_lon": 16.3754,
                "to_lat": 47.8127,
                "to_lon": 13.0467,
                "from_stop_id": f"HAFAS:{from_uic}",
                "to_stop_id": f"HAFAS:{to_uic}",
                "departure": dep_iso,
                "arrival": dep_iso,
                "route_short_name": route,
                "feed_id": "fahrplan.oebb.at",
            }
        ],
        "feed_id": "fahrplan.oebb.at",
    }


class TestFetchPlanPaginated:
    @pytest.mark.asyncio
    async def test_single_page_covers_window_no_pagination(self, monkeypatch) -> None:
        # First batch's latest departure already reaches the target
        # window end - one call, return what came back.
        async def fake_fetch_plan(**kw):
            when = kw["when"]
            trip = _make_hafas_trip(
                dep_iso=(when + timedelta(hours=7)).isoformat(),
                route="RJ 63",
                from_uic="8100002",
                to_uic="8100173",
            )
            return {"status": "ok", "response_ms": 400}, [trip]

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert len(trips) == 1
        assert raw["status"] == "ok"
        assert "pages" not in raw  # single page — no pagination diagnostic noise

    @pytest.mark.asyncio
    async def test_paginates_until_window_covered(self, monkeypatch) -> None:
        # Each batch's latest trip is +1h ahead of its anchor. Target
        # window 4h → need 4 calls to walk past the 4h mark.
        calls: list[datetime] = []

        async def fake_fetch_plan(**kw):
            calls.append(kw["when"])
            base = kw["when"]
            trips = [
                _make_hafas_trip(
                    dep_iso=(base + timedelta(minutes=30)).isoformat(),
                    route="RJ 1",
                    from_uic=f"810{1000 + len(calls)}",
                    to_uic="8100173",
                ),
                _make_hafas_trip(
                    dep_iso=(base + timedelta(minutes=60)).isoformat(),
                    route="RJ 2",
                    from_uic=f"810{2000 + len(calls)}",
                    to_uic="8100173",
                ),
            ]
            return {"status": "ok", "response_ms": 300}, trips

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        start = datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC)
        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=start,
            timeout_ms=5000,
            target_window_seconds=4 * 3600,
            max_pages=8,
        )
        assert raw.get("pages") == 4
        assert len(trips) == 8  # 2 unique trips per page x 4 pages
        assert raw["response_ms"] == 300 * 4  # summed across pages
        for i in range(1, len(calls)):
            assert calls[i] > calls[i - 1]

    @pytest.mark.asyncio
    async def test_empty_batch_stops_pagination(self, monkeypatch) -> None:
        page_calls = {"n": 0}

        async def fake_fetch_plan(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {"status": "ok", "response_ms": 200}, [
                    _make_hafas_trip(
                        dep_iso=(kw["when"] + timedelta(minutes=30)).isoformat(),
                        route="RJ 1",
                        from_uic="8100001",
                        to_uic="8100173",
                    )
                ]
            return {"status": "no_route", "response_ms": 150}, []

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert page_calls["n"] == 2
        assert len(trips) == 1
        # Partial data recovered from page 1 -> overall status still 'ok'.
        assert raw["status"] == "ok"

    @pytest.mark.asyncio
    async def test_first_page_error_propagates_status(self, monkeypatch) -> None:
        async def fake_fetch_plan(**kw):
            return {"status": "error", "error": "boom", "response_ms": 50}, []

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert trips == []
        assert raw["status"] == "error"
        assert raw["error"] == "boom"

    @pytest.mark.asyncio
    async def test_all_duplicates_stops_pagination(self, monkeypatch) -> None:
        # Fake ignores the anchor and always echoes back the exact same
        # connection (fixed dep_iso -> identical transit_fingerprint
        # every page). Page 1 adds it as new; page 2 sees only a dup and
        # must bail rather than looping to max_pages asking the same
        # question forever.
        page_calls = {"n": 0}

        async def fake_fetch_plan(**kw):
            page_calls["n"] += 1
            trip = _make_hafas_trip(
                dep_iso="2026-06-28T08:30:00+00:00",
                route="RJ 1",
                from_uic="8100001",
                to_uic="8100173",
            )
            return {"status": "ok", "response_ms": 100}, [trip]

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        _raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert page_calls["n"] == 2  # page 1: new trip; page 2: dup detected, bail
        assert len(trips) == 1

    @pytest.mark.asyncio
    async def test_midflight_page_error_surfaces_partial_not_clean_ok(self, monkeypatch) -> None:
        """A page-2+ failure must keep the trips gathered so far BUT
        flag `partial` + `error` — otherwise a transient HAFAS 5xx on
        page 2 renders as a clean, complete-looking result set that is
        silently short of the target window."""
        page_calls = {"n": 0}

        async def fake_fetch_plan(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {"status": "ok", "response_ms": 200}, [
                    _make_hafas_trip(
                        dep_iso=(kw["when"] + timedelta(minutes=30)).isoformat(),
                        route="RJ 1",
                        from_uic="8100001",
                        to_uic="8100173",
                    )
                ]
            return {"status": "error", "error": "HAFAS 502", "response_ms": 90}, []

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert len(trips) == 1  # page 1's trip survives
        assert raw["status"] == "ok"  # trips render
        assert raw["partial"] is True  # ...but coverage is short
        assert "HAFAS 502" in raw["error"]

    @pytest.mark.asyncio
    async def test_clean_no_route_on_later_page_is_not_flagged_partial(self, monkeypatch) -> None:
        """The mirror of the test above: HAFAS cleanly running out of
        connections (`no_route`) is NOT an error and must not set
        `partial` — otherwise every fully-paginated search would carry
        a spurious warning."""
        page_calls = {"n": 0}

        async def fake_fetch_plan(**kw):
            page_calls["n"] += 1
            if page_calls["n"] == 1:
                return {"status": "ok", "response_ms": 200}, [
                    _make_hafas_trip(
                        dep_iso=(kw["when"] + timedelta(minutes=30)).isoformat(),
                        route="RJ 1",
                        from_uic="8100001",
                        to_uic="8100173",
                    )
                ]
            return {"status": "no_route", "response_ms": 90}, []

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=5000,
            target_window_seconds=21600,
            max_pages=4,
        )
        assert len(trips) == 1
        assert raw["status"] == "ok"
        assert "partial" not in raw
        assert "error" not in raw

    @pytest.mark.asyncio
    async def test_total_timeout_budget_stops_pagination(self, monkeypatch) -> None:
        """The whole paginated call is bounded by `total_timeout_ms`, not
        just each page. Without this a slow-but-not-erroring HAFAS could
        burn max_pages x timeout_ms (4 x 10 s) while /fanout holds the
        journey concurrency semaphore.

        Simulated by advancing a fake monotonic clock inside each page
        so no real time passes."""
        page_calls = {"n": 0}
        clock = {"t": 0.0}

        monkeypatch.setattr(hafas_client.time, "monotonic", lambda: clock["t"])

        async def fake_fetch_plan(**kw):
            page_calls["n"] += 1
            clock["t"] += 4.0  # each page "takes" 4 s
            return {"status": "ok", "response_ms": 4000}, [
                _make_hafas_trip(
                    dep_iso=(kw["when"] + timedelta(minutes=30)).isoformat(),
                    route=f"RJ {page_calls['n']}",
                    from_uic=f"810{1000 + page_calls['n']}",
                    to_uic="8100173",
                )
            ]

        monkeypatch.setattr(hafas_client, "fetch_plan", fake_fetch_plan)

        raw, trips = await hafas_client.fetch_plan_paginated(
            from_lat=0,
            from_lon=0,
            to_lat=0,
            to_lon=0,
            when=datetime(2026, 6, 28, 8, 0, 0, tzinfo=UTC),
            timeout_ms=10_000,
            target_window_seconds=21600,  # would otherwise want all 4 pages
            max_pages=4,
            total_timeout_ms=10_000,  # only ~2 pages' worth of budget
        )
        # 4 s + 4 s = 8 s spent; a 3rd page has < 500 ms floor... it has
        # 2 s, so it runs, reaching 12 s > 10 s -> 4th page refused.
        assert page_calls["n"] < 4, "budget must stop pagination before max_pages"
        assert raw["pagination_stopped"] == "budget"
        assert len(trips) == page_calls["n"]  # every issued page's trip kept
