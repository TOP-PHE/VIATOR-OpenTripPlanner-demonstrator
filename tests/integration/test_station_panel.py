"""Integration: the station panel against a real Postgres.

Mirrors the fresh-DB harness of the other integration modules (there is no
shared integration conftest in this repo). Skips when Postgres is unreachable.

Every station file used here is synthetic: PLC prefix `ZZ`, invented names.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app.master import station_files as sf
from tests.station_fixtures import STATION_FILE_SET, csv_bytes, master_rows, station_file

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
        "Exampleville | Exampleville Hbf", ["ZZ"], 1,
    )  # fmt: skip
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

    flags = _rows(
        "SELECT r.plc, f.token, f.payload, o.plc FROM station_ref_flag f"
        " JOIN station_ref r ON r.id = f.station_id"
        " LEFT JOIN station_ref o ON o.id = f.related_station_id"
        " ORDER BY r.plc, r.era_uopid, f.token"
    )
    assert flags == [
        ("ZZ00001", "candidate_displaced_to", "ZZ00002", "ZZ00002"),
        ("ZZ00001", "plc_kind_national", "", None),
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
    ]
    assert _scalar("SELECT count(*) FROM crd_subsidiary") == 4
    assert _scalar("SELECT count(*) FROM era_operational_point") == 5
    stats = _scalar(
        "SELECT v.stats FROM station_source_version v JOIN station_source s ON s.id = v.source_id"
        " WHERE s.key = 'CRD'"
    )
    assert stats["crd_locations"] == 3  # type: ignore[index]
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
    assert _scalar("SELECT count(*) FROM crd_location") == 3
    assert _scalar("SELECT count(*) FROM station_ref_history") == 0
    assert _scalar("SELECT count(*) FROM station_ref_alias") == 2  # one per build
    assert _rows("SELECT status FROM station_build ORDER BY id") == [("done",), ("done",)]


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
