"""Integration: the station panel against a real Postgres.

Mirrors the fresh-DB harness of the other integration modules (there is no
shared integration conftest in this repo). Skips when Postgres is unreachable.

Every station file used here is synthetic: PLC prefix `ZZ`, invented names.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app.master import station_files as sf
from tests.station_fixtures import (
    STATION_FILE_SET,
    crd_location_rows,
    csv_bytes,
    master_rows,
    station_file,
)

# Not a real secret — the bootstrap token used only by these fixtures.
_BOOTSTRAP = "test-bootstrap-token"

TELREF = csv_bytes(
    sf.FILE_SHAPES[sf.ERA_TELREF],
    {"plc": "ZZ00001", "uopid": "ZZ00001", "name": "Exampleville Central", "iso2": "ZZ"},
)


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres; skipping station panel tests")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc})")
    return url


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_or_skip()
    from app.settings import settings as live

    monkeypatch.setattr(live, "bootstrap_token", _BOOTSTRAP)

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")

    from app import config_service

    config_service.invalidate_cache()
    return url


@pytest.fixture
def client(fresh_db: str):
    from app.main import app

    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def inbox(_isolated_inbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's inbox singleton at the per-test isolated inbox."""
    from app.settings import settings as live

    monkeypatch.setattr(live, "inbox_dir", _isolated_inbox)
    return _isolated_inbox


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/auth/bootstrap-platform-user",
        json={
            "token": _BOOTSTRAP,
            "email": "admin@viator.example",
            "name": "Admin",
            "password": "a-strong-admin-password",
        },
    )
    r.raise_for_status()
    jwt = r.json()["jwt"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {jwt}"}


def _seed_user(role: str) -> tuple[uuid.UUID, str]:
    """Insert a user of `role` directly; returns (id, email)."""
    from app.auth.passwords import hash_password
    from app.db import SessionLocal
    from app.models import User

    email = f"{role.replace('_', '-')}@viator.example"
    with SessionLocal() as db:
        user = User(
            email=email,
            name=role,
            password_hash=hash_password("an-irrelevant-passphrase"),
            role=role,
        )
        db.add(user)
        db.commit()
        return user.id, email


def _headers_for(role: str) -> dict[str, str]:
    from app.auth import tokens

    user_id, email = _seed_user(role)
    return {"Authorization": f"Bearer {tokens.issue_jwt(user_id, email, role)}"}


@pytest.fixture
def content_manager(client: TestClient) -> dict[str, str]:
    return _headers_for("content_manager")


@pytest.fixture
def end_user(client: TestClient) -> dict[str, str]:
    return _headers_for("end_user")


def _count(model: type) -> int:
    from app.db import SessionLocal

    with SessionLocal() as db:
        return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def _upload(
    client: TestClient,
    headers: dict[str, str],
    key: str,
    data: bytes,
    filename: str,
    **form: str,
):
    return client.post(
        f"/api/admin/stations/sources/{key}/versions",
        headers=headers,
        data=form,
        files={"file": (filename, data, "text/csv")},
    )


# ───────────────────────── unit 2: the upload route ─────────────────────────


def test_upload_writes_a_version_and_stores_the_file(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.db import SessionLocal
    from app.models import AuditEvent, StationSourceVersion

    r = _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv", as_of="2026-09-14")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["version"]["source_key"] == "ERA_TELREF"
    assert body["version"]["as_of"] == "2026-09-14"
    assert body["version"]["status"] == "uploaded"

    with SessionLocal() as db:
        (version,) = db.execute(select(StationSourceVersion)).scalars().all()
        stored = Path(version.stored_path)
        actions = set(db.execute(select(AuditEvent.action)).scalars())
    assert stored.parent == inbox / "_stations" / "ERA_TELREF"
    assert stored.read_bytes() == TELREF
    assert version.sha256 == body["version"]["sha256"]
    assert version.uploaded_by is not None
    assert "station_source.version.uploaded" in actions


def test_re_uploading_the_same_file_is_a_no_op(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.models import StationSourceVersion

    first = _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv")
    again = _upload(client, admin, "ERA_TELREF", TELREF, "another_name.csv")
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["version"]["id"] == first.json()["version"]["id"]
    assert _count(StationSourceVersion) == 1
    files = [p for p in (inbox / "_stations" / "ERA_TELREF").rglob("*") if p.is_file()]
    assert len(files) == 1


def test_upload_refuses_a_file_of_another_shape(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.models import StationSourceVersion

    links = csv_bytes(sf.FILE_SHAPES[sf.LINKS])
    r = _upload(client, admin, "ERA_TELREF", links, "station_links_crd_2026-09.csv")
    assert r.status_code == 400
    assert "missing columns" in r.json()["detail"]
    assert _count(StationSourceVersion) == 0


def test_upload_refuses_an_unknown_or_disabled_source(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.db import SessionLocal
    from app.models import StationSource

    assert _upload(client, admin, "NOPE", TELREF, "x.csv").status_code == 404
    with SessionLocal() as db:
        source = db.execute(
            select(StationSource).where(StationSource.key == "ERA_TELREF")
        ).scalar_one()
        source.enabled = False
        db.commit()
    assert _upload(client, admin, "ERA_TELREF", TELREF, "x.csv").status_code == 409


def test_upload_is_for_platform_admins_only(
    client: TestClient,
    admin: dict[str, str],
    content_manager: dict[str, str],
    end_user: dict[str, str],
    inbox: Path,
) -> None:
    from app.models import StationSourceVersion

    assert _upload(client, {}, "ERA_TELREF", TELREF, "x.csv").status_code == 401
    assert _upload(client, end_user, "ERA_TELREF", TELREF, "x.csv").status_code == 403
    assert _upload(client, content_manager, "ERA_TELREF", TELREF, "x.csv").status_code == 403
    assert _count(StationSourceVersion) == 0


def test_session_upload_of_a_station_csv_is_400_not_500(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    """Both pre-existing upload routes refuse a station file; this one used to
    do it with an unhandled ValueError out of `detect`."""
    r = client.post(
        "/api/sessions",
        headers=admin,
        json={"id": "st-test", "name": "Station test", "category": "NAP", "config": {}},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/sessions/st-test/uploads",
        headers=admin,
        data={"declared_standard": "SNCF-Stations"},
        files={"file": ("station_master_crd_2026-09.csv", csv_bytes(sf.FILE_SHAPES[sf.MASTER]))},
    )
    assert r.status_code == 400
    assert "Detection failed" in r.json()["detail"]


# ───────────────────── unit 3: sources API and screen E ─────────────────────

SOURCES = "/api/admin/stations/sources"

# Every JSON route of the platform-admin router: (method, path).
ADMIN_ROUTES = [
    ("GET", SOURCES),
    ("POST", SOURCES),
    ("PATCH", f"{SOURCES}/CRD"),
    ("DELETE", f"{SOURCES}/CRD"),
    ("GET", f"{SOURCES}/CRD/versions"),
    ("POST", f"{SOURCES}/CRD/versions"),
    ("GET", "/api/admin/stations/builds"),
    ("POST", "/api/admin/stations/builds"),
]


def test_sources_list_carries_the_seeds_and_the_latest_version(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    r = client.get(SOURCES, headers=admin)
    assert r.status_code == 200, r.text
    rows = {s["key"]: s for s in r.json()}
    assert len(rows) == 22
    assert rows["CRD"]["format"] == sf.CRD_LOCATIONS
    assert rows["CRD"]["triggers_rebuild"] is True
    assert rows["TRAINLINE"]["acquisition"] == "url"
    assert rows["nap_FR_regional"]["source_key_unresolved"] is True
    assert all(s["latest_version"] is None and s["version_count"] == 0 for s in rows.values())
    assert all(s["access_state"] == "none" for s in rows.values())

    _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv").raise_for_status()
    rows = {s["key"]: s for s in client.get(SOURCES, headers=admin).json()}
    assert rows["ERA_TELREF"]["version_count"] == 1
    assert rows["ERA_TELREF"]["latest_version"]["filename"] == "telref_locations_v3.csv"
    assert rows["CRD"]["latest_version"] is None


def test_source_create_edit_disable_delete(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    body = {
        "key": "REG_ZZ_TEST",
        "label": "A test register",
        "kind": "registry",
        "format": "other",
        "acquisition": "upload",
        "country_iso": "zz",
        "access_expires_on": "2020-01-01",
    }
    r = client.post(SOURCES, headers=admin, json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["country_iso"] == "ZZ"
    assert created["access_state"] == "expired"  # the date is past: red
    assert client.post(SOURCES, headers=admin, json=body).status_code == 409
    assert (
        client.post(
            SOURCES, headers=admin, json={**body, "key": "X2", "kind": "planet"}
        ).status_code
        == 400
    )

    one = f"{SOURCES}/REG_ZZ_TEST"
    r = client.patch(one, headers=admin, json={"enabled": False, "access_expires_on": None})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["access_state"] == "none"
    assert r.json()["label"] == "A test register"  # not sent, not touched
    assert client.patch(one, headers=admin, json={"label": None}).status_code == 400
    assert client.patch(f"{SOURCES}/NOPE", headers=admin, json={"enabled": True}).status_code == 404

    assert client.delete(one, headers=admin).status_code == 204
    assert client.delete(one, headers=admin).status_code == 404


def test_a_source_with_versions_cannot_be_deleted(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    _upload(client, admin, "ERA_TELREF", TELREF, "telref_locations_v3.csv").raise_for_status()
    r = client.delete(f"{SOURCES}/ERA_TELREF", headers=admin)
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"]


def test_versions_are_listed_newest_first(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    second = csv_bytes(
        sf.FILE_SHAPES[sf.ERA_TELREF],
        {"plc": "ZZ00002", "uopid": "ZZ00002", "name": "Sampleton", "iso2": "ZZ"},
    )
    _upload(client, admin, "ERA_TELREF", TELREF, "telref_v1.csv").raise_for_status()
    _upload(client, admin, "ERA_TELREF", second, "telref_v2.csv").raise_for_status()
    r = client.get(f"{SOURCES}/ERA_TELREF/versions", headers=admin)
    assert r.status_code == 200
    assert [v["filename"] for v in r.json()] == ["telref_v2.csv", "telref_v1.csv"]
    assert client.get(f"{SOURCES}/NOPE/versions", headers=admin).status_code == 404


def test_builds_list_shows_history_and_queued_station_jobs_only(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app.db import SessionLocal
    from app.models import RebuildJob, StationBuild

    r = client.get("/api/admin/stations/builds", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert (body["builds"], body["queued"]) == ([], [])
    assert len(body["missing_inputs"]) == 5  # nothing uploaded yet

    with SessionLocal() as db:
        db.add(StationBuild(status="done", counts={"station_ref": 3}, diff_summary={"created": 3}))
        db.add(RebuildJob(status="pending", kind="station_build"))
        db.add(RebuildJob(status="pending"))  # a graph job: not ours
        db.add(RebuildJob(status="done", kind="station_build"))  # finished: not queued
        db.commit()
    body = client.get("/api/admin/stations/builds", headers=admin).json()
    assert [b["status"] for b in body["builds"]] == ["done"]
    assert body["builds"][0]["counts"] == {"station_ref": 3}
    assert [j["status"] for j in body["queued"]] == ["pending"]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_every_sources_route_refuses_anyone_but_a_platform_admin(
    client: TestClient,
    content_manager: dict[str, str],
    end_user: dict[str, str],
    method: str,
    path: str,
) -> None:
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers=end_user).status_code == 403
    assert client.request(method, path, headers=content_manager).status_code == 403


def test_sources_page_is_for_platform_admins(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str]
) -> None:
    page = "/admin/stations/sources"
    anonymous = client.get(page)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == f"/login?next={page}"
    assert client.get(page, headers=content_manager).status_code == 403
    r = client.get(page, headers=admin)
    assert r.status_code == 200
    assert "Sources and integration" in r.text
    assert "VIATOR" in r.text


# ─────────────────── unit 4: the worker kind and the importer ───────────────────


def _upload_all(
    client: TestClient,
    admin: dict[str, str],
    replace: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, dict]:
    """Upload the five synthetic files; `replace` swaps the rows of some of them."""
    out = {}
    for key in STATION_FILE_SET:
        filename, content = station_file(key, (replace or {}).get(key))
        r = _upload(client, admin, key, content, filename)
        assert r.status_code in (200, 201), r.text
        out[key] = r.json()
    return out


def _run_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """One worker tick, with the rebuild debounce out of the way."""
    from app import worker

    monkeypatch.setattr(worker, "_debounce_seconds", lambda: 0)
    worker.tick()


def _rows(sql: str, **params: object) -> list[tuple]:
    from app.db import SessionLocal

    with SessionLocal() as db:
        return [tuple(row) for row in db.execute(text(sql), params).all()]


def _scalar(sql: str, **params: object) -> object:
    return _rows(sql, **params)[0][0]


@pytest.fixture
def built(
    client: TestClient, admin: dict[str, str], inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, dict]:
    """The five files uploaded and build #1 run by the worker."""
    uploads = _upload_all(client, admin)
    _run_worker(monkeypatch)
    return uploads


def test_the_fifth_upload_queues_exactly_one_build(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    uploads = list(_upload_all(client, admin).values())
    for early in uploads[:4]:
        assert early["build"]["queued"] is False
        assert early["build"]["note"].startswith("no build queued yet: ")
    assert uploads[4]["build"] == {"queued": True, "note": "a station build is queued"}
    jobs = _rows("SELECT kind, status, session_id FROM rebuild_jobs")
    assert jobs == [("station_build", "pending", None)]


def test_a_station_job_and_a_session_less_graph_job_do_not_coalesce(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app import ingestion
    from app.db import SessionLocal
    from app.master import station_import

    _upload_all(client, admin)  # queues the station job
    with SessionLocal() as db:
        assert station_import.enqueue_build(db, "again") is False  # same kind: coalesced
        assert ingestion._enqueue_rebuild(db, session_id=None, reason="legacy upload") is True
        assert ingestion._enqueue_rebuild(db, session_id=None, reason="legacy upload") is False
    kinds = _rows("SELECT kind FROM rebuild_jobs WHERE status = 'pending' ORDER BY kind")
    assert kinds == [("graph",), ("station_build",)]


def test_build_one_is_run_by_the_worker(built: dict[str, dict], inbox: Path) -> None:
    (job,) = _rows("SELECT kind, status, graph_path, log FROM rebuild_jobs")
    assert job[:3] == ("station_build", "done", None)
    assert "done: 5 created, 0 changed, 0 unchanged, 0 absent" in job[3]

    (build,) = _rows(
        "SELECT id, status, builder_version, inputs, counts, diff_summary, log_path FROM station_build"
    )
    build_id, status, version, inputs, counts, diff, log_path = build
    assert (status, version) == ("done", "offline-import/1")
    assert set(inputs) == set(STATION_FILE_SET)
    assert inputs["CRD"]["sha256"] == built["CRD"]["version"]["sha256"]
    assert diff["created"] == 5
    assert counts["station_ref"] == 5
    assert counts["links_orphaned"] == 1
    assert Path(log_path) == inbox / "_stations" / "_builds" / f"build-{build_id}.log"
    assert "master: 5 rows" in Path(log_path).read_text(encoding="utf-8")
    # Every input version is marked as imported.
    assert _rows("SELECT DISTINCT status FROM station_source_version") == [("imported",)]


def test_build_one_writes_the_reference_at_its_grain(built: dict[str, dict]) -> None:
    rows = _rows(
        "SELECT plc, era_uopid, name, is_current, plc_op_max_sep_m, crd_start::text, crd_end::text,"
        " is_passenger, uic_merits, alt_name_text, iso2_all, n_op_with_plc"
        " FROM station_ref ORDER BY plc, era_uopid"
    )
    assert [r[:2] for r in rows] == [
        ("ZZ00001", "ZZ00001"),
        ("ZZ00002", "ZZ00002"),
        ("ZZ00003", "ZZOP03A"),  # two operational points of one PLC stay two rows
        ("ZZ00003", "ZZOP03B"),
        ("ZZ00004", "ZZ00004"),
    ]
    central, sampleton, yard_a, _, halt = rows
    assert central[2:] == (
        "Exampleville Central", True, 0, "2019-12-15", None, True, "9900001",
        "Exampleville; Exampleville Hbf", ["ZZ"], 1,
    )  # fmt: skip
    # One bilingual name, stored as published: its pipe is not a separator.
    assert sampleton[9] == "Sampleton-Midi | Sampelstad-Zuid"
    assert _rows(
        "SELECT alt_name FROM station_ref WHERE plc IN ('ZZ00001', 'ZZ00002') ORDER BY plc"
    ) == [
        (["Exampleville", "Exampleville Hbf"],),
        (["Sampleton-Midi | Sampelstad-Zuid"],),
    ]
    assert sampleton[10] == ["ZZ", "YY"]
    assert yard_a[3:7] == (False, 140, "2019-12-15", "2021-06-30")  # retired in CRD
    assert halt[8] is None  # a calculated MERITS code that was not chosen
    build_id = _scalar("SELECT id FROM station_build")
    assert _rows("SELECT DISTINCT first_seen_build_id, last_built_build_id FROM station_ref") == [
        (build_id, build_id)
    ]


def test_build_one_writes_codes_merits_flags_aliases_and_links(built: dict[str, dict]) -> None:
    codes = _rows(
        "SELECT r.plc, c.source_key, c.code, c.series, c.is_primary, c.evidence_only"
        " FROM station_ref_code c JOIN station_ref r ON r.id = c.station_id"
        " ORDER BY r.plc, c.source_key, c.code"
    )
    assert codes == [
        ("ZZ00001", "nap_CH_SBB", "9900001", "CH_service_point_number", True, False),
        ("ZZ00001", "nap_CH_SBB", "9900001:0:1", None, False, False),
        # An aggregate column: its code is evidence, and its series came from the links.
        ("ZZ00001", "nap_ES_regional", "ZZ_FEED#MC", "ZZ_series_from_links", True, True),
        ("ZZ00002", "nap_DE_DELFI", "de:99:2", None, True, False),
    ]
    # The vocabulary grew by INSERT, not by a migration.
    assert _scalar("SELECT count(*) FROM station_code_series") == 15

    merits = _rows(
        "SELECT r.plc, m.code, m.origin, m.is_chosen FROM station_ref_merits m"
        " JOIN station_ref r ON r.id = m.station_id ORDER BY r.plc, m.code"
    )
    assert merits == [
        ("ZZ00001", "9900001", "Trainline = calculated", True),
        ("ZZ00002", "9900002", "Trainline (calculated differs)", True),
        ("ZZ00002", "9900012", "Calculated", False),  # the calculated code is kept
        ("ZZ00002", "9900022", "Conflict value", False),
        ("ZZ00002", "9900032", "Conflict value", False),
        ("ZZ00004", "9900004", "Calculated", False),
    ]
    # A conflict value is `code=labels` in the file: the code alone is the
    # candidate, and the labels are its sources. The one that names the chosen
    # code again is no second candidate; the chosen code gains its label.
    sources = _rows(
        "SELECT m.code, m.sources FROM station_ref_merits m"
        " JOIN station_ref r ON r.id = m.station_id WHERE r.plc = 'ZZ00002' ORDER BY m.code"
    )
    assert sources == [
        ("9900002", ["Trainline_via_EVA"]),
        ("9900012", ["CALC"]),
        ("9900022", ["ZZ_Rail"]),
        ("9900032", ["ZZ_Rail", "ZZ_Timetable"]),
    ]

    flags = _rows(
        "SELECT r.plc, f.token, f.payload, o.plc FROM station_ref_flag f"
        " JOIN station_ref r ON r.id = f.station_id"
        " LEFT JOIN station_ref o ON o.id = f.related_station_id"
        " ORDER BY r.plc, r.era_uopid, f.token, f.payload"
    )
    assert flags == [
        ("ZZ00001", "candidate_displaced_to", "ZZ00002", "ZZ00002"),
        ("ZZ00001", "plc_kind_national", "", None),
        # One flag naming two PLCs: one row per PLC, each linked to its station.
        ("ZZ00002", "candidate_displaced_to", "ZZ00001", "ZZ00001"),
        ("ZZ00002", "candidate_displaced_to", "ZZ00003", "ZZ00003"),
        ("ZZ00002", "swap_partner", "ZZ00001", "ZZ00001"),
        ("ZZ00003", "shares_plc_with", "ZZ00003", "ZZ00003"),
        ("ZZ00004", "bare_token", "", None),
        ("ZZ00004", "token_of_tomorrow", "with:colons", None),  # unknown token: stored
    ]

    aliases = _rows(
        "SELECT a.alias_plc, r.plc, a.reason FROM station_ref_alias a"
        " JOIN station_ref r ON r.id = a.station_id"
    )
    assert aliases == [("ZZ00009", "ZZ00002", "previous_plc")]

    links = _rows(
        "SELECT l.offline_station_id, r.plc, l.asserted, l.label, l.nearest_plc"
        " FROM station_ref_link l LEFT JOIN station_ref r ON r.id = l.station_id"
        " ORDER BY l.offline_station_id"
    )
    assert links == [
        ("NAPST0001", "ZZ00001", True, "Rail", None),
        ("NAPST0002", "ZZ00002", False, "Rail", None),
        ("NAPST0003", "ZZ00003", False, "Multimodal", None),
        ("NAPST0005", "ZZ00001", True, "Rail", None),
        # Stops nothing matched: no station, and the nearest PLC as a hint.
        ("NAPST0101", None, False, "Urban", "ZZ00001"),
        ("NAPST0102", None, False, "Rail", "ZZ00002"),
        ("NAPST0103", None, False, "Multimodal", None),
        ("NAPST0104", None, False, "unknown", None),
    ]


def test_build_one_loads_the_registers(built: dict[str, dict]) -> None:
    crd = _rows(
        "SELECT plc, country, location_code, start_validity, end_validity FROM crd_location ORDER BY plc"
    )
    assert crd == [
        ("ZZ00001", "ZZ", "00001", "2019-12-15", None),
        ("ZZ00002", "ZZ", "00002", "2019-12-15", None),
        ("ZZ00003", "ZZ", "00003", "2019-12-15", "2021-06-30"),  # once, not per operational point
        ("ZZ00008", "ZZ", "00008", "2019-12-15", "2022-12-10"),
    ]
    # The register holds CRD's own name and position. ZZ00008 is retired in
    # CRD: the file carries ERA's for it (name_src, pos_src), so it has none.
    assert _rows("SELECT plc, name, lat, lon FROM crd_location ORDER BY plc") == [
        ("ZZ00001", "Exampleville Central", 50.0, 4.0),
        ("ZZ00002", "Sampleton", 50.1, 4.1),
        ("ZZ00003", "Testbury Yard", 50.2, 4.2),
        ("ZZ00008", None, None, None),
    ]
    assert _scalar("SELECT count(*) FROM crd_subsidiary") == 4
    assert _scalar("SELECT count(*) FROM era_operational_point") == 5
    stats = _scalar(
        "SELECT v.stats FROM station_source_version v JOIN station_source s ON s.id = v.source_id"
        " WHERE s.key = 'CRD'"
    )
    assert stats["crd_locations"] == 4  # type: ignore[index]
    assert stats["duplicates_dropped"] == 1  # type: ignore[index]


def test_rebuilding_from_the_same_files_changes_nothing(built: dict[str, dict]) -> None:
    from app.master import station_import

    before = _rows("SELECT id, plc, era_uopid, name FROM station_ref ORDER BY id")
    output, success = station_import.run_build()
    assert success, output
    assert "done: 0 created, 0 changed, 5 unchanged, 0 absent" in output
    assert _rows("SELECT id, plc, era_uopid, name FROM station_ref ORDER BY id") == before
    # Derived rows are replaced, not piled up; registers are loaded once per version.
    assert _scalar("SELECT count(*) FROM station_ref_code") == 4
    assert _scalar("SELECT count(*) FROM station_ref_merits") == 6
    assert _scalar("SELECT count(*) FROM station_ref_link") == 8
    assert _scalar("SELECT count(*) FROM station_ref_flag") == 8
    assert _scalar("SELECT count(*) FROM crd_location") == 4
    assert _scalar("SELECT count(*) FROM station_ref_history") == 0
    # The aliases too: the set of this build, not one more set per build.
    second_build = _scalar("SELECT max(id) FROM station_build")
    assert _rows("SELECT alias_plc, build_id FROM station_ref_alias") == [("ZZ00009", second_build)]
    assert _rows("SELECT status FROM station_build ORDER BY id") == [("done",), ("done",)]
    # Nothing changed on any station: none is marked as changed by this build.
    assert (
        _scalar(
            "SELECT count(*) FROM station_ref WHERE last_changed_build_id = :build",
            build=second_build,
        )
        == 0
    )


def test_a_rebuild_leaves_exactly_the_current_aliases(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    from app.master import station_import

    def rebuild(rows: list[dict[str, str]], filename: str) -> None:
        _, content = station_file("OFFLINE_MASTER", rows)
        _upload(client, admin, "OFFLINE_MASTER", content, filename).raise_for_status()
        output, success = station_import.run_build()
        assert success, output

    aliases = (
        "SELECT a.alias_plc, r.plc FROM station_ref_alias a"
        " JOIN station_ref r ON r.id = a.station_id ORDER BY a.alias_plc, r.plc"
    )
    assert _rows(aliases) == [("ZZ00009", "ZZ00002")]

    # The next issue of the master gives the old PLC to another station.
    rows = master_rows()
    rows[0] = {**rows[0], "previous_plc": "ZZ00009"}
    rows[1] = {**rows[1], "previous_plc": ""}
    rebuild(rows, "station_master_crd_2026-10.csv")
    assert _rows(aliases) == [("ZZ00009", "ZZ00001")]  # one lookup, one station
    sampleton = client.get(f"{REF}/{_station_id('ZZ00002')}", headers=admin).json()
    assert (sampleton["previous_plc"], sampleton["aliases"]) == (None, [])
    central = client.get(f"{REF}/{_station_id('ZZ00001')}", headers=admin).json()
    assert [a["alias_plc"] for a in central["aliases"]] == ["ZZ00009"]

    # The one after withdraws it: nothing of an earlier build stays behind.
    rows[0] = {**rows[0], "previous_plc": ""}
    rebuild(rows, "station_master_crd_2026-11.csv")
    assert _rows(aliases) == []
    central = client.get(f"{REF}/{_station_id('ZZ00001')}", headers=admin).json()
    assert (central["previous_plc"], central["aliases"]) == (None, [])


def test_a_change_of_codes_merits_or_flags_alone_is_a_change(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    from app.master import station_import

    first_build = _scalar("SELECT max(id) FROM station_build")
    central, sampleton = _station_id("ZZ00001"), _station_id("ZZ00002")

    rows = master_rows()
    rows[0] = {
        **rows[0],
        # A stop key replaced inside a provider cell: same feed count, same tier.
        "nap_CH_SBB": "9900001|9900001:0:2",
        # One more conflict value: the chosen code and the confidence stay.
        "uic_merits_conflict_values": "9900041=ZZ_Rail",
        # One more flag, at the same warning level.
        "flags": rows[0]["flags"] + ";platform_count_differs:ZZ00002",
    }
    filename, content = station_file("OFFLINE_MASTER", rows)
    _upload(client, admin, "OFFLINE_MASTER", content, filename).raise_for_status()
    output, success = station_import.run_build()
    assert success, output

    # No column of station_ref moved, and the station is still not "unchanged".
    assert "done: 0 created, 1 changed, 4 unchanged, 0 absent" in output
    second_build = _scalar("SELECT max(id) FROM station_build")
    history = _rows(
        "SELECT station_id, build_id, field_name, old_value, new_value"
        " FROM station_ref_history ORDER BY field_name"
    )
    assert history == [
        (
            central,
            second_build,
            "codes",
            "source_key=nap_CH_SBB code=9900001:0:1",
            "source_key=nap_CH_SBB code=9900001:0:2",
        ),
        (
            central,
            second_build,
            "flags",
            None,
            f"token=platform_count_differs payload=ZZ00002 related_station_id={sampleton}",
        ),
        (
            central,
            second_build,
            "merits",
            None,
            "code=9900041 origin=Conflict value sources=ZZ_Rail",
        ),
    ]
    changed = _rows(
        "SELECT plc, last_changed_build_id FROM station_ref WHERE era_uopid = plc ORDER BY plc"
    )
    assert changed == [
        ("ZZ00001", second_build),
        ("ZZ00002", first_build),
        ("ZZ00004", first_build),
    ]
    diff = _scalar("SELECT diff_summary FROM station_build ORDER BY id DESC LIMIT 1")
    assert diff["fields_changed"] == {"codes": 1, "flags": 1, "merits": 1}  # type: ignore[index]
    # The children themselves are the new ones, once.
    assert _rows(
        "SELECT code FROM station_ref_code WHERE station_id = :id ORDER BY code", id=central
    ) == [("9900001",), ("9900001:0:2",), ("ZZ_FEED#MC",)]
    detail = client.get(f"{REF}/{central}", headers=admin).json()
    assert [h["field_name"] for h in detail["history"]] == ["codes", "flags", "merits"]


def test_a_build_inserts_each_table_in_one_statement(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.db import engine
    from app.master import station_import

    _upload_all(client, admin)
    inserts: Counter[str] = Counter()

    def count(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.startswith("INSERT INTO "):
            inserts[statement.split()[2]] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        output, success = station_import.run_build()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert success, output

    # The fixture rows differ in which of their cells are empty, as the real
    # ones do. Left to itself the ORM bulk path sends a new statement each
    # time the set of non-empty cells changes from one row to the next.
    tables = (
        "crd_location",
        "crd_subsidiary",
        "era_operational_point",
        "station_ref",
        "station_ref_code",
        "station_ref_merits",
        "station_ref_flag",
        "station_ref_alias",
        "station_ref_link",
    )
    assert {table: inserts[table] for table in tables} == dict.fromkeys(tables, 1)
    # And what is written is unchanged: an empty cell is NULL, a default applies.
    assert _rows("SELECT lat, eva, is_current FROM station_ref WHERE plc = 'ZZ00004'") == [
        (None, None, True)
    ]
    assert _rows(
        "SELECT series, code_raw, is_primary FROM station_ref_code WHERE code = 'de:99:2'"
    ) == [(None, None, True)]
    assert _scalar("SELECT is_joinable FROM station_code_series WHERE key = 'ZZ_series_from_links'")


def test_a_new_master_issue_updates_in_place_and_writes_history(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    from app.master import station_import

    first_build = _scalar("SELECT max(id) FROM station_build")
    sampleton_id = _scalar("SELECT id FROM station_ref WHERE plc = 'ZZ00002'")

    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf"}
    rows = [
        *rows[:4],
        {**rows[0], "plc": "ZZ00005", "era_uopid": "ZZ00005", "era_name": "Newville"},
    ]
    filename, content = station_file("OFFLINE_MASTER", rows)
    _upload(
        client, admin, "OFFLINE_MASTER", content, filename.replace("09", "10")
    ).raise_for_status()

    output, success = station_import.run_build()
    assert success, output
    assert "done: 1 created, 1 changed, 3 unchanged, 1 absent" in output
    second_build = _scalar("SELECT max(id) FROM station_build")

    # Identity is stable across builds: the row is updated, not replaced.
    assert _rows("SELECT id, name FROM station_ref WHERE plc = 'ZZ00002'") == [
        (sampleton_id, "Sampleton Hbf")
    ]
    assert _rows("SELECT field_name, old_value, new_value, build_id FROM station_ref_history") == [
        ("name", "Sampleton", "Sampleton Hbf", second_build)
    ]
    lineage = {
        plc: (first, built_in, changed)
        for plc, first, built_in, changed in _rows(
            "SELECT plc, first_seen_build_id, last_built_build_id, last_changed_build_id"
            " FROM station_ref WHERE era_uopid = plc"
        )
    }
    assert lineage["ZZ00002"] == (first_build, second_build, second_build)
    assert lineage["ZZ00001"] == (first_build, second_build, first_build)
    assert lineage["ZZ00005"] == (second_build, second_build, second_build)
    # A row the new issue no longer has is kept, recognisable by its lineage.
    assert lineage["ZZ00004"] == (first_build, first_build, first_build)
    assert _scalar("SELECT count(*) FROM station_ref") == 6


def test_a_hand_correction_survives_a_rebuild(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    from app.db import SessionLocal
    from app.master import station_import
    from app.models import StationRef, StationRefOverride

    with SessionLocal() as db:
        station = db.execute(select(StationRef).where(StationRef.plc == "ZZ00002")).scalar_one()
        station.name = "Sampleton Central"
        db.add(
            StationRefOverride(
                station_id=station.id,
                field_name="name",
                value="Sampleton Central",
                reason="the name on the platform signs",
                computed_value_at_set="Sampleton",
                computed_value_latest="Sampleton",
            )
        )
        db.commit()

    # The next issue renames the station AND moves it.
    rows = master_rows()
    rows[1] = {**rows[1], "era_name": "Sampleton Hbf", "lat": "50.123000"}
    filename, content = station_file("OFFLINE_MASTER", rows)
    _upload(client, admin, "OFFLINE_MASTER", content, filename).raise_for_status()
    output, success = station_import.run_build()
    assert success, output

    assert _rows("SELECT name, lat FROM station_ref WHERE plc = 'ZZ00002'") == [
        ("Sampleton Central", 50.123)  # the correction holds, the other field improves
    ]
    assert _rows(
        "SELECT value, computed_value_at_set, computed_value_latest FROM station_ref_override"
    ) == [("Sampleton Central", "Sampleton", "Sampleton Hbf")]
    assert _rows("SELECT field_name FROM station_ref_history") == [("lat",)]
    counts = _scalar("SELECT counts FROM station_build ORDER BY id DESC LIMIT 1")
    assert counts["overrides_applied"] == 1  # type: ignore[index]
    assert counts["overrides_drifted"] == 1  # type: ignore[index]


def test_a_build_without_all_five_inputs_is_refused(
    client: TestClient, admin: dict[str, str], inbox: Path
) -> None:
    from app.master import station_import

    for key in ("CRD", "ERA_TELREF", "OFFLINE_MASTER", "OFFLINE_LINKS"):
        filename, content = station_file(key)
        _upload(client, admin, key, content, filename).raise_for_status()

    r = client.post("/api/admin/stations/builds", headers=admin)
    assert r.status_code == 409
    assert "OFFLINE_UNMAPPED: no file uploaded yet" in r.json()["detail"]
    assert _scalar("SELECT count(*) FROM rebuild_jobs") == 0

    output, success = station_import.run_build()
    assert success is False
    assert "refused: inputs missing" in output
    (build,) = _rows("SELECT status, counts FROM station_build")
    assert build[0] == "failed"
    assert build[1]["error"].startswith("inputs missing: OFFLINE_UNMAPPED")
    # A partial import would leave screens silently empty: nothing was written.
    for table in ("station_ref", "crd_location", "era_operational_point", "station_ref_link"):
        assert _scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608 - fixed table names


def test_a_master_with_a_repeated_pair_is_refused_and_nothing_changes(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    from app.master import station_import

    rows = master_rows()
    rows.append({**rows[2], "era_name": "A twin of Testbury Yard A"})
    filename, content = station_file("OFFLINE_MASTER", rows)
    _upload(client, admin, "OFFLINE_MASTER", content, filename).raise_for_status()

    output, success = station_import.run_build()
    assert success is False
    assert "the pair (plc, era_uopid) repeats" in output
    (failed,) = _rows("SELECT status, counts FROM station_build ORDER BY id DESC LIMIT 1")
    assert failed[0] == "failed"
    assert "OFFLINE_MASTER" in failed[1]["error"]
    assert "line 7 ('ZZ00003', 'ZZOP03A')" in failed[1]["error"]
    # The reference is exactly what build #1 left.
    assert _scalar("SELECT count(*) FROM station_ref") == 5
    assert _scalar("SELECT count(*) FROM station_ref_code") == 4
    assert _scalar("SELECT count(*) FROM station_ref WHERE name LIKE 'A twin%'") == 0


def test_rebuild_now_queues_a_build_and_coalesces(
    client: TestClient, admin: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.post("/api/admin/stations/builds", headers=admin)
    assert r.status_code == 202, r.text
    assert r.json() == {"queued": True, "note": "a station build is queued"}
    again = client.post("/api/admin/stations/builds", headers=admin)
    assert again.json()["note"] == "a station build was already queued"
    assert _scalar("SELECT count(*) FROM rebuild_jobs WHERE status = 'pending'") == 1

    body = client.get("/api/admin/stations/builds", headers=admin).json()
    assert body["missing_inputs"] == []
    assert [j["status"] for j in body["queued"]] == ["pending"]
    assert [b["status"] for b in body["builds"]] == ["done"]
    assert set(body["builds"][0]["inputs"]) == set(STATION_FILE_SET)


def test_a_build_left_running_by_a_dead_worker_is_closed_at_startup(
    client: TestClient, admin: dict[str, str]
) -> None:
    from app.db import SessionLocal
    from app.master import station_import
    from app.models import StationBuild

    with SessionLocal() as db:
        db.add(StationBuild(status="running"))
        db.add(StationBuild(status="done"))
        db.commit()
    assert station_import.mark_orphaned_builds() == 1
    assert _rows("SELECT status FROM station_build ORDER BY id") == [("failed",), ("done",)]


# ───────────────────── unit 5: the nav group and the pages ─────────────────────

# The four screens a content manager works on, and the address the Trainline
# panel had before: all open to both roles.
SHARED_PAGES = [
    "/admin/stations/nap",
    "/admin/stations/registers",
    "/admin/stations/trainline",
    "/admin/stations/reference",
    "/admin/master/stations",
]


@pytest.mark.parametrize("page", SHARED_PAGES)
def test_station_pages_render_for_both_roles(
    client: TestClient,
    admin: dict[str, str],
    content_manager: dict[str, str],
    end_user: dict[str, str],
    page: str,
) -> None:
    anonymous = client.get(page)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == f"/login?next={page}"
    for headers in (admin, content_manager):
        r = client.get(page, headers=headers)
        assert r.status_code == 200, f"{page}: {r.status_code}"
        assert 'class="sp-tabs"' in r.text
        assert "<summary>Stations</summary>" in r.text
    assert client.get(page, headers=end_user).status_code == 403


def test_the_nav_group_shows_the_fifth_entry_to_platform_admins_only(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str]
) -> None:
    page = "/admin/stations/reference"
    as_admin = client.get(page, headers=admin).text
    as_manager = client.get(page, headers=content_manager).text
    assert as_admin.count('href="/admin/stations/sources"') == 2  # nav group and tab bar
    assert 'href="/admin/stations/sources"' not in as_manager
    assert as_manager.count('href="/admin/stations/nap"') == 2


# ───────────────────── unit 6: screen D, the reference ─────────────────────

REF = "/api/master/station-ref"

# Every JSON route of the reference router: (method, path).
REFERENCE_ROUTES = [
    ("GET", REF),
    ("GET", f"{REF}/summary"),
    ("GET", f"{REF}/1"),
    ("POST", f"{REF}/1/overrides"),
    ("DELETE", f"{REF}/1/overrides/1"),
    ("POST", f"{REF}/complexes"),
    ("DELETE", f"{REF}/complexes/1"),
]


def _station_id(plc: str, uopid: str | None = None) -> int:
    return int(
        _scalar(
            "SELECT id FROM station_ref WHERE plc = :plc AND era_uopid = :uopid",
            plc=plc,
            uopid=uopid or plc,
        )
    )


def test_the_reference_list_shows_one_row_per_plc_by_default(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(REF, headers=content_manager)
    assert r.status_code == 200, r.text
    assert r.headers["X-Total-Count"] == "4"  # four PLCs, five operational points
    rows = {row["plc"]: row for row in r.json()}
    assert list(rows) == ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00004"]

    yard = rows["ZZ00003"]
    # era_uopid is half the key, and the badge says the PLC is more than this row.
    assert yard["era_uopid"] == "ZZOP03A"
    assert yard["op_badge"] == "PLC carries 2 operational points, 140 m apart"
    assert yard["is_current"] is False
    assert rows["ZZ00001"]["op_badge"] is None

    central = rows["ZZ00001"]
    # One column per provider, pivoted after the page was cut.
    assert central["codes"] == {
        "nap_CH_SBB": ["9900001", "9900001:0:1"],
        "nap_ES_regional": ["ZZ_FEED#MC"],
    }
    assert central["flags"] == ["candidate_displaced_to", "plc_kind_national"]
    # A token once, although the flag names two PLCs and is two rows.
    assert rows["ZZ00002"]["flags"] == ["candidate_displaced_to", "swap_partner"]
    assert (central["uic_merits"], central["uic_merits_origin"]) == (
        "9900001",
        "Trainline = calculated",
    )
    assert central["in_latest_build"] is True
    assert central["has_override"] is False


def test_the_reference_list_uncollapsed_and_paginated(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(REF, headers=content_manager, params={"collapse": "false"})
    assert r.headers["X-Total-Count"] == "5"
    assert [(row["plc"], row["era_uopid"]) for row in r.json()][2:4] == [
        ("ZZ00003", "ZZOP03A"),
        ("ZZ00003", "ZZOP03B"),
    ]
    page = client.get(REF, headers=content_manager, params={"size": 2, "page": 1})
    assert page.headers["X-Total-Count"] == "4"  # the total, not the page length
    assert [row["plc"] for row in page.json()] == ["ZZ00003", "ZZ00004"]
    beyond = client.get(REF, headers=content_manager, params={"size": 2, "page": 9})
    assert beyond.json() == []
    # The operational points of one PLC, as the "expand" control asks for them.
    both = client.get(REF, headers=content_manager, params={"plc": "ZZ00003", "collapse": "false"})
    assert [row["era_uopid"] for row in both.json()] == ["ZZOP03A", "ZZOP03B"]
    assert client.get(REF, headers=content_manager, params={"size": 0}).status_code == 422


@pytest.mark.parametrize(
    ("params", "plcs"),
    [
        ({"q": "sampl"}, ["ZZ00002"]),  # by name, case-insensitive
        ({"q": "Hbf"}, ["ZZ00001"]),  # by an alternative name
        # A bilingual alternative name, typed as the register publishes it.
        ({"q": "Sampleton-Midi | Sampelstad-Zuid"}, ["ZZ00002"]),
        ({"q": "ZZ0000"}, ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00004"]),  # by PLC
        ({"q": "de:99:2"}, ["ZZ00002"]),  # by any code in any series
        ({"q": "9900001:0:1"}, ["ZZ00001"]),
        ({"q": "9900002"}, ["ZZ00002"]),  # by MERITS code
        ({"q": "ZZ00009"}, ["ZZ00002"]),  # by the PLC it had before
        ({"q": "100%"}, []),  # a wildcard is a character, not a pattern
        ({"country": "zz"}, ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00004"]),
        ({"country": "FR"}, []),
        ({"confidence": "conflict"}, ["ZZ00002"]),
        ({"confidence": "none"}, ["ZZ00003"]),
        ({"flag": "swap_partner"}, ["ZZ00002"]),
        ({"flag": "candidate_displaced_to"}, ["ZZ00001", "ZZ00002"]),
        ({"has_code": "true"}, ["ZZ00001", "ZZ00002"]),
        ({"has_code": "false"}, ["ZZ00003", "ZZ00004"]),
        ({"q": "Testbury Yard B"}, ["ZZ00003"]),  # a match on the second operational point
    ],
)
def test_reference_search_and_filters(
    client: TestClient,
    content_manager: dict[str, str],
    built: dict[str, dict],
    params: dict[str, str],
    plcs: list[str],
) -> None:
    r = client.get(REF, headers=content_manager, params=params)
    assert r.status_code == 200, r.text
    assert [row["plc"] for row in r.json()] == plcs
    assert r.headers["X-Total-Count"] == str(len(plcs))


def test_a_search_matching_one_operational_point_shows_that_one(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    (row,) = client.get(REF, headers=content_manager, params={"q": "Testbury Yard B"}).json()
    assert row["era_uopid"] == "ZZOP03B"


def test_reference_summary(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(f"{REF}/summary", headers=content_manager)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stations"] == 5
    assert body["plcs"] == 4
    assert set(body["build"]["inputs"]) == set(STATION_FILE_SET)
    assert body["build"]["diff_summary"]["created"] == 5
    with_codes = [p["key"] for p in body["providers"] if p["has_codes"]]
    assert with_codes == ["nap_CH_SBB", "nap_DE_DELFI", "nap_ES_regional"]
    assert len(body["providers"]) == 16
    assert next(p for p in body["providers"] if p["key"] == "nap_ES_regional")["unresolved"] is True
    assert body["countries"] == [{"value": "ZZ", "count": 5}]
    assert {"value": "swap_partner", "count": 1} in body["flags"]
    # Two stations carry it, on three rows (one of the flags names two PLCs).
    assert {"value": "candidate_displaced_to", "count": 2} in body["flags"]
    assert {c["value"] for c in body["confidences"]} == {"high", "conflict", "low", None}
    assert "uic_merits" in body["overridable_fields"]
    assert len(body["complex_kinds"]) == 4


def test_reference_summary_before_any_build(
    client: TestClient, content_manager: dict[str, str]
) -> None:
    body = client.get(f"{REF}/summary", headers=content_manager).json()
    assert body["build"] is None
    assert body["stations"] == 0
    assert client.get(REF, headers=content_manager).json() == []


def test_reference_detail(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    central = client.get(f"{REF}/{_station_id('ZZ00001')}", headers=content_manager).json()
    assert central["plc"] == "ZZ00001"
    assert central["crd_start"] == "2019-12-15"
    assert [(c["source_key"], c["code"], c["series"]) for c in central["codes"]] == [
        ("nap_CH_SBB", "9900001", "CH_service_point_number"),
        ("nap_CH_SBB", "9900001:0:1", None),
        ("nap_ES_regional", "ZZ_FEED#MC", "ZZ_series_from_links"),
    ]
    assert [m["code"] for m in central["merits"]] == ["9900001"]
    assert [link["offline_station_id"] for link in central["links"]] == ["NAPST0001", "NAPST0005"]
    # A flag links to the station its payload names.
    displaced = next(f for f in central["flags"] if f["token"] == "candidate_displaced_to")
    assert displaced["related"]["plc"] == "ZZ00002"
    assert central["siblings"] == []
    assert central["complex"] is None
    assert central["overrides"] == []

    sampleton = client.get(f"{REF}/{_station_id('ZZ00002')}", headers=content_manager).json()
    # Every MERITS candidate, the chosen one first.
    assert [(m["code"], m["is_chosen"]) for m in sampleton["merits"]] == [
        ("9900002", True),
        ("9900012", False),
        ("9900022", False),
        ("9900032", False),
    ]
    # A conflict value's label is the candidate's source, not part of its code.
    assert [m["sources"] for m in sampleton["merits"]][2:] == [
        ["ZZ_Rail"],
        ["ZZ_Rail", "ZZ_Timetable"],
    ]
    # A flag naming several PLCs links to every station it names.
    named = [
        (f["payload"], f["related"]["plc"], f["related"]["era_uopid"])
        for f in sampleton["flags"]
        if f["token"] == "candidate_displaced_to"
    ]
    assert named == [("ZZ00001", "ZZ00001", "ZZ00001"), ("ZZ00003", "ZZ00003", "ZZOP03A")]
    assert sampleton["alt_name"] == ["Sampleton-Midi | Sampelstad-Zuid"]
    assert [a["alias_plc"] for a in sampleton["aliases"]] == ["ZZ00009"]
    assert sampleton["links"][0]["asserted"] is False

    yard = client.get(f"{REF}/{_station_id('ZZ00003', 'ZZOP03A')}", headers=content_manager).json()
    assert [s["era_uopid"] for s in yard["siblings"]] == ["ZZOP03B"]
    assert yard["op_badge"] == "PLC carries 2 operational points, 140 m apart"

    assert client.get(f"{REF}/999999", headers=content_manager).status_code == 404


def test_a_correction_is_applied_survives_a_rebuild_and_can_be_released(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    from app.master import station_import

    station_id = _station_id("ZZ00002")
    one = f"{REF}/{station_id}"
    r = client.post(
        f"{one}/overrides",
        headers=content_manager,
        json={"field_name": "name", "value": "Sampleton Central", "reason": "platform signs"},
    )
    assert r.status_code == 201, r.text
    override = r.json()
    assert override["computed_value_at_set"] == "Sampleton"
    assert _scalar("SELECT set_by IS NOT NULL FROM station_ref_override") is True

    listed = client.get(REF, headers=content_manager, params={"q": "Sampleton Central"}).json()
    assert [(row["plc"], row["has_override"]) for row in listed] == [("ZZ00002", True)]

    output, success = station_import.run_build()
    assert success, output
    detail = client.get(one, headers=content_manager).json()
    assert detail["name"] == "Sampleton Central"  # re-applied by the build
    assert detail["overrides"][0]["active"] is True

    released = client.delete(f"{one}/overrides/{override['id']}", headers=content_manager)
    assert released.status_code == 200, released.text
    assert released.json()["active"] is False
    assert client.get(one, headers=content_manager).json()["name"] == "Sampleton"
    again = client.delete(f"{one}/overrides/{override['id']}", headers=content_manager)
    assert again.status_code == 409


def test_correction_refusals(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    one = f"{REF}/{_station_id('ZZ00001')}/overrides"
    for body, status in (
        ({"field_name": "plc", "value": "ZZ00099", "reason": "not valid"}, 400),  # identity
        ({"field_name": "lat", "value": "north", "reason": "not valid"}, 400),
        ({"field_name": "name", "value": "x", "reason": ""}, 422),  # a reason is required
    ):
        assert client.post(one, headers=content_manager, json=body).status_code == status
    missing = client.post(
        f"{REF}/999999/overrides",
        headers=content_manager,
        json={"field_name": "name", "value": "x", "reason": "none"},
    )
    assert missing.status_code == 404
    assert _scalar("SELECT count(*) FROM station_ref_override") == 0


def test_a_merits_correction_changes_the_chosen_candidate_and_is_reversible(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    one = f"{REF}/{_station_id('ZZ00002')}"
    r = client.post(
        f"{one}/overrides",
        headers=content_manager,
        json={"field_name": "uic_merits", "value": "9900777", "reason": "per the operator"},
    )
    assert r.status_code == 201, r.text
    detail = client.get(one, headers=content_manager).json()
    assert (detail["uic_merits"], detail["uic_merits_origin"]) == ("9900777", "Manual")
    # Nothing computed was withdrawn.
    assert [(m["code"], m["is_chosen"]) for m in detail["merits"]] == [
        ("9900777", True),
        ("9900002", False),
        ("9900012", False),
        ("9900022", False),
        ("9900032", False),
    ]

    client.delete(f"{one}/overrides/{r.json()['id']}", headers=content_manager).raise_for_status()
    detail = client.get(one, headers=content_manager).json()
    assert (detail["uic_merits"], detail["uic_merits_origin"]) == (
        "9900002",
        "Trainline (calculated differs)",
    )
    assert [m["code"] for m in detail["merits"]] == ["9900002", "9900012", "9900022", "9900032"]


def test_group_and_ungroup_a_complex(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    central, sampleton = _station_id("ZZ00001"), _station_id("ZZ00002")
    body = {
        "label": "Exampleville",
        "kind": "adjacent_treated_as_one",
        "station_ids": [central, sampleton],
        "principal_id": central,
        "requires_physical_separation": True,
        "separation_reason": "border control between the two halves",
    }
    r = client.post(f"{REF}/complexes", headers=content_manager, json=body)
    assert r.status_code == 201, r.text
    complex_id = r.json()["id"]

    detail = client.get(f"{REF}/{sampleton}", headers=content_manager).json()
    assert detail["complex"]["label"] == "Exampleville"
    assert detail["complex"]["source"] == "manual"
    assert detail["complex"]["requires_physical_separation"] is True
    assert [(m["plc"], m["complex_role"]) for m in detail["complex"]["members"]] == [
        ("ZZ00001", "principal"),
        ("ZZ00002", "member"),
    ]
    listed = {row["plc"]: row for row in client.get(REF, headers=content_manager).json()}
    assert listed["ZZ00001"]["complex_label"] == "Exampleville"

    # A station is in one complex at most; a rebuild leaves the grouping alone.
    assert client.post(f"{REF}/complexes", headers=content_manager, json=body).status_code == 409
    from app.master import station_import

    assert station_import.run_build()[1] is True
    assert _scalar("SELECT count(*) FROM station_ref WHERE complex_id IS NOT NULL") == 2

    assert (
        client.delete(f"{REF}/complexes/{complex_id}", headers=content_manager).status_code == 204
    )
    assert _scalar("SELECT count(*) FROM station_ref WHERE complex_role IS NOT NULL") == 0
    assert (
        client.delete(f"{REF}/complexes/{complex_id}", headers=content_manager).status_code == 404
    )


@pytest.mark.parametrize(("method", "path"), REFERENCE_ROUTES)
def test_every_reference_route_refuses_an_end_user_and_an_anonymous_caller(
    client: TestClient, end_user: dict[str, str], method: str, path: str
) -> None:
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers=end_user).status_code == 403


def test_the_reference_is_open_to_both_working_roles(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str]
) -> None:
    for headers in (admin, content_manager):
        assert client.get(REF, headers=headers).status_code == 200
        assert client.get(f"{REF}/summary", headers=headers).status_code == 200
        page = client.get("/admin/stations/reference/1", headers=headers)
        assert page.status_code == 200
        assert 'data-station-id="1"' in page.text


# ───────────────────── unit 7: screen B, the registers ─────────────────────

REG = "/api/master/station-registers"

REGISTER_ROUTES = [
    ("GET", f"{REG}/crd"),
    ("GET", f"{REG}/era"),
    ("GET", f"{REG}/crd/versions"),
    ("GET", f"{REG}/era/delta"),
]


def test_the_crd_list_reads_the_latest_loaded_version(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(f"{REG}/crd", headers=content_manager)
    assert r.status_code == 200, r.text
    assert r.headers["X-Total-Count"] == "4"
    assert r.headers["X-Version-Id"] == built["CRD"]["version"]["id"]
    rows = r.json()
    assert [row["plc"] for row in rows] == ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00008"]
    # CRD's own name and position, or none: never ERA's under the CRD heading.
    assert [(row["name"], row["lat"], row["lon"]) for row in rows] == [
        ("Exampleville Central", 50.0, 4.0),
        ("Sampleton", 50.1, 4.1),
        ("Testbury Yard", 50.2, 4.2),
        (None, None, None),  # retired in CRD: the file has ERA's name and position
    ]
    central = rows[0]
    assert (central["country"], central["location_code"]) == ("ZZ", "00001")
    assert (central["start_validity"], central["end_validity"]) == ("2019-12-15", None)
    assert central["subsidiaries"] == [
        {"type": "crd_dium_codes", "code": "990001"},
        {"type": "crd_rl100", "code": "ZEXC"},
        {"type": "crd_sncf_codes", "code": "99001"},
        {"type": "crd_sncf_codes", "code": "99002"},
    ]
    assert rows[1]["subsidiaries"] == []


def test_the_era_list_and_the_register_filters(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(f"{REG}/era", headers=content_manager)
    assert r.headers["X-Total-Count"] == "5"
    assert r.headers["X-Version-Id"] == built["ERA_TELREF"]["version"]["id"]
    assert [(row["plc"], row["uopid"]) for row in r.json()][2:4] == [
        ("ZZ00003", "ZZOP03A"),
        ("ZZ00003", "ZZOP03B"),
    ]
    assert r.json()[0]["op_type"] == "station"

    def plcs(register: str, **params: object) -> list[str]:
        found = client.get(f"{REG}/{register}", headers=content_manager, params=params)
        assert found.status_code == 200, found.text
        return [row["plc"] for row in found.json()]

    assert plcs("crd", q="sampl") == ["ZZ00002"]
    assert plcs("crd", q="00003") == ["ZZ00003"]  # by PLC substring and by CRD code
    assert plcs("crd", country="zz") == ["ZZ00001", "ZZ00002", "ZZ00003", "ZZ00008"]
    assert plcs("crd", country="FR") == []
    # ERA's name of a location retired in CRD is not in the CRD register.
    assert plcs("crd", q="formerton") == []
    assert plcs("crd", q="ZZ00008") == ["ZZ00008"]
    assert plcs("era", q="ZZOP03B") == ["ZZ00003"]  # by operational point id
    assert plcs("era", q="halt") == ["ZZ00004"]
    paged = client.get(f"{REG}/era", headers=content_manager, params={"size": 2, "page": 2})
    assert paged.headers["X-Total-Count"] == "5"
    assert [row["plc"] for row in paged.json()] == ["ZZ00004"]
    assert client.get(f"{REG}/gtfs", headers=content_manager).status_code == 422


def test_register_versions_carry_the_licence_of_the_source(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    body = client.get(f"{REG}/crd/versions", headers=content_manager).json()
    assert body["current"] == built["CRD"]["version"]["id"]
    (version,) = body["versions"]
    assert version["loaded"] is True
    assert version["as_of"] == "2026-09-01"  # read off crd_locations_2026-09.csv
    assert version["sha256"] == built["CRD"]["version"]["sha256"]
    assert version["stats"]["rows"] == 6
    (source,) = body["sources"]
    assert source["key"] == "CRD"
    assert "RNE licence" in source["licence"]  # what the licence banner shows

    era = client.get(f"{REG}/era/versions", headers=content_manager).json()
    assert era["sources"][0]["licence"] is None


def test_a_register_before_any_import(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str], inbox: Path
) -> None:
    assert client.get(f"{REG}/crd", headers=content_manager).status_code == 404
    empty = client.get(f"{REG}/crd/versions", headers=content_manager).json()
    assert (empty["current"], empty["versions"]) == (None, [])
    assert empty["sources"][0]["key"] == "CRD"  # the banner does not wait for a file

    # Uploaded, not imported yet: listed, and marked as not loaded.
    filename, content = station_file("CRD")
    _upload(client, admin, "CRD", content, filename).raise_for_status()
    waiting = client.get(f"{REG}/crd/versions", headers=content_manager).json()
    assert waiting["current"] is None
    assert [v["loaded"] for v in waiting["versions"]] == [False]
    assert client.get(f"{REG}/crd", headers=content_manager).status_code == 404
    assert client.get(f"{REG}/crd/delta", headers=content_manager).status_code == 409


def test_the_delta_between_two_crd_versions(
    client: TestClient,
    admin: dict[str, str],
    content_manager: dict[str, str],
    built: dict[str, dict],
) -> None:
    from app.master import station_import

    # One loaded version: nothing to compare yet.
    assert client.get(f"{REG}/crd/delta", headers=content_manager).status_code == 409

    rows = crd_location_rows()
    rows[0] = {**rows[0], "name": "Exampleville Central Station"}  # renamed
    # Sampleton keeps its name and place and gets a new code: renumbered.
    rows[1] = {**rows[1], "plc": "ZZ00012", "uopid": "ZZ00012", "crd_location_code": "00012"}
    rows[2] = {**rows[2], "lat": "50.21"}  # moved by about 1.1 km
    rows[3] = {**rows[3], "lat": "50.21"}
    newville = {"plc": "ZZ00007", "uopid": "ZZ00007", "name": "Newville", "lat": "50.7"}
    rows.append({**rows[0], **newville, "lon": "4.7", "crd_location_code": "00007"})  # created
    filename, content = station_file("CRD", rows)
    second = _upload(client, admin, "CRD", content, filename.replace("09", "10"))
    assert second.status_code == 201, second.text
    output, success = station_import.run_build()
    assert success, output

    r = client.get(f"{REG}/crd/delta", headers=content_manager)
    assert r.status_code == 200, r.text
    delta = r.json()
    assert delta["older"] == built["CRD"]["version"]["id"]
    assert delta["newer"] == second.json()["version"]["id"]
    assert delta["counts"] == {
        "created": 1,
        "removed": 0,
        "renamed": 1,
        "moved": 1,
        "renumbered": 1,
        # ZZ00008, retired in CRD: no CRD name or position in either version.
        "unchanged": 1,
    }
    assert [c["key"] for c in delta["created"]] == ["ZZ00007"]
    assert delta["renamed"][0]["new"]["name"] == "Exampleville Central Station"
    assert delta["moved"][0]["old"]["key"] == "ZZ00003"
    assert 1000 < delta["moved"][0]["distance_m"] < 1200
    renumbered = delta["renumbered"][0]
    assert (renumbered["old"]["key"], renumbered["new"]["key"]) == ("ZZ00002", "ZZ00012")
    assert delta["truncated"] == []

    # The list now reads the newer version; the older one is still there to compare.
    assert client.get(f"{REG}/crd", headers=content_manager).headers["X-Total-Count"] == "5"
    older = client.get(f"{REG}/crd", headers=content_manager, params={"version_id": delta["older"]})
    assert older.headers["X-Total-Count"] == "4"

    # A higher threshold: the yard no longer counts as moved.
    loose = client.get(f"{REG}/crd/delta", headers=content_manager, params={"threshold_m": 5000})
    assert loose.json()["counts"]["moved"] == 0
    assert loose.json()["counts"]["unchanged"] == 2

    # Reversed, a created location is a removed one.
    reverse = client.get(
        f"{REG}/crd/delta",
        headers=content_manager,
        params={"older": delta["newer"], "newer": delta["older"]},
    )
    assert reverse.json()["counts"]["removed"] == 1
    unknown = client.get(
        f"{REG}/crd/delta", headers=content_manager, params={"older": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize(("method", "path"), REGISTER_ROUTES)
def test_every_register_route_refuses_an_end_user_and_an_anonymous_caller(
    client: TestClient, end_user: dict[str, str], method: str, path: str
) -> None:
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers=end_user).status_code == 403


def test_the_registers_are_open_to_both_working_roles(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str]
) -> None:
    for headers in (admin, content_manager):
        assert client.get(f"{REG}/crd/versions", headers=headers).status_code == 200
        assert client.get(f"{REG}/era/versions", headers=headers).status_code == 200


# ── screen A: the two lists of NAP stops (unit 8) ────────────────────────

LINKS = "/api/master/station-links"
LINK_ROUTES = [f"{LINKS}/summary", f"{LINKS}/unmatched", f"{LINKS}/contradictions"]


def _stops(client: TestClient, headers: dict[str, str], name: str, **params: object) -> list[str]:
    found = client.get(f"{LINKS}/{name}", headers=headers, params=params)
    assert found.status_code == 200, found.text
    return [row["offline_station_id"] for row in found.json()]


def test_unmatched_stops_default_to_rail_and_multimodal(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    r = client.get(f"{LINKS}/unmatched", headers=content_manager)
    assert r.status_code == 200, r.text
    assert r.headers["X-Total-Count"] == "2"
    rows = r.json()
    # The nearest first; a stop with no nearest reference row comes last.
    assert [row["offline_station_id"] for row in rows] == ["NAPST0102", "NAPST0103"]
    west = rows[0]
    assert (west["stop_name"], west["label"], west["iso2"]) == ("Sampleton West", "Rail", "ZZ")
    assert west["feed_key"] == "DE_DELFI|ZZ_FEED"
    assert (west["lat"], west["lon"]) == (50.11, 4.11)
    assert west["reason"] == "name_mismatch"
    assert (west["nearest_plc"], west["nearest_distance_m"]) == ("ZZ00002", 150.0)
    # The reference row of the nearest PLC, to link to.
    assert west["nearest"] == [
        {
            "id": _station_id("ZZ00002"),
            "plc": "ZZ00002",
            "era_uopid": "ZZ00002",
            "name": "Sampleton",
            "iso2": "ZZ",
        }
    ]
    interchange = rows[1]
    assert (interchange["nearest_plc"], interchange["nearest"]) == (None, [])


def test_unmatched_stops_filters(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    cm = content_manager
    every = ["NAPST0102", "NAPST0101", "NAPST0103", "NAPST0104"]
    assert _stops(client, cm, "unmatched", label="all") == every
    assert _stops(client, cm, "unmatched", label="Urban") == ["NAPST0101"]
    assert _stops(client, cm, "unmatched", label=["Urban", "unknown"]) == [
        "NAPST0101",
        "NAPST0104",
    ]
    # A stop listed for two feeds is found under each of them.
    assert _stops(client, cm, "unmatched", feed="DE_DELFI") == ["NAPST0102"]
    assert _stops(client, cm, "unmatched", feed="ZZ_FEED") == ["NAPST0102", "NAPST0103"]
    assert _stops(client, cm, "unmatched", feed="ZZ") == []  # a feed, not a prefix of one
    assert _stops(client, cm, "unmatched", country="yy") == ["NAPST0103"]
    assert _stops(client, cm, "unmatched", q="west") == ["NAPST0102"]
    assert _stops(client, cm, "unmatched", q="tram") == []  # urban: outside the default
    assert _stops(client, cm, "unmatched", q="tram", label="all") == ["NAPST0101"]

    paged = client.get(
        f"{LINKS}/unmatched", headers=cm, params={"label": "all", "size": 1, "page": 1}
    )
    assert paged.headers["X-Total-Count"] == "4"
    assert [row["offline_station_id"] for row in paged.json()] == ["NAPST0101"]


def test_stops_whose_code_contradicts_the_reference(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    cm = content_manager
    r = client.get(f"{LINKS}/contradictions", headers=cm)
    assert r.status_code == 200, r.text
    assert r.headers["X-Total-Count"] == "1"
    # NAPST0003 is not asserted either, but it carries no code: a candidate
    # by name only, not a contradiction.
    (row,) = r.json()
    assert row["offline_station_id"] == "NAPST0002"
    assert (row["feed_key"], row["stop_key"], row["stop_name"]) == (
        "DE_DELFI",
        "de:99:2",
        "Sampleton Nord",
    )
    assert (row["code_value"], row["code_series"]) == ("9900002", "DELFI_stop_key")
    assert (row["tier"], row["distance_m"], row["name_sim"]) == ("T2_name_distance", 480.0, 0.61)
    assert row["note"] == "code points here, name does not"
    assert row["station"]["plc"] == "ZZ00002"
    assert row["station"]["id"] == row["station_id"]

    assert _stops(client, cm, "contradictions", feed="DE_DELFI") == ["NAPST0002"]
    assert _stops(client, cm, "contradictions", feed="CH_SBB") == []
    assert _stops(client, cm, "contradictions", country="zz") == ["NAPST0002"]
    assert _stops(client, cm, "contradictions", country="FR") == []
    assert _stops(client, cm, "contradictions", q="9900002") == ["NAPST0002"]  # the code
    assert _stops(client, cm, "contradictions", q="de:99:2") == ["NAPST0002"]  # the stop key
    assert _stops(client, cm, "contradictions", q="nord") == ["NAPST0002"]  # the name
    assert _stops(client, cm, "contradictions", q="9900001") == []
    assert _stops(client, cm, "contradictions", page=1) == []


def test_links_summary(
    client: TestClient, content_manager: dict[str, str], built: dict[str, dict]
) -> None:
    def counts(facet: list[dict]) -> dict[str, int]:
        return {item["value"]: item["count"] for item in facet}

    body = client.get(f"{LINKS}/summary", headers=content_manager).json()
    assert body["default_labels"] == ["Rail", "Multimodal"]
    assert body["links"] == 8
    unmatched = body["unmatched"]
    assert unmatched["total"] == 4
    assert counts(unmatched["labels"]) == {"Multimodal": 1, "Rail": 1, "Urban": 1, "unknown": 1}
    # Sampleton West is listed for two feeds and counts under each.
    assert unmatched["feeds"] == [
        {"value": "DE_DELFI", "count": 1},
        {"value": "ZZ_FEED", "count": 4},
    ]
    assert counts(unmatched["countries"]) == {"YY": 1, "ZZ": 3}
    contradictions = body["contradictions"]
    assert contradictions["total"] == 1
    assert contradictions["feeds"] == [{"value": "DE_DELFI", "count": 1}]
    assert contradictions["countries"] == [{"value": "ZZ", "count": 1}]


def test_links_before_any_build(client: TestClient, content_manager: dict[str, str]) -> None:
    body = client.get(f"{LINKS}/summary", headers=content_manager).json()
    assert (body["links"], body["unmatched"]["total"], body["contradictions"]["total"]) == (0, 0, 0)
    assert body["unmatched"]["labels"] == []
    for name in ("unmatched", "contradictions"):
        r = client.get(f"{LINKS}/{name}", headers=content_manager)
        assert r.status_code == 200, r.text
        assert r.headers["X-Total-Count"] == "0"
        assert r.json() == []


@pytest.mark.parametrize("path", LINK_ROUTES)
def test_every_links_route_refuses_an_end_user_and_an_anonymous_caller(
    client: TestClient, end_user: dict[str, str], path: str
) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers=end_user).status_code == 403


def test_the_links_are_open_to_both_working_roles(
    client: TestClient, admin: dict[str, str], content_manager: dict[str, str]
) -> None:
    for headers in (admin, content_manager):
        for path in LINK_ROUTES:
            assert client.get(path, headers=headers).status_code == 200


# ── screen C: the Trainline codes panel, three fixes (unit 9) ────────────

MASTER = "/api/master/stations"
# Synthetic stations, in the order the list sorts them: three pages of two, and one.
MASTER_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf"]


@pytest.fixture
def master_stations(client: TestClient) -> list[str]:
    """Seven synthetic Trainline rows in country ZZ; Echo has a pending drift."""
    from app.db import SessionLocal
    from app.models import MasterStation, MasterStationPendingDrift

    with SessionLocal() as db:
        for index, name in enumerate(MASTER_NAMES, start=1):
            db.add(
                MasterStation(uic=f"99000{index:02d}", name=f"{name} Synthetic", country_iso="ZZ")
            )
        db.flush()
        db.add(
            MasterStationPendingDrift(
                uic="9900005",
                trainline_snapshot={"name": "Echo Renamed Synthetic"},
                fields_differing=["name"],
            )
        )
        db.commit()
    return MASTER_NAMES


def _names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["name"].split()[0] for row in response.json()]


def test_a_context_search_lands_on_its_first_match_when_no_page_is_pinned(
    client: TestClient, content_manager: dict[str, str], master_stations: list[str]
) -> None:
    search = {"q": "echo", "mode": "context", "size": 2}
    r = client.get(MASTER, headers=content_manager, params=search)
    assert _names(r) == ["Echo", "Foxtrot"]
    assert r.headers["X-Match-Page"] == "2"
    assert r.headers["X-Match-Count"] == "1"
    assert r.headers["X-Total-Count"] == "7"  # context mode does not shrink the list
    assert [row["is_match"] for row in r.json()] == [True, False]


def test_page_zero_is_the_first_page_during_a_context_search(
    client: TestClient, content_manager: dict[str, str], master_stations: list[str]
) -> None:
    search = {"q": "echo", "mode": "context", "size": 2}
    # « First, and typing 1 in the page box: page 0 is a page, not "no page".
    first = client.get(MASTER, headers=content_manager, params={**search, "page": 0})
    assert _names(first) == ["Alpha", "Bravo"]
    assert first.headers["X-Match-Page"] == "2"  # where the match is, all the same
    last = client.get(MASTER, headers=content_manager, params={**search, "page": 3})
    assert _names(last) == ["Golf"]
    match = client.get(MASTER, headers=content_manager, params={**search, "page": 2})
    assert _names(match) == ["Echo", "Foxtrot"]
    assert client.get(MASTER, headers=content_manager, params={"page": -1}).status_code == 422


def test_the_default_mode_still_filters(
    client: TestClient, content_manager: dict[str, str], master_stations: list[str]
) -> None:
    # What the journey typeahead sends: `q` and `size`. It must keep hiding
    # the rows that do not match.
    r = client.get(MASTER, headers=content_manager, params={"q": "echo", "size": 20})
    assert _names(r) == ["Echo"]
    assert r.headers["X-Total-Count"] == "1"
    assert r.json()[0]["is_match"] is True
    by_code = client.get(MASTER, headers=content_manager, params={"q": "9900007", "size": 20})
    assert _names(by_code) == ["Golf"]

    everything = client.get(MASTER, headers=content_manager, params={"size": 3})
    assert _names(everything) == ["Alpha", "Bravo", "Charlie"]
    assert "X-Match-Page" not in everything.headers
    second = client.get(MASTER, headers=content_manager, params={"size": 3, "page": 1})
    assert _names(second) == ["Delta", "Echo", "Foxtrot"]


def test_the_list_flags_the_stations_with_a_pending_drift(
    client: TestClient, content_manager: dict[str, str], master_stations: list[str]
) -> None:
    rows = client.get(MASTER, headers=content_manager).json()
    assert [row["name"].split()[0] for row in rows if row["has_drift"]] == ["Echo"]
    assert len(rows) == 7
    # The drift queue still returns the full rows.
    (drift,) = client.get(f"{MASTER}/drift", headers=content_manager).json()
    assert drift["trainline_snapshot"] == {"name": "Echo Renamed Synthetic"}


def test_the_trainline_page_carries_the_promoted_styles(
    client: TestClient, content_manager: dict[str, str]
) -> None:
    for page in ("/admin/stations/trainline", "/admin/master/stations"):
        html = client.get(page, headers=content_manager).text
        assert ".flag.SUBSET" in html
        assert ".hint { color: var(--rail-steel); }" in html
    journey = client.get("/journey", headers=content_manager)
    assert journey.status_code == 200
    assert ".flag { font-size: 0.7rem;" in journey.text  # from the base template now
    # The base hint rule first, then the page's own opt-out, which wins.
    assert journey.text.index(".hint { color: var(--rail-steel); }") < journey.text.index(
        ".hint { color: inherit; }"
    )
    assert ".flag.SUBSET     { background: #fff8e1; color: #b3760e; }" in journey.text
