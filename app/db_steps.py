"""The threads web's async handlers run their database steps in (#397).

An `async def` handler never runs a query on the event loop: each database
step is one call in a worker thread, on the request's session, one after the
other. `db_step(fn, db, ...)` picks the thread:

- a step on a session that holds no pooled connection yet (the first read,
  or the first step after a commit gave the connection back) runs in anyio's
  thread pool, the one FastAPI runs the plain `def` handlers in (40 threads);
- a step on a session that already holds its connection (an earlier step of
  the same transaction ran, and the handler awaited an engine or a download
  since) runs on a limiter of its own, `DB_STEP_TOKENS` threads.

Why two: when the pool (SQLAlchemy's default, 5 + 10 overflow) is exhausted,
up to 40 sync handlers can hold every anyio thread, each waiting up to
`pool_timeout` (30 s) for a connection. The connections are held by async
handlers whose next step, the commit that gives one back, needs a thread. In
the shared pool that commit would queue behind the waiters, and the waiters
would time out (500s) while the connections were about to come back (the
lens-A review of #405 reproduced it on PostgreSQL). On their own limiter
those steps never wait for a sync handler, and never for the pool either:
they already hold their connection, so a token is held only for the step's
own work. The limiter has one token more than the pool has connections, so a
held step never even waits for a token. A step that may wait for the pool
stays in anyio's pool, so it never holds one of these tokens while it waits.

Only steps that use the request's session go through `db_step`: a step that
opens its own session (a progress claim, an audit row of its own) takes a
connection of its own and stays on `run_in_threadpool`.

Work with no database (a CSV parse, a GTFS read, file copies) runs on
asyncio's default executor (`asyncio.to_thread`), a third pool of
min(32, CPUs + 4) threads that no request handler waits on.

Context variables (the request id structlog logs, the OpenTelemetry span) go
with each call: anyio runs it in a copy of the caller's context.
"""

from __future__ import annotations

import asyncio
import functools
import weakref
from collections.abc import Callable
from typing import Any

import anyio
import anyio.to_thread
from sqlalchemy.orm import Session

# One more than the pool's 5 + 10 connections (app/db.py keeps SQLAlchemy's
# defaults): at most 15 sessions hold a connection at once, so at most 15
# held steps run at once and none waits for a token.
DB_STEP_TOKENS = 16

# Per event loop, as anyio's own default limiter is: web's loop and each
# test's loop get their own.
_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)


def holds_connection(db: Session) -> bool:
    """True while `db`'s transaction holds a pooled connection (a statement
    ran since the last commit or rollback). Anything that is not an ORM
    session (a test's stand-in) holds none."""
    if not isinstance(db, Session):
        return False
    transaction = db.get_transaction()
    return transaction is not None and bool(transaction._connections)


def held_step_limiter() -> anyio.CapacityLimiter:
    """The running loop's limiter for the steps of a session that holds its
    connection."""
    loop = asyncio.get_running_loop()
    limiter = _LIMITERS.get(loop)
    if limiter is None:
        limiter = _LIMITERS[loop] = anyio.CapacityLimiter(DB_STEP_TOKENS)
    return limiter


async def db_step[T](fn: Callable[..., T], db: Session, /, *args: Any, **kwargs: Any) -> T:
    """`fn(db, *args, **kwargs)` in a worker thread, the event loop free
    meanwhile: on the held-step limiter when `db` holds its connection, in
    anyio's thread pool otherwise (see the module docstring)."""
    call = functools.partial(fn, db, *args, **kwargs)
    if holds_connection(db):
        return await anyio.to_thread.run_sync(call, limiter=held_step_limiter())
    return await anyio.to_thread.run_sync(call)
