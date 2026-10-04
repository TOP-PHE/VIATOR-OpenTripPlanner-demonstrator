"""One lock between the station build and the hand edits of the reference.

The build reads `station_ref` and the active corrections once, plans from
that reading, and then writes every built column of each row it changes and
replaces every station's MERITS candidates. A correction set or released
between its reading and its writing would be overwritten, or left half
applied: the override row active and the column computed, or the `Manual`
candidate deleted and its code still on the row. The two also take their row
locks in opposite orders, so Postgres could abort the build as a deadlock
victim.

They are serialised on one Postgres advisory lock, which ends with the
transaction that took it:

  * the build takes it exclusively as the first statement of its write
    stage, and reads the reference and the corrections only once it has it.
    It waits: the edits in flight are short;
  * a route that writes a correction or a complex takes it shared, before it
    reads anything, and does not wait. While a build is writing, or waiting
    to, the route is refused and says so: a request does not hang for the
    length of a build.

Shared, because two edits need not keep each other out: it is the build they
must not interleave with. Nothing here commits or rolls back; the lock goes
with the caller's transaction.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, BindParameter, func, literal, select
from sqlalchemy.orm import Session as DbSession

# "STATREF" in ASCII. Any constant will do, as long as nothing else locks on it.
REFERENCE_LOCK_KEY = 0x53544154524546


def _key() -> BindParameter[int]:
    # Typed, so that the driver sends a bigint whatever the size of the number.
    return literal(REFERENCE_LOCK_KEY, BigInteger)


def hold_for_build(db: DbSession) -> None:
    """Wait for the edits in flight to end, then keep every edit out until the
    caller's transaction ends."""
    db.execute(select(func.pg_advisory_xact_lock(_key())))


def try_for_edit(db: DbSession) -> bool:
    """Keep a build out until the caller's transaction ends. False, at once,
    when a build is writing or waiting to: the caller must not write."""
    return bool(db.execute(select(func.pg_try_advisory_xact_lock_shared(_key()))).scalar_one())
