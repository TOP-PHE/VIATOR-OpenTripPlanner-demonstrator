"""Run `alembic upgrade head` end-to-end against a real Postgres.

CI provides a postgres service container (see .github/workflows/ci.yml).
Locally, run `docker compose up -d postgres` first OR set DATABASE_URL to a
disposable instance.

These tests are gated on a Postgres being reachable. If the connection fails,
they SKIP rather than fail — so this file is safe to run in environments
without Postgres (e.g. a contributor's laptop without the stack up).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from alembic import command

REQUIRED_TABLES = {
    "users",
    "verification_tokens",
    "password_reset_tokens",
    "sessions",
    "uploads",
    "rebuild_jobs",
    "graph_snapshots",
    "master_stations",
    "master_stations_pending_drift",
    "master_stations_edit_archive",
    "route_aliases",
    "master_carriers",
    "master_carriers_pending_drift",
    "mct_overrides",
    "stations_xref",
    "journey_searches",
    "journey_search_executions",
    "journey_trips",
    "audit_events",
    "platform_config",
}


def _postgres_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip(f"DATABASE_URL is not a Postgres URL ({url!r}); skipping migration test")
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc}); skipping migration test")
    return url


@pytest.fixture
def alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    return cfg


def test_upgrade_head_creates_full_schema(alembic_cfg: Config) -> None:
    """`alembic upgrade head` lays down every table the spec requires."""
    url = _postgres_or_skip()

    # Start fresh — drop everything if a prior test left residue.
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")

    actual_tables = set(inspect(engine).get_table_names())
    missing = REQUIRED_TABLES - actual_tables
    assert not missing, f"missing tables after upgrade head: {sorted(missing)}"


def test_upgrade_head_creates_provenance_view(alembic_cfg: Config) -> None:
    """The cross-session provenance VIEW is present and queryable."""
    url = _postgres_or_skip()

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        # Empty result set is fine — we're testing that the view exists and is queryable.
        result = conn.execute(text("SELECT * FROM journey_trip_provenance LIMIT 1;"))
        result.fetchall()


def test_downgrade_base_drops_everything(alembic_cfg: Config) -> None:
    """`alembic downgrade base` from head leaves the schema empty (modulo extensions)."""
    url = _postgres_or_skip()

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")

    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert not remaining, f"downgrade base left tables behind: {sorted(remaining)}"


# ── 20261009_1200_hub_uic: a station code on the coverage hubs ──

_BEFORE_HUB_UIC = "20261002_2100_rebuild_cancel"
_HUB_UIC = "20261009_1200_hub_uic"


def _hub_columns(engine: Any) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("network_coverage_hubs")}


def _set_code(engine: Any, uic: str | None, origin: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE network_coverage_hubs SET uic = :uic, uic_origin = :origin WHERE id = 'zz-hub'"
            ),
            {"uic": uic, "origin": origin},
        )


def test_hub_uic_revision_up_down_up_and_its_check(alembic_cfg: Config) -> None:
    """The revision adds two null columns to the existing hubs and writes
    nothing else; the CHECK keeps a code and its origin together; the
    downgrade drops both and keeps the hub; a second upgrade works."""
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))

    command.upgrade(alembic_cfg, _BEFORE_HUB_UIC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO network_coverage_hubs (id, name, short, country, lat, lon) "
                "VALUES ('zz-hub', 'ZZ Hub Central', 'ZZ Hub', 'ZZ', 45.5, 6.25)"
            )
        )

    command.upgrade(alembic_cfg, _HUB_UIC)
    assert {"uic", "uic_origin"} <= _hub_columns(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name, uic, uic_origin FROM network_coverage_hubs WHERE id = 'zz-hub'")
        ).one()
    assert tuple(row) == ("ZZ Hub Central", None, None)

    for origin in ("msmm", "manual"):
        _set_code(engine, "9900001", origin)
    _set_code(engine, None, None)
    _set_code(engine, "ZZ9", "manual")
    _set_code(engine, "Z" * 20, "msmm")
    refused = (
        ("9900001", None),
        ("9900001", "zz"),
        (None, "msmm"),
        (None, "manual"),
        ("", "manual"),
        ("ZZ", "msmm"),
    )
    for uic, origin in refused:
        with pytest.raises(IntegrityError):
            _set_code(engine, uic, origin)

    _set_code(engine, "9900001", "manual")
    command.downgrade(alembic_cfg, _BEFORE_HUB_UIC)
    assert not {"uic", "uic_origin"} & _hub_columns(engine)
    with engine.connect() as conn:
        names = (
            conn.execute(text("SELECT name FROM network_coverage_hubs WHERE id = 'zz-hub'"))
            .scalars()
            .all()
        )
    assert names == ["ZZ Hub Central"]

    command.upgrade(alembic_cfg, "head")
    assert {"uic", "uic_origin"} <= _hub_columns(engine)


def test_the_models_check_is_the_revisions_check() -> None:
    """The model's CHECK text and the revision's are the same rule, written twice."""
    import importlib.util
    from pathlib import Path

    from app.models.network_coverage import UIC_ORIGIN_CHECK

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{_HUB_UIC}.py"
    spec = importlib.util.spec_from_file_location("hub_uic_revision", path)
    assert spec is not None
    assert spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    assert revision._CHECK == UIC_ORIGIN_CHECK


# ── 20261010_1200_edit_archive: the end of station edits in VIATOR ──

_STATION_EDIT_ARCHIVE = "20261010_1200_edit_archive"

# Invented rows only: ZZ names, 99 codes.
_EDITED_WITH_DRIFT = "9900001"
_EDITED = "9900002"
_UNTOUCHED = "9900003"
_DRIFT_NOT_MANUAL = "9900004"


def _seed_stations(engine: Any) -> None:
    """Four invented stations as VIATOR's edits left them before step 3."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO master_stations "
                "(uic, name, country_iso, latitude, longitude, source, other_codes, updated_at) "
                "VALUES "
                "('9900003', 'ZZ Plain Central', 'ZZ', 45.5, 6.25, 'trainline', '{}', "
                " '2026-01-02T03:04:05+00:00'), "
                "('9900001', 'ZZ Edited Halt', 'ZZ', 45.1, 6.1, 'manual', '{\"zz\": \"ZZ1\"}', "
                " '2026-01-03T03:04:05+00:00'), "
                "('9900002', 'ZZ Edited Quay', 'ZY', 45.2, NULL, 'manual', '{}', "
                " '2026-01-04T03:04:05+00:00'), "
                "('9900004', 'ZZ Odd Drift', 'ZZ', 45.3, 6.3, 'sncf', '{}', "
                " '2026-01-05T03:04:05+00:00')"
            )
        )
        conn.execute(
            text("UPDATE master_stations SET parent_uic = '9900003' WHERE uic = '9900002'")
        )
        conn.execute(
            text(
                "INSERT INTO master_stations_pending_drift "
                "(uic, trainline_snapshot, fields_differing, detected_at) VALUES "
                "('9900001', '{\"name\": \"ZZ Upstream Halt\"}', '{name}', "
                " '2026-02-01T00:00:00+00:00'), "
                "('9900004', '{\"latitude\": 45.4}', '{latitude}', "
                " '2026-02-02T00:00:00+00:00')"
            )
        )


