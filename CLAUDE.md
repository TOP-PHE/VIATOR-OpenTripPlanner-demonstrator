# CLAUDE.md — VIATOR journey-planner setup

Working notes for AI sessions resuming this project. Concise + structured.
Companion to: `README.md`, `VIATOR-strategy.md`, `VIATOR-technical-spec.md`, `docs/admin-guide.md`.

---

## 1. Objective + scope

**VIATOR** is a rail journey-planning **demonstrator** — not a production planner. It exists to **validate cross-border routing quality** by comparing the same OD query across multiple engines: VIATOR's own MOTIS/OTP, Swiss OJP reference, ÖBB HAFAS. Owner: TrackOnPath SAS (`patrick.heuguet@trackonpath.com`).

**Two main UIs:**
- **Search page** (`/journey`): one OD query → results from each enabled engine, optionally side-by-side comparison
- **Network coverage matrix** (`/admin/network-coverage`): N×N origin→destination grid across selected hubs, run as a batch job, with ÖBB heatmap overlay showing per-cell alignment

Operator-driven (no end-user surface). Multi-session: each MOTIS session = one country/region timetable (`eu19` for 19-country EU, `ch-multi`, `eu-rail`, `eu11`, `sp-rail`).

---

## 2. Architecture decisions (already made)

