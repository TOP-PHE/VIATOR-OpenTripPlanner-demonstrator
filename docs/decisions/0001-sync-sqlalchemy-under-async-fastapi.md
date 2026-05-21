# ADR 0001 — Sync SQLAlchemy under async FastAPI

| Field | Value |
|-------|-------|
| Status | **Accepted** (single-operator demonstrator phase) |
| Date | 2026-05-08 |
| Closes | audit-2026-05.md item #9 |
| Decision-makers | Operator: TOP-PHE; assisted by Claude Opus 4.7 |

## Context

VIATOR is a FastAPI application that wraps OpenTripPlanner with multi-session
isolation (per-session OTP containers, per-session graphs, per-session
configs). The web tier is one uvicorn process per `viator-web` container,
typically with `--workers 1` (the default in our compose setup) talking to
PostgreSQL via SQLAlchemy 2.0.

The current code is **mixed sync/async**:

- **Async**: FastAPI route handlers use `async def`, especially I/O-bound
  paths like the OTP HTTP client (`httpx.AsyncClient` at
  `app/journey/otp_client.py:131`), the NAP catalogue fetcher, and the
  outbound SMTP for magic-link emails.
- **Sync**: Database access is sync SQLAlchemy 2.0.36 with `psycopg`
  (sync) — every `session.execute()` blocks the calling worker. Per the
  audit-2026-05 survey, **219 sync `def`** vs **38 `async def`** across
  `app/`, ~82.7% sync.

The architectural risk surfaced in the audit (`docs/audit-2026-05.md` §2):

> Every DB-touching route blocks a worker thread. Under any meaningful
> concurrency, this is the throughput ceiling — not OTP, not network, but
> the Python event loop being held by `session.execute()`.

Specifically: when a sync function is called from an async FastAPI handler,
FastAPI offloads it to a default thread pool (`anyio`'s default executor,
typically 40 threads). So under heavy concurrent load:

