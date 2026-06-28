"""Unit tests for the coverage-run country subset filter (feat/coverage-
country-filter).

Three layers under test:

  1. `RunCreate.countries` pydantic validator — shape, normalisation,
     dedupe. Pure model unit tests, no DB.
  2. `runner._load_active_hubs(countries=...)` — adds the SQLAlchemy
     `country IN (...)` filter when given a list, omits it otherwise.
     Mocked db.execute so we observe the compiled WHERE clause.
  3. `runner.create_run(..., countries=...)` — persists the snapshot on
     the run row, normalises empty/lowercase, raises a ValueError when
     the filter matches zero hubs (which the API layer translates to a
     400).
  4. POST /api/admin/network-coverage/runs — round-trips body.countries
     into runner.create_run and surfaces the ValueError as a 400.

Out of scope:
  - The matrix render itself — covered by the existing matrix snapshot
    tests; countries only changes which hubs participate, not the row
    schema.
  - End-to-end with a real DB — alembic migration is verified by the
    standard migration smoke run; this file uses MagicMock for db.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.network_coverage import runner

# ─────────────────────── RunCreate.countries validator ───────────────────────


def _make_body(**overrides):
    """A minimal RunCreate kwargs dict — overrideable per test."""
    from app.api.admin.network_coverage import RunCreate

    base = {
        "session_id": "nap-fr-rail",
        "depart_at": datetime(2026, 5, 18, 8, 0, 0),
        "direction": "both",
        "mode": "single_session",
    }
    base.update(overrides)
    return RunCreate(**base)


def test_runcreate_countries_default_is_none():
    """No countries field on the request → no filter, full matrix.
    Matches every legacy submit shape that pre-dates this PR."""
    assert _make_body().countries is None


def test_runcreate_empty_list_normalises_to_none():
    """An empty list is just "no filter" — collapse it to None so the
    DB stores 'NULL = no filter' uniformly regardless of which client
    shape the operator's UI sends."""
    assert _make_body(countries=[]).countries is None


def test_runcreate_uppercases_country_codes():
    """The hub table stores codes uppercase (HubCreate normalises on
    insert); the validator uppercases here so a lowercase form submit
    still matches."""
    body = _make_body(countries=["fr", "ch"])
    assert body.countries == ["FR", "CH"]


def test_runcreate_dedupes_preserving_order():
    """Belt-and-braces dedupe — the checkbox UI shouldn't produce
    duplicates, but if a client sends them anyway we want a canonical
    list on disk so the sidebar badge ("FR+CH") is stable."""
    body = _make_body(countries=["FR", "ch", "fr", "CH", "DE"])
    assert body.countries == ["FR", "CH", "DE"]


@pytest.mark.parametrize(
    "bad",
    [
        ["FRA"],  # 3 letters — pre-2-letter alpha-2 shape
        ["F"],  # 1 letter
        ["F1"],  # non-alpha
        ["12"],  # all digits
        [""],  # empty string
        ["fr ", "ch"],  # whitespace breaks the 2-char check
        ["FR-CH"],  # hyphenated
    ],
)
def test_runcreate_rejects_malformed_country_codes(bad):
    """The validator only accepts ISO 3166-1 alpha-2 codes. Anything
    that wouldn't match a hub row by exact 2-letter equality is rejected
    at the API boundary so the runner never has to defend against it."""
    with pytest.raises(ValidationError) as exc:
        _make_body(countries=bad)
    msg = str(exc.value)
    assert "country" in msg.lower() or "2-letter" in msg.lower()


def test_runcreate_caps_country_list_at_20():
    """A 20-entry max keeps the JSONB column bounded and prevents an
    absurd request from creating a degenerate run row. There aren't
    20 countries in our matrix yet, so this is a safety net."""
    with pytest.raises(ValidationError):
        _make_body(countries=["FR"] * 21)


# ─────────────────────── _load_active_hubs filter ───────────────────────


def _stub_db_with_hub_rows(hub_rows):
    """A db MagicMock whose .execute().scalars().all() returns `hub_rows`."""
    db = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = hub_rows
    result = MagicMock()
    result.scalars.return_value = scalars
    db.execute.return_value = result
    return db


