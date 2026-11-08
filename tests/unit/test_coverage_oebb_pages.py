"""The coverage check's later pages of ÖBB connections (`oebb_journeys.fetch_cell_answer`).

One ÖBB `TripSearch` is its next 5 connections (`numF: 5`); a cell's VIATOR journeys span the
day. The check asks ÖBB again from just after its last departure until that reaches VIATOR's last
first boarding (`alignment.last_boarding_to_cover`), at most `COVERAGE_VERIFY_MAX_PAGES` pages.
Pinned here: the anchors and the stations reused, each stop condition (covered, the cap, an empty
page, an all-seen page, the time budget, a failed later page), the pause between pages, the merged
verdict, the ids, the scorer's window over every page, and the runner's wiring (the knob, the
snapshot of a run from before, the cell's detail). No network, no database.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config_schema import CONFIG_SCHEMA, coerce, default_for
from app.network_coverage import external_verify, oebb_journeys, runner
from app.network_coverage.alignment import (
    compute_alignment,
    explain_cell,
    last_boarding_to_cover,
    reference_window,
)
from app.network_coverage.external_verify import HafasTripPayload
from tests.unit import canonical_fixtures as fx
from tests.unit.matching_journeys import journey, ride

VIENNA = ZoneInfo("Europe/Vienna")
DAY = "2026-11-09"  # CET: Vienna = UTC+1
FROM_LID = "A=1@O=Wien Hbf@X=16375326@Y=48185507@U=81@L=8103000@"
TO_LID = "A=1@O=Salzburg Hbf@X=13045819@Y=47812988@U=81@L=8100002@"
DEPART_AT = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # 07:00 Vienna


def vienna(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hhmm}").replace(tzinfo=VIENNA)


def conn(dep: str, arr: str, route: str) -> list[dict[str, Any]]:
    """One ÖBB connection Wien Hbf → Salzburg Hbf, Vienna wall clock."""
    return [
        {
            "mode": "RAIL",
            "from_uic": "UIC:8103000",
            "to_uic": "UIC:8100002",
            "dep": f"{DAY}T{dep}",
            "arr": f"{DAY}T{arr}",
            "route": route,
        }
    ]


def page(
    *connections: list[dict[str, Any]], durations: list[str] | None = None
) -> HafasTripPayload:
    """One TripSearch answer as `fetch_oebb_two_step` / `fetch_oebb_trip_search` return it."""
    payload = fx.oebb_payload(*connections)
    if durations is not None:
        for raw, dur in zip(payload["svcResL"][0]["res"]["outConL"], durations, strict=True):
            raw["dur"] = dur
    return HafasTripPayload(
        verdict=external_verify._parse_hafas_response(payload),
        payload=payload,
        from_lid=FROM_LID,
        to_lid=TO_LID,
    )


def no_route() -> HafasTripPayload:
    verdict = external_verify.VerifyResult(source="fahrplan.oebb.at", ok=False, num_connections=0)
    return HafasTripPayload(verdict=verdict, from_lid=FROM_LID, to_lid=TO_LID)


def failed(error: str = "HTTP 500") -> HafasTripPayload:
    verdict = external_verify.VerifyResult(source="fahrplan.oebb.at", ok=False, error=error)
    return HafasTripPayload(verdict=verdict, from_lid=FROM_LID, to_lid=TO_LID)


# The three pages of a long day, 5 / 5 / 2 connections an hour apart from 07:00 Vienna.
PAGE_1 = page(*(conn(f"{h:02d}:00", f"{h + 2:02d}:30", f"RJ {700 + h}") for h in range(7, 12)))
PAGE_2 = page(*(conn(f"{h:02d}:00", f"{h + 2:02d}:30", f"RJ {700 + h}") for h in range(12, 17)))
PAGE_3 = page(*(conn(f"{h:02d}:00", f"{h + 2:02d}:30", f"RJ {700 + h}") for h in range(17, 19)))

POLICY = oebb_journeys.PagePolicy(max_pages=5, pause_s=0.0, page_timeout_s=30.0, budget_s=60.0)


async def ask(
    first: HafasTripPayload,
    later: list[HafasTripPayload],
    *,
    until: datetime | None,
    policy: oebb_journeys.PagePolicy = POLICY,
) -> tuple[oebb_journeys.CellAnswer, AsyncMock, AsyncMock]:
    """`fetch_cell_answer` with ÖBB stood in: page 1 by `first`, the later pages by `later`."""
    two_step = AsyncMock(return_value=first)
    trip_search = AsyncMock(side_effect=later)
    with (
        patch.object(external_verify, "fetch_oebb_two_step", two_step),
        patch.object(external_verify, "fetch_oebb_trip_search", trip_search),
    ):
        answer = await oebb_journeys.fetch_cell_answer(
            from_lat=48.18,
            from_lon=16.37,
            to_lat=47.81,
            to_lon=13.04,
            depart_at=DEPART_AT,
            client=MagicMock(spec=httpx.AsyncClient),
            until=until,
            policy=policy,
        )
    return answer, two_step, trip_search


def departures(answer: oebb_journeys.CellAnswer) -> list[datetime]:
    return [j.departure.timetabled for j in answer.journeys if j.departure is not None]


# ── Paging until ÖBB reaches VIATOR's last departure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_later_pages_are_asked_until_oebb_reaches_viator_s_last_departure() -> None:
    answer, two_step, trip_search = await ask(PAGE_1, [PAGE_2, PAGE_3], until=vienna("17:30"))

    assert two_step.await_count == 1
    assert trip_search.await_count == 2
    assert (answer.pages, answer.stopped, answer.page_error) == (
        3,
        oebb_journeys.STOP_COVERED,
        None,
    )
    assert departures(answer) == [vienna(f"{h:02d}:00") for h in range(7, 19)]
    assert answer.pages_detail() == {"asked": 3, "stopped": "covered"}


@pytest.mark.asyncio
async def test_a_later_page_starts_a_minute_after_oebb_s_last_departure_between_the_same_stations() -> (
    None
):
    _, _, trip_search = await ask(PAGE_1, [PAGE_2, PAGE_3], until=vienna("17:30"))

    first, second = (call.kwargs for call in trip_search.await_args_list)
    assert (first["from_lid"], first["to_lid"]) == (FROM_LID, TO_LID)
    assert first["depart_at"] == vienna("11:01")  # page 1's last connection 11:00, +60 s
    assert second["depart_at"] == vienna("16:01")
    assert first["timeout_s"] <= POLICY.page_timeout_s


@pytest.mark.asyncio
async def test_no_later_page_when_page_1_already_reaches_viator_s_last_departure() -> None:
    answer, _, trip_search = await ask(PAGE_1, [PAGE_2], until=vienna("11:00"))

    trip_search.assert_not_awaited()
    assert (answer.pages, answer.stopped) == (1, oebb_journeys.STOP_COVERED)
    assert answer.journeys == oebb_journeys.journeys_from_answer(PAGE_1)


@pytest.mark.asyncio
async def test_the_cap_stops_the_paging_and_says_so() -> None:
    capped = oebb_journeys.PagePolicy(max_pages=2, budget_s=60.0)

    answer, _, trip_search = await ask(
        PAGE_1, [PAGE_2, PAGE_3], until=vienna("20:00"), policy=capped
    )

    assert trip_search.await_count == 1
    assert (answer.pages, answer.stopped) == (2, oebb_journeys.STOP_MAX_PAGES)
    assert len(answer.journeys) == 10


@pytest.mark.asyncio
async def test_one_page_is_today_s_check_unchanged() -> None:
    """COVERAGE_VERIFY_MAX_PAGES = 1: ÖBB is asked once, and its verdict and journeys are page 1's
    as they were before paging (ids included)."""
    answer, _, trip_search = await ask(
        PAGE_1, [PAGE_2], until=vienna("20:00"), policy=oebb_journeys.PagePolicy(max_pages=1)
    )

    trip_search.assert_not_awaited()
    assert answer.verdict == PAGE_1.verdict
    assert [j.model_dump() for j in answer.journeys] == [
        j.model_dump() for j in oebb_journeys.journeys_from_answer(PAGE_1)
    ]
    assert answer.pages_detail() == {"asked": 1, "stopped": "max_pages"}


@pytest.mark.parametrize(
    ("first", "until"),
    [
        (PAGE_1, None),  # VIATOR has nothing to reach
        (no_route(), vienna("20:00")),  # ÖBB found nothing
        (failed(), vienna("20:00")),  # ÖBB could not be asked: page 1's error, as before
        (PAGE_1.model_copy(update={"to_lid": None}), vienna("20:00")),  # no station to page between
    ],
)
@pytest.mark.asyncio
async def test_paging_is_not_considered_without_a_target_or_a_first_answer(
    first: HafasTripPayload, until: datetime | None
) -> None:
    answer, _, trip_search = await ask(first, [PAGE_2], until=until)

    trip_search.assert_not_awaited()
    assert answer.verdict == first.verdict
    assert answer.pages_detail() is None


# ── Stop conditions of a later page ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_later_page_ends_the_paging() -> None:
    answer, _, trip_search = await ask(PAGE_1, [no_route(), PAGE_3], until=vienna("20:00"))

    assert trip_search.await_count == 1
    assert (answer.pages, answer.stopped) == (2, oebb_journeys.STOP_NO_MORE)
    assert len(answer.journeys) == 5


@pytest.mark.asyncio
async def test_a_connection_seen_on_an_earlier_page_is_dropped() -> None:
    overlapping = page(conn("11:00", "13:30", "RJ 711"), conn("12:00", "14:30", "RJ 712"))

    answer, _, _ = await ask(PAGE_1, [overlapping, PAGE_3], until=vienna("17:30"))

    assert departures(answer) == [
        *(vienna(f"{h:02d}:00") for h in range(7, 13)),
        vienna("17:00"),
        vienna("18:00"),
    ]
    assert answer.verdict.num_connections == 5 + 1 + 2


@pytest.mark.asyncio
async def test_a_page_of_connections_already_seen_ends_the_paging() -> None:
    repeat = page(conn("10:00", "12:30", "RJ 710"), conn("11:00", "13:30", "RJ 711"))

    answer, _, trip_search = await ask(PAGE_1, [repeat, PAGE_3], until=vienna("20:00"))

    assert trip_search.await_count == 1
    assert (answer.pages, answer.stopped) == (2, oebb_journeys.STOP_NO_PROGRESS)
    assert answer.verdict == PAGE_1.verdict


@pytest.mark.asyncio
async def test_a_failed_later_page_keeps_the_earlier_pages_and_is_reported_as_partial(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.network_coverage.oebb_journeys"):
        answer, _, _ = await ask(
            PAGE_1, [PAGE_2, failed("http: ReadTimeout")], until=vienna("20:00")
        )

    assert (answer.pages, answer.stopped) == (3, oebb_journeys.STOP_ERROR)
    assert answer.page_error == "http: ReadTimeout"
    # The cell keeps ÖBB's answer: the verdict is no error, its connections are counted.
    assert answer.verdict.error is None
    assert answer.verdict.ok is True
    assert answer.verdict.num_connections == 10
    assert len(answer.journeys) == 10
    assert answer.pages_detail() == {"asked": 3, "stopped": "error", "error": "http: ReadTimeout"}
    assert "stopped at page 3" in caplog.text


def test_no_later_page_without_a_departure_to_start_from() -> None:
    undated = journey("v0").model_copy(update={"departure": None})
    pages = oebb_journeys._Pages([undated], vienna("20:00"))

    assert pages.next_anchor(1, 5) == (None, oebb_journeys.STOP_NO_PROGRESS)
    assert pages.take(PAGE_2) == (None, None)  # a dated page gives it one
    assert pages.next_anchor(2, 5)[0] == vienna("16:01")


@pytest.mark.asyncio
async def test_no_later_page_once_the_cell_s_budget_is_spent() -> None:
    tight = oebb_journeys.PagePolicy(max_pages=5, pause_s=0.0, budget_s=0.4)

    answer, _, trip_search = await ask(PAGE_1, [PAGE_2], until=vienna("20:00"), policy=tight)

    trip_search.assert_not_awaited()
    assert (answer.pages, answer.stopped) == (1, oebb_journeys.STOP_BUDGET)


@pytest.mark.asyncio
async def test_a_later_page_gets_what_is_left_of_the_budget_at_most_its_own_timeout() -> None:
    policy = oebb_journeys.PagePolicy(max_pages=2, pause_s=0.0, page_timeout_s=30.0, budget_s=5.0)

    _, _, trip_search = await ask(PAGE_1, [PAGE_2], until=vienna("20:00"), policy=policy)

    assert 0.5 <= trip_search.await_args.kwargs["timeout_s"] <= 5.0


@pytest.mark.asyncio
async def test_the_run_s_pause_is_kept_between_pages() -> None:
    polite = oebb_journeys.PagePolicy(max_pages=3, pause_s=0.5, budget_s=60.0)
    sleep = AsyncMock()

    with patch.object(oebb_journeys.asyncio, "sleep", sleep):
        await ask(PAGE_1, [PAGE_2, PAGE_3], until=vienna("20:00"), policy=polite)

    assert [call.args for call in sleep.await_args_list] == [(0.5,), (0.5,)]


# ── The merged answer ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_journeys_of_several_pages_get_ids_unique_in_their_union() -> None:
    answer, _, _ = await ask(PAGE_1, [PAGE_2, PAGE_3], until=vienna("17:30"))

    ids = [j.id for j in answer.journeys]
    assert len(set(ids)) == len(ids) == 12
    assert ids[0] == "hafas:oebb:0"
    assert ids[-1] == "hafas:oebb:11"


@pytest.mark.asyncio
async def test_the_verdict_counts_every_page_and_takes_the_shortest_connection() -> None:
    faster = page(
        conn("12:00", "13:50", "RJ 712"),
        conn("13:00", "15:30", "RJ 713"),
        durations=["005000", "023000"],
    )

    answer, _, _ = await ask(PAGE_1, [faster, PAGE_3], until=vienna("17:30"))

    assert answer.verdict.num_connections == 5 + 2 + 2
    assert answer.verdict.best_duration_seconds == 50 * 60
    assert answer.verdict.best_transfers == 0
    assert answer.verdict.source == PAGE_1.verdict.source


def test_the_verdict_keeps_page_1_s_best_when_no_later_connection_is_shorter() -> None:
    later = oebb_journeys.journeys_from_answer(PAGE_2)

    merged = oebb_journeys._merged_verdict(PAGE_1.verdict, later)

    assert merged.best_duration_seconds == PAGE_1.verdict.best_duration_seconds == 3600
    assert merged.num_connections == 10
    assert oebb_journeys._merged_verdict(PAGE_1.verdict, []) is PAGE_1.verdict


# ── The scorer's window spans every page (M9) ───────────────────────────────────────────────


def viator_rides(*hhmm: str) -> list:
    return [
        journey(f"v{i}", ride("Wien Hbf", "Salzburg Hbf", start, f"{int(start[:2]) + 2:02d}:30"))
        for i, start in enumerate(hhmm)
    ]


def test_last_boarding_to_cover_is_viator_s_last_first_boarding_less_the_tolerance() -> None:
    viator = viator_rides("08:00", "17:00", "12:00")

    target = last_boarding_to_cover(viator, 120)

    assert target == datetime(2026, 11, 9, 16, 58, tzinfo=UTC)
    assert last_boarding_to_cover([], 120) is None


def test_an_oebb_span_reaching_the_target_holds_every_viator_journey() -> None:
    viator = viator_rides("08:00", "13:00", "17:00")
    target = last_boarding_to_cover(viator, 120)
    assert target is not None
    oebb = viator_rides("08:00", target.strftime("%H:%M"))

    inside, outside = reference_window(viator, oebb, 120)

    assert (len(inside), outside) == (3, [])


@pytest.mark.asyncio
async def test_the_scorer_compares_viator_journeys_within_every_page() -> None:
    """VIATOR boards at 08:00 and 15:00 Vienna; ÖBB's page 1 ends at 11:00. With one page the
    15:00 journey is after ÖBB's last connection; with the later pages it is compared."""
    viator = [
        journey(f"v{i}", ride("Wien Hbf", "Salzburg Hbf", start, end))
        for i, (start, end) in enumerate([("07:00", "09:30"), ("14:00", "16:30")])  # UTC
    ]
    until = last_boarding_to_cover(viator, 120)

    one, _, _ = await ask(PAGE_1, [], until=until, policy=oebb_journeys.PagePolicy(max_pages=1))
    paged, _, _ = await ask(PAGE_1, [PAGE_2], until=until)

    assert compute_alignment(viator, one.journeys).detail["counts"]["outside_reference_window"] == 1
    assert (
        compute_alignment(viator, paged.journeys).detail["counts"]["outside_reference_window"] == 0
    )