def _stations(engine: Any) -> dict[str, dict[str, Any]]:
    """Every master_stations row, as JSON, by code."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT uic, to_jsonb(s) FROM master_stations AS s")).all()
    return {uic: dict(row) for uic, row in rows}


def _drift(engine: Any) -> dict[str, tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT uic, trainline_snapshot, fields_differing, detected_at "
                "FROM master_stations_pending_drift"
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _archive(engine: Any) -> dict[str, tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT uic, station, drift_snapshot, drift_fields, drift_detected_at, "
                "archived_at FROM master_stations_edit_archive"
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _tables(engine: Any) -> set[str]:
    return set(inspect(engine).get_table_names())


def _version(engine: Any) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _without(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in keys}


def test_station_edit_archive_is_one_transaction(alembic_cfg: Config) -> None:
    """A failure after the copy (here: deleting a drift row) leaves nothing
    behind: no archive table, the rows still `manual`, the drift rows there,
    the revision not applied. The archive and the hand-back are one
    transaction."""
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(alembic_cfg, _HUB_UIC)
    _seed_stations(engine)
    before_stations, before_drift = _stations(engine), _drift(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION zz_refuse() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'zz refused'; END $$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER zz_refuse BEFORE DELETE ON master_stations_pending_drift "
                "FOR EACH ROW EXECUTE FUNCTION zz_refuse()"
            )
        )

    with pytest.raises(Exception, match="zz refused"):
        command.upgrade(alembic_cfg, _STATION_EDIT_ARCHIVE)

    assert "master_stations_edit_archive" not in _tables(engine)
    assert _stations(engine) == before_stations
    assert _drift(engine) == before_drift
    assert _version(engine) == _HUB_UIC


def test_station_edit_archive_up_down_up(
    alembic_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Edited rows and pending drift rows are archived as they stood (the
    archive still says `manual`: it was written before the flip), then the
    rows are handed back to the import and the drift rows deleted; a plain
    Trainline row is untouched; the count is logged. The downgrade gives the
    edits, `manual` and the drift rows back over what an import wrote in
    between; a second upgrade works."""
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(alembic_cfg, _HUB_UIC)
    _seed_stations(engine)
    before_stations, before_drift = _stations(engine), _drift(engine)

    capsys.readouterr()
    command.upgrade(alembic_cfg, _STATION_EDIT_ARCHIVE)

    printed = [line for line in capsys.readouterr().err.splitlines() if "edit archive" in line]
    assert printed == [
        "station edit archive: 3 station row(s) archived in master_stations_edit_archive; "
        "if not 0, read them with the psql line of docs/admin-guide.md section 11.1"
    ]
    archive = _archive(engine)
    assert set(archive) == {_EDITED_WITH_DRIFT, _EDITED, _DRIFT_NOT_MANUAL}
    for uic in archive:
        station, *_ = archive[uic]
        assert station == before_stations[uic]  # every column, as it stood
    assert archive[_EDITED_WITH_DRIFT][0]["source"] == "manual"
    assert archive[_EDITED_WITH_DRIFT][1:4] == before_drift[_EDITED_WITH_DRIFT]
    assert archive[_EDITED][1:4] == (None, None, None)
    assert archive[_DRIFT_NOT_MANUAL][1:4] == before_drift[_DRIFT_NOT_MANUAL]
    assert all(row[4] is not None for row in archive.values())

    after = _stations(engine)
    for uic in (_EDITED_WITH_DRIFT, _EDITED):
        assert after[uic]["source"] == "trainline"
        # Only the source changes: the next import brings Trainline's values.
        assert _without(after[uic], "source") == _without(before_stations[uic], "source")
    assert after[_UNTOUCHED] == before_stations[_UNTOUCHED]
    assert after[_DRIFT_NOT_MANUAL] == before_stations[_DRIFT_NOT_MANUAL]
    assert _drift(engine) == {}

    # The next import rewrites an archived row, as trainline.py does.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE master_stations SET name = 'ZZ Upstream Halt' WHERE uic = '9900001'")
        )

    command.downgrade(alembic_cfg, _HUB_UIC)

    assert "master_stations_edit_archive" not in _tables(engine)
    assert _stations(engine) == before_stations
    assert _drift(engine) == before_drift

    command.upgrade(alembic_cfg, "head")
    assert set(_archive(engine)) == {_EDITED_WITH_DRIFT, _EDITED, _DRIFT_NOT_MANUAL}
    assert _drift(engine) == {}
    assert "3 station row(s) archived" in capsys.readouterr().err


