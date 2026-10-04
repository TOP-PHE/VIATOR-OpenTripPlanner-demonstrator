"""Station sources API and screen E (unit 3 of the station panel).

The SQL of the list routes runs in tests/integration/test_station_panel.py;
here the rules are checked without a database: when an access grant turns
amber and red, which vocabulary values are accepted, what a PATCH may and may
not do, and that the page renders for a platform admin only.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api import pages
from app.api.admin import station_sources as api
from app.master import station_files as sf
from tests.station_fixtures import (
    assert_scripts_parse,
    inline_scripts,
    node_or_skip,
    page_request,
    run_in_node,
)

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin"
ACTOR = SimpleNamespace(id=uuid.uuid4(), username="ops@example.org", role="platform_admin")
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
TODAY = date(2026, 10, 3)


# ── access expiry ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expires_on", "state", "days"),
    [
        (None, "none", None),
        (TODAY - timedelta(days=1), "expired", -1),  # past: red
        (TODAY, "soon", 0),  # the last day is not past yet
        (TODAY + timedelta(days=90), "soon", 90),  # within 90 days: amber
        (TODAY + timedelta(days=91), "ok", 91),
        # The grant the design names: it ends 2027-02-10, 130 days from TODAY.
        (date(2027, 2, 10), "ok", 130),
    ],
)
def test_access_state(expires_on: date | None, state: str, days: int | None) -> None:
    assert api.access_state(expires_on, TODAY) == (state, days)


def test_a_grant_turns_amber_ninety_days_before_it_ends() -> None:
    grant = date(2027, 2, 10)
    assert api.access_state(grant, grant - timedelta(days=91))[0] == "ok"
    assert api.access_state(grant, grant - timedelta(days=90))[0] == "soon"
    assert api.access_state(grant, grant + timedelta(days=1))[0] == "expired"
    assert api.ACCESS_WARN_DAYS == 90


# ── vocabularies, validated here and not by a CHECK ────────────────────


def test_the_five_offline_shapes_are_accepted_formats() -> None:
    assert set(sf.FILE_SHAPES) <= api.SOURCE_FORMATS
    assert {"trainline_csv", "offline_master_column", "other"} <= api.SOURCE_FORMATS
    assert sorted(api.SOURCE_ACQUISITIONS) == ["resolver", "upload", "url"]
    assert {"spine", "timetable", "registry", "merits_input", "crosscheck"} <= api.SOURCE_KINDS


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (("planet", sf.MASTER, "upload", None), "unknown kind 'planet'"),
        (("spine", "pdf", "upload", None), "unknown format 'pdf'"),
        (("spine", sf.MASTER, "carrier_pigeon", None), "unknown acquisition 'carrier_pigeon'"),
        (("timetable", "other", "resolver", None), "needs a resolver_type"),
        (("timetable", "other", "resolver", "telepathy"), "needs a resolver_type"),
        (("timetable", "other", "upload", "tdg"), "only meaningful with acquisition 'resolver'"),
    ],
)
def test_check_vocabulary_refuses_what_no_code_knows(args: tuple[Any, ...], fragment: str) -> None:
    with pytest.raises(ValueError, match=re.escape(fragment)):
        api.check_vocabulary(*args)


def test_check_vocabulary_accepts_the_seeded_combinations() -> None:
    api.check_vocabulary("spine", sf.CRD_LOCATIONS, "upload", None)
    api.check_vocabulary("merits_input", "trainline_csv", "url", None)
    api.check_vocabulary("offline_build", sf.MASTER, "upload", None)
    api.check_vocabulary("timetable", "other", "resolver", "tdg")


# ── response shaping ───────────────────────────────────────────────────


def _source(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "key": "CRD",
        "label": "CRD primary locations",
        "kind": "spine",
        "format": sf.CRD_LOCATIONS,
        "acquisition": "upload",
        "resolver_type": None,
        "resolver_config": None,
        "credential_id": None,
        "country_iso": None,
        "operator": None,
        "licence": "RNE licence",
        "licence_url": None,
        "access_expires_on": None,
        "refresh_cadence": None,
        "triggers_rebuild": True,
        "enabled": True,
        "source_key_unresolved": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _version(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "acquired_at": datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        "as_of": date(2026, 9, 1),
        "filename": "crd_locations_2026-09.csv",
        "bytes": 1234,
        "sha256": "ab" * 32,
        "status": "uploaded",
        "error": None,
        "stats": {"columns": 65},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_source_response_carries_the_latest_version_and_the_expiry_state() -> None:
    source = _source(access_expires_on=TODAY + timedelta(days=30))
    out = api.source_response(
        source,  # type: ignore[arg-type]
        today=TODAY,
        credential_name="SI NAP key",
        version_count=3,
        latest=_version(),  # type: ignore[arg-type]
    )
    assert out.key == "CRD"
    assert out.access_state == "soon"
    assert out.access_days_left == 30
    assert out.access_expires_on == "2026-11-02"
    assert out.credential_name == "SI NAP key"
    assert out.version_count == 3
    assert out.latest_version is not None
    assert out.latest_version.sha256 == "ab" * 32
    assert out.latest_version.as_of == "2026-09-01"
    assert out.latest_version.source_key == "CRD"


def test_source_response_without_any_version() -> None:
    out = api.source_response(_source(), today=TODAY)  # type: ignore[arg-type]
    assert out.latest_version is None
    assert out.version_count == 0
    assert out.access_state == "none"


def test_build_response_computes_the_duration() -> None:
    started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    build = SimpleNamespace(
        id=1,
        started_at=started,
        finished_at=started + timedelta(seconds=42.5),
        status="done",
        builder_version="1",
        inputs={"CRD": {"sha256": "ab" * 32}},
        counts={"station_ref": 3},
        diff_summary={"created": 3},
    )
    out = api.build_response(build)  # type: ignore[arg-type]
    assert out.duration_seconds == 42.5
    assert out.inputs == {"CRD": {"sha256": "ab" * 32}}

    build.finished_at = None  # still running
    assert api.build_response(build).duration_seconds is None  # type: ignore[arg-type]


def test_normalise_fields() -> None:
    out = api._normalise_fields({"country_iso": "si", "licence_url": "https://example.org/l"})
    assert out == {"country_iso": "SI", "licence_url": "https://example.org/l"}
    assert api._normalise_fields({"country_iso": ""}) == {"country_iso": None}
    assert api._normalise_fields({"label": "x"}) == {"label": "x"}


# ── create / patch / delete, against a scripted database ───────────────


class _FakeDb:
    """Answers `execute(...)` from a queue, in call order."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.committed = False

    def execute(self, _stmt: object) -> Any:
        value = self._answers.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: value, scalar_one=lambda: value)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def refresh(self, _obj: Any) -> None:
        return None


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(api.audit, "record", lambda _db, **kw: events.append(kw))
    monkeypatch.setattr(
        api,
        "_one_source_response",
        lambda _db, source: api.source_response(source, today=TODAY),
    )
    return events