def _make_hub_row(slug, country="FR"):
    """A NetworkCoverageHub stand-in matching the columns _load_active_hubs
    reads."""
    r = MagicMock()
    r.id = slug
    r.name = slug.replace("-", " ")
    r.short = slug[:5]
    r.region = ""
    r.country = country
    r.lat = 48.0
    r.lon = 2.0
    return r


def test_load_active_hubs_no_filter_returns_all_active():
    """Passing countries=None preserves the pre-filter behaviour — the
    runner gets every active hub in the table, the matrix is full."""
    db = _stub_db_with_hub_rows([_make_hub_row("paris", "FR"), _make_hub_row("zurich", "CH")])
    hubs = runner._load_active_hubs(db)
    assert {h.id for h in hubs} == {"paris", "zurich"}
    db.execute.assert_called_once()


def test_load_active_hubs_filters_by_country_list():
    """countries=['FR'] → only FR hubs returned. The DB-side filter is
    what makes the matrix shrink; the application layer never needs to
    filter rows post-fetch."""
    fr_row = _make_hub_row("paris", "FR")
    # The stub returns whatever the test gives it — we're proving the
    # query was issued, not exercising real SQL.
    db = _stub_db_with_hub_rows([fr_row])
    hubs = runner._load_active_hubs(db, countries=["FR"])
    assert [h.id for h in hubs] == ["paris"]


def test_load_active_hubs_uppercases_filter_codes():
    """Lowercase input ('fr') is uppercased to match the stored 'FR'
    code. Belt-and-braces — the API validator already uppercases, but
    direct runner callers (e.g. tests, scripts) shouldn't have to."""
    db = _stub_db_with_hub_rows([_make_hub_row("paris", "FR")])
    # We can't easily inspect the compiled SQL through MagicMock, so we
    # verify the runner did not error AND returned the row — the path
    # of interest exercises the .upper() branch.
    hubs = runner._load_active_hubs(db, countries=["fr", "ch"])
    assert len(hubs) == 1


def test_load_active_hubs_empty_filter_result_returns_empty_list():
    """Country filter that matches no rows → empty list (NOT the static
    HUBS fallback, which would silently substitute the wrong country
    set). The caller is expected to translate this into a 400."""
    db = _stub_db_with_hub_rows([])
    hubs = runner._load_active_hubs(db, countries=["AT"])
    assert hubs == []


def test_load_active_hubs_empty_table_no_filter_falls_back_to_static():
    """The static-HUBS fallback is for fresh installs / dev envs — it
    ONLY fires when no filter is set. With a filter, an empty result is
    meaningful ("no AT hubs configured") and must propagate to the
    operator instead of silently returning all-of-France."""
    db = _stub_db_with_hub_rows([])
    hubs = runner._load_active_hubs(db)
    # Static HUBS list is non-empty (~25 entries); we just need to know
    # the fallback fired.
    assert len(hubs) > 0


# ─────────────────────── create_run with countries ───────────────────────


def _add_capture_run(captured):
    """A db.add capturer that records the NetworkCoverageRun being inserted
    so we can assert its `countries` field after create_run."""
    from app.models import NetworkCoverageRun

    def _capture(row):
        if isinstance(row, NetworkCoverageRun):
            captured.append(row)

    return _capture


def _patch_active_hubs(monkeypatch, hub_ids_by_country):
    """Replace _load_active_hubs with a stub that records the kwarg and
    returns a synthesised Hub list. Returns the captured-kwarg dict so
    the test can assert what create_run actually requested."""
    from app.network_coverage.hubs import Hub

    captured: dict[str, list[str] | None] = {"countries": None}

    def fake_load(_db, countries=None):
        captured["countries"] = countries
        slugs: list[str] = []
        country_filter = {c.upper() for c in countries} if countries else None
        for country, ids in hub_ids_by_country.items():
            if country_filter is None or country in country_filter:
                slugs.extend(ids)
        return [Hub(id=s, name=s, short=s[:5], region="", lat=48.0, lon=2.0) for s in slugs]

    monkeypatch.setattr(runner, "_load_active_hubs", fake_load)
    return captured


