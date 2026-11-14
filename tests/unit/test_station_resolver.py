"""Unit tests for app.journey.station_resolver — the stations of journeys' stops (#303 PR 5).

The station module is never reached: `station_module.stop_stations_outcome` is replaced by a
stand-in that answers from a table and records each call. Keys, feed keys and ids are
invented (`zz`, `99...`, `ZZ-99...`).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app import station_module
from app.journey import station_resolver
from app.journey.canonical.canonical_journey import CanonicalJourney
from app.journey.canonical.canonical_stop_place import CanonicalStopPlace
from app.journey.canonical.station_resolution import ResolvedStation, with_resolved_stations
from app.journey.station_resolver import (
    CODE,
    STOP,
    ModuleStationResolver,
    SessionFeeds,
    batches,
    keys_of,
    oebb_stop_series,
    session_feeds,
)

_real_stop_stations = station_module.stop_stations_outcome
DB_FEED = "url:" + "9" * 16
OTP_FEED = "url:" + "8" * 16
FEEDS = SessionFeeds(motis={"db": DB_FEED}, otp={"FR-SNCF": OTP_FEED})
SERIES = "DELFI_stop_key"
FALLBACK = "Trainline_EVA"
ONE = (SERIES,)
BOTH = (SERIES, FALLBACK)


def motis_stop(ref: str, parent: str | None = None) -> CanonicalStopPlace:
    return CanonicalStopPlace(ref=ref, source="motis:db", parent_ref=parent)


def oebb_stop(**fields: Any) -> CanonicalStopPlace:
    return CanonicalStopPlace(ref="A=1@L=8100002@", source="hafas:oebb", **fields)


class Module:
    """Answers `stop_stations_outcome` from `stations` ((kind, owner, key) -> ids), or fails
    with `reason`; records the keys of each call."""

    def __init__(self) -> None:
        self.stations: dict[tuple[str, str, str], list[tuple[str, str | None]]] = {}
        self.calls: list[tuple[str, str, list[str]]] = []
        self.reason: str | None = None

    async def __call__(self, **form: Any) -> station_module.Outcome:
        kind, owner, keys = (
            (STOP, form["feed"], form["stops"])
            if "feed" in form
            else (CODE, form["series"], form["codes"])
        )
        self.calls.append((kind, owner, list(keys)))
        if self.reason is not None:
            return station_module.Outcome(None, self.reason)
        rows = [
            {"key": key, "viator_id": vid, "parent_viator_id": parent}
            for key in keys
            for vid, parent in self.stations.get((kind, owner, key), [])
        ]
        return station_module.Outcome(rows, "ok")


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Module]:
    stand_in = Module()
    monkeypatch.setattr(station_module, "stop_stations_outcome", stand_in)
    station_resolver.forget()
    yield stand_in
    station_resolver.forget()


# ── The keys a stop is asked by ───────────────────────────────────────────────────────────


def test_a_motis_stop_is_asked_by_its_parent_first_then_itself() -> None:
    stop = motis_stop("db_de:09162:100:27:27", parent="db_de:09162:100")

    assert keys_of(stop, FEEDS, ONE) == [
        (STOP, DB_FEED, "de:09162:100"),
        (STOP, DB_FEED, "de:09162:100:27:27"),
    ]
    # A stop without a parent is its own station.
    assert keys_of(motis_stop("db_8000105"), FEEDS, ONE) == [(STOP, DB_FEED, "8000105")]


def test_a_stop_of_a_feed_without_a_key_or_of_another_shape_is_not_asked() -> None:
    assert keys_of(CanonicalStopPlace(ref="zz_1", source="motis:zz"), FEEDS, ONE) == []
    # A parent of another feed is no key of this one.
    assert keys_of(motis_stop("db_1", parent="nl_2"), FEEDS, ONE) == [(STOP, DB_FEED, "1")]
    assert keys_of(CanonicalStopPlace(ref="ch:1:sloid:7000", source="ojp:ch"), FEEDS, ONE) == []


def test_an_otp_stop_is_asked_by_its_local_id() -> None:
    stop = CanonicalStopPlace(ref="FR-SNCF:StopPoint:OCE87686006", source="otp:FR-SNCF")

    assert keys_of(stop, FEEDS, ONE) == [(STOP, OTP_FEED, "StopPoint:OCE87686006")]


def test_an_oebb_stop_is_asked_by_its_number_in_the_series() -> None:
    by_ext = oebb_stop(codes={"OEBB_EXT": "8100002"}, uic="8101003")
    by_uic = oebb_stop(uic="8101003")
    by_token = oebb_stop(unresolved_token="OEBB:904050")

    assert keys_of(by_ext, FEEDS, ONE) == [(CODE, SERIES, "8100002")]
    assert keys_of(by_uic, FEEDS, ONE) == [(CODE, SERIES, "8101003")]
    assert keys_of(by_token, FEEDS, ONE) == [(CODE, SERIES, "904050")]
    assert keys_of(oebb_stop(), FEEDS, ONE) == []
    # Without a series, ÖBB's stops are left out.
    assert keys_of(by_ext, FEEDS, ()) == []


def test_an_oebb_stop_is_asked_in_each_series_in_their_order() -> None:
    stop = oebb_stop(codes={"OEBB_EXT": "8700012"})

    assert keys_of(stop, FEEDS, BOTH) == [(CODE, SERIES, "8700012"), (CODE, FALLBACK, "8700012")]


@pytest.mark.parametrize(
    ("value", "series"),
    [
        ("DELFI_stop_key,Trainline_EVA", BOTH),
        ("DELFI_stop_key", ONE),
        (" DELFI_stop_key , Trainline_EVA ,, DELFI_stop_key ", BOTH),
        ("", ()),
        (" , ", ()),
        (None, ()),
        (7, ()),
    ],
)
def test_the_setting_names_its_series_in_order_each_once(value: Any, series: Any) -> None:
    assert oebb_stop_series(value) == series


def test_the_feeds_of_a_session_by_motis_dataset_and_otp_feed_id() -> None:
    providers = [{"id": "DB"}, {"id": "NL-NAP"}, {"id": "DERIVED"}]
    keys = {"DB": DB_FEED, "NL-NAP": OTP_FEED}

    feeds = session_feeds(providers, lambda provider: keys.get(provider["id"]))

    assert feeds == SessionFeeds(
        motis={"db": DB_FEED, "nl-nap": OTP_FEED}, otp={"DB": DB_FEED, "NL-NAP": OTP_FEED}
    )


def test_batches_keep_to_fifty_keys_and_the_module_s_body_cap() -> None:
    many = [(STOP, DB_FEED, f"zz:{n}") for n in range(120)]
    long = [(STOP, OTP_FEED, f"{n:0190d}") for n in range(30)]
    codes = [(CODE, SERIES, "9900001"), (CODE, SERIES, "9900001")]

    calls = batches([*many, *long, *codes])

    assert [(kind, owner, len(keys)) for kind, owner, keys in calls[:3]] == [
        (STOP, DB_FEED, 50),
        (STOP, DB_FEED, 50),
        (STOP, DB_FEED, 20),
    ]
    for kind, owner, keys in calls:
        form = {"feed": owner, "stops": keys} if kind == STOP else {"series": owner, "codes": keys}
        station_module.stop_stations_body(**form)  # never refused
    assert sum(len(keys) for kind, owner, keys in calls if owner == OTP_FEED) == 30
    assert calls[-1] == (CODE, SERIES, ["9900001"])


# ── The resolution ────────────────────────────────────────────────────────────────────────


async def test_the_stops_get_their_stations_parent_first(module: Module) -> None:
    module.stations[(STOP, DB_FEED, "de:09162:100")] = [("ZZ-99001", "ZZ-99009")]
    module.stations[(STOP, DB_FEED, "8000105")] = [("ZZ-99002", None)]
    module.stations[(CODE, SERIES, "8100002")] = [("ZZ-99003", None)]
    platform = motis_stop("db_de:09162:100:27:27", parent="db_de:09162:100")
    alone = motis_stop("db_8000105")
    oebb = oebb_stop(codes={"OEBB_EXT": "8100002"})
    unknown = motis_stop("db_zz")

    placed = await ModuleStationResolver(FEEDS, ONE).resolve([platform, alone, oebb, unknown])

    assert placed == {
        ("motis:db", "db_de:09162:100:27:27"): ResolvedStation(
            station_id="ZZ-99001", parent_id="ZZ-99009"
        ),
        ("motis:db", "db_8000105"): ResolvedStation(station_id="ZZ-99002"),
        ("hafas:oebb", "A=1@L=8100002@"): ResolvedStation(station_id="ZZ-99003"),
    }
    # One call a feed or series, each key once.
    assert sorted(module.calls) == [
        (CODE, SERIES, ["8100002"]),
        (STOP, DB_FEED, ["de:09162:100", "de:09162:100:27:27", "8000105", "zz"]),
    ]


async def test_a_stop_whose_parent_is_placed_nowhere_takes_its_own_station(
    module: Module,
) -> None:
    module.stations[(STOP, DB_FEED, "de:99:1:2")] = [("ZZ-99004", None)]
    stop = motis_stop("db_de:99:1:2", parent="db_de:99:1")

    placed = await ModuleStationResolver(FEEDS, ONE).resolve([stop])

    assert placed == {("motis:db", "db_de:99:1:2"): ResolvedStation(station_id="ZZ-99004")}


async def test_a_code_of_two_stations_places_nothing(module: Module) -> None:
    module.stations[(CODE, SERIES, "8100002")] = [("ZZ-99005", None), ("ZZ-99006", None)]

    placed = await ModuleStationResolver(FEEDS, ONE).resolve(
        [oebb_stop(codes={"OEBB_EXT": "8100002"})]
    )

    assert placed == {}


async def test_answers_are_kept_and_a_key_is_asked_once_a_while(
    module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(station_resolver, "clock", lambda: now[0])
    module.stations[(STOP, DB_FEED, "8000105")] = [("ZZ-99002", None)]
    stops = [motis_stop("db_8000105"), motis_stop("db_zz")]
    resolver = ModuleStationResolver(FEEDS, ONE)

    first = await resolver.resolve(stops)
    again = await resolver.resolve(stops)

    assert first == again
    # Both keys asked once: the one placed nowhere is kept too.
    assert len(module.calls) == 1
    now[0] += station_resolver.CACHE_SECONDS + 1
    await resolver.resolve(stops)
    assert len(module.calls) == 2


async def test_a_failure_places_what_was_kept_and_keeps_nothing_new(module: Module) -> None:
    module.stations[(STOP, DB_FEED, "8000105")] = [("ZZ-99002", None)]
    resolver = ModuleStationResolver(FEEDS, ONE)
    await resolver.resolve([motis_stop("db_8000105")])
    module.reason = "status_429"

    placed = await resolver.resolve(
        [motis_stop("db_8000105"), oebb_stop(codes={"OEBB_EXT": "8100002"})]
    )

    assert placed == {("motis:db", "db_8000105"): ResolvedStation(station_id="ZZ-99002")}
    module.reason = None
    module.stations[(CODE, SERIES, "8100002")] = [("ZZ-99003", None)]
    # The failed key was not kept as "placed nowhere": asked again.
    assert ("hafas:oebb", "A=1@L=8100002@") in await resolver.resolve(
        [oebb_stop(codes={"OEBB_EXT": "8100002"})]
    )


async def test_a_failing_call_ends_the_resolution(module: Module) -> None:
    module.reason = "timeout"
    stops = [motis_stop(f"db_zz:{n}") for n in range(120)]

    assert await ModuleStationResolver(FEEDS, ONE).resolve(stops) == {}
    # The first call failed: none of the two others was made.
    assert len(module.calls) == 1


async def test_journeys_get_their_station_ids_through_the_resolver(module: Module) -> None:
    from app.journey.canonical_adapters import motis_adapter

    module.stations[(STOP, DB_FEED, "de:09162:100")] = [("ZZ-99001", "ZZ-99009")]
    answer = {
        "itineraries": [
            {
                "duration": 600,
                "transfers": 0,
                "legs": [
                    {
                        "mode": "HIGHSPEED_RAIL",
                        "from": {
                            "name": "Zz Hbf",
                            "stopId": "db_de:09162:100:27:27",
                            "parentId": "db_de:09162:100",
                            "lat": 48.14,
                            "lon": 11.56,
                            "departure": "2026-11-09T09:48:00Z",
                        },
                        "to": {
                            "name": "Zz Ost",
                            "stopId": "db_zz",
                            "lat": 48.13,
                            "lon": 11.6,
                            "arrival": "2026-11-09T09:58:00Z",
                        },
                        "startTime": "2026-11-09T09:48:00Z",
                        "endTime": "2026-11-09T09:58:00Z",
                        "duration": 600,
                        "routeShortName": "88",
                    }
                ],
            }
        ]
    }
    journeys: list[CanonicalJourney] = motis_adapter.journeys_from_plan(answer)
    assert journeys, "the MOTIS adapter read the invented answer"

    (journey,) = await with_resolved_stations(journeys, ModuleStationResolver(FEEDS, ONE))

    origin = journey.legs[0].origin.stop_place
    destination = journey.legs[0].destination.stop_place
    assert origin is not None
    assert destination is not None
    assert (origin.station_id, origin.station_parent_id) == ("ZZ-99001", "ZZ-99009")
    assert destination.station_id is None


def test_a_resolver_for_a_session_reads_its_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fcntl")  # the extractor's module: Linux only
    from app import station_extract

    seen: list[tuple[str, str | None]] = []

    def key_of(provider: Any, *, upload_sha256: str | None = None) -> str | None:
        seen.append((provider["id"], upload_sha256))
        return DB_FEED if provider["id"] == "DB" else None

    monkeypatch.setattr(station_extract, "feed_key_for", key_of)
    config = {
        "sources": {
            "providers": [
                {"id": "DB", "timetable": {"format": "gtfs", "url": "https://zz.invalid/db.zip"}},
                {"id": "ZZUP", "timetable": {"format": "gtfs", "source": "upload"}},
            ]
        }
    }

    resolver = station_resolver.resolver_for_session(config, {"ZZUP": "9" * 64}, ONE)

    assert resolver._feeds.motis == {"db": DB_FEED}
    assert seen == [("DB", None), ("ZZUP", "9" * 64)]


async def test_a_series_the_module_refuses_places_nothing_and_never_raises(
    module: Module,
) -> None:
    resolver = ModuleStationResolver(FEEDS, ("zz\x00",))
    stop = oebb_stop(codes={"OEBB_EXT": "8100002"})

    with pytest.MonkeyPatch.context() as patch:
        # The real client: it refuses the body before any call.
        patch.setattr(station_module, "stop_stations_outcome", _real_stop_stations)
        assert await resolver.resolve([stop]) == {}


# ── ÖBB's numbers in several series (the module's 22.19, decision 72) ─────────────────────


def _numbers(*numbers: str) -> list[CanonicalStopPlace]:
    return [
        CanonicalStopPlace(ref=f"A=1@L={n}@", source="hafas:oebb", codes={"OEBB_EXT": n})
        for n in numbers
    ]


async def test_the_next_series_is_asked_only_for_the_numbers_placed_nowhere(
    module: Module,
) -> None:
    module.stations[(CODE, SERIES, "8100002")] = [("ZZ-99001", None)]
    module.stations[(CODE, FALLBACK, "8700012")] = [("ZZ-99002", None)]
    # Several stations in the first series: not silent, so never settled by the next.
    module.stations[(CODE, SERIES, "8300046")] = [("ZZ-99003", None), ("ZZ-99004", None)]
    module.stations[(CODE, FALLBACK, "8300046")] = [("ZZ-99005", None)]
    # The next series is not asked for a number the first one placed.
    module.stations[(CODE, FALLBACK, "8100002")] = [("ZZ-99006", None)]

    placed = await ModuleStationResolver(FEEDS, BOTH).resolve(
        _numbers("8100002", "8700012", "8300046", "999099")
    )

    assert placed == {
        ("hafas:oebb", "A=1@L=8100002@"): ResolvedStation(station_id="ZZ-99001"),
        ("hafas:oebb", "A=1@L=8700012@"): ResolvedStation(station_id="ZZ-99002"),
    }
    assert module.calls == [
        (CODE, SERIES, ["8100002", "8700012", "8300046", "999099"]),
        (CODE, FALLBACK, ["8700012", "999099"]),
    ]


async def test_a_feed_s_stops_are_asked_with_the_first_series_and_never_again(
    module: Module,
) -> None:
    module.stations[(STOP, DB_FEED, "8000105")] = [("ZZ-99002", None)]
    module.stations[(CODE, FALLBACK, "8700012")] = [("ZZ-99003", None)]

    placed = await ModuleStationResolver(FEEDS, BOTH).resolve(
        [motis_stop("db_8000105"), motis_stop("db_zz"), *_numbers("8700012")]
    )

    assert set(placed) == {("motis:db", "db_8000105"), ("hafas:oebb", "A=1@L=8700012@")}
    assert sorted(module.calls[:2]) == [
        (CODE, SERIES, ["8700012"]),
        (STOP, DB_FEED, ["8000105", "zz"]),
    ]
    assert module.calls[2:] == [(CODE, FALLBACK, ["8700012"])]


async def test_kept_answers_settle_every_series_without_a_call(module: Module) -> None:
    module.stations[(CODE, FALLBACK, "8700012")] = [("ZZ-99002", None)]
    resolver = ModuleStationResolver(FEEDS, BOTH)
    first = await resolver.resolve(_numbers("8700012", "999099"))
    module.calls.clear()

    assert await resolver.resolve(_numbers("8700012", "999099")) == first
    assert module.calls == []


async def test_a_failure_in_the_next_series_keeps_the_first_series_answers(
    module: Module,
) -> None:
    module.stations[(CODE, SERIES, "8100002")] = [("ZZ-99001", None)]
    resolver = ModuleStationResolver(FEEDS, BOTH)

    class FailsOnTheFallback(Module):
        async def __call__(self, **form: Any) -> station_module.Outcome:
            self.reason = "status_429" if form.get("series") == FALLBACK else None
            return await super().__call__(**form)

    failing = FailsOnTheFallback()
    failing.stations = module.stations
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(station_module, "stop_stations_outcome", failing)
        placed = await resolver.resolve(_numbers("8100002", "8700012"))

    assert placed == {("hafas:oebb", "A=1@L=8100002@"): ResolvedStation(station_id="ZZ-99001")}
    # Once the module answers again, only the series that failed is asked.
    module.stations[(CODE, FALLBACK, "8700012")] = [("ZZ-99002", None)]
    again = await resolver.resolve(_numbers("8100002", "8700012"))
    assert again[("hafas:oebb", "A=1@L=8700012@")] == ResolvedStation(station_id="ZZ-99002")
    assert module.calls == [(CODE, FALLBACK, ["8700012"])]


async def test_a_failure_in_the_first_series_asks_no_other(module: Module) -> None:
    module.reason = "timeout"

    assert await ModuleStationResolver(FEEDS, BOTH).resolve(_numbers("8700012")) == {}
    assert module.calls == [(CODE, SERIES, ["8700012"])]
