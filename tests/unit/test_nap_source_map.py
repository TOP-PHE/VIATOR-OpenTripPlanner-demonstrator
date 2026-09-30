"""Per-country switch to automated NAP sources (PR 3 of the NAP feed work).

`app/nap_source_map.py` is pure (plan / apply over plain dicts); the two
endpoints in app/api/admin/sessions.py are exercised with a stand-in DB,
the same no-Postgres approach as the rest of tests/unit. Template checks
are static trip-wires, like test_sessions_template_js.py.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app import ingestion, nap_source_map
from app.api.admin import sessions as sessions_api

TDG = {"type": "tdg", "dataset_id": "64635525318cc75a9a8a771f", "resource_id": 81653}
SOURCE_MAP: dict[str, dict[str, Any]] = {
    "SNCF": {"format": "gtfs", "source": "nap", "resolver": TDG},
    "DB": {"format": "netex_epip", "source": "url", "url": "https://x.example/db.zip"},
    "OURA": {"format": "gtfs", "source": "nap", "resolver": TDG},
    "ABSENT": {"format": "gtfs", "source": "url", "url": "https://x.example/a.zip"},
}


def _config() -> dict[str, Any]:
    return {
        "sources": {
            "providers": [
                {
                    "id": "SNCF",
                    "label": "SNCF Voyageurs",
                    "country_iso": "FR",
                    "timetable": {"format": "gtfs", "source": "upload"},
                },
                {
                    "id": "OURA",
                    "country_iso": "FR",
                    "timetable": {"format": "netex_epip", "source": "upload"},
                },
                {
                    "id": "DB",
                    "country_iso": "DE",
                    "timetable": {
                        "format": "netex_epip",
                        "source": "url",
                        "url": "https://x.example/db.zip",
                    },
                },
                {
                    "id": "UNMAPPED",
                    "country_iso": "IT",
                    "timetable": {"format": "gtfs", "source": "upload"},
                },
            ]
        }
    }


# ─────────────────────────── plan ───────────────────────────


def test_plan_groups_mapped_providers_by_country() -> None:
    providers = ingestion.normalize_providers(_config())
    plan = nap_source_map.plan(providers, SOURCE_MAP)
    assert [c.country_iso for c in plan] == ["DE", "FR"]  # IT has nothing mapped
    fr = {p.id: p for p in plan[1].providers}
    assert list(fr) == ["OURA", "SNCF"]
    assert fr["SNCF"].status == "available"
    assert fr["SNCF"].proposed_source == "nap"
    assert fr["SNCF"].proposed_detail.startswith("tdg · https://transport.data.gouv.fr/")
    assert fr["OURA"].status == "format_mismatch"
    assert plan[0].providers[0].status == "applied"
    assert plan[0].providers[0].proposed_detail == "https://x.example/db.zip"


def test_plan_puts_providers_without_country_under_a_dash() -> None:
    providers = [{"id": "SNCF", "country_iso": None, "timetable": {"format": "gtfs"}}]
    (group,) = nap_source_map.plan(providers, SOURCE_MAP)
    assert group.country_iso == "—"


# ─────────────────────────── apply ───────────────────────────


def test_apply_switches_only_available_providers() -> None:
    config = _config()
    before = copy.deepcopy(config)
    new, changed, skipped = nap_source_map.apply(
        config, {"SNCF", "OURA", "DB", "UNMAPPED", "NOPE"}, SOURCE_MAP
    )
    assert config == before, "apply must not mutate its input"
    assert changed == ["SNCF"]
    assert {s["id"]: s["reason"] for s in skipped} == {
        "DB": "already uses this source",
        "NOPE": "not a mapped provider of this session",
        "OURA": "format differs from the map — switch by hand",
        "UNMAPPED": "not a mapped provider of this session",
    }
    sncf = next(p for p in new["sources"]["providers"] if p["id"] == "SNCF")
    assert sncf["timetable"] == SOURCE_MAP["SNCF"]
    assert sncf["label"] == "SNCF Voyageurs"  # only the timetable changes
    sncf["timetable"]["resolver"]["resource_id"] = 1
    assert SOURCE_MAP["SNCF"]["resolver"]["resource_id"] == 81653, "map entries are copied"


def test_switched_config_still_validates() -> None:
    new, _, _ = nap_source_map.apply(_config(), {"SNCF"}, SOURCE_MAP)
    providers = ingestion.normalize_providers(new)
    assert next(p for p in providers if p["id"] == "SNCF")["timetable"]["source"] == "nap"


def test_shipped_map_loads_without_comment_keys() -> None:
    nap_source_map.load_map.cache_clear()
    shipped = nap_source_map.load_map()
    assert len(shipped) == 23
    assert not any(k.startswith("_") for k in shipped)


def test_shipped_map_is_inside_the_web_image() -> None:
    """The web Dockerfile copies only app/ — the map must live under it."""
    root = Path(__file__).resolve().parents[2]
    assert nap_source_map.MAP_PATH.is_relative_to(root / "app")
    assert "COPY app ./app" in (root / "docker" / "web" / "Dockerfile").read_text(encoding="utf-8")


# ─────────────────────────── endpoints ───────────────────────────


class _Session:
    def __init__(self, config: dict[str, Any] | None) -> None:
        self.id = "s1"
        self.config = config


class _Db:
    def __init__(self, session: _Session | None) -> None:
        self.session = session
        self.commits = 0

    def get(self, model: Any, sid: str) -> _Session | None:
        return self.session

    def commit(self) -> None:
        self.commits += 1


class _Actor:
    id = "u1"


class _Request:
    """Opaque: `client_ip` is stubbed in the `audits` fixture."""


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sessions_api.audit, "record", lambda db, **kw: calls.append(kw))
    monkeypatch.setattr(sessions_api, "client_ip", lambda request: "127.0.0.1")
    monkeypatch.setattr(nap_source_map, "load_map", lambda: SOURCE_MAP)
    return calls


def _apply(db: _Db, ids: list[str]) -> sessions_api.NapSourcesApplyResponse:
    body = sessions_api.NapSourcesApplyBody(provider_ids=ids)
    return sessions_api.apply_nap_sources("s1", body, _Request(), db, _Actor())  # type: ignore[arg-type]


def test_get_plan_lists_countries_and_unmatched_map_entries(
    audits: list[dict[str, Any]],
) -> None:
    db = _Db(_Session(_config()))
    plan = sessions_api.get_nap_sources("s1", db, None)  # type: ignore[arg-type]
    assert [c.country_iso for c in plan.countries] == ["DE", "FR"]
    assert plan.not_in_session == ["ABSENT"]


def test_get_plan_404s_for_an_unknown_session(audits: list[dict[str, Any]]) -> None:
    db = _Db(None)
    with pytest.raises(HTTPException) as exc:
        sessions_api.get_nap_sources("nope", db, None)  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_apply_saves_marks_sources_changed_and_audits(audits: list[dict[str, Any]]) -> None:
    session = _Session(_config())
    db = _Db(session)
    out = _apply(db, ["SNCF", "OURA"])
    assert out.changed == ["SNCF"]
    assert [s["id"] for s in out.skipped] == ["OURA"]
    assert db.commits == 1
    assert session.config is not None
    sncf = next(p for p in session.config["sources"]["providers"] if p["id"] == "SNCF")
    assert sncf["timetable"]["source"] == "nap"
    assert session.config["_meta"]["sources_changed_at"]  # staleness banner will show
    assert out.config == session.config
    (audit,) = audits
    assert audit["action"] == "session.nap_sources.applied"
    assert audit["metadata"]["changed"] == ["SNCF"]
    assert audit["metadata"]["previous"] == {"SNCF": {"format": "gtfs", "source": "upload"}}


def test_apply_with_nothing_to_change_writes_nothing(audits: list[dict[str, Any]]) -> None:
    session = _Session(_config())
    before = copy.deepcopy(session.config)
    db = _Db(session)
    out = _apply(db, ["DB"])
    assert out.changed == []
    assert db.commits == 0
    assert audits == []
    assert session.config == before


def test_apply_refuses_a_legacy_sources_shape(audits: list[dict[str, Any]]) -> None:
    db = _Db(_Session({"sources": {"gtfs": "https://x.example/g.zip"}}))
    with pytest.raises(HTTPException) as exc:
        _apply(db, ["SNCF"])
    assert exc.value.status_code == 400
    assert "legacy" in str(exc.value.detail)


def test_apply_rejects_an_empty_selection() -> None:
    with pytest.raises(ValueError):
        sessions_api.NapSourcesApplyBody(provider_ids=[])


# ─────────────────────────── template ───────────────────────────

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"


@pytest.fixture(scope="module")
def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _block(text: str) -> str:
    start = text.index("// ── Automated NAP sources")
    return text[start : text.index("// ── end automated NAP sources", start)]


@pytest.mark.parametrize(
    "name",
    [
        "napSection",
        "napCountryCounts",
        "napCountrySummary",
        "napProviderRowHTML",
        "napCountryHTML",
        "renderNapSourcesPlan",
        "loadNapSourcesPlan",
        "napSelectedProviderIds",
        "napSyncSessionRow",
        "napErrorText",
        "applyNapSources",
    ],
)
def test_picker_helpers_are_defined(template_text: str, name: str) -> None:
    assert re.search(rf"function\s+{name}\s*\(", _block(template_text))


def test_section_is_in_every_session_detail_and_loads_on_expand(template_text: str) -> None:
    assert 'data-role="nap-sources" data-sid="{{ s.id }}"' in template_text
    assert "loadNapSourcesPlan(sid);" in template_text


def test_apply_refreshes_the_pages_copy_of_the_config(template_text: str) -> None:
    """A stale Configure form would silently undo the switch on the next save."""
    sync = _block(template_text)
    sync = sync[sync.index("function napSyncSessionRow") :]
    sync = sync[: sync.index("\n}\n")]
    assert "row.dataset.config = JSON.stringify(config)" in sync
    assert "populateProvidersList(form" in sync
    assert "setStalenessBanner(sid)" in sync


def test_only_available_providers_are_sent(template_text: str) -> None:
    assert "p.status === 'available'" in _block(template_text)


def test_picker_never_calls_a_feed_validated(template_text: str) -> None:
    assert not re.search(r"validat", _block(template_text), re.IGNORECASE)
