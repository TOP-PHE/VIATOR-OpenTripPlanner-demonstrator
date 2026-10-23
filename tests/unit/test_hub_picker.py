"""The hub picker of the Add / Edit hub dialog (#398).

A country and a typed city or station name ask the station module (faked
here with `httpx.MockTransport`) for at most 10 stations, through the admin
search (`station_module.admin_search_outcome`, with its new `country`
field, MSMM issue #69); picking one fills in the hub. Invented values only:
ZZ names and countries, codes beginning with 99, VIATOR ids `ZZ-99xxx`.
The slugs taken in the table are given by a stand-in here; the statement
itself is proved on PostgreSQL in tests/integration/test_hub_uic_selection.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import station_module
from app.api.admin import network_coverage
from app.auth import tokens
from app.db import get_db
from app.main import app
from app.osm_geo import COUNTRY_NAMES
from app.settings import settings

BASE = "/api/admin/network-coverage/hubs"
MODULE_URL = "http://msmm.invalid:8000"
TEMPLATE = (
    Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "network_coverage.html"
)


def _row(name: str, uic: str | None, *, country: str = "ZZ", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "latitude": 45.5,
        "longitude": 6.25,
        "country_iso": country,
        "uic": uic,
        **extra,
    }


class Module:
    """The module: `search` answers by the body; `countries` the list."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.search: Callable[[dict[str, Any]], httpx.Response] = lambda _b: httpx.Response(
            200, json={"stations": []}
        )
        self.countries: httpx.Response = httpx.Response(200, json={"countries": ["ZZ", "ZY"]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/countries"):
            return self.countries
        return self.search(json.loads(request.content))

    def bodies(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content) for r in self.requests if r.url.path.endswith("/stations/search")
        ]


def _answer(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"stations": list(rows)})


def _older(*rows: dict[str, Any]) -> Callable[[dict[str, Any]], httpx.Response]:
    """A module without the `country` field: its body check refuses it."""

    def answer(body: dict[str, Any]) -> httpx.Response:
        if "country" in body:
            return httpx.Response(422, json={"detail": "ZZ", "code": "invalid"})
        return _answer(*rows)

    return answer


class FakeDb:
    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no statement expected: the slugs come from the stand-in")