# ── The lines under a cell's tier ───────────────────────────────────────────────────────────

SUMMARY = "VIATOR vs ÖBB: 0 same service, 0 probably the same, 0 VIATOR only, 0 ÖBB only"


@pytest.mark.parametrize(
    ("pages", "line"),
    [
        ({"asked": 3, "stopped": "covered"}, "ÖBB answered over 3 pages"),
        (
            {"asked": 3, "stopped": "max_pages"},
            "ÖBB answered over 3 pages, the most a cell asks for",
        ),
        (
            {"asked": 2, "stopped": "error", "error": "HTTP 500"},
            "ÖBB's page 2 could not be read: its first 1 page are compared",
        ),
        (
            {"asked": 3, "stopped": "error", "error": "HTTP 500"},
            "ÖBB's page 3 could not be read: its first 2 pages are compared",
        ),
    ],
)
def test_a_cell_asked_over_several_pages_says_so(pages: dict[str, Any], line: str) -> None:
    lines = explain_cell({"counts": {}, "reasons": {}, "oebb_pages": pages})

    assert lines == [SUMMARY, line]
    assert "HTTP 500" not in " ".join(lines)  # the error stays off the share page


@pytest.mark.parametrize("pages", [{"asked": 1, "stopped": "covered"}, None, "zz", {"asked": "3"}])
def test_one_page_or_an_unreadable_record_adds_no_line(pages: Any) -> None:
    assert explain_cell({"counts": {}, "reasons": {}, "oebb_pages": pages}) == [SUMMARY]


