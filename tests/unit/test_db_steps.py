"""`app.db_steps` — which threads web's async handlers run their database
steps in (#397, review round 1 of #405). An in-memory SQLite database; the
pool-full scenario on PostgreSQL is in
tests/integration/test_web_loop_steps.py."""

from __future__ import annotations

import asyncio

import anyio.to_thread
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import db_steps


def _session() -> Session:
    return Session(create_engine("sqlite://"))


def test_a_session_holds_its_connection_from_its_first_statement_to_its_commit() -> None:
    db = _session()
    try:
        assert not db_steps.holds_connection(db)
        db.execute(text("SELECT 99"))
        assert db_steps.holds_connection(db)
        db.commit()
        assert not db_steps.holds_connection(db)
        db.execute(text("SELECT 99"))
        db.rollback()
        assert not db_steps.holds_connection(db)
    finally:
        db.close()


async def test_a_step_runs_on_the_held_limiter_only_while_its_session_holds_a_connection() -> None:
    held = db_steps.held_step_limiter()
    default = anyio.to_thread.current_default_thread_limiter()

    def _borrowed(_db: Session) -> tuple[float, float]:
        return held.borrowed_tokens, default.borrowed_tokens

    db = _session()
    try:
        assert await db_steps.db_step(_borrowed, db) == (0, 1)
        await db_steps.db_step(Session.execute, db, text("SELECT 99"))
        assert await db_steps.db_step(_borrowed, db) == (1, 0)
        await db_steps.db_step(Session.commit, db)
        assert await db_steps.db_step(_borrowed, db) == (0, 1)
    finally:
        db.close()


def test_each_loop_has_its_own_limiter_with_one_token_more_than_the_pool() -> None:
    async def _limiter() -> object:
        await asyncio.sleep(0)
        limiter = db_steps.held_step_limiter()
        assert db_steps.held_step_limiter() is limiter
        return limiter

    first, second = asyncio.run(_limiter()), asyncio.run(_limiter())
    assert first is not second
    pool = create_engine("postgresql+psycopg://zz:zz@zz.invalid/zz").pool
    assert pool.size() + pool._max_overflow + 1 == db_steps.DB_STEP_TOKENS