def _create(db: _FakeDb, **overrides: Any) -> Any:
    body = {
        "key": "REG_ZZ_TEST",
        "label": "A test register",
        "kind": "registry",
        "format": "other",
        "acquisition": "upload",
        "country_iso": "zz",
        "access_expires_on": "2027-02-10",
    }
    body.update(overrides)
    return api.create_source(
        api.SourceCreate(**body),
        REQUEST,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
    )


def test_create_source(recorded: list[dict[str, Any]]) -> None:
    db = _FakeDb(None)  # no source with that key yet
    out = _create(db)
    (source,) = db.added
    assert source.key == "REG_ZZ_TEST"
    assert source.country_iso == "ZZ"
    assert source.access_expires_on == date(2027, 2, 10)
    assert source.credential_id is None
    assert out.access_state == "ok"
    assert db.committed
    assert recorded[0]["action"] == "station_source.created"
    assert recorded[0]["target_id"] == "REG_ZZ_TEST"


def test_create_source_refusals(recorded: list[dict[str, Any]]) -> None:
    key_taken = _FakeDb(uuid.uuid4())
    with pytest.raises(HTTPException) as clash:
        _create(key_taken)
    assert clash.value.status_code == 409

    key_free = _FakeDb(None)
    with pytest.raises(HTTPException) as vocab:
        _create(key_free, kind="planet")
    assert vocab.value.status_code == 400
    assert "unknown kind" in vocab.value.detail
    assert recorded == []