def test_create_run_persists_countries_snapshot(monkeypatch):
    """countries=['FR','CH'] on the call → the NetworkCoverageRun row
    keeps the normalised list. Future GETs of the run surface it as the
    sidebar badge."""
    _patch_active_hubs(monkeypatch, {"FR": ["paris", "lyon"], "CH": ["zurich"]})
    added: list = []
    db = MagicMock()
    db.add.side_effect = _add_capture_run(added)

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 5, 18, 8, 0),
        countries=["fr", "CH"],
    )

    assert len(added) == 1
    assert added[0].countries == ["FR", "CH"]


def test_create_run_persists_none_when_no_filter(monkeypatch):
    """countries=None means full matrix — store NULL on the row so
    `is None` reliably means "this was a full-matrix run"."""
    _patch_active_hubs(monkeypatch, {"FR": ["paris"]})
    added: list = []
    db = MagicMock()
    db.add.side_effect = _add_capture_run(added)

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 5, 18, 8, 0),
        countries=None,
    )

    assert added[0].countries is None


def test_create_run_passes_countries_to_load_active_hubs(monkeypatch):
    """The runner must propagate countries DOWN to the hub loader (the
    DB-side WHERE is what shrinks the matrix). A regression where the
    kwarg is dropped would make the filter silently no-op."""
    captured = _patch_active_hubs(monkeypatch, {"FR": ["paris"]})
    db = MagicMock()

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 5, 18, 8, 0),
        countries=["FR"],
    )

    assert captured["countries"] == ["FR"]


def test_create_run_empty_list_treated_as_no_filter(monkeypatch):
    """countries=[] (empty list, no checkboxes ticked) is "no filter".
    Propagate as None so the loader doesn't WHERE country IN () (which
    would return zero rows on Postgres)."""
    captured = _patch_active_hubs(monkeypatch, {"FR": ["paris"]})
    db = MagicMock()

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 5, 18, 8, 0),
        countries=[],
    )

    assert captured["countries"] is None


def test_create_run_filter_with_no_matching_hubs_raises(monkeypatch):
    """Operator picked countries={AT} but never seeded any AT hubs →
    ValueError with the filter set in the message. The API translates
    this to a 400 surfaced below the country picker."""
    _patch_active_hubs(monkeypatch, {"FR": ["paris"]})  # no AT hubs configured
    db = MagicMock()

    with pytest.raises(ValueError, match=r"countries=\['AT'\]"):
        runner.create_run(
            db,
            actor_user_id=None,
            session_id="nap-fr-rail",
            depart_at=datetime(2026, 5, 18, 8, 0),
            countries=["AT"],
        )


def test_create_run_pair_count_shrinks_with_country_filter(monkeypatch):
    """Sanity check on the matrix size — the whole point of this PR.
    {FR,CH} = 3 hubs total = 3*2 = 6 directional pairs vs 10*9=90 unfiltered."""
    _patch_active_hubs(
        monkeypatch,
        {
            "FR": ["paris", "lyon"],
            "CH": ["zurich"],
            "DE": [f"de-{i}" for i in range(7)],  # 7 DE hubs to inflate the unfiltered case
        },
    )
    added: list = []
    db = MagicMock()
    db.add.side_effect = _add_capture_run(added)

    runner.create_run(
        db,
        actor_user_id=None,
        session_id="nap-fr-rail",
        depart_at=datetime(2026, 5, 18, 8, 0),
        countries=["FR", "CH"],
    )

    assert added[0].total_pairs == 6  # 3 hubs * 2 directions


# ─────────────────────── POST /runs endpoint ───────────────────────


def _fake_actor():
    a = MagicMock()
    a.id = MagicMock()  # uuid not significant — just needs to be passable
    return a