class Taken:
    """The hub ids already in the table, for `_taken_slugs`."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.asked: list[set[str]] = []

    def __call__(self, _db: Any, bases: set[str]) -> set[str]:
        self.asked.append(set(bases))
        return {i for i in self.ids if any(i == b or i.startswith(f"{b}-") for b in bases)}


@pytest.fixture
def taken(monkeypatch: pytest.MonkeyPatch) -> Taken:
    stand_in = Taken()
    monkeypatch.setattr(network_coverage, "_taken_slugs", stand_in)
    return stand_in


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Module]:
    stand_in = Module()
    transport = httpx.MockTransport(stand_in)
    real_async = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async(*args, **kwargs)

    monkeypatch.setattr(station_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "station_module_url", MODULE_URL)
    monkeypatch.setattr(settings, "station_module_token", SecretStr(secrets.token_hex(32)))
    station_module.reset()
    yield stand_in
    station_module.reset()


def _jwt(user_id: uuid.UUID, role: str) -> str:
    return tokens.issue_jwt(user_id, f"zz-{user_id.hex[:8]}@example.invalid", role)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = FakeDb
    test_client = TestClient(app)
    test_client.cookies.set(settings.jwt_cookie_name, _jwt(uuid.uuid4(), "platform_admin"))
    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _pick(client: TestClient, q: str = "ZZ Zzville", country: str = "ZZ") -> dict[str, Any]:
    answer = client.post(f"{BASE}/pick", json={"q": q, "country": country})
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


# ───────────────────────────── the client ─────────────────────────────


def test_the_admin_search_sends_the_country_only_when_given(module: Module) -> None:
    user = uuid.uuid4()
    module.search = lambda _b: _answer(_row("ZZ Zzville", "9900001"))

    with_country = asyncio.run(station_module.admin_search_outcome("ZZ Zzville", user, "XK"))
    without = asyncio.run(station_module.admin_search_outcome("ZZ Zzville", user))

    assert with_country.reason == without.reason == "ok"
    assert module.bodies() == [{"q": "ZZ Zzville", "country": "XK"}, {"q": "ZZ Zzville"}]


@pytest.mark.parametrize("country", ["zz", "Z", "ZZZ", "Z1", "ÄZ", ""])
def test_the_admin_search_refuses_a_malformed_country_before_any_call(
    module: Module, country: str
) -> None:
    with pytest.raises(ValueError, match="two capital letters"):
        asyncio.run(station_module.admin_search_outcome("ZZ Zzville", uuid.uuid4(), country))
    assert module.requests == []


def test_an_older_modules_refusal_of_the_country_starts_no_pause(module: Module) -> None:
    module.search = _older()

    outcome = asyncio.run(station_module.admin_search_outcome("ZZ Zzville", uuid.uuid4(), "ZZ"))

    assert (outcome.rows, outcome.reason) == (None, station_module.TEXT_REFUSED)
    assert not station_module.paused()


# ───────────────────────────── the route ─────────────────────────────


def test_a_search_offers_the_stations_with_the_fields_they_fill_in(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = lambda _b: _answer(
        _row("ZZ Zzville Zzcentral Zzstation", "9900001", viator_id="ZZ-99001"),
        _row("ZZ Zzhof", None, viator_id="ZZ-99002"),
    )

    answer = _pick(client, "  ZZ   Zzville ")

    assert module.bodies() == [{"q": "ZZ Zzville", "country": "ZZ"}]
    assert answer["status"] == "ok"
    assert answer["incomplete"] is False
    first, second = answer["stations"]
    assert first == {
        "name": "ZZ Zzville Zzcentral Zzstation",
        "short": first["short"],
        "slug": "zz-zzville-zzcentral-zzstation",
        "country": "ZZ",
        "lat": 45.5,
        "lon": 6.25,
        "uic": "9900001",
        "station_id": "ZZ-99001",
    }
    assert 1 <= len(first["short"]) <= network_coverage.PICK_SHORT_MAX
    assert (second["uic"], second["station_id"], second["slug"]) == (None, "ZZ-99002", "zz-zzhof")
    assert taken.asked == [{"zz-zzville-zzcentral-zzstation", "zz-zzhof"}]


def test_a_station_without_a_country_takes_the_chosen_one(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = lambda _b: _answer(
        _row("ZZ Zzone", "9900001", country=None), _row("ZZ Zztwo", "9900002", country="zz")
    )

    stations = _pick(client, country="ZY")["stations"]

    assert [s["country"] for s in stations] == ["ZY", "ZY"]


def test_the_proposed_slug_is_unique_among_the_hubs_and_the_answer(
    client: TestClient, module: Module, taken: Taken
) -> None:
    taken.ids = {"zz-zzville", "zz-zzville-2", "zz-zzville-4"}
    module.search = lambda _b: _answer(
        _row("ZZ Zzville", "9900001"),
        _row("ZZ-Zzville", "9900002"),
        _row("Zz Zzvillé", "9900003"),
    )

    slugs = [s["slug"] for s in _pick(client)["stations"]]

    assert slugs == ["zz-zzville-3", "zz-zzville-5", "zz-zzville-6"]


def test_the_slug_helpers() -> None:
    assert network_coverage.slug_base("Zz Zzbahnhof (Zzplatz)") == "zz-zzbahnhof-zzplatz"
    assert network_coverage.slug_base("Ζζ Ζζ") == "hub"  # no Latin letter or digit
    assert network_coverage.slug_base("-Zz-") == "zz"
    assert len(network_coverage.slug_base("Zz" * 80)) <= 60
    assert network_coverage.unique_slug("zz", set()) == "zz"
    assert network_coverage.unique_slug("zz", {"zz", "zz-2"}) == "zz-3"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", network_coverage.slug_base("99 Zz"))


def test_only_rows_a_hub_can_take_are_offered_each_once_at_most_ten(
    client: TestClient, module: Module, taken: Taken
) -> None:
    rows = [
        _row("ZZ Refused", "9 9"),  # a code the lookup would refuse
        _row("ZZ Far", "9900090", latitude=91.0),  # a position the hub form refuses
        _row("ZZ Once", "9900001"),
        _row("ZZ Once again", "9900001"),
    ]
    rows += [_row(f"ZZ Zz{index:02d}", f"99000{index:02d}") for index in range(10, 20)]
    module.search = lambda _b: httpx.Response(200, json={"stations": rows})

    stations = _pick(client)["stations"]

    # The client keeps ten rows of the module's answer; four of them go.
    assert [s["uic"] for s in stations] == ["9900001"] + [f"99000{i:02d}" for i in range(10, 16)]


def test_an_older_module_is_asked_again_without_the_country(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = _older(
        _row("ZZ Zzville", "9900001"),
        _row("ZY Zzville", "9900002", country="ZY"),
        _row("ZZ Zzville Zzhof", "9900003"),
        _row("Zz nowhere", "9900004", country=None),
    )

    answer = _pick(client)

    assert module.bodies() == [{"q": "ZZ Zzville", "country": "ZZ"}, {"q": "ZZ Zzville"}]
    assert answer["status"] == "ok"
    assert answer["incomplete"] is True
    assert "may be incomplete" in answer["message"]
    assert [s["uic"] for s in answer["stations"]] == ["9900001", "9900003"]
    assert not station_module.paused()


def test_a_text_the_module_refuses_twice_is_a_plain_refusal(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = lambda _b: httpx.Response(422, json={"detail": "ZZ"})

    answer = _pick(client)

    assert answer["status"] == "refused"
    assert answer["message"] == network_coverage._PICK_TEXT_REFUSED
    assert answer["stations"] == []
    assert len(module.bodies()) == 2


def test_the_modules_limit_is_a_plain_message_with_its_retry_after(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = lambda _b: httpx.Response(
        429, json={"detail": "ZZ", "code": "user_minute"}, headers={"Retry-After": "17"}
    )

    answer = _pick(client)

    assert (answer["status"], answer["retry_after"]) == ("limited", 17)
    assert answer["message"] == "The station module's limit is reached; try again in 17 seconds."
    assert len(module.bodies()) == 1


def test_a_module_failure_is_unavailable_and_starts_no_pause(
    client: TestClient, module: Module, taken: Taken
) -> None:
    module.search = lambda _b: httpx.Response(500)

    answer = _pick(client)

    assert answer["status"] == "unavailable"
    assert answer["message"] == network_coverage._MODULE_DID_NOT_ANSWER
    assert not station_module.paused()


def test_a_module_that_is_not_configured_is_not_called(
    client: TestClient, module: Module, taken: Taken, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_url", "")

    answer = _pick(client)

    assert (answer["status"], answer["message"]) == (
        "unavailable",
        network_coverage._PICK_MODULE_OFF,
    )
    assert module.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"q": "ZZ Zzville"},
        {"q": "ZZ Zzville", "country": "zz"},
        {"q": "ZZ Zzville", "country": "ZZZ"},
        {"q": "ZZ Zzville", "country": "ZZ", "limit": 10},
        {"q": 99, "country": "ZZ"},
        {"q": "Zz", "country": "ZZ"},
        {"q": "Z.-", "country": "ZZ"},  # fewer than 3 characters once folded
        {"q": "ZZ\u0000ZZ", "country": "ZZ"},
    ],
)
def test_a_body_or_text_the_search_would_refuse_is_a_422_without_a_call(
    client: TestClient, module: Module, taken: Taken, body: dict[str, Any]
) -> None:
    answer = client.post(f"{BASE}/pick", json=body)

    assert answer.status_code == 422
    assert isinstance(answer.json()["detail"], str)
    assert module.requests == []


@pytest.mark.parametrize("escaped", ["\\ud800", "\\u0000"], ids=["surrogate", "nul"])
def test_a_lone_surrogate_or_a_nul_is_a_422_never_a_500(
    client: TestClient, module: Module, taken: Taken, escaped: str
) -> None:
    raw = '{"q": "ZZ Zz' + escaped + 'ville", "country": "ZZ"}'

    answer = client.post(
        f"{BASE}/pick", content=raw.encode("ascii"), headers={"Content-Type": "application/json"}
    )

    assert answer.status_code == 422
    assert answer.json()["detail"] == "q must not contain a control character or a lone surrogate"
    assert module.requests == []


def test_the_typed_text_is_never_logged(
    client: TestClient, module: Module, taken: Taken, caplog: pytest.LogCaptureFixture
) -> None:
    module.search = _older()
    caplog.set_level(logging.DEBUG)

    _pick(client, "ZZ Zzsecretville")

    assert caplog.records  # the refusal and the fallback were logged
    assert not any("Zzsecretville" in record.getMessage() for record in caplog.records)


# ───────────────────────────── the countries ─────────────────────────────


def test_the_countries_are_the_modules(client: TestClient, module: Module) -> None:
    answer = client.get(f"{BASE}/countries").json()

    assert answer == {
        "source": "module",
        "countries": [{"code": "ZY", "name": "ZY"}, {"code": "ZZ", "name": "ZZ"}],
    }


def test_the_countries_fall_back_to_viators_list(client: TestClient, module: Module) -> None:
    module.countries = httpx.Response(503, json={"code": "down"})

    answer = client.get(f"{BASE}/countries").json()

    assert answer["source"] == "viator"
    assert [c["code"] for c in answer["countries"]] == sorted(COUNTRY_NAMES)
    assert {"code": "AT", "name": "Austria"} in answer["countries"]
    assert not station_module.paused()


def test_the_countries_without_the_module_are_viators(
    client: TestClient, module: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "station_module_token", SecretStr(""))

    answer = client.get(f"{BASE}/countries").json()

    assert answer["source"] == "viator"
    assert module.requests == []


# ───────────────────────────── who may ─────────────────────────────


@pytest.mark.parametrize("role", ["content_manager", "end_user"])
def test_non_administrators_are_refused(client: TestClient, module: Module, role: str) -> None:
    client.cookies.set(settings.jwt_cookie_name, _jwt(uuid.uuid4(), role))

    assert client.post(f"{BASE}/pick", json={"q": "ZZ Zzville", "country": "ZZ"}).status_code == 403
    assert client.get(f"{BASE}/countries").status_code == 403
    assert module.requests == []


def test_an_anonymous_caller_is_refused(client: TestClient, module: Module) -> None:
    client.cookies.clear()

    assert client.post(f"{BASE}/pick", json={"q": "ZZ Zzville", "country": "ZZ"}).status_code == 401
    assert client.get(f"{BASE}/countries").status_code == 401
    assert module.requests == []


# ───────────────────────────── the page ─────────────────────────────


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _picker_script(text: str) -> str:
    start = text.index("// #398 — the hub picker")
    end = text.index("// MSMM step 3 — station codes of the hubs", start)
    return text[start:end]


def test_the_picker_is_shown_only_with_the_module_and_the_manual_form_stays(
    template_text: str,
) -> None:
    blocks = re.findall(r"{% if station_module_enabled %}(.*?){% endif %}", template_text, re.S)
    picker = next(block for block in blocks if 'id="hub-pick"' in block)
    assert template_text.count('id="hub-pick"') == 1
    # Every control has its label; the answers are announced.
    for control in ("hub-pick-country", "hub-pick-q"):
        assert f'<label for="{control}">' in picker
        assert f'id="{control}"' in picker
    assert 'aria-live="polite"' in picker
    # The manual fields stay, folded only while the picker is offered.
    assert (
        '<details id="hub-manual" class="hub-manual"'
        "{% if not station_module_enabled %} open{% endif %}>"
    ) in template_text
    for field in ("country", "name", "lat", "lon", "uic"):
        assert f'id="hub-form-{field}"' in template_text.split('<details id="hub-manual"')[1]
    # A module position keeps all its decimals.
    assert 'name="lat" step="any"' in template_text


def test_the_picked_station_is_stored_through_confirm_never_as_a_typed_code(
    template_text: str,
) -> None:
    script = _picker_script(template_text)
    assert "fetch(`${HUB_CODES_API}/confirm`" in script
    assert "fetch(`${HUB_CODES_API}/pick`" in script
    code_lines = [line for line in script.splitlines() if not line.strip().startswith("//")]
    assert not any("innerHTML" in line or "insertAdjacentHTML" in line for line in code_lines)
    assert "if (!picked && typedUic !== HUB_EDIT_UIC) {" in template_text
    assert "const warning = picked ? await hubPickConfirm(savedId, picked) : null;" in template_text


_JS_PRELUDE = """
globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
"""

_JS_SCENARIO = """
const out = {};
function fakeForm() {
  const elements = {};
  for (const name of ['name', 'short', 'id', 'lat', 'lon', 'country', 'uic']) {
    elements[name] = { value: 'before' };
  }
  return { elements };
}
const station = { name: 'ZZ Zzville', short: 'ZZ Zzville', slug: 'zz-zzville-2',
                  country: 'ZZ', lat: 45.5, lon: 6.25, uic: null, station_id: 'ZZ-99001' };