1. ~40 simultaneous DB-touching requests can run in parallel
2. The 41st waits for a thread-pool slot
3. The event loop itself is fine (the thread pool is what's saturated)

This is **not** the catastrophic "single-thread blocked" failure mode some
docs warn about — but it IS a hard ceiling on concurrent DB operations
that scales as `min(thread-pool size, DB connection-pool size)` rather
than as `cpu-cores × event-loop work`.

## Forces at play

**Pro keeping sync**:

- Every existing line of DB code works as-is. No code-wide rewrite.
- SQLAlchemy 2.0's sync API is more mature, more documented, and has
  better tooling support (Alembic migrations are sync-native; `mypy`
  plugin support is more complete).
- Mixing sync + async correctly in tests is harder than just being sync.
- The 235-test suite, the 18 model files, the 12 API modules all currently
  use the sync API. Migration cost is non-trivial.
- This codebase is a **single-operator demonstrator** for a rail
  journey-planning research programme, not a high-concurrency production SaaS. Realistic concurrent
  user count: 1–5 admins, occasional public journey-search hits. The 40-
  thread ceiling is invisible at this scale.

**Pro migrating to async**:

- Eliminates the throughput ceiling at higher concurrency
- Aligns the whole stack on one paradigm (less context-switching for
  contributors)
- Async SQLAlchemy + asyncpg is the canonical FastAPI stack going forward
- New code (incl. the journey-search hot path) would benefit
- Some advanced async patterns (e.g. `asyncio.gather` over multiple OTP
  calls) become natural rather than awkward

**Pro a hybrid: keep most code sync, async-only the hot path**:

- The journey-search route is the single highest-RPS endpoint
- A targeted async DB layer for that one route would lift the ceiling
  where it matters most, with localised cost
- BUT: the route's DB queries currently pull from session, master_stations,
  journey_search, journey_trips — most of the schema. Localising async
  there is leaky: the same models must work in both sync and async
  contexts, which means doubling the model layer or using SQLAlchemy 2.x's
  async-compat models (which exist but are less battle-tested).

## Decision

**Keep sync SQLAlchemy under async FastAPI for the foreseeable future.**

Specifically:

- New DB code MUST use the existing sync session pattern. Don't sprinkle
  `AsyncSession` for individual hot paths — the half-and-half model is
  worse than either pure choice.
- New I/O code (HTTP, file, subprocess) SHOULD continue to use async
  where it's natural. The mixed model is OK for non-DB I/O.
- The `Depends(get_db)` pattern stays. SQLAlchemy `Session` instances
  remain request-scoped via the existing FastAPI dependency.

We accept the throughput ceiling because:

1. **Realistic concurrency is well below the ceiling.** A 1–5-admin
   demonstrator with occasional public traffic doesn't approach 40 in-
   flight DB ops.
2. **Migration cost is concentrated, not spread out.** When we do migrate,
   it should be a focused effort with a benchmark before/after, not
   incremental piecemeal that leaves us in the worst-of-both-worlds state.
3. **The bottleneck for journey latency is OTP, not the DB.** Per
   `admin-guide.md` §6.7, observed timeouts are graph-load and routing
   compute, not query time.

## Consequences

### What this makes easier

- **Continued contributions** without paradigm-shift cost. Every line of
  existing DB code remains a valid template.
- **Alembic migrations** stay clean — sync API is what `alembic upgrade
  head` already runs.
- **Test isolation** — sync `Session` rollback in tests is straightforward;
  the existing 235-test suite pattern continues to work.
- **Mypy / ruff strict** stays green — async SQLAlchemy's type stubs are
  newer and would have surfaced typing issues we'd need to triage.

### What this makes harder

- **Scaling beyond ~40 concurrent DB requests** requires either horizontal
  scaling (more uvicorn workers / more containers) or migration to async
  DB.
- **Latency under load** has a non-obvious cliff: from "fine" to "thread-
  pool saturated and queueing" once concurrent DB ops > ~40.
- **Some elegant async patterns** are awkward — e.g. concurrent NAP fetch
  + DB write requires manual thread offload rather than `asyncio.gather`.

### What we're NOT giving up

- **OTP latency**: stays async (httpx.AsyncClient). No change.
- **Outbound HTTP**: stays async. No change.
- **WebSocket support** (if added later): not blocked by this decision —
  WebSockets don't typically issue per-message DB calls.

## Trigger to revisit

Any **one** of these signals should prompt re-opening this ADR:

1. **Concurrent users > 25** sustained over a 5-minute window (operator
   sees this in admin dashboard or via metrics — see audit #14).
2. **p95 latency on `/admin/sessions`** (or any other admin route)
   **> 2 seconds**, traced to thread-pool saturation rather than DB query
   slowness or network. The audit's planned Prometheus instrumentation
   (#14) + OTel tracing (#19) would surface this directly.
3. **`anyio` thread-pool depleted** (`asyncio` debug logs show thread-pool
   queue depth > 0 for >30 s). Diagnostic via `py-spy dump` or similar.
4. **Operator complaint**: "the admin UI is slow when I'm doing X" with
   X being a workflow that involves multiple parallel DB-touching tabs.
5. **Architectural shift**: VIATOR moves out of the demonstrator phase
   to a multi-tenant production deployment. At that point, the trade-off
   reverses regardless of measured perf.

When any of these fire, write **ADR 0002 — async DB migration plan**
that supersedes this one. Reference both ADRs in the audit doc tracker
(`docs/audit-2026-05.md` items #9 + the new one).

## Migration sketch (for future reference, not action)

If/when we decide to migrate, the recommended path:

1. **Bench baseline** — measure current concurrent-DB throughput with k6
   or locust against journey-search and admin-sessions. (Audit #20 sets
   this up.)
2. **Switch the engine** — `create_async_engine` with `asyncpg` driver.
   Update `app/db.py` to expose `AsyncSession` factory.
3. **Convert dependencies** — `get_db()` → `async def get_db()` returning
   `AsyncSession`.
4. **Rewrite call sites** — `db.execute(...)` → `await db.execute(...)`.
   This is mechanical but spans most of `app/api/`. Ruff has a transformer.
5. **Update tests** — `pytest-asyncio` already in deps; convert sync test
   fixtures to async.
6. **Re-bench** — confirm the migration actually moves the throughput
   number, otherwise we're done a lot of work for no gain.
7. **Update mypy/sqlalchemy stubs** — async return types differ; the
   `cast(CursorResult[Any], ...)` pattern from audit #23 may need
   revisiting.

Estimate: 3–5 focused days of work, plus testing. Not free, but bounded
and well-understood.
