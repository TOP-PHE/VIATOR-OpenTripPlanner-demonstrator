"""Saving and progress of a coverage run's ÖBB check (the verify sweep).

Once every pair of a run with "Verify externally" is done, the completion
phase (`runner._finalise_completed_run`) asks ÖBB HAFAS about every cell but
the skipped ones, about one a second: 12 882 cells take hours. The answers
used to stay in the completion's ORM session until its one commit, so the
page showed nothing for hours and a worker restart (a deploy) threw away
every answer and started the check again from zero.

`SweepWriter` saves each answer on its result row as it comes, a few at a
time in a short transaction of its own, and records the check's progress on
the run (the `verify_*` columns) in the same transaction. Each write is
fenced: it happens only while the run is `running` and, when the executor's
id is known, held by that executor. A write that finds otherwise stops the
writer (`stopped`): the run was ended (a `cancelled` written by SQL, PR-187)
or another executor took it over (#392), and the sweep stops asking ÖBB.

Best-effort and never in the way: a write that fails (the database briefly
away, a lock held past `LOCK_TIMEOUT`) is logged and its answers are kept
for the next write. The completion's own commit still writes every answer,
as before, so a lost progress write loses nothing. A resumed completion
skips the cells that already have an answer (`external_verified_at` set):
only the sweep writes that column, the modal's Verify button saves nothing.

The writes are blocking calls. The sweep runs on the completion's private
loop, in a thread of its own (`runner._run_on_private_loop`), never on the
executor's loop, which carries the heartbeat.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.orm import Session as DbSession

from ..db import SessionLocal
from ..models import NetworkCoverageResult, NetworkCoverageRun

log = logging.getLogger(__name__)

# The external_* fields the sweep fills on a result row (`_verify_cell` and
# `_persist_alignment_on_row` in the runner). A write saves all of them.
SAVED_FIELDS: tuple[str, ...] = (
    "external_source",
    "external_ok",
    "external_num_connections",
    "external_best_duration_seconds",
    "external_best_transfers",
    "external_error",
    "external_verified_at",
    "external_itineraries",
    "external_alignment_score",
    "external_alignment_tier",
)

# One write every 5 s or every 25 answers, whichever comes first: at about
# one answer a second, ~12 small transactions a minute.
FLUSH_EVERY_S = 5.0
FLUSH_EVERY_CELLS = 25
# One INFO line every 500 saved answers (~10 min at one a second).
LOG_EVERY_CELLS = 500
# A write waits at most this long for a row lock (the heartbeat's and a
# cancel's UPDATEs of the run row are brief); past it the write fails, is
# logged, and its answers go with the next one.
LOCK_TIMEOUT = "5s"


def saved_values(row: Any) -> dict[str, Any]:
    """The values a write saves for one result row, keyed for an ORM bulk
    UPDATE by primary key."""
    values = {name: getattr(row, name, None) for name in SAVED_FIELDS}
    values["id"] = row.id
    return values


def already_checked(row: Any) -> bool:
    """True when the cell has an ÖBB answer saved (by an earlier attempt of
    this run's check: only the sweep sets `external_verified_at`)."""
    return getattr(row, "external_verified_at", None) is not None


class SweepWriter:
    """Saves the sweep's answers and its progress, fenced on the run.

    `start` once, `cell_checked` after each cell, `flush` at the end. Every
    method is safe to call after `stopped`: it then does nothing."""

    def __init__(
        self,
        run_id: uuid.UUID,
        executor_id: str | None,
        *,
        session_factory: Callable[[], DbSession] | None = None,
        clock: Callable[[], float] = time.monotonic,
        flush_every_s: float | None = None,
        flush_every_cells: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.executor_id = executor_id
        # None = the module's `SessionLocal`, looked up at each write.
        self._session_factory = session_factory
        self._clock = clock
        # None = the module's FLUSH_EVERY_* (read here, so a test can set them).
        self._flush_every_s = FLUSH_EVERY_S if flush_every_s is None else flush_every_s
        self._flush_every_cells = (
            FLUSH_EVERY_CELLS if flush_every_cells is None else flush_every_cells
        )
        self._pending: list[dict[str, Any]] = []
        self._total = 0
        self._saved = 0
        self._last_flush = clock()
        self._last_logged = 0
        self.stopped = False

    @property
    def saved(self) -> int:
        """The cells whose answer is saved, this attempt's and earlier ones."""
        return self._saved

    @property
    def pending(self) -> int:
        """The answers waiting for the next write."""
        return len(self._pending)

    def start(self, *, total: int, already_saved: int) -> bool:
        """Record the check's start: `total` cells, `already_saved` of them
        answered by an earlier attempt. False when the write did not happen
        (`stopped` tells whether the run is gone)."""
        self._total = total
        self._saved = already_saved
        self._last_logged = already_saved
        self._last_flush = self._clock()
        return self._write(
            {
                "verify_total": total,
                "verify_done": already_saved,
                "verify_done_at_start": already_saved,
                "verify_started_at": func.now(),
                "verify_progress_at": func.now(),
            },
            [],
        )

    def cell_checked(self, row: Any) -> None:
        """Queue one cell's answer; write when one is due."""
        if self.stopped:
            return
        self._pending.append(saved_values(row))
        due = self._clock() - self._last_flush >= self._flush_every_s
        if due or len(self._pending) >= self._flush_every_cells:
            self.flush()

    def flush(self) -> bool:
        """Write the queued answers and the progress now. False when nothing
        was written (the run is gone, or the write failed: the answers are
        then kept for the next one)."""
        if self.stopped:
            return False
        rows = self._pending
        done = self._saved + len(rows)
        written = self._write({"verify_done": done, "verify_progress_at": func.now()}, rows)
        self._last_flush = self._clock()
        if written:
            self._saved = done
            self._pending = []
            self._log_progress()
        return written

    def _fenced_update(self, values: dict[str, Any]) -> Any:
        stmt = update(NetworkCoverageRun).where(
            NetworkCoverageRun.id == self.run_id,
            NetworkCoverageRun.status == "running",
        )
        if self.executor_id is not None:
            stmt = stmt.where(NetworkCoverageRun.claimed_by == self.executor_id)
        return stmt.values(**values).returning(NetworkCoverageRun.id)

    def _write(self, run_values: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
        try:
            factory = self._session_factory or SessionLocal
            with factory() as db:
                db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                # The run row first: its UPDATE is the fence, and it holds the
                # row lock until the commit, so no cancel or claim slips in
                # between the check and the answers.
                held = db.execute(self._fenced_update(run_values)).scalar_one_or_none()
                if held is None:
                    db.rollback()
                    self._stop()
                    return False
                if rows:
                    db.execute(update(NetworkCoverageResult), rows)
                db.commit()
                return True
        except Exception:  # best-effort: the completion's commit writes every answer anyway
            log.warning(
                "external-verify progress write failed for run %s (%d answers kept for the next one)",
                self.run_id,
                len(rows),
                exc_info=True,
            )
            return False

    def _stop(self) -> None:
        self.stopped = True
        self._pending = []
        log.warning(
            "external-verify sweep for run %s stops: the run is no longer running"
            " or another executor holds it (%d of %d answers saved)",
            self.run_id,
            self._saved,
            self._total,
        )

    def _log_progress(self) -> None:
        finished = self._saved >= self._total > self._last_logged
        if self._saved - self._last_logged < LOG_EVERY_CELLS and not finished:
            return
        self._last_logged = self._saved
        log.info(
            "external-verify progress run=%s done=%d total=%d",
            self.run_id,
            self._saved,
            self._total,
        )