const created = fakeForm();
hubPickFill(created, station, true);
out.created = Object.fromEntries(Object.entries(created.elements).map(([k, v]) => [k, v.value]));
const edited = fakeForm();
hubPickFill(edited, station, false);
out.editedSlug = edited.elements.id.value;

HUB_PICK.station = station;
out.pickedSame = hubPickedFor('') === station;
out.pickedTypedOver = hubPickedFor('9900077');
HUB_PICK.station = null;
out.pickedNone = hubPickedFor('');

out.stored = hubPickConfirmText({ status: 'ok', results: [{ state: 'stored' }] });
out.notServed = hubPickConfirmText({ status: 'ok', results: [{ state: 'not_served' }] });
out.limited = hubPickConfirmText({ status: 'limited', message: 'ZZ limit.' });
out.failed = hubPickConfirmText(null);

out.none = hubPickAnswerText({ status: 'ok', stations: [] });
out.one = hubPickAnswerText({ status: 'ok', stations: [station] });
out.two = hubPickAnswerText({ status: 'ok', stations: [station, station] });
out.incomplete = hubPickAnswerText({ status: 'ok', stations: [station], incomplete: true,
                                     message: 'ZZ may be incomplete.' });
out.refused = hubPickAnswerText({ status: 'refused', message: 'ZZ refused.' });