# ── The setting, the snapshot, the runner ───────────────────────────────────────────────────


def test_the_setting_defaults_to_three_pages_and_keeps_one_as_before() -> None:
    assert default_for("COVERAGE_VERIFY_MAX_PAGES") == 3
    assert coerce("COVERAGE_VERIFY_MAX_PAGES", "1") == 1
    assert coerce("COVERAGE_VERIFY_MAX_PAGES", 10) == 10
    for bad in (0, 11):
        with pytest.raises(ValueError):
            coerce("COVERAGE_VERIFY_MAX_PAGES", bad)
    assert CONFIG_SCHEMA["COVERAGE_VERIFY_MAX_PAGES"]["type"] == "int"


def test_the_runner_reads_the_setting_and_freezes_it() -> None:
    assert runner.CoverageConfig().verify_max_pages == default_for("COVERAGE_VERIFY_MAX_PAGES")
    with patch.object(
        runner.config_service, "get_all", return_value={"COVERAGE_VERIFY_MAX_PAGES": 4}
    ):
        cfg = runner._load_coverage_config(db=None)  # type: ignore[arg-type]
    assert cfg.verify_max_pages == 4
    assert runner._config_from_snapshot(asdict(cfg)) == cfg


def test_a_run_frozen_before_the_setting_keeps_one_page_on_a_resume() -> None:
    before = {k: v for k, v in asdict(runner.CoverageConfig()).items() if k != "verify_max_pages"}

    frozen = runner._config_from_snapshot(before)

    assert frozen is not None
    assert frozen.verify_max_pages == 1


