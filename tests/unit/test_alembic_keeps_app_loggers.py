"""Loading alembic's config in-process leaves the app's loggers enabled (#337).

alembic/env.py calls `fileConfig(alembic.ini)`. With the default
`disable_existing_loggers=True` that disabled every logger that already
existed. The container runs `alembic upgrade head` in a process of its own, so
production never noticed, but the integration tests run it inside the pytest
process: from then on every `app.*` logger was disabled, and a later test that
asserted a line was NOT logged passed whether or not the line was written.

The migration runs offline (`--sql`, no database): env.py, and so fileConfig,
runs exactly as it does online.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[2]
FIRST_REVISION = "20260427_2200_initial"


@pytest.fixture
def logging_restored() -> Iterator[None]:
    """fileConfig also sets the root level and handler and the alembic and
    sqlalchemy.engine loggers; put them back for the tests that follow."""
    names = ("", "alembic", "sqlalchemy.engine")
    saved = {
        name: (lg.level, list(lg.handlers), lg.propagate, lg.disabled)
        for name in names
        for lg in [logging.getLogger(name)]
    }
    yield
    for name, (level, handlers, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.disabled = disabled


class _Lines(logging.Handler):
    """Collects messages. Attached to a logger of its own, because fileConfig
    removes the root handlers, caplog's among them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _run_env_py_offline() -> str:
    sql = io.StringIO()
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=sql)
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, FIRST_REVISION, sql=True)
    return sql.getvalue()


def test_app_loggers_stay_enabled_after_alembic_loads_its_config(
    logging_restored: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A logger of the app that exists before alembic runs, as in a test run.
    probe = logging.getLogger("app.zz_probe_337")
    lines = _Lines()
    monkeypatch.setattr(probe, "handlers", [lines])
    monkeypatch.setattr(probe, "disabled", False)

    sql = _run_env_py_offline()

    assert "CREATE TABLE" in sql  # env.py really ran
    disabled = sorted(
        name
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger) and name.startswith("app") and lg.disabled
    )
    assert disabled == []
    # And its lines are still written.
    probe.warning("zz-probe-337 line")
    assert lines.lines == ["zz-probe-337 line"]


def test_alembic_still_logs_its_steps(
    logging_restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # alembic.ini sends the `alembic` logger, at INFO, to a stderr handler: the
    # container's `alembic upgrade head` prints its "Running upgrade" lines
    # there, and still must. (fileConfig resets the handlers of alembic's
    # child loggers, so stderr is where to look.)
    _run_env_py_offline()

    err = capsys.readouterr().err
    assert f"INFO  [alembic.runtime.migration] Running upgrade  -> {FIRST_REVISION}," in err
