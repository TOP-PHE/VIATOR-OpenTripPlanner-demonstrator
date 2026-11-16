"""ÖBB's stop list sent to the station module (its design 22.20; `app/hafas_stop_list.py`).

The module is never reached: a fake module answers through `httpx.MockTransport`, a fake clock
replaces every pause. The tests that write the extract use the extractor's module, which needs
`fcntl` (Linux: CI). Every value is invented: `ZZ` names, numbers with 99, a `.invalid` address,
a token drawn at run time.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import hashlib
import io
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import hafas_stop_list as hsl
from app import station_module
from app.hafas_stop_list import ListedPlace, Sent, StopListWorker
from app.journey.station_resolver import OEBB_STOP_LIST
from app.settings import settings

MODULE_URL = "http://msmm.invalid:8000"
AT = datetime(2099, 9, 9, 9, 9, tzinfo=UTC)
NORD = ListedPlace("9900001", "ZZ Nord", 47.5, 14.5, "9900001", 9)
SUD = ListedPlace("9900002", "ZZ Sued", 45.0, -30.0, None, 1)


# ── the list's version and stations (no extractor needed) ───────────────────


def test_the_version_is_the_same_for_the_same_places_in_any_order() -> None:
    assert hsl.list_version([NORD, SUD]) == hsl.list_version([SUD, NORD])
    assert len(hsl.list_version([NORD])) == 64


@pytest.mark.parametrize(
    "changed",
    [
        ListedPlace("9900001", "ZZ Nord Hbf", 47.5, 14.5, "9900001", 9),
        ListedPlace("9900001", "ZZ Nord", 47.5000001, 14.5, "9900001", 9),
        ListedPlace("9900001", "ZZ Nord", 47.5, 14.5, None, 9),
        ListedPlace("9900001", "ZZ Nord", 47.5, 14.5, "9900001", 10),
    ],
)
def test_any_change_of_a_place_is_another_version(changed: ListedPlace) -> None:
    assert hsl.list_version([changed, SUD]) != hsl.list_version([NORD, SUD])


def test_a_new_place_is_another_version() -> None:
    assert hsl.list_version([NORD, SUD]) != hsl.list_version([NORD])


def test_each_place_is_a_station_of_rail_calls_with_its_number_and_uic() -> None:
    stations = hsl.stations_of([NORD, SUD])

    nord, sud = stations["9900001"], stations["9900002"]
    assert (nord.key, nord.name, nord.lat, nord.lon) == ("9900001", "ZZ Nord", 47.5, 14.5)
    assert dict(nord.calls) == {"RAIL_MAIN": 9}
    assert nord.operators == {"ÖBB"}
    assert nord.code_pairs() == [("extId", "9900001"), ("uic", "9900001")]
    # No UIC seen: the number alone.
    assert sud.code_pairs() == [("extId", "9900002")]


def test_without_the_module_or_places_nothing_is_written_or_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(station_module, "enabled", lambda: False)
    assert hsl.send_list([NORD]) == Sent(hsl.OFF)
    monkeypatch.setattr(station_module, "enabled", lambda: True)
    assert hsl.send_list([]) == Sent(hsl.EMPTY)


# ── the worker's pass ────────────────────────────────────────────────────────


class Pass:
    """The worker's pass with stand-ins: the list it reads, what a send answers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.places: list[ListedPlace] = [NORD, SUD]
        self.answer = "sent"
        self.sent: list[list[ListedPlace]] = []
        self.collected = 0
        self.collect_fails = False
        monkeypatch.setattr(station_module, "enabled", lambda: True)
        monkeypatch.setattr(hsl, "load_places", lambda _db: list(self.places))
        self.worker = StopListWorker(
            lambda: contextlib.nullcontext(object()), send=self.send, collect=self.collect
        )

    def collect(self, _factory: Any) -> None:
        if self.collect_fails:
            raise RuntimeError("zz: the table is not there yet")
        self.collected += 1

    def send(self, places: Sequence[ListedPlace]) -> Sent:
        self.sent.append(list(places))
        return Sent(self.answer, None, hsl.list_version(places), len(places))


def test_a_pass_adds_the_runs_then_sends_a_list_the_module_does_not_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Pass(monkeypatch)

    first = run.worker.pass_once()

    assert run.collected == 1
    assert first is not None
    assert first.state == "sent"
    assert run.sent == [[NORD, SUD]]
    assert run.worker.held_version == hsl.list_version([NORD, SUD])


def test_a_list_the_module_holds_is_not_sent_again_until_it_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Pass(monkeypatch)
    run.worker.pass_once()

    assert run.worker.pass_once() is None
    run.places = [NORD]
    assert run.worker.pass_once() is not None
    assert len(run.sent) == 2
    assert run.collected == 3


def test_a_list_the_module_did_not_take_is_sent_again_at_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Pass(monkeypatch)
    run.answer = "unavailable"
    run.worker.pass_once()
    run.answer = "held"

    again = run.worker.pass_once()

    assert again is not None
    assert again.held
    assert len(run.sent) == 2


def test_without_the_module_the_runs_are_added_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Pass(monkeypatch)
    monkeypatch.setattr(station_module, "enabled", lambda: False)

    assert run.worker.pass_once() is None
    assert run.collected == 1
    assert run.sent == []


def test_a_pass_that_cannot_add_the_runs_sends_nothing_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Pass(monkeypatch)
    run.collect_fails = True

    assert run.worker.pass_once() is None
    assert run.sent == []