def test_station_edit_archive_on_a_database_without_edits(
    alembic_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """No edited row: an empty archive, the count 0, every row untouched."""
    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(alembic_cfg, _HUB_UIC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO master_stations (uic, name, source) "
                "VALUES ('9900003', 'ZZ Plain Central', 'trainline')"
            )
        )
    before = _stations(engine)

    capsys.readouterr()
    command.upgrade(alembic_cfg, _STATION_EDIT_ARCHIVE)

    assert _archive(engine) == {}
    assert _stations(engine) == before
    assert "0 station row(s) archived" in capsys.readouterr().err
    command.downgrade(alembic_cfg, _HUB_UIC)
    assert _stations(engine) == before


def _guide_queries() -> list[str]:
    """The read-only psql queries the admin guide gives for the archive."""
    import re
    from pathlib import Path

    guide = Path(__file__).resolve().parents[2] / "docs" / "admin-guide.md"
    return re.findall(
        r'^sudo docker compose exec -T postgres psql -U viator -d viator -c "(.+)"$',
        guide.read_text(encoding="utf-8"),
        re.MULTILINE,
    )


# A Trainline CSV after the revision, as `trainline.parse_csv` reads it:
# 9900001 with every edited field replaced; 9900011 with a new name and
# country but no operator code and no position (empty cells are skipped);
# 9900002 dropped by Trainline; the two rows never edited carried as before.
_CSV_AFTER = (
    "id;uic;name;country;latitude;longitude;sncf_id;db_id\n"
    "1;9900001;ZZ Upstream Halt;ZY;45.11;6.11;;\n"
    "3;9900003;ZZ Plain Central;ZZ;45.5;6.25;;\n"
    "4;9900004;ZZ Odd Drift;ZZ;45.4;6.3;;\n"
    "11;9900011;ZZ Upstream Gare;ZY;;;;\n"
)