| Decision | Rationale | File:line / PR |
|---|---|---|
| Multi-MOTIS-session orchestrator | One docker container per session; sessions hot-swap on rebuild | `app/sessions_orchestrator.py:102-128` |
| FastAPI BackgroundTask for coverage runs (in web container, NOT worker) | Simpler than queue; `worker` only does GTFS rebuilds | `app/api/admin/network_coverage.py:652` |
| Cooperative cancel = in-memory `asyncio.Event` + DB-status check | SQL UPDATE alone wasn't reaching the runner; PR-186 added DB check | `app/network_coverage/runner.py:217, 234` (in-mem) + `_process_pair_with_cancel` (DB) |
| K-slot time-slicing (K=6 default, 4h slots) | Apples-to-apples cross-engine + avoids per-pair timeout cliffs | PR-3 (#184); knobs in `CONFIG_SCHEMA` |
| `platform_config` table for runtime tuning | Operator can tune 14 `COVERAGE_*` knobs without redeploy | `app/config_schema.py` + `/admin/config` UI |
| Country filter must be threaded through BOTH `create_run` AND `execute_run` | PR-187 fixed regression where filter was dropped at exec time | `runner.py:797` |
| Cross-engine itinerary matching via `transit_fingerprint` | DB-free, UIC-normalised, was built for OTP-vs-OJP federation | `app/journey/signature.py` |
| `first_transit_leg_departure_utc` is the canonical "trip departs" timestamp | Excludes walk-leg start so OTP/MOTIS (`startTime`=walk) align with HAFAS (=board time) | `app/journey/trip_normalize.py` (PR-3) |
| ÖBB HAFAS as journey comparison engine | Mirrors OJP pattern; reuses `external_verify.fetch_oebb_two_step` adapter | `app/journey/hafas_client.py` (PR-185 / #185) |
| ÖBB alignment heatmap (4 tiers + one-sided + no-data) | Replaces broken binary "disagrees" filter; viridis palette for accessibility | PR-196a (in flight) |

**MOTIS quirks operationally important:**
- MOTIS HTTP server can die silently while process stays alive → docker healthcheck uses `wget --spider` (PR-191 / #191)
- MOTIS doesn't notice client disconnect → orphans pile up CPU; httpx now sends `Connection: close` (PR-188 / #188)
- `docker compose restart` hangs on uvicorn graceful shutdown when BackgroundTasks in flight → use `docker kill` + `docker compose up -d`

---

## 3. OSCaR / OSDM conventions

- **Stop IDs**: UIC numeric code is canonical (`8503000` = Zürich HB). Adapters normalise via regex:
  - MOTIS form: `ScheduledStopPoint:8503000` or `feed:NNNNNNN`
  - HAFAS form: `A=1@L=8503000`
  - Canonical: `UIC:8503000`
  - Fallback: lat/lon rounded to ~110 m when no UIC available
- **Timezones**: IANA names everywhere (`Europe/Zurich`, never `CET`/`CEST`)
- **Time semantics**: trip "departs" at `first_transit_leg_departure_utc` (boarding time of the first non-walk/non-transfer leg)
- **Mode vocabulary**: upper-case (`WALK`, `RAIL`, `BUS`, `TRAM`, `SUBWAY`, `FERRY`, `COACH`)
- **Coverage filter "trains only"** actually means "excluding walk legs" — does NOT filter out bus/tram (PR-194 / #192 renamed the label to be honest)

---

## 4. Build / test / run

```bash
# Dev setup
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
pre-commit install && pre-commit install --hook-type pre-push

# Test loop
pytest                                              # unit + integration (integration skips without Postgres)
pytest tests/unit/test_coverage_*.py -v             # coverage subsystem
pytest tests/unit/test_hafas_client.py -v           # HAFAS adapter
ruff check . && ruff format --check .
pre-commit run --all-files

# Local stack
cd docker && docker compose -p viator up -d

# VPS deploy (after merge to main + tag push)
git tag -a v0.1.43.X -m "..." && git push origin v0.1.43.X
# Triggers docker.yml on `push: tags: ['v*']` → builds + publishes GHCR image
ssh viator@vps "cd /opt/viator/docker && docker compose -p viator pull web worker && docker compose -p viator up -d --force-recreate web worker"

# Recovery patterns (operational gotchas)
docker compose -p viator kill <container>          # force-kill when restart hangs
docker compose -p viator up -d <container>         # bring back fresh
docker logs -f viator-motis-<sid>-1 --tail 0       # watch MOTIS during cold start (90-180s for eu19)
```

---

## 5. Key files

```
app/
├── main.py                              FastAPI entry; orphan-run cleanup hook at startup
├── config_schema.py                     CONFIG_SCHEMA — 14 COVERAGE_* keys + OTP/OJP/HAFAS/fanout
├── config_service.py                    get_all(db) with 30s in-process cache
├── sessions_orchestrator.py             MOTIS docker container lifecycle (per-session)
├── api/
│   ├── admin/network_coverage.py        Coverage matrix API (runs, results, cell-trips, verify-external, stop)
│   ├── admin/config.py                  Platform config CRUD
│   └── journey.py                       /plan + /fanout (live UI); semaphores.journey gate (limit 20)
├── network_coverage/
│   ├── runner.py                        execute_run + cancel registry + K-slot fan-out
│   ├── external_verify.py               ÖBB HAFAS adapter (LocGeoPos→TripSearch two-step)
│   ├── alignment.py                     [PR-196a] Cross-engine alignment scorer (hybrid signature + fuzzy)
│   └── hubs.py                          Static fallback hub list (DB takes precedence)
├── journey/
│   ├── motis_client.py                  MOTIS /api/v6/plan adapter (Connection: close per PR-188)
│   ├── otp_client.py                    OTP GraphQL adapter
│   ├── ojp_client.py                    Swiss OJP 2.0 reference comparison
│   ├── hafas_client.py                  Journey-level ÖBB HAFAS wrapper (PR-185)
│   ├── signature.py                     transit_fingerprint (UIC-normalised cross-engine hash)
│   ├── trip_normalize.py                first_transit_leg_departure_utc
│   ├── planner_dispatch.py              Engine→client routing
│   └── federated_planner.py             Hub-stitched cross-NAP fallback
├── models/network_coverage.py           NetworkCoverageRun + NetworkCoverageResult (incl. external_*, alignment_*)
└── templates/
    ├── journey.html                     Search UI + side-by-side compare-grid (PR-194)
    └── admin/network_coverage.html      Coverage matrix UI + heatmap + cell modal
docker/                                  Compose + nginx + base Dockerfile
alembic/versions/                        Migrations (YYYYMMDD_HHMM_descriptor.py pattern)
tests/unit/                              ~150 unit tests, no DB needed
tests/integration/                       Integration tests (skip without Postgres)
```

---

## 6. What ships today (v0.1.43.24 release)

**Merged to main (13 PRs)**:
- #182 stop button + cancel endpoint
- #183 runner knobs in platform_config
- #184 K-slot time-slicing + per-run day window/TZ
- #185 ÖBB HAFAS journey comparison
- #186 runner honors DB status='cancelled' between pairs
- #187 country filter respected at execute time
- #188 httpx Connection: close (stops MOTIS orphan compute)
- #189 admin UI section for COVERAGE_* knobs
- #190 banner: start/duration/per-cell stats
- #191 MOTIS docker healthcheck (curl → wget)
- #192 honest "Compare trains only" label + side-by-side comparison columns
- #193 responsive admin layout (CSS Grid + clamp())

**In flight (workflow building)**:
- PR-196a (`feat/coverage-oebb-alignment-heatmap`) — ÖBB alignment heatmap + sweep verifies ALL cells (fixes white-matrix bug at root) + migration adding `external_itineraries JSONB`, `external_alignment_score FLOAT`, `external_alignment_tier VARCHAR`
- PR-196b (`feat/coverage-oebb-side-by-side-modal`) — side-by-side VIATOR/ÖBB cell-detail modal reusing PR-194 compare-grid

---

## 7. Next steps (priorities)

1. **Merge PR-196a + PR-196b** when they open (rebase if BEHIND main — standard dance)
2. **Cut v0.1.43.25** tag + deploy
3. **Run validation coverage** on eu19 with `verify_externally=true` + new heatmap visible
4. **Sweep cost grows ~3×** (verifying all cells, not just failures) — monitor ÖBB courtesy rate-limiting; may need throttle bump in `CONFIG_SCHEMA`
5. **Hub trim for eu19**: current 94 hubs × both directions = 8742 pairs, runs for hours. Recommendation: 3 hubs/country = 42 hubs = 1722 pairs = ~90-150 min
6. **PKP Intercity GTFS** (Polish national rail) not in eu19 — Warsaw missing from station typeahead. Needs auth FTP credentials per `docs/eu19-compliance-summary.md`
7. **Counter race** on `completed_pairs`: observed 1857/342 in a cancelled run, suggests multi-write race. Investigate before next major coverage feature
8. **OJP comparison** for HAFAS-style data is already shipped; can add more reference engines (DB Navigator, SBB CFF, SNCF) by mirroring `hafas_client.py` pattern

---

## 8. Recurring operational patterns

**Coverage run debugging recipe** (when matrix is stuck or hammering MOTIS):
```bash
# 1. Find the rogue process
docker stats --no-stream viator-motis-<sid>-1
docker exec viator-motis-<sid>-1 sh -c 'cat /proc/net/tcp | awk "\$2~/:1F90/" | head -10'

# 2. Cancel runs in DB
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "UPDATE network_coverage_runs SET status='cancelled', finished_at=NOW() WHERE status='running';"

# 3. Kill the rogue task (BackgroundTask lives in WEB, not worker)
docker compose -p viator kill web
docker compose -p viator up -d web

# 4. Verify MOTIS calm
docker logs viator-motis-<sid>-1 --tail 5 --since 30s  # should be empty
```

**Setting a coverage knob without admin UI** (psql fallback):
```sql
INSERT INTO platform_config (key, value) VALUES
  ('COVERAGE_PAIR_PARALLELISM', '2'),
  ('COVERAGE_SLOT_TIMEOUT_MS', '60000')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```
Runner reads config at `execute_run` start and freezes for that run's lifetime.

**Sonar coverage gate** is strict (≥80% on new code). Pattern that has bitten ~5 PRs this week: add tests for tiny utility helpers (regex parsers, format helpers) — Sonar counts them generously.