def test_an_empty_list_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Pass(monkeypatch)
    run.places = []

    assert run.worker.pass_once() is None
    assert run.sent == []


# ── the extract and its two calls (the extractor's module: Linux) ───────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 9000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Module:
    """A fake module: the `HEAD` answers `head`, a `PUT` the right 200 for its body."""

    def __init__(self, head: int = 404) -> None:
        self.head = head
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(self.head)
        body = request.read()
        self.bodies.append(body)
        rows = len(gzip.decompress(body).decode("utf-8").splitlines()) - 1
        return httpx.Response(
            200,
            json={
                "status": "recorded",
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "rows": rows,
            },
        )


@pytest.fixture
def extractor(monkeypatch: pytest.MonkeyPatch) -> Any:
    pytest.importorskip("fcntl")  # the extractor's module: Linux only
    from app import station_extract

    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))
    return station_extract


def _send(module: Module, places: list[ListedPlace]) -> Sent:
    return hsl.send_list(
        places, transport=httpx.MockTransport(module), clock=FakeClock(), now=lambda: AT
    )


def _rows(body: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(gzip.decompress(body).decode("utf-8"))))


def test_the_list_goes_to_the_module_as_an_extract_of_the_feed_hafas(extractor: Any) -> None:
    module = Module()

    sent = _send(module, [NORD, SUD])

    assert (sent.state, sent.places, sent.version) == ("sent", 2, hsl.list_version([NORD, SUD]))
    head, put = module.requests
    version = hsl.list_version([NORD, SUD])
    assert head.method == "HEAD"
    assert head.url.path == f"/internal/v1/feeds/{OEBB_STOP_LIST}/{version}"
    assert head.url.params["extractor_version"] == f"{extractor.EXTRACTOR_VERSION}+{hsl.LIST_FORM}"
    assert put.url.path == f"/internal/v1/feeds/{OEBB_STOP_LIST}/extract"
    # No session: the list is no session's feed.
    assert "X-Viator-Session-Id" not in head.headers
    assert "X-Viator-Session-Id" not in put.headers
    nord, sud = _rows(module.bodies[0])
    assert list(nord) == list(extractor.COLUMNS)
    assert nord["feed_key"] == OEBB_STOP_LIST
    assert nord["feed_version"] == version
    assert nord["extracted_at"] == "2099-09-09T09:09:00+00:00"
    assert (nord["stop_key"], nord["stop_name"]) == ("9900001", "ZZ Nord")
    assert (nord["lat"], nord["lon"]) == ("47.5000000", "14.5000000")
    assert nord["iso2_point"] == "AT"
    assert (nord["modes"], nord["n_calls"], nord["operators"]) == ("RAIL_MAIN", "9", "ÖBB")
    assert nord["codes"] == "extId=9900001|uic=9900001"
    assert sud["codes"] == "extId=9900002"
    assert sud["iso2_point"] == ""
    assert (nord["provider_id"], nord["provider_country_iso"]) == ("OEBB_HAFAS", "AT")
    assert nord["provider_provenance"] == ""


def test_a_list_the_module_holds_is_not_written_to_it_again(extractor: Any) -> None:
    module = Module(head=200)

    sent = _send(module, [NORD])

    assert sent.state == "held"
    assert [request.method for request in module.requests] == ["HEAD"]


def test_a_refused_list_is_told_with_its_word(extractor: Any) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(422, json={"detail": "zz", "code": "row"})

    sent = hsl.send_list(
        [NORD], transport=httpx.MockTransport(refuse), clock=FakeClock(), now=lambda: AT
    )

    assert (sent.state, sent.reason) == ("refused", "row")
    assert not sent.held


def test_a_place_the_extract_cannot_take_is_left_out_not_the_list(extractor: Any) -> None:
    nameless = ListedPlace("9900003", "\x00", 47.5, 14.5, None, 1)
    module = Module()

    sent = _send(module, [NORD, nameless])

    assert sent.state == "sent"
    assert [row["stop_key"] for row in _rows(module.bodies[0])] == ["9900001"]


def test_a_list_with_no_row_to_write_is_not_sent(extractor: Any) -> None:
    module = Module()

    sent = _send(module, [ListedPlace("9900003", "\x00", 47.5, 14.5, None, 1)])

    assert (sent.state, sent.reason) == (hsl.NOT_WRITTEN, "no_rail_stop")
    assert module.requests == []


def test_a_listed_extract_refuses_what_the_module_would(extractor: Any, tmp_path: Any) -> None:
    feed = extractor.ListedFeed(
        feed_key=OEBB_STOP_LIST,
        feed_version="9" * 64,
        extractor_version="zz-1",
        provider_id="OEBB_HAFAS",
        provider_label="ZZ",
        provider_country_iso="AT",
    )
    stations = hsl.stations_of([NORD])
    target = tmp_path / "zz.csv.gz"
    for bad, reason in (
        ({"feed_key": "zz:" + "9" * 16}, "no_feed_key"),
        ({"feed_version": "zz"}, "bad_sha256"),
        ({"provider_id": "zz"}, "bad_provider"),
    ):
        changed = extractor.ListedFeed(**{**feed.__dict__, **bad})
        with pytest.raises(extractor.ExtractError) as refused:
            extractor.write_listed_extract(stations, changed, target, extracted_at=AT)
        assert refused.value.reason == reason
    with pytest.raises(extractor.ExtractError) as naive:
        extractor.write_listed_extract(stations, feed, target, extracted_at=datetime(2099, 9, 9))
    assert naive.value.reason == "bad_time"