def test_the_admin_guides_psql_lines_read_the_archive(alembic_cfg: Config) -> None:
    """The guide's two lines run as written. The first lists every archived
    row. The second, after a real Trainline upsert, lists the edited rows
    with a field that still holds its archived value, and names the
    fields: the import sets only the fields its CSV fills, so the operator
    codes and the position of 9900011 survive, and 9900002 (dropped by
    Trainline) keeps everything. 9900001, whose every edited field the CSV
    replaces, and 9900004, never edited, are not listed."""
    from sqlalchemy.orm import Session

    from app.master import trainline

    url = _postgres_or_skip()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE;"))
        conn.execute(text("CREATE SCHEMA public;"))
    command.upgrade(alembic_cfg, _HUB_UIC)
    _seed_stations(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO master_stations (uic, name, country_iso, latitude, longitude, "
                "trigramme_sncf, db_code, source) VALUES ('9900011', 'ZZ Edited Gare', 'ZZ', "
                "45.7, 6.7, 'ZZTRI', 'ZZDB1', 'manual')"
            )
        )
    command.upgrade(alembic_cfg, _STATION_EDIT_ARCHIVE)

    parsed, parents = trainline.parse_csv(_CSV_AFTER)
    with Session(engine) as session:
        trainline.upsert_with_drift_protection(session, parsed, parents)
    after = _stations(engine)
    assert after["9900011"]["name"] == "ZZ Upstream Gare"
    assert after["9900011"]["db_code"] == "ZZDB1"  # the edit the import leaves
    assert _drift(engine) == {}  # no row is manual any more: no drift written

    listing, left = _guide_queries()
    for query in (listing, left):
        assert query.lstrip().upper().startswith("SELECT ")
    with engine.connect() as conn:
        listed = [row[0] for row in conn.execute(text(listing)).all()]
        still_edited = {row[0]: row[2] for row in conn.execute(text(left)).all()}

    assert listed == [_EDITED_WITH_DRIFT, _EDITED, _DRIFT_NOT_MANUAL, "9900011"]
    assert still_edited == {
        _EDITED: "name, country_iso, latitude, parent_uic",
        "9900011": "latitude, longitude, trigramme_sncf, db_code",
    }