out.noCountry = hubPickBlocked('ZZ Zzville', '', 0);
out.short = hubPickBlocked('ZZ', 'ZZ', 0);
out.empty = hubPickBlocked('', 'ZZ', 0);
out.ok = hubPickBlocked('ZZ Zzville', 'ZZ', 0);
HUB_PICK.heldUntil = 17000;
out.held = hubPickBlocked('ZZ Zzville', 'ZZ', 500);

out.options = hubPickCountryOptions({ source: 'module',
  countries: [{ code: 'AT', name: 'Austria' }, { code: 'ZZ', name: 'ZZ' }] });
out.noteViator = hubPickCountriesNote({ source: 'viator', countries: [] });
out.noteModule = hubPickCountriesNote({ source: 'module', countries: [] });
out.noteNone = hubPickCountriesNote(null);
out.meta = hubPickMeta(station);
console.log(JSON.stringify(out));
"""


def test_the_picker_script_fills_the_form_and_words_its_answers(template_text: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = _JS_PRELUDE + _picker_script(template_text) + _JS_SCENARIO
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    assert out["created"] == {
        "name": "ZZ Zzville",
        "short": "ZZ Zzville",
        "id": "zz-zzville-2",
        "lat": 45.5,
        "lon": 6.25,
        "country": "ZZ",
        "uic": "",
    }
    assert out["editedSlug"] == "before"  # a saved hub's slug never changes
    assert out["pickedSame"] is True
    assert out["pickedTypedOver"] is None  # a code typed over it is a typed code
    assert out["pickedNone"] is None
    assert out["stored"] is None
    assert out["notServed"] == (
        "Hub saved, but its station was not stored: the station module does not serve it any more."
    )
    assert out["limited"] == (
        'Hub saved, but its station was not stored: ZZ limit. Use "Propose station codes" later.'
    )
    assert out["failed"].startswith("Hub saved, but its station was not stored: the station")
    assert out["none"] == "No station found."
    assert out["one"] == "1 station found; pick it to fill in the hub."
    assert out["two"] == "2 stations found; pick one to fill in the hub."
    assert out["incomplete"] == (
        "1 station found; pick it to fill in the hub. ZZ may be incomplete."
    )
    assert out["refused"] == "ZZ refused."
    assert out["noCountry"] == "Choose a country first."
    assert out["short"] == "Type 3 characters or more."
    assert out["empty"] == ""
    assert out["ok"] is None
    assert out["held"] == "The station module's limit is reached; try again in 17 seconds."
    assert out["options"] == [
        {"value": "", "label": "Choose a country"},
        {"value": "AT", "label": "AT · Austria"},
        {"value": "ZZ", "label": "ZZ"},
    ]
    assert "VIATOR's own list" in out["noteViator"]
    assert out["noteModule"] == ""
    assert out["noteNone"].startswith("The countries could not be loaded")
    assert out["meta"] == "ZZ · no code · VIATOR id ZZ-99001 · 45.5000, 6.2500"