@pytest.mark.parametrize("key", ["../etc", "has space", "9starts_with_digit", "x", "a.b"])
def test_a_source_key_must_be_a_safe_folder_name(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        api.SourceCreate(key=key, label="x", kind="spine", format="other", acquisition="upload")


def _patch(source: Any, monkeypatch: pytest.MonkeyPatch, **body: Any) -> tuple[Any, _FakeDb]:
    monkeypatch.setattr(api, "_source_or_404", lambda _db, _key: source)
    db = _FakeDb()
    out = api.patch_source(
        source.key,
        api.SourcePatch(**body),
        REQUEST,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
    )
    return out, db


def test_patch_applies_only_the_fields_that_were_sent(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(access_expires_on=date(2027, 2, 10), refresh_cadence="monthly")
    out, db = _patch(source, monkeypatch, enabled=False)
    assert source.enabled is False
    assert source.access_expires_on == date(2027, 2, 10)  # untouched: it was not sent
    assert source.refresh_cadence == "monthly"
    assert out.enabled is False
    assert db.committed
    assert recorded[0]["action"] == "station_source.updated"
    assert recorded[0]["metadata"] == {"fields": ["enabled"]}


def test_patch_with_an_explicit_null_clears_a_nullable_field(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(access_expires_on=date(2027, 2, 10))
    out, _ = _patch(source, monkeypatch, access_expires_on=None)
    assert source.access_expires_on is None
    assert out.access_state == "none"


def test_patch_without_a_credential_field_leaves_the_credential_attached(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent is not null. The edit dialog relies on it: it leaves the field
    out unless the operator changed the selection."""
    credential = uuid.uuid4()
    source = _source(credential_id=credential)
    _patch(source, monkeypatch, label="A renamed register")
    assert source.credential_id == credential
    assert recorded[0]["metadata"] == {"fields": ["label"]}


def test_patch_with_an_explicit_null_detaches_the_credential(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(credential_id=uuid.uuid4())
    _patch(source, monkeypatch, credential_id=None)
    assert source.credential_id is None
    assert recorded[0]["metadata"] == {"fields": ["credential_id"]}


def test_patch_that_changes_nothing_writes_no_audit_row(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(_source(), monkeypatch, enabled=True, label="CRD primary locations")
    assert recorded == []


def test_patch_refusals(recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    with pytest.raises(HTTPException) as blank:
        _patch(source, monkeypatch, label=None, enabled=None)
    assert blank.value.status_code == 400
    assert "enabled, label" in blank.value.detail

    with pytest.raises(HTTPException) as vocab:
        _patch(source, monkeypatch, acquisition="resolver")  # no resolver_type
    assert vocab.value.status_code == 400
    assert recorded == []


def test_a_source_that_acquired_files_cannot_be_deleted(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    monkeypatch.setattr(api, "_source_or_404", lambda _db, _key: source)
    db = _FakeDb(2)  # two versions
    with pytest.raises(HTTPException) as exc:
        api.delete_source("CRD", REQUEST, db, ACTOR)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert db.deleted == []

    db = _FakeDb(0)
    api.delete_source("CRD", REQUEST, db, ACTOR)  # type: ignore[arg-type]
    assert db.deleted == [source]
    assert recorded[0]["action"] == "station_source.deleted"


def test_credential_id_must_be_a_uuid_of_an_existing_credential() -> None:
    db = SimpleNamespace(get=lambda _model, _id: None)
    assert api._resolve_credential_id(db, None) is None  # type: ignore[arg-type]
    assert api._resolve_credential_id(db, "") is None  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as bad:
        api._resolve_credential_id(db, "not-a-uuid")  # type: ignore[arg-type]
    assert bad.value.status_code == 400
    unknown = str(uuid.uuid4())
    with pytest.raises(HTTPException) as missing:
        api._resolve_credential_id(db, unknown)  # type: ignore[arg-type]
    assert missing.value.status_code == 404


# ── the page ───────────────────────────────────────────────────────────


def test_the_sources_page_renders_for_a_platform_admin() -> None:
    response = pages.admin_station_sources_page(
        page_request("/admin/stations/sources", "platform_admin")
    )
    assert response.status_code == 200
    html = response.body.decode()
    assert "Sources and integration" in html
    assert 'href="/admin/stations/sources" aria-current="page"' in html
    # The vocabularies the API validates are the ones the form offers.
    for value in (*api.SOURCE_KINDS, *api.SOURCE_FORMATS, *api.SOURCE_ACQUISITIONS):
        assert f'<option value="{value}">' in html
    assert "within 90 days" in html
    assert "station master (station_master_crd_*.csv)" in html


def test_the_sources_page_is_refused_to_a_content_manager() -> None:
    response = pages.admin_station_sources_page(
        page_request("/admin/stations/sources", "content_manager")
    )
    assert response.status_code == 403


def test_the_sources_page_redirects_an_anonymous_browser_to_login() -> None:
    response = pages.admin_station_sources_page(page_request("/admin/stations/sources", None))
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/admin/stations/sources"


# ── the templates' JavaScript ──────────────────────────────────────────


def test_every_inline_script_of_the_sources_page_parses(tmp_path: Path) -> None:
    response = pages.admin_station_sources_page(
        page_request("/admin/stations/sources", "platform_admin")
    )
    # The shared helpers, the page's module, and the base template's logout script.
    assert assert_scripts_parse(response.body.decode(), tmp_path) == 3


def test_the_shared_helpers_behave() -> None:
    """The helpers every station screen relies on, executed in Node."""
    html = (TEMPLATES / "_station_panel.html").read_text(encoding="utf-8")
    ((is_module, script),) = inline_scripts(html)
    assert not is_module
    out = run_in_node(
        script,
        """{
          esc: StationPanel.esc('<a href="x">Tom & \\'Jerry\\'</a>'),
          escNull: StationPanel.esc(null),
          query: StationPanel.query({q: 'Gare du Nord', country: '', page: 0, size: 50, x: null}),
          bytes: [0, 512, 2048, 5 * 1024 * 1024].map(StationPanel.fmtBytes),
          date: StationPanel.fmtDate('2026-10-03T12:34:56+00:00'),
          noDate: StationPanel.fmtDate(null),
          sha: StationPanel.shortSha('abcdef0123456789abcdef'),
          int: StationPanel.fmtInt(63049),
          pill: StationPanel.pill('warn', '<b>'),
          total: StationPanel.totalOf(new Map([['X-Total-Count', '63049']])),
          noTotal: StationPanel.totalOf(new Map()),
        }""",
    )
    assert out == {
        "esc": "&lt;a href=&quot;x&quot;&gt;Tom &amp; &#39;Jerry&#39;&lt;/a&gt;",
        "escNull": "",
        "query": "q=Gare+du+Nord&page=0&size=50",
        "bytes": ["0 B", "512 B", "2 KB", "5.0 MB"],
        "date": "2026-10-03 12:34",
        "noDate": "—",
        "sha": "abcdef012345",
        "int": "63,049",
        "pill": '<span class="sp-pill warn">&lt;b&gt;</span>',
        "total": 63049,
        "noTotal": 0,
    }


@pytest.fixture(scope="module")
def sources_html() -> str:
    return (TEMPLATES / "station_sources.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shared_html() -> str:
    return (TEMPLATES / "_station_panel.html").read_text(encoding="utf-8")


def test_the_shared_partial_exports_every_helper_the_screens_use(
    sources_html: str, shared_html: str
) -> None:
    exported = re.search(r"return \{([^}]+)\};\s*\}\)\(\);", shared_html)
    assert exported
    names = {name.strip() for name in exported.group(1).split(",")}
    for name in names:
        assert re.search(rf"function {name}\(", shared_html), f"{name} is exported but not defined"
    used = set(re.findall(r"\bSP\.(\w+)\(", sources_html))
    assert used <= names, f"used but not exported: {sorted(used - names)}"


def test_template_javascript_follows_the_house_rules(sources_html: str, shared_html: str) -> None:
    for html in (sources_html, shared_html):
        assert "window." not in html  # globalThis, not window
        assert not re.search(r"catch\s*(\([^)]*\))?\s*\{\s*\}", html)  # no empty catch
        for body in re.findall(r"catch \(err\) \{(.*?)\}", html, re.DOTALL):
            assert "err" in body  # the caught error is used, not swallowed


def test_the_sources_screen_wires_its_actions(sources_html: str) -> None:
    assert '{% include "admin/_station_panel.html" %}' in sources_html
    assert "{% set station_tab = 'sources' %}" in sources_html
    for action in ("edit", "upload", "toggle"):
        assert f'data-act="{action}"' in sources_html
    assert (
        "/api/admin/stations/sources/' + encodeURIComponent(uploadKey) + '/versions'"
        in sources_html
    )
    assert "/api/admin/stations/builds" in sources_html
    # Everything interpolated into innerHTML goes through the escaper.
    assert "SP.esc(s.label)" in sources_html
    assert "SP.esc(s.key)" in sources_html


# ── the source dialog and a credential the caller cannot list ──────────
#
# GET /api/credentials lists the caller's own credentials, while a source may
# carry one saved by another platform admin. The page's module is loaded in
# Node on a stand-in DOM, and its dialog is opened, changed and saved.

MINE = "11111111-1111-4111-8111-111111111111"
THEIRS = "22222222-2222-4222-8222-222222222222"
NOT_LISTED = " (not in your credential list)"

FORM_DOM = """
const MINE = __MINE__;
const FAKE_SOURCES = __SOURCES__;
const FAKE_CREDENTIALS = __CREDENTIALS__;  // null: the list cannot be read
const fakeSent = [];
const fakeToasts = [];

// An element that accepts any call and any assignment.
function fakeNode() {
  return new Proxy({}, {
    get: (target, key) => (key in target ? target[key] : () => fakeNode()),
    set: (target, key, value) => { target[key] = value; return true; },
  });
}

function fakeInput(defaults = {}) {
  const el = {value: '', checked: false, ...defaults};
  el.reset = () => { Object.assign(el, {value: '', checked: false, ...defaults}); };
  return el;
}

// A <select> as the HTML standard defines `value`: assigning a value that no
// option has selects nothing, and the select then reads back ''.
class FakeOption {
  constructor(text, value) {
    this.text = text;
    this.value = value;
    this.select = null;
  }

  remove() {
    const select = this.select;
    if (!select) return;
    const at = select.options.indexOf(this);
    select.options.splice(at, 1);
    this.select = null;
    // With its selected option gone, a select shows its first one.
    if (select.selectedIndex === at) select.selectedIndex = 0;
    else if (select.selectedIndex > at) select.selectedIndex -= 1;
  }
}

function fakeSelect(...options) {
  const el = {
    options: [],
    selectedIndex: 0,
    add(option) { option.select = el; el.options.push(option); },
    get value() { return el.options[el.selectedIndex]?.value ?? ''; },
    set value(wanted) {
      el.selectedIndex = el.options.findIndex((option) => option.value === String(wanted));
    },
    reset() { el.selectedIndex = 0; },
  };
  for (const option of options) el.add(option);
  return el;
}

const fakeFields = {
  credential_id: fakeSelect(new FakeOption('no authentication', '')),
  enabled: fakeInput({checked: true}),
};
const fakeForm = {
  elements: new Proxy(fakeFields, {get: (fields, name) => (fields[name] ??= fakeInput())}),
  reset() { for (const field of Object.values(fakeFields)) field.reset(); },
  addEventListener() {},
};
const fakeById = {'format-hints': {textContent: '{}'}, 'source-form': fakeForm};

globalThis.Option = FakeOption;
globalThis.document = {
  getElementById: (id) => (fakeById[id] ??= fakeNode()),
  querySelectorAll: () => [],
};
globalThis.fetch = async (url, options = {}) => {
  if (options.method) {
    fakeSent.push({method: options.method, url, body: JSON.parse(options.body)});
    return {ok: true, status: 200, json: async () => ({})};
  }
  const data = {
    '/api/admin/stations/sources': FAKE_SOURCES,
    '/api/admin/stations/builds': {builds: [], queued: [], missing_inputs: []},
    '/api/credentials': FAKE_CREDENTIALS,
  }[url];
  if (data === null) return {ok: false, status: 503, json: async () => ({detail: 'unavailable'})};
  return {ok: true, status: 200, json: async () => data, headers: new Map()};
};
"""

# The real toast arms a 6 s timer; the scenario only needs what was said.
FORM_TOAST = """
globalThis.StationPanel.toast = (kind, text) => { fakeToasts.push(kind + ': ' + text); };
"""

FORM_SCENARIO = """
{
  const select = sourceForm.elements.credential_id;
  const submit = {preventDefault() {}};
  const shown = () => ({
    value: select.value,
    label: select.options[select.selectedIndex]?.text ?? null,
    options: select.options.map((option) => option.value),
  });
  const save = async () => {
    await saveSource(submit);
    return fakeSent.at(-1);
  };
  const edit = async (key, change) => {
    openSourceDialog(sources.find((s) => s.key === key));
    const before = shown();
    change(sourceForm.elements);
    return {shown: before, sent: await save()};
  };
  const add = async (key, credential) => {
    openSourceDialog(null);
    const before = shown();
    sourceForm.elements.key.value = key;
    sourceForm.elements.label.value = 'A new register';
    select.value = credential;
    return {shown: before, sent: await save()};
  };

  const out = {};
  out.renamed = await edit('REG_ZZ_THEIRS', (fields) => { fields.label.value = 'A renamed register'; });
  out.own = await edit('REG_ZZ_MINE', (fields) => { fields.label.value = 'A renamed register'; });
  out.detached = await edit('REG_ZZ_THEIRS', () => { select.value = ''; });
  out.replaced = await edit('REG_ZZ_THEIRS', () => { select.value = MINE; });
  out.added = await add('REG_ZZ_NEW', '');
  out.addedWithMine = await add('REG_ZZ_NEWER', MINE);
  out.toasts = fakeToasts;
  process.stdout.write(JSON.stringify(out));
}
"""


def _listed(key: str, credential_id: str, credential_name: str) -> dict[str, Any]:
    """A source as GET /api/admin/stations/sources returns it."""
    source = _source(
        key=key,
        label="A test register",
        kind="registry",
        format="other",
        licence=None,
        country_iso="ZZ",
        credential_id=uuid.UUID(credential_id),
    )
    listed = api.source_response(source, today=TODAY, credential_name=credential_name)  # type: ignore[arg-type]
    return listed.model_dump()


def _run_source_dialog(folder: Path, credentials: list[dict[str, str]] | None) -> dict[str, Any]:
    """Drive the source dialog of the real page, the caller's credential list
    being `credentials` (None: it cannot be read)."""
    node = node_or_skip()
    ((_, shared),) = inline_scripts((TEMPLATES / "_station_panel.html").read_text(encoding="utf-8"))
    ((is_module, page),) = inline_scripts(
        (TEMPLATES / "station_sources.html").read_text(encoding="utf-8")
    )
    assert is_module
    listed = [
        _listed("REG_ZZ_THEIRS", THEIRS, "Other admin key"),
        _listed("REG_ZZ_MINE", MINE, "My key"),
    ]
    dom = (
        FORM_DOM.replace("__MINE__", json.dumps(MINE))
        .replace("__SOURCES__", json.dumps(listed))
        .replace("__CREDENTIALS__", json.dumps(credentials))
    )
    # A file, not `node -e`: the page's script is a module with a top-level await.
    program = folder / "source_dialog.mjs"
    program.write_text(dom + shared + FORM_TOAST + page + FORM_SCENARIO, encoding="utf-8")
    result = subprocess.run(
        [node, str(program)], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def dialog(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The caller owns `My key`; `Other admin key` is another admin's."""
    return _run_source_dialog(tmp_path_factory.mktemp("dialog"), [{"id": MINE, "name": "My key"}])


@pytest.fixture(scope="module")
def dialog_without_list(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """GET /api/credentials fails: the select holds `no authentication` only."""
    return _run_source_dialog(tmp_path_factory.mktemp("dialog"), None)


def test_renaming_a_source_keeps_a_credential_the_caller_cannot_list(
    dialog: dict[str, Any], recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin B renames a source that carries admin A's credential."""
    renamed = dialog["renamed"]
    # The select names the credential instead of falling back to "no authentication" ...
    assert renamed["shown"]["value"] == THEIRS
    assert renamed["shown"]["label"] == "Other admin key" + NOT_LISTED
    assert renamed["shown"]["options"] == ["", MINE, THEIRS]
    # ... and the request leaves the credential out, since it was not changed.
    assert renamed["sent"]["method"] == "PATCH"
    assert renamed["sent"]["url"] == "/api/admin/stations/sources/REG_ZZ_THEIRS"
    assert "credential_id" not in renamed["sent"]["body"]
    assert dialog["toasts"] == ["success: Source saved"] * 6

    # The route, given that very body: the label changes, the credential stays.
    source = _source(
        key="REG_ZZ_THEIRS",
        label="A test register",
        kind="registry",
        format="other",
        licence=None,
        country_iso="ZZ",
        credential_id=uuid.UUID(THEIRS),
    )
    _patch(source, monkeypatch, **renamed["sent"]["body"])
    assert source.label == "A renamed register"
    assert source.credential_id == uuid.UUID(THEIRS)
    assert recorded[0]["metadata"] == {"fields": ["label"]}


def test_the_dialog_sends_the_credential_only_when_the_selection_changed(
    dialog: dict[str, Any],
) -> None:
    # The caller's own credential is an ordinary option, and is not sent either.
    assert dialog["own"]["shown"]["value"] == MINE
    assert dialog["own"]["shown"]["label"] == "My key"
    assert dialog["own"]["shown"]["options"] == ["", MINE]
    assert "credential_id" not in dialog["own"]["sent"]["body"]
    # Choosing "no authentication" detaches: an explicit null.
    assert dialog["detached"]["sent"]["body"]["credential_id"] is None
    # Choosing another credential replaces it.
    assert dialog["replaced"]["sent"]["body"]["credential_id"] == MINE


def test_the_placeholder_of_one_source_is_not_offered_to_the_next(
    dialog: dict[str, Any],
) -> None:
    # "Add a source", opened after an edit of the source with admin A's credential.
    assert dialog["added"]["shown"] == {
        "value": "",
        "label": "no authentication",
        "options": ["", MINE],
    }
    assert dialog["added"]["sent"]["method"] == "POST"
    assert dialog["added"]["sent"]["body"]["key"] == "REG_ZZ_NEW"
    assert "credential_id" not in dialog["added"]["sent"]["body"]
    assert dialog["addedWithMine"]["sent"]["body"]["credential_id"] == MINE


def test_a_credential_list_that_cannot_be_read_does_not_detach_the_credential(
    dialog_without_list: dict[str, Any],
) -> None:
    """The owner edits their own source while GET /api/credentials fails."""
    own = dialog_without_list["own"]
    assert own["shown"]["value"] == MINE
    assert own["shown"]["label"] == "My key" + NOT_LISTED
    assert "credential_id" not in own["sent"]["body"]


def test_the_tab_bar_hides_the_sources_entry_from_content_managers(shared_html: str) -> None:
    guarded = re.search(
        r"\{% if current_user\.role == 'platform_admin' %\}(.*?)\{% endif %\}",
        shared_html,
        re.DOTALL,
    )
    assert guarded
    assert "/admin/stations/sources" in guarded.group(1)
    outside = shared_html.replace(guarded.group(0), "")
    for path in ("nap", "registers", "trainline", "reference"):
        assert f'href="/admin/stations/{path}"' in outside
    assert "/admin/stations/sources" not in outside