def _hub(slug: str, lat: float, lon: float) -> MagicMock:
    hub = MagicMock()
    hub.id, hub.lat, hub.lon = slug, lat, lon
    return hub


def _row() -> MagicMock:
    row = MagicMock()
    row.origin_hub_id, row.dest_hub_id, row.status = "wien", "salzburg", "ok"
    row.journey_search_id = None
    for name in (
        "external_ok",
        "external_num_connections",
        "external_best_duration_seconds",
        "external_best_transfers",
        "external_source",
        "external_error",
        "external_verified_at",
        "external_journeys",
        "external_alignment_score",
        "external_alignment_tier",
        "external_alignment_detail",
    ):
        setattr(row, name, None)
    return row


@pytest.mark.asyncio
async def test_the_sweep_pages_toward_viator_s_last_journey_and_keeps_every_page() -> None:
    hubs = {"wien": _hub("wien", 48.18, 16.37), "salzburg": _hub("salzburg", 47.81, 13.04)}
    db = MagicMock()
    db.get.side_effect = lambda _model, key: hubs.get(key)
    run = MagicMock()
    run.depart_at = DEPART_AT
    run.session_id = None
    row = _row()
    viator = viator_rides("07:00", "14:00")  # UTC: 08:00 and 15:00 Vienna
    cfg = runner.CoverageConfig(verify_sleep_ms=0, verify_max_pages=3)

    with (
        patch.object(external_verify, "fetch_oebb_two_step", AsyncMock(return_value=PAGE_1)),
        patch.object(
            external_verify, "fetch_oebb_trip_search", AsyncMock(side_effect=[PAGE_2])
        ) as trip_search,
        patch.object(runner, "_fetch_viator_journeys_for_search", return_value=viator),
        patch("app.station_module.enabled", return_value=False),
    ):
        counters = await runner._run_external_verify_sweep(db=db, run=run, rows=[row], cfg=cfg)

    assert trip_search.await_count == 1
    assert counters == {"verified": 1, "ok": 1, "no_route": 0, "error": 0}
    assert row.external_num_connections == 10
    assert len(row.external_journeys) == 10
    assert row.external_alignment_detail["oebb_pages"] == {"asked": 2, "stopped": "covered"}
    assert row.external_alignment_detail["counts"]["outside_reference_window"] == 0
