"""The Europe-extension provider catalogue (app/provider_catalogue.py).

What matters: every entry is a valid, automatically refreshed session
provider, and a reviewer can always tell a NAP feed from an official or a
community one, from the label alone.
"""

from __future__ import annotations

from app import ingestion, nap_source_map, provider_catalogue


def test_every_entry_is_a_valid_session_provider() -> None:
    catalogue = provider_catalogue.load()
    providers = [provider_catalogue.session_provider(e) for e in catalogue.values()]
    canon = ingestion.normalize_providers({"sources": {"providers": providers}})
    assert [p["id"] for p in canon] == list(catalogue)
    for p in providers:
        assert not set(provider_catalogue.CATALOGUE_ONLY_FIELDS) & set(p)


def test_every_entry_refreshes_automatically() -> None:
    for e in provider_catalogue.load().values():
        assert e["timetable"]["source"] in ("url", "nap"), e["id"]
        assert e["country_iso"], e["id"]


def test_the_label_says_when_a_feed_is_not_from_a_nap() -> None:
    for e in provider_catalogue.load().values():
        assert e["provenance"] in provider_catalogue.PROVENANCES, e["id"]
        marker = provider_catalogue.LABEL_MARKERS.get(e["provenance"])
        if marker is None:
            assert "[" not in e["label"], e["id"]
        else:
            assert marker in e["label"], e["id"]
        assert e["nap_reference"], e["id"]


def test_ids_do_not_clash_with_the_switch_map() -> None:
    assert not set(provider_catalogue.load()) & set(nap_source_map.load_map())


def test_plan_add_all_skips_what_the_session_has() -> None:
    catalogue = {"A1": {"id": "A1"}, "B1": {"id": "B1"}, "C1": {"id": "C1"}}
    to_add, present, unknown = provider_catalogue.plan_add(catalogue, {"B1", "X"}, None)
    assert [e["id"] for e in to_add] == ["A1", "C1"]
    assert present == ["B1"]
    assert unknown == []


def test_plan_add_reports_unknown_ids() -> None:
    catalogue = {"A1": {"id": "A1"}}
    to_add, present, unknown = provider_catalogue.plan_add(catalogue, set(), {"A1", "ZZ"})
    assert [e["id"] for e in to_add] == ["A1"]
    assert present == []
    assert unknown == ["ZZ"]