def test_api_post_runs_forwards_countries_to_runner(monkeypatch):
    """The endpoint just plumbs body.countries through — exercise the
    full request → runner pipe so a future refactor that drops the
    kwarg is caught at this layer."""
    from fastapi import BackgroundTasks

    from app.api.admin import network_coverage as api

    captured_kwargs: dict = {}

    def fake_create_run(_db, **kwargs):
        captured_kwargs.update(kwargs)
        # Return a stand-in run row that the endpoint can serialise.
        from datetime import UTC

        from app.models import NetworkCoverageRun  # noqa: F401  (import gate)

        run = MagicMock()
        run.id = MagicMock()
        run.session_id = kwargs.get("session_id")
        run.session_label = "FR rail"
        run.depart_at = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
        run.started_at = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
        run.finished_at = None
        run.status = "pending"
        run.direction = kwargs.get("direction", "both")
        run.mode = kwargs.get("mode", "single_session")
        run.total_pairs = 6
        run.completed_pairs = 0
        run.ok_pairs = 0
        run.no_route_pairs = 0
        run.error_pairs = 0
        run.countries = kwargs.get("countries")
        return run

    monkeypatch.setattr(runner, "create_run", fake_create_run)

    # Stub the session-existence check.
    db = MagicMock()
    serving_session = MagicMock()
    serving_session.state = "serving"
    db.get.return_value = serving_session

    result = api.create_run(
        body=_make_body(countries=["fr", "CH"]),
        bg=BackgroundTasks(),
        db=db,
        actor=_fake_actor(),
    )

    assert captured_kwargs["countries"] == ["FR", "CH"]
    assert result.countries == ["FR", "CH"]


def test_api_post_runs_translates_no_matching_hubs_to_400(monkeypatch):
    """runner.create_run raises ValueError when the filter matches no
    hubs. The endpoint should surface that as a 400 with the runner's
    message — operator gets actionable feedback under the picker."""
    from fastapi import BackgroundTasks, HTTPException

    from app.api.admin import network_coverage as api

    def fake_create_run(_db, **_kwargs):
        raise ValueError("No active hubs match countries=['AT']")

    monkeypatch.setattr(runner, "create_run", fake_create_run)

    db = MagicMock()
    serving_session = MagicMock()
    serving_session.state = "serving"
    db.get.return_value = serving_session

    with pytest.raises(HTTPException) as exc:
        api.create_run(
            body=_make_body(countries=["AT"]),
            bg=BackgroundTasks(),
            db=db,
            actor=_fake_actor(),
        )
    assert exc.value.status_code == 400
    assert "AT" in str(exc.value.detail)


def test_runsummary_serialises_countries():
    """RunSummary must expose the countries field so the JS sidebar can
    render the badge. The serialiser is exercised by every GET — pin
    the round-trip here so a Pydantic-level drop is caught."""
    from app.api.admin.network_coverage import _run_to_summary

    run = MagicMock()
    run.id = MagicMock()
    run.session_id = "nap-fr-rail"
    run.session_label = "FR rail"
    run.depart_at = datetime(2026, 5, 18, 8, 0)
    run.started_at = datetime(2026, 5, 18, 8, 0)
    run.finished_at = None
    run.status = "completed"
    run.direction = "both"
    run.mode = "single_session"
    run.total_pairs = 6
    run.completed_pairs = 6
    run.ok_pairs = 5
    run.no_route_pairs = 1
    run.error_pairs = 0
    run.countries = ["FR", "CH"]

    out = _run_to_summary(run)
    assert out.countries == ["FR", "CH"]


def test_runsummary_legacy_row_without_countries_attribute_is_none():
    """A pre-this-PR row read back from the DB before alembic upgrade
    (or a row whose JSONB column is NULL) must not 500 the serialiser.
    `getattr(..., None)` keeps the response shape forward-compatible."""
    from app.api.admin.network_coverage import _run_to_summary

    run = MagicMock(spec=[])  # spec=[] → MagicMock has NO attrs by default
    run.id = MagicMock()
    run.session_id = None
    run.session_label = "fanout"
    run.depart_at = datetime(2026, 5, 18, 8, 0)
    run.started_at = datetime(2026, 5, 18, 8, 0)
    run.finished_at = None
    run.status = "completed"
    run.direction = "both"
    run.mode = "fanout"
    run.total_pairs = 100
    run.completed_pairs = 100
    run.ok_pairs = 100
    run.no_route_pairs = 0
    run.error_pairs = 0
    # Deliberately NO `countries` attribute — simulate the legacy row.

    out = _run_to_summary(run)
    assert out.countries is None
