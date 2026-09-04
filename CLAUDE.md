# CLAUDE.md — VIATOR journey-planner setup

Working notes for AI sessions resuming this project. Concise + structured.
Companion to: `README.md`, `VIATOR-strategy.md`, `VIATOR-technical-spec.md`, `docs/admin-guide.md`.

**Start with `docs/architecture.md` (#242)** if you are new to the code — chapter 2 is the map
(module map, runtime topology, the three principal flows) and every chapter ends with *Invariants &
traps*. This file is the operator's log: state, incidents and recipes. That one is the reference.

Last updated: 2026-09-04. §2/§3/§5/§7 curated through #245; #206–#227 triaged in the same pass.

**Read §7 items 1 and 2 before writing any coverage code.** `main` currently ships one PR whose own
title says DO NOT MERGE, and endpoint-UIC matching has never worked in production.

---

## 1. Objective + scope

**VIATOR** is a rail journey-planning **demonstrator** — not a production planner. It exists to **validate cross-border routing quality** by comparing the same OD query across multiple engines: VIATOR's own MOTIS/OTP, Swiss OJP reference, ÖBB HAFAS. Owner: TrackOnPath SAS (`patrick.heuguet@trackonpath.com`).

**Two main UIs:**
- **Search page** (`/journey`): one OD query → results from each enabled engine, optionally side-by-side comparison, with an honest "excluding walk legs" toggle and per-engine ÖBB/OJP reference panels
- **Network coverage matrix** (`/admin/network-coverage`): N×N origin→destination grid across selected hubs, run as a batch job, with a viridis ÖBB-alignment heatmap overlay and a click-cell VIATOR/ÖBB side-by-side detail modal. A "Download HTML" export produces a self-contained offline report mirroring the same heatmap.

Operator-driven (no end-user surface). Multi-session: each MOTIS/OTP session = one country/region timetable (`eu19` = 19-country EU via MOTIS, `ch-multi`, `eu-rail`, `eu11`, `sp-rail` MOTIS; `nap-fr-rail`, `nap-de-rail`, `nap-ch-rail`, `nap-sp-rail`, `nap-eu-corridors` OTP).

---

## 2. Architecture decisions (already made)

| Decision | Rationale | File:line / PR |
|---|---|---|
| Multi-MOTIS/OTP-session orchestrator | One docker container per session; sessions hot-swap on rebuild | `app/sessions_orchestrator.py` |
| FastAPI BackgroundTask for coverage runs (in web container, NOT worker) | Simpler than queue; `worker` only does GTFS rebuilds | `app/api/admin/network_coverage.py` |
| Cooperative cancel = in-memory `asyncio.Event` + DB-status check | SQL UPDATE alone wasn't reaching the runner; PR-186 added DB check | `app/network_coverage/runner.py` |
| K-slot time-slicing (K=6 default, 4h slots) | Apples-to-apples cross-engine + avoids per-pair timeout cliffs | PR-3 (#184); knobs in `CONFIG_SCHEMA` |
| `platform_config` table for runtime tuning | Operator can tune 14 `COVERAGE_*` knobs without redeploy | `app/config_schema.py` + `/admin/config` UI |
| Country filter must be threaded through BOTH `create_run` AND `execute_run` | PR-187 fixed regression where filter was dropped at exec time | `runner.py` |
| Cross-engine itinerary matching via `transit_fingerprint` | DB-free, UIC-normalised, was built for OTP-vs-OJP federation | `app/journey/signature.py` |
| `first_transit_leg_departure_utc` is the canonical "trip departs" timestamp | Excludes walk-leg start so OTP/MOTIS (`startTime`=walk) align with HAFAS (=board time) | `app/journey/trip_normalize.py` (PR-3) |
| ÖBB HAFAS as journey comparison engine | Mirrors OJP pattern; reuses `external_verify.fetch_oebb_two_step` adapter | `app/journey/hafas_client.py` (PR-185 / #185) |
| ÖBB alignment heatmap (9 tiers incl. one-sided + no-data) | Replaces broken binary "disagrees" filter (white-matrix bug: PR-E only verified failure cells, so `status='ok'` cells had NULL `external_ok` and were all hidden). Viridis palette, WCAG-AA contrast | PR-195 (#195, "PR-196a") |
| Sweep verifies EVERY non-skipped cell (was failures-only) | Root fix for the white-matrix bug above; sweep cost grows ~12× | `runner.py::_maybe_run_external_verify_sweep` |
| Cross-engine alignment scorer: exact `transit_fingerprint` match (1.0) + train-number-guarded ±5min fuzzy fallback (0.7), both behind an endpoint-UIC equality guard, both preceded by a naive→Europe/Vienna conversion of the ÖBB side | The fuzzy tier avoids false-positives on high-frequency corridors (same endpoints/minute, different trains). **Two preconditions decide whether either tier ever fires.** (1) `_oebb_naive_to_utc_iso` must run first (#220) or the ÖBB side is a whole CET/CEST offset out and nothing matches. (2) `_is_fuzzy_candidate` requires both spines to share `(first_UIC, last_UIC)` and returns False *there*, before the train number is read — and `extract_uic` does not return UICs (see §3), **so the guard rejects essentially everything on main today.** #226 would have removed it; it was closed unmerged (§7 item 2) | `alignment.py` `_endpoint_uics` / `_first_train_number` / `_is_fuzzy_candidate` (~:194–257) |
| **One clock per coverage run**: `run.depart_at` is an instant anchored in that run's `window_timezone`; `reference_date` is DERIVED from it and never defaulted; ÖBB's persisted `dep_utc`/`arr_utc` are naive **Europe/Vienna** wall-clock strings that must be localised at every boundary | Before #224 `reference_date` defaulted to "tomorrow at run-create time" while every comparison anchored on `depart_at` — the K-slot grid searched one calendar day and the ÖBB sweep queried another. **Every `external_alignment_score` persisted before 2026-07-08 is structurally invalid.** #220 fixed the other half: the `_utc` suffix is aspirational (`_hafas_time_to_utc_iso` skips the conversion deliberately, for hot-loop simplicity), so both tiers saw a whole CET/CEST offset. #225 is the recovery PR — redefining what `depart_at` *means* broke four consumers that had been round-tripping the typed wall clock by accident | `runner.py::_anchor_reference_date`, `alignment.py::_oebb_naive_to_utc_iso`, `external_verify.py::_to_oebb_local`, `api/admin/network_coverage.py::_depart_at_local_iso` — #220, #224, #225 |
| **Both engines must be asked about the same time range** — and the fix runs in OPPOSITE directions per surface: coverage narrows VIATOR down to ÖBB, `/journey` widens ÖBB up to VIATOR | ÖBB HAFAS `TripSearch` is hardcoded `numF=5` — "the next 5 connections from one anchor", NOT a time span — while a coverage cell's VIATOR list spans the whole K-slot day. Comparing them scored `No overlap 0.00` on cells that visibly agreed. Paginating ÖBB across all K slots in the sweep was considered and REJECTED (high cost, low value), so on coverage the filter is the contract; `/journey` has one pair so pagination is affordable, but HAFAS's window is fixed before the concurrent fanout finishes, so it overshoots and is clipped afterwards. The anchor-advance + dedup loop lives ONCE in `trip_normalize`, shared by `ojp_client` and `hafas_client` — two copies had already drifted | `alignment.py::filter_trips_from_depart_at`, `hafas_client.py::fetch_plan_paginated`, `trip_normalize.py`, `api/journey.py::_truncate_hafas_to_viator_window` — #221, #223 |
| SBS paired grid keys on `first_transit_leg_departure_utc`, and dispatches ONLY when ÖBB HAFAS is the sole reference column | The one field every engine client computes on a consistent UTC basis — the canonical-departure decision extended into the UI. Any OJP-involved layout keeps the original independent-columns `CompareGrid.renderGrid` path. Nothing is dropped: unmatched trips keep a row with a "— not found by …" placeholder, and a matched row whose sources disagree on train identifier despite sharing departure AND arrival gets a "very likely the same physical service" warning | `templates/journey.html::renderViatorOebbPairedGrid` — #222 |
| **Unauthenticated share link `/share/coverage/{run_id}` on its OWN router** (`app/api/coverage_share.py`), never under `/api/admin/*` | The run id IS the capability token — `gen_random_uuid()`, 128 bits — and this router deliberately has no listing route to discover one. "Unlisted, not secret", matching the data's real sensitivity. Own-router placement makes the missing auth structural: it cannot inherit `require_platform_admin` by accident, and an admin-wide auth change cannot silently break it. The 60/min limit is anti-scraping, NOT anti-guessing. **Never add a listing route.** Governing invariant: the public endpoint reveals only what the share page renders — #215 stripped `external_itineraries`, #216 made the page render the side-by-side and removed the strip. **Do not "re-fix" that**; the strip is still in git history with a security rationale and reads as a regression | `app/api/coverage_share.py` (rationale in the module docstring); guards in `tests/unit/test_coverage_share.py` — #211, #216 |
| Share page and downloaded export are two deliberately DIFFERENT tiers: the share page embeds no trip detail (`lazy_trips`, fetched per cell at 120/min); the download embeds full detail only up to `_EXPORT_LEG_DETAIL_MAX_PAIRS = 500` rows, then keeps summaries and drops legs | An 8742-pair run pegged `web` at 128% CPU / 13.75 GB and never returned. Legs are ~1.7 KB/trip and ~90% of report bytes, so #214's per-cell trip cap still left a ~150 MB page: nginx's proxy timeout cuts it ("cannot download") and no browser opens it ("cannot open"). Lazy = a constant few-MB page at ANY run size. The no-legs query projects explicit columns so ~372 MB of legs JSON never leaves Postgres. Lazy-fetch bookkeeping lives in a `Map` keyed by `pairKey` and must never be written onto the shared `CELLS` objects — the raw-JSON panel serialises them verbatim | `api/admin/network_coverage.py`, `templates/admin/network_coverage_export.html` — #214, #215 |
| Coverage fetches retry ONLY connection-level errors (`httpx.ConnectError`, `RemoteProtocolError`) with 5s/15s/40s backoff; MOTIS healthcheck probe budget widened to `wget --timeout=15` / docker `timeout: 20s`, `interval`/`retries` untouched | A bounced session takes 90–180 s to cold-boot and every pair scheduled in that window used to persist as a wrong `'error'` cell — one transient bounce became dozens of misleading matrix cells. A bounce landing MID-request surfaces as `RemoteProtocolError`, not `ConnectError` (#209). Timeouts, HTTP errors and bad shapes must NOT be retried — a genuinely broken pair should report fast. Widening only the probe budget stops "briefly busy with real work" reading as "hung" without softening zombie detection | `runner.py::_call_with_connect_retry`, `sessions_orchestrator.py::_MOTIS_SVC_TEMPLATE` — #207, #209 |
| Coverage runs are HARD-deleted; hubs stay SOFT-deleted; delete returns 409 while `status=='running'` | A run is referenced by nothing (`NetworkCoverageResult.run_id` is `ondelete=CASCADE`), so one clean DELETE; a hub is referenced by many historical runs' `hub_id` strings with no FK, so it can only be deactivated. The 409 is not tidiness — deleting an in-flight run races the BackgroundTask about to write terminal-state fields onto that row | `api/admin/network_coverage.py::delete_run` — #206 |
| Country band hues confined to 160–345° (teal→magenta), fixed alphabetical country→hue map, duplicated in Python AND JS on purpose | The `ok`/`no_route`/`error` status colours and the viridis heatmap occupy the red-orange-green band this range skips, so no country colour can be misread as a cell status. The live matrix renders client-side from `HUBS`, the export is server-rendered Jinja, and they share no runtime — the same tradeoff already accepted for the viridis palettes (§7). The colspan merge assumes hubs arrive sorted by `(country, sort_order, id)`; re-sort and each band silently fragments into stripes | `_COUNTRY_HUES` in `api/admin/network_coverage.py` + `COUNTRY_HUES` in `templates/admin/network_coverage.html` — #212 |
| In BOTH matrix templates, `table-layout: fixed` + explicit `<colgroup>` + `width: max-content` are load-bearing, and every sticky band carries an explicit `z-index` | Sticky columns' `left` offsets are `calc()`'d from DECLARED widths, so anything letting a column render narrower opens a gap that scrolled data shows through. `table-layout: auto` shrinks a column below its declared width when every cell is narrower (the type band is one `?` glyph); `fixed` with `width: auto` proportionally shrinks them back to fit and recreates the identical gap. A rowspan'd sticky `<th>` with no explicit `z-index` loses to plain `<td>`s from later rows in Chromium. Trip-wire tests pin all three — if one fails, do not "simplify" it away | both matrix templates, `tests/unit/test_coverage_country_bands.py` — #217 |
| Shared `CompareGrid` JS/CSS primitive (`app/static/{css,js}/compare_grid.js`) | One source of truth for the N-column side-by-side layout, used by both `/journey` and the coverage cell modal | PR-197 (#197, "PR-196b") |
| Side-by-side VIATOR column label derived from `payload.executions[*].engine`, not hardcoded | Was showing "VIATOR · MOTIS / OTP" even when Engine=MOTIS-only was selected | PR-198 (#198) |
| "Compare excluding walk legs" toggle lives on the SBS wrapper itself, not only inside the OTP+MOTIS comparison grid | Toggle vanished entirely when only one engine ran (its old only-host required both engines) | PR-198 (#198) |
| willfarrell/autoheal watchdog, opt-in via `viator.autoheal="true"` label | Docker doesn't auto-restart on `(unhealthy)` alone; the eu19-transit-motis incident (below) ran undetected for ~10h | PR-199 (#199) |
| Coverage cell modal "Re-run" link coerces coords via `Number.isFinite`, gates the whole link on all 4 non-null | A stale/null hub coord produced `from_lat=undefined` in the URL, which journey.html's `setPair()` treats as truthy → `parseFloat("undefined")` → NaN at submit → "search does nothing" | PR-200 (#200) |
| Offline HTML export mirrors the live matrix's alignment heatmap, opt-in toggle, all CSS inlined | Export is `Content-Disposition: attachment`, zero external assets, must stay viewable offline forever | PR-201 (#201) |
| Coverage cell modal "Re-run" link also passes `&from_uic=&to_uic=` | Prep for the UIC backfill (Scope B, not yet built); safe no-op today since coverage hubs carry no UIC column | PR-202 (#202) |
| `AutohealExcessiveRestarts` Prometheus alert (`>3 restarts/hour`) + cadvisor `viator.autoheal` label-whitelist fix | Autoheal restarting silently forever would mask a *recurring* problem exactly as it did in the 07-01 slot-window incident (below) — restarts alone aren't a fix if the same container keeps flipping unhealthy | PR-203 (#203) |
| **Dockerfile linting lives ONLY in `docker.yml`'s `hadolint` job** — the pre-commit `hadolint-docker` hook is gone | It ran the same linter twice. The pre-commit copy's entry carried no image tag so it resolved `:latest` and drifted (`rev:` pins the hook definition, never the image; `v2.13.1-beta` has no image tag at all). Worse, `--all-files` linted Dockerfiles on **every** PR, reintroducing the docs-only stall `docker-gate` exists to prevent. The action is SHA-pinned and Dependabot-managed, so it stays current *and* each bump is a reviewable PR. Also means `pre-commit` no longer needs a local Docker daemon | #244 (after #243's stop-gap pin) |
| **Trivy `skip-files` excludes embedded SBOMs** — `**/pip/_vendor/bom.cdx.json`, `**/dist-info/sboms/**` | Trivy treats *any* embedded SBOM as authoritative for the whole language ecosystem and stops walking the filesystem. pip's vendored manifest made it report phantom HIGHs **and ignore the ~100 packages actually installed** — a real CVE would have gone unreported. See §8 recipe | #245 |
| Scan image tagged `:scan-${{ github.run_id }}`; buildx `provenance`/`sbom` attestations off for the scan build | Both were diagnostics that did **not** fix #245's mismatch. Kept as defence in depth: a unique tag can't collide with a cached result, and an attestation is a second route to the same embedded-SBOM bug | #245 |

**MOTIS quirks operationally important:**
- MOTIS HTTP server can die silently while process stays alive → docker healthcheck uses `wget --spider` (PR-191 / #191)
- MOTIS doesn't notice client disconnect → orphans pile up CPU; httpx now sends `Connection: close` (PR-188 / #188)
- `docker compose restart` hangs on uvicorn graceful shutdown when BackgroundTasks in flight → use `docker kill` + `docker compose up -d`
- **Silent-death + no auto-restart**: on 2026-06-30/07-01, `motis-eu19-transit-motis` sat `(unhealthy)` at 99% CPU for ~10 hours (healthcheck correctly flagged it, but nothing acted — docker doesn't auto-restart on unhealthy, that's a k8s liveness-probe feature, not a plain-docker one). PR-199's autoheal watchdog is the fix; PR-203 (open) adds a Prometheus alert (`AutohealExcessiveRestarts`, >3 restarts/hour) so a *recurring* unhealthy condition pages someone instead of silently auto-recovering forever. **No Alertmanager/Grafana contact point exists yet** — the alert fires and is visible in the UI but nobody gets paged externally until a notification channel (SMTP/webhook) is configured.
- **Full stack recovery recipe** when things look broken after a VPS reboot: `docker compose -p viator down && docker compose -p viator up -d` — recreates the docker network cleanly (fixes a `postgres` DNS-resolution failure observed once after an unclean host reboot) and re-runs the sessions-orchestrator regen on `web` boot, so newly-templated `viator.autoheal` labels land on MOTIS/OTP containers that predate PR-199.
- **Autoheal restarting ≠ autoheal fixing — a second, different-cause incident (2026-07-01)**: mid-sweep on a fresh eu19 coverage run, `viator-autoheal-1` restarted `motis-eu19-transit-motis` **8 times in ~43 minutes** (every ~6 min). This time MOTIS was NOT a zombie — `platform_config.COVERAGE_SLOT_COUNT` was stuck at `2` (a stale manual override from earlier incident tuning) instead of the code default `6`, widening each K-slot RAPTOR query to a 12h search window instead of the documented-safe 4h (`runner.py` comments: RAPTOR cost scales near-quadratically with window size). Even just ~2 concurrent 12h-window queries pegged MOTIS at 199.91% CPU, starving its own `GET /` healthcheck → flagged unhealthy → autoheal restarted it → repeat every ~6 min, each restart wiping in-flight pairs (the coverage matrix showed scattered fully-red origin rows interleaved with rows that had real durations — not a clean single-point crash). Fix: `DELETE FROM platform_config WHERE key='COVERAGE_SLOT_COUNT';` to restore the default, then re-run (the polluted run's error cells aren't real `no_route` findings). **Two gotchas that cost debugging time**: (1) the autoheal container is named `viator-autoheal-1` (compose v2 `<project>-<service>-<replica>`, no `container_name:` override) — not bare `autoheal`; (2) `docker inspect --format='{{.RestartCount}}'` stays `0` for autoheal-triggered restarts because autoheal calls the Docker API directly rather than going through the container's own `restart:` policy — don't let `restarts=0` next to an obviously-fresh `Up 11 seconds` fool you into thinking nothing restarted it.

---

## 3. OSCaR / OSDM conventions

- **Stop IDs**: UIC numeric code is canonical (`8503000` = Zürich HB). Adapters normalise via regex:
  - MOTIS form: `ScheduledStopPoint:8503000` or `feed:NNNNNNN`
  - HAFAS form: `A=1@L=8503000` — **but `external_verify.extract_uic` does not actually extract this.**
    Its regex is `(?<!\d)(\d{7,8})(?!\d)` used with `.search()`, i.e. the *first standalone 7–8 digit
    run anywhere in the string*. A real HAFAS lid carries the coordinates first:
    `A=1@O=Wien Hbf@X=16375326@Y=48185507@U=81@L=008100002@B=1@` → it returns **`16375326`**, the
    longitude in micro-degrees. (And `L=008100002` is 9 digits with leading zeros, so it would not
    match even if reached.) **This has never worked in production, in any country** — not a
    cross-border-only problem. Every unit fixture uses a stripped lid (`A=1@L=8000207@`) with no
    `X=`/`Y=`, which is exactly why the suite is green. Consequence: the alignment matcher's
    endpoint-UIC guard compares two longitudes and rejects essentially every pair. §7 item 2
  - Canonical: `UIC:8503000`
  - Fallback: lat/lon rounded to ~110 m when no UIC available
  - **Coverage hubs (`network_coverage_hubs` table) carry NO UIC column today** — no FK/join to `master_stations`. The Re-run link's `&from_uic=&to_uic=` (PR-202, merged) is wired but always resolves to empty string until a follow-up adds the column + backfill.
- **Timezones**: IANA names everywhere (`Europe/Zurich`, never `CET`/`CEST`)
- **One timezone per coverage run**: `run.depart_at` is an instant anchored in that run's
  `window_timezone`, and `reference_date` is derived from it. `depart_at` is `timestamptz` so psycopg
  hands back UTC — every UI surface goes through `_depart_at_local_iso`, and the ÖBB wire through
  `external_verify._to_oebb_local` (HAFAS reads `outDate`/`outTime` as Europe/Vienna local)
- **`VerifyLeg.dep_utc` / `VerifyItinerary.arr_utc` ARE NOT UTC.** They are naive Europe/Vienna
  wall-clock strings; the `_utc` suffix is aspirational and `external_verify._hafas_time_to_utc_iso`
  skips the conversion on purpose. Anything comparing them to a VIATOR timestamp must localise first
  (`alignment._oebb_naive_to_utc_iso`). Unit tests do NOT protect this: fixtures build both sides with
  the same naive-looking timestamp, while production is asymmetric — only the ÖBB side is naive
- **The canonical non-transit predicate is `{WALK, TRANSFER, ''}`**, not `mode !== 'WALK'`. A bare
  WALK check leaves a MOTIS `TRANSFER` leg or a `mode: null` section VISIBLE while excluding it from
  any recompute — a divergence #222 already had to fix once in `journey.html`
- **Time semantics**: trip "departs" at `first_transit_leg_departure_utc` (boarding time of the first non-walk/non-transfer leg)
- **Mode vocabulary**: upper-case (`WALK`, `RAIL`, `BUS`, `TRAM`, `SUBWAY`, `FERRY`, `COACH`)
- **Coverage filter "trains only"** actually means "excluding walk legs" — does NOT filter out bus/tram (PR-194 / #192 renamed the label to be honest)
- **Alignment tier vocabulary** (9 values, `Literal` at the API boundary in `app/api/admin/network_coverage.py`): `agree` (1.00) / `mostly_agree` (≥0.70) / `partial` (≥0.40) / `disagree` (>0) / `no_overlap` (0.0, both sides non-empty) / `one_sided_viator` / `one_sided_oebb` / `no_service` (both empty) / `no_data` (never scored — legacy row or sweep skipped it)

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

# Full-stack clean recovery (VPS reboot, or "everything looks broken")
docker compose -p viator down && docker compose -p viator up -d
# Recreates the network + regenerates sessions-orchestrator fragments (backfills
# viator.autoheal labels onto pre-PR-199 MOTIS/OTP containers for free).
```

---

## 5. Key files

```
app/
├── main.py                              FastAPI entry; orphan-run cleanup hook + /static/app mount (compare_grid CSS/JS)
├── config_schema.py                     CONFIG_SCHEMA — 14 COVERAGE_* keys + OTP/OJP/HAFAS/fanout
├── config_service.py                    get_all(db) with 30s in-process cache
├── sessions_orchestrator.py             MOTIS/OTP docker container lifecycle (per-session); both templates carry viator.autoheal="true"
├── api/
│   ├── admin/network_coverage.py        Coverage matrix API (runs, results, cell-trips, verify-external, export,
│   │                                    stop, delete). Also hosts _build_export_context / _build_cell_trips_response /
│   │                                    _fetch_trips_by_search, which the UNAUTHENTICATED share router imports —
│   │                                    edits here have a public blast radius. Size guards
│   │                                    _MAX_TRIPS_PER_CELL_EXPORT (#214), _EXPORT_LEG_DETAIL_MAX_PAIRS (#215);
│   │                                    depart_at semantics _depart_at_local_iso (#225); _COUNTRY_HUES (#212)
│   ├── coverage_share.py                **PUBLIC, NO AUTH**: GET /share/coverage/{run_id} + per-cell trips.
│   │                                    Its own router on purpose — see §2. Rationale in the module docstring
│   ├── admin/config.py                  Platform config CRUD
│   └── journey.py                       /plan + /fanout (live UI); semaphores.journey gate (limit 20);
│                                        _truncate_hafas_to_viator_window clips ÖBB pagination back (#223)
├── network_coverage/
│   ├── runner.py                        execute_run + cancel registry + K-slot fan-out + alignment persistence
│   ├── external_verify.py               ÖBB HAFAS adapter (LocGeoPos→TripSearch two-step) + VerifyItinerary/VerifyLeg
│   │                                    + _to_oebb_local. THREE traps: extract_uic returns a longitude, not a UIC
│   │                                    (§3); _hafas_time_to_utc_iso deliberately does NOT convert; _hafas_cat_to_mode
│   │                                    is a deliberate twin of hafas_client's — fix both or neither. And one absence:
│   │                                    VerifyLeg carries NO per-leg duration_seconds (root cause under #227)
│   ├── alignment.py                     Cross-engine alignment scorer (exact fingerprint + train-guarded fuzzy,
│   │                                    behind an endpoint-UIC guard) + _oebb_naive_to_utc_iso (#220)
│   │                                    + filter_trips_from_depart_at (#221)
│   └── hubs.py                          Static fallback hub list (DB takes precedence); no UIC field yet
├── journey/
│   ├── motis_client.py                  MOTIS /api/v6/plan adapter (Connection: close per PR-188)
│   ├── otp_client.py                    OTP GraphQL adapter
│   ├── ojp_client.py                    Swiss OJP 2.0 reference comparison
│   ├── hafas_client.py                  Journey-level ÖBB HAFAS wrapper (PR-185); fetch_plan_paginated (#223);
│   │                                    _localise_when (the journey twin of external_verify._to_oebb_local);
│   │                                    _map_cat_to_mode (deliberate twin — see external_verify.py)
│   ├── signature.py                     transit_fingerprint (UIC-normalised cross-engine hash)
│   ├── trip_normalize.py                first_transit_leg_departure_utc + the SHARED reference-engine pagination
│   │                                    helpers dedup_batch_and_track_latest_dep / next_anchor_or_none, used by
│   │                                    BOTH ojp_client and hafas_client (#223) — the reuse surface for a 3rd engine
│   ├── planner_dispatch.py              Engine→client routing
│   └── federated_planner.py             Hub-stitched cross-NAP fallback
├── models/network_coverage.py           NetworkCoverageRun (depart_at is timestamptz — psycopg returns UTC) +
│                                        NetworkCoverageResult (incl. external_*, alignment_*) +
│                                        NetworkCoverageHub (modes String(20) nullable, #212; still NO uic column)
├── static/
│   ├── css/compare_grid.css             Shared N-column grid CSS + alignment-tier-pill palette (modal variant)
│   └── js/compare_grid.js               window.CompareGrid.{renderGrid, tierPill, escHTML}
└── templates/
    ├── journey.html                     Search UI + side-by-side compare-grid; URL-param prefill (from_lat/lon/name/uic, depart_at)
    └── admin/
        ├── network_coverage.html        Coverage matrix UI + heatmap + country/type header bands (#212) +
        │                                VIATOR/ÖBB side-by-side cell modal + Re-run link. The `.cov-matrix` CSS
        │                                block's fixed layout / colgroup / z-index are load-bearing (#217, §2).
        │                                Lines ~1786–2238 are #227's REJECTED recompute — read §7 item 1 first
        └── network_coverage_export.html Dual-purpose: the downloadable report AND the share page. Self-contained
                                         only in the non-lazy, ≤500-row mode (#215)
docker/
├── docker-compose.yml                   Main stack incl. autoheal service (opt-in via viator.autoheal label)
└── prometheus/
    ├── prometheus.yml                   Scrape config + rule_files stanza (added by #203)
    └── rules/autoheal.yml               AutohealExcessiveRestarts alert (PR-203, merged; no notification channel wired yet)
alembic/versions/                        Migrations (YYYYMMDD_HHMM_descriptor.py pattern). **Revision id must be
                                         ≤32 chars** — alembic_version.version_num is varchar(32) and the house
                                         prefix burns 14, leaving 18 for the descriptor. Over that, the file
                                         imports fine and `alembic history` looks fine; it fails at upgrade time
                                         with StringDataRightTruncation (#212)
tests/unit/                              ~200 unit tests, no DB needed
tests/integration/                       Integration tests (skip without Postgres)
docs/                                    (#242) Reference documentation set
├── architecture.md                      12 chapters, one per module cluster, each ending "Invariants & traps".
│                                        Ch.2 is the map (module map, topology, the 3 principal flows);
│                                        ch.12 is the PROPOSED OJP API — plan, not as-built
├── user-guide.md                        Task-oriented, no dev experience assumed
├── diagrams/                            6 diagrams, .svg (renders on GitHub) + .png (embedded in Word)
├── brand/                               TrackOnPath logo + the symbol-only mark used in page footers
├── VIATOR-*.docx                        Word builds: title page, copyright/licence page, per-page footer
└── _build/                              Regenerates all of the above. Node only — no pandoc, no LibreOffice.
                                         `npm install docx sharp` then build-diagrams.js / md2docx.js.
                                         READ _build/README.md before touching it: the .docx must stay
                                         under 1024 KB for check-added-large-files, which is why the
                                         diagram PNGs are palette-quantised
```

---

## 6. What ships today

**Live on VPS: v0.1.43.28** (tag pushed + deployed 2026-07-01).

**Merged to main since the last update (12 PRs, #194→#203)**:
- #194 CLAUDE.md (this file, first version)
- #195 ÖBB alignment heatmap + sweep verifies ALL cells ("PR-196a")
- #196 hotfix: TDZ ReferenceError broke the journey-search submit button entirely (v0.1.43.25 regression from #192 — a top-level IIFE read a `let`/`const` declared ~700 lines below it)
- #197 side-by-side VIATOR/ÖBB cell-detail modal ("PR-196b") + shared `CompareGrid` primitive
- #198 hotfix: SBS column label hardcoded "VIATOR · MOTIS / OTP" regardless of engine filter + walk-toggle vanished in single-engine SBS
- #199 willfarrell/autoheal watchdog (opt-in label, no self-heal loop, docker socket `:ro`)
- #200 hotfix: coverage modal Re-run link leaked `from_lat=undefined` on stale/null hub coords → journey search appeared to "do nothing"
- #201 offline HTML export renders the alignment heatmap (was PR-E's binary legend only)
- #202 wires `&from_uic=&to_uic=` into the Re-run link (Scope A of the UIC backfill); safe no-op today since coverage hubs carry no UIC column yet
- #203 `AutohealExcessiveRestarts` Prometheus alert + cadvisor `viator.autoheal` label-whitelist fix; **still no Alertmanager/Grafana contact point configured**, so nobody is paged externally yet

**Merged 2026-09-04 (4 PRs, #242→#245)** — a docs set, and two CI faults found while trying to land it:

- **#242 docs set.** `docs/architecture.md` (12 chapters, 6 diagrams), `docs/user-guide.md`, Word
  builds of both with logo + copyright/licence page, and `docs/_build/` so they stay reproducible.
  The diagrams were built from ground truth read out of the code, which corrected three things prose
  had blurred: `journey`↔`network_coverage` are **not** mutually dependent (the reverse is one line,
  `hafas_client`→`external_verify`, and `external_verify` imports nothing internal so no chain
  closes); `web` and `worker` **share no Python at all**; `configured` and `deleted` are in
  `SessionState` and are **never assigned**.
- **#243 → #244 hadolint.** #243 pinned the pre-commit hadolint image as a stop-gap; #244 removed the
  hook entirely. See §2. Net effect: Dockerfile linting has one home, stays current via Dependabot,
  and no longer blocks PRs that contain no Dockerfile.
- **#245 Trivy was lying.** It failed the `web` build on two HIGH CVEs that were **not in the image**.
  Cause: pip's vendored SBOM (`pip/_vendor/bom.cdx.json`). See §2 and the §8 recipe. **The false
  positive was the harmless half** — while Trivy read that SBOM it reported pip's vendor manifest
  *instead of* the ~100 real packages, so green scans were not trustworthy. Now 89 real targets, 0
  findings.

Also enabled `deleteBranchOnMerge` on the repo (it was off; hundreds of stale branches had accumulated).

**Merged #206→#227 (22 PRs, 2026-07-01 → 2026-07-08)** — triaged into §2/§3/§5/§7; the table of commit
subjects that stood here is gone. One arc dominates: making VIATOR and ÖBB **comparable**. #220/#224/#225
fixed the timebase (ÖBB's persisted timestamps are naive Vienna, and coverage runs had been searching the
wrong calendar day invisibly); #221/#223 made both engines answer for the same time range; #222 paired them
row-for-row in the SBS view. In parallel, an 8742-pair run made the coverage report unopenable and
#211/#214/#215/#216 rebuilt it as a lazy, publicly shareable surface; #212/#217 gave the matrix country and
mode header bands and stopped scrolled cells bleeding through the sticky columns; #206–#209/#213 cleaned up
run deletion, autoheal false-positives and a crash that was silently voiding ÖBB scores.

Two carry live consequences:

- **#227 is MERGED on `main` and its own title says DO NOT MERGE.** Verified: `gh pr view 227` returns
  state `MERGED`, title *"DO NOT MERGE — fix(coverage): modal transit times (approach rejected by
  review)"*. There is no revert. Sonar passed it. `main` today can display a wrong duration for a night
  train when "Show walk legs" is unchecked. **Do not read a friendly commit subject as an endorsement** —
  check the PR title and comments before building on merged code. §7 item 1.
- **#226 was closed unmerged** (title also *"DO NOT MERGE — … design rejected by review"*), so the
  endpoint-UIC guard it would have removed is still live and rejecting pairs. §7 item 2.

Treat every alignment score currently in the database as unusable — §7 item 3 gives three independent
reasons.

**None currently open.**

**Incident #1 (2026-06-30/07-01)**: `motis-eu19-transit-motis` silent-death (~10h at 99% CPU, undetected) during a coverage run. Root-cause confirmed via direct MOTIS curl post-recovery: **not** a walk-graph/coord problem (Brussels-Midi routes correctly once MOTIS is healthy) — it was purely the zombie process. Fixed operationally with `docker compose down/up`; PR-199 + PR-203 are the structural fix so it self-heals + eventually pages next time.

**Incident #2 (2026-07-01), same alarm, different cause**: with PR-199+203 live, a fresh eu19 sweep hit the exact same "autoheal keeps restarting the container" symptom (8 restarts in ~43 min) — but this time it wasn't a zombie, it was a **stale `platform_config.COVERAGE_SLOT_COUNT=2` override** (should be the code default `6`) tripling each K-slot query's RAPTOR search window to 12h instead of the documented-safe 4h. The override was real and resetting it was correct. **The stated mechanism is now disputed**: this file originally recorded ~199.91% CPU as "MOTIS pegged, starving its own healthcheck", but #207's commit message points out the host has 18 cores — so 199.91% is ~2 of them, an idle machine — and attributes the restarts instead to a healthcheck probe budget (`wget --timeout 5s`) too tight for a container briefly busy with real RAPTOR work: a false-positive restart. #207 widened the probe to 15s and added connect-level retries. Both stories cannot be the mechanism; the dispute is recorded rather than resolved. **Lesson**: "autoheal is restarting this container repeatedly" now has **three** known causes — a genuine zombie (incident #1), a knob really overloading the engine, and a probe budget too tight for healthy work. Check the probe budget and the host core count before concluding overload.

**Data gap discovered**: eu19 MOTIS session's Dutch (NS) GTFS appears stale/incomplete — Amsterdam↔Rotterdam and Amsterdam↔Leiden return `no_route` from VIATOR while ÖBB HAFAS confirms real trains exist. Needs an NS GTFS re-import into the eu19 graph (not yet actioned).

---

## 7. Next steps (priorities)

1. **Decide #227** — `main` ships code its own review REJECTED. `gh pr view 227` returns state MERGED
   with the title "DO NOT MERGE — … (approach rejected by review)" and an owner comment listing 10
   confirmed findings; there is no revert. Live symptom: unchecking "Show walk legs" in the coverage
   cell modal recomputes a NightJet 21:00→02:45 from a correct 5h45 to 4h02, and the number differs
   per viewer's browser timezone. Root cause is structural — `VerifyLeg` carries no per-leg
   `duration_seconds` (VIATOR legs have one; ÖBB's was dropped when `VerifyLeg` was built), so
   client-side timestamp subtraction was the only option available. Either revert, or land the design
   the review specified: `total_duration − Σ(non-transit leg durations)`, pure second arithmetic,
   immune to tz/DST/day-roll/>24h. That touches persisted JSONB, so it needs a coverage re-run.
   Alignment TIERS are unaffected (`_strip_walk_legs` already runs on both sides) — display only.
   Also delete or fix `test_modal_non_transit_modes_matches_the_backend_set`: it never imports the
   backend set, and its `assert ".toUpperCase()" in template_text` is satisfied by unrelated
   country-filter code, so the protection it claims can be removed with CI green.
2. **Fix `extract_uic`, then re-decide the #226 matcher rework** (closed unmerged 2026-07-08). Two
   faults stacked. **(a) `extract_uic` has never worked, anywhere** — verified 2026-09-04. Its regex
   `(?<!\d)(\d{7,8})(?!\d)` used with `.search()` takes the first standalone 7–8 digit run in the lid,
   and a real HAFAS lid puts coordinates first, so
   `A=1@O=Wien Hbf@X=16375326@Y=48185507@U=81@L=008100002@B=1@` yields `16375326` — a longitude in
   micro-degrees. Every unit fixture uses a stripped lid with no `X=`/`Y=`, which is why the suite is
   green. This is **not** the cross-border-only problem it was previously believed to be. **(b)** the
   fuzzy tier's guard `_is_fuzzy_candidate` requires both spines to share `(first_UIC, last_UIC)` and
   returns False there, before the train number is read — so combined with (a) it compares two
   longitudes and rejects essentially every pair. #226 would have dropped the guard and made
   `_first_train_number` extract the numeric part ("EUR 9322" → 9322, None for digit-less brand
   labels). Fix (a) first, then re-decide whether the guard is still wanted. **Blocks item 3.**
3. **Run a full eu19 validation sweep** with `verify_externally=true` — still the first "real"
   alignment-heatmap dataset, but not runnable until item 2 lands, and every earlier sweep is invalid
   for at least one of three separate reasons: pre-#213 (2026-07-04) sweeps lost their ÖBB scores to a
   swallowed `AttributeError` persisted as `external_error='sweep_exception'`; pre-#224 (2026-07-08)
   runs asked the two engines about DIFFERENT CALENDAR DAYS; and endpoint matching has never worked
   (item 2). The 2026-07-01 run's error cells are additionally restart-loop collateral, not genuine
   `no_route`. **Treat every alignment score currently in the DB as unusable.**
4. **Re-import NS (Netherlands) GTFS** into the eu19 MOTIS session — confirmed data gap, not a code
   bug (incident #1). Cheap sanity check first: the evidence came from a coverage run predating #224,
   so re-confirm Amsterdam↔Rotterdam with one `/journey` search before spending an import cycle.
5. **Decide a notification channel** for #203's alert: Alertmanager (new subsystem) vs.
   Grafana-provisioned alerting (fits the existing dashboards-as-code pattern better) — either needs a
   real SMTP/webhook contact point that does not exist today. Incident #2 reinforces this: the alert
   would have fired (8 restarts/hour ≫ the >3 threshold) but nobody was paged.
6. **Reconcile `depart_at` vs `first_transit_leg_departure_utc`** — #221's two window filters key off
   `JourneyTrip.departure_at` (an itinerary start that may be a walk leg) while the runner's own window
   gate uses `first_transit_leg_departure_utc`. #224 filed the divergence knowingly; it is still open.
   Pick one and make §2's canonical-departure row true again.
7. **Hub metadata backfill** (merges the old UIC item with the orphan from #212): nullable `uic` column
   + FK to `master_stations` + backfill by name/coord match, which activates PR-202's `&from_uic=`
   passthrough and would give item 2's matcher a real UIC source; AND populate
   `NetworkCoverageHub.modes`. #212 shipped the column and the R/T/M/B/C header band, but every hub
   renders `?` — the band is decorative until something classifies them.
8. **Hub trim for eu19**: 94 hubs × both directions = 8742 pairs runs ~14 h at current knob defaults.
   Recommendation: 3 hubs/country ≈ 42 hubs = 1722 pairs ≈ 90–150 min. Operational decision (toggle
   `is_active` in Manage Hubs), not a code change — ask before building an automated top-3 picker.
   *(The reporting half of this item is DONE: #214/#215 made the 8742-pair report openable.)*
9. **Reconcile the 3 near-duplicate viridis palettes + the country-hue table** (compare_grid.css
   modal-pill, network_coverage.html live matrix, network_coverage_export.html; plus `_COUNTRY_HUES`
   in Python vs `COUNTRY_HUES` in JS, added by #212 with a comment deferring to this item). PR-201
   aligned the export to the WCAG-AA-safe values; the live matrix still carries the old `#e76f51` /
   `#8a8a8a` / `#8a939d`, confirmed still divergent 2026-09-04.
10. **Small known defects, batched** — none individually worth a session: the two `TEMP DEBUG (2026-07)`
    logs #220 left on main (`journey/motis_client.py:128`, `network_coverage/external_verify.py:764`);
    the Manage Hubs lat/lon inputs at `templates/admin/network_coverage.html:748,752` still using
    `step="0.0001"`, the identical trap #210 removed from journey.html and the very form #212 points
    operators at; and #225's deliberately-unfixed limitation that `expected_reference_date` re-derives
    a historical run's window from the LIVE `CoverageConfig` when the run's window columns are NULL, so
    editing `COVERAGE_DEFAULT_TIMEZONE` can move the banner verdict for a run near a date boundary
    (real fix: freeze the resolved window onto the run row).
11. **Audit `platform_config` for stale `COVERAGE_*` overrides** and clamp obviously-unsafe values —
    e.g. warn if the effective per-slot window (`day_window / slot_count`) exceeds ~6 h. The schema
    bound is only `min: 1, max: 24`, which does not prevent the `slot_count=2` that triggered incident
    #2. Demoted from the top of this list because that incident's mechanism is now disputed (§6) — this
    is hygiene, not an incident fix.
12. **Counter race** on `completed_pairs`: a cancelled run showed 1857/342. Note the per-pair path
    already uses an atomic SQL-side increment (`completed_pairs = NetworkCoverageRun.completed_pairs + 1`,
    runner.py:2136) which is NOT racy, while two other sites overwrite it with `len(rows)`
    (runner.py:1269, :1298). So the likely fault is the increment overcounting before a reconcile pass,
    not a race on the increment itself. Investigate before the next major coverage feature.
13. **PKP Intercity GTFS** (Polish national rail) not in eu19 — Warsaw missing from station typeahead.
    Needs auth FTP credentials per `docs/eu19-compliance-summary.md`.
14. **More reference engines**: DB Navigator / SBB CFF / SNCF. The reuse surface is
    `trip_normalize.dedup_batch_and_track_latest_dep` + `next_anchor_or_none` (#223), NOT copying
    `hafas_client.py` a third time — two copies of the anchor-advance loop had already drifted, which
    is why #223 extracted them.

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

# 4. If MOTIS itself is the culprit (hot CPU / many open sockets after web is calm)
docker stats --no-stream viator-motis-<sid>-1
docker exec viator-motis-<sid>-1 sh -c 'cat /proc/net/tcp | awk "\$2~/:1F90/" | wc -l'
docker compose -p viator kill motis-<sid> && docker compose -p viator up -d motis-<sid>
# 90-180s cold start for eu19; watch: docker compose -p viator logs -f motis-<sid> --tail 0

# 5. Verify calm
docker logs viator-motis-<sid>-1 --tail 5 --since 30s  # should be empty
```

**Diagnosing "is this a MOTIS-health problem or a real routing gap?"** — curl MOTIS directly, bypassing the runner entirely:
```bash
docker compose -p viator exec web sh -c \
  "curl -s -m 30 'http://motis-<sid>:8080/api/v6/plan?fromPlace=<LAT>,<LON>&toPlace=<LAT>,<LON>&time=<ISO8601>&numItineraries=3&searchWindow=3600&transitModes=TRANSIT' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({\"itineraries\": len(d.get(\"itineraries\",[])), \"from\": d.get(\"from\"), \"to\": d.get(\"to\")}, indent=2))'"
```
(No `jq` in the web image — pipe through `python3 -c` instead.) `itineraries: 0` + `from.stopId: null` on ONE direction only = walk-graph dead-zone at that coord (nudge it). Zero on BOTH directions = real data gap (missing GTFS feed). Non-zero once MOTIS is freshly restarted = it was just the zombie.

**Diagnosing an autoheal restart-loop (recurring unhealthy, not a one-off zombie)**:
```bash
# Is autoheal actually cycling this container, and how often?
docker logs viator-autoheal-1 --tail 50 | grep -i <sid>
# (container is `viator-autoheal-1` — no container_name override — not bare `autoheal`)
# 3+ restarts within an hour = NOT a one-off zombie; something is making it
# unhealthy repeatedly. `docker inspect .RestartCount` will misleadingly
# read 0 here — autoheal restarts via the Docker API directly, bypassing
# the container's own `restart:` policy counter.

# Before assuming it's a repeat of the zombie-MOTIS incident, check for a
# stale coverage knob overload instead:
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT key, value FROM platform_config WHERE key LIKE 'COVERAGE_%' ORDER BY key;"
# COVERAGE_SLOT_COUNT below the code default (6, with the default 24h day
# window) means each slot's searchWindow is wider than the documented-safe
# 4h — RAPTOR cost scales near-quadratically with it, and even 2 concurrent
# queries at 12h+ windows can peg MOTIS's CPU enough to starve its own
# healthcheck (confirm live with `docker stats --no-stream <container>`).
# Reset the stale override:
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "DELETE FROM platform_config WHERE key = 'COVERAGE_SLOT_COUNT';"
```

**Coverage run finishes green but the ÖBB alignment column is empty, unscored, or `No overlap 0.00`**
— four distinct causes, checked in this order. All four are silent; the sweep is best-effort and
swallows per-cell.

```bash
# 0. Is the cell even eligible? _VERIFY_STATUSES = ("no_route","timeout","error") —
#    a status='ok' cell is NEVER sent to OeBB, checkbox or not. An empty OeBB column
#    plus "No alignment data recorded" on an ok cell is CORRECT, not a bug.

# 1. Cells carry external_error='sweep_exception'? → an exception inside _verify_one.
#    It never surfaces in the UI. The verify sweep runs in WEB, not worker:
docker compose -p viator logs web --since 2h | grep -i "external_verify\|sweep_exception"
#    Known instance: HAFAS returning a NUMERIC product category blew up
#    _hafas_cat_to_mode with "'int' object has no attribute 'upper'" (#213).
#    General rule: HAFAS field types are untrusted — str() before any string method.

# 2. Amber "reference_date != depart_at" banner showing, or the run predates
#    2026-07-08? → the grid was searched on one calendar day and OeBB queried on
#    another (#224). Recognition WITHOUT the banner: the cell modal reads
#    "Status: ok · 26 itineraries · best 37m" directly above "no itineraries found"
#    (num_itineraries is a run-time len() persisted on the row, never re-queried),
#    and leg times render HH:MM with NO DATE so the wrong day is invisible.
#    Not fixable after the fact — re-run.

# 3. Both columns visibly show the SAME train at the SAME minute, yet the tier is
#    no_overlap/disagree? → the matcher, not the data.
#      - whole 1-2h skew across the board = OeBB timestamps not localised (#220,
#        fixed; VerifyLeg.dep_utc is naive Europe/Vienna despite the name)
#      - otherwise = the endpoint-UIC guard. extract_uic returns a LONGITUDE, not a
#        UIC (§3), so the guard compares two coordinates and rejects nearly
#        everything. STILL OPEN — §7 item 2. Expect this on essentially every pair.
```

**The `web` container pegs at ~128% CPU and 13 GB+ RAM and never returns** (#214/#215): someone opened
a coverage export or share link for a large run. **The misleading fix** is capping trips per cell —
legs are ~1.7 KB/trip and ~90% of report bytes, so a count cap barely dents a bytes problem; the same
run still produced a ~150 MB page (nginx's proxy timeout cuts it → "cannot download"; no browser opens
it → "cannot open"). The working fix already shipped: lazy per-cell fetching on the share page, and
dropping leg detail above `_EXPORT_LEG_DETAIL_MAX_PAIRS = 500` in the download. Do not reach for a
different trip cap.

**After any squash-merge that raced a push to the same branch**, confirm nothing was left behind:

```bash
git diff <branch-head-sha> origin/main -- app/     # empty = the branch really is on main
```

#224 was squash-merged at its first commit; the adversarial-review rework was pushed afterwards and
never reached `main`. GitHub's UI shows the branch as merged either way, so nothing flags it. Cost:
`main` shipped two bugs the review had already caught, plus a wrong predicate, and needed a whole
recovery PR (#225).

**Setting a coverage knob without admin UI** (psql fallback):
```sql
INSERT INTO platform_config (key, value) VALUES
  ('COVERAGE_PAIR_PARALLELISM', '2'),
  ('COVERAGE_SLOT_TIMEOUT_MS', '60000')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```
Runner reads config at `execute_run` start and freezes for that run's lifetime.

**Trivy reports a CVE for a version the image does not contain** (#245, 2026-09-04):

```bash
# 1. Name the SBOM Trivy is reading. NOTHING else names it — the SARIF only
#    says "Python". Add to the trivy-action step, then remove once diagnosed:
#      env:
#        TRIVY_DEBUG: "true"
#    Look for:  file_path="...bom.cdx.json" name="pip"

# 2. Prove what is ACTUALLY in the image, without needing Docker locally.
#    The pipeline already produces a Syft SBOM — download and grep it:
gh run download <run-id> --name sbom-web.cyclonedx.json --dir /tmp/sbom
python -c "import json;d=json.load(open('/tmp/sbom/sbom-web.cyclonedx.json'));\
print([f\"{c['name']} {c['version']}\" for c in d['components'] if c['name'] in ('setuptools','msgpack')])"
```

Tells that Trivy is reading an embedded SBOM rather than the filesystem: the warning
`Third-party SBOM may lead to inaccurate vulnerability detection`, `Number of language-specific
files: num=1`, a target named literally `Python` instead of a path, and per-package
`dist-info/METADATA` rows all showing `0` while the summary reports findings. Fix by adding the file
to `skip-files` in **both** Trivy steps (keep them identical, or the Security tab disagrees with what
gated the build). **Do not reach for `.trivyignore`** — that file is for findings with no upstream
fix, and this is not a finding at all.

**Git operations fail with permission / "Invalid argument" errors** — OneDrive marks files read-only
in this tree. It silently breaks `git worktree prune` (33 stale admin dirs had accumulated against 5
real worktrees), half-applies `git pull` with `unable to unlink old '<file>': Invalid argument`, and
blocks folder deletion. Run this first:

```bash
attrib.exe -R "<path>\*.*" /S /D
```

To recover a half-applied pull: it leaves tracked files on disk with HEAD unmoved, so they show as
*untracked*. Verify they match `origin/main` (`git hash-object` vs `git rev-parse origin/main:<path>`),
confirm `git rev-list --count origin/main..main` is 0 and no tracked file is modified, then
`git reset --hard origin/main`.

**Sonar coverage gate** is strict (≥80% on new code, CC≤15/function). Patterns that have bitten multiple PRs this project:
- Add tests for tiny utility helpers (regex parsers, format helpers) — Sonar counts them generously toward the ratio.
- **Contrast findings on new CSS**: WCAG AA needs ≥4.5:1 for normal-size text/badges. Known-safe replacements already adopted project-wide: `#e76f51`→`#c4452a`, `#8a8a8a`→`#6e6e6e`, `#8a939d`(text)→`#5b6470` — reuse these exact hex values rather than re-deriving new ones each time a palette gets flagged.
- **Cognitive complexity on JS in Jinja templates**: extract the offending nested-if/ternary block into a small named helper function (matches the existing style: `fmtDuration`, `fmtTime`, `statusPill`, etc. in `network_coverage_export.html` / `journey.html`).
- **`window` vs `globalThis`**: Sonar prefers `globalThis` for new code.
- **Empty/comment-only `catch` blocks**: add a `console.warn(...)` that names the operation + references the caught error.
- **A red gate on MAIN is not necessarily your PR's fault.** All three conditions in #208 were
  pre-existing debt surfaced by a screenshot, unrelated to the work in flight. Check whether the
  finding is on new code before rewriting anything.
- **Sonar's taint tracker only recognises library sanitizers, not project-local ones**, so any log call
  fed by `_sanitize_for_log` raises `pythonsecurity:S5145` forever. Suppress with a trailing
  `# NOSONAR python:S5145` on the log-call line (established convention in `nap_importer.py`) rather
  than "fixing" the sanitizer.

**Two front-end traps no test and no code review will catch** — both found only in a real browser:

- **A valid lat/lon refused when promoting a station to a hub**, with a NATIVE browser bubble (not
  VIATOR's styling), nothing in the server log, and a suggested value that is the typed one truncated:
  that is the HTML `step` attribute acting as a hard validation grid, never backend precision (the API
  range-checks only). Fix is `step="any"` (#210). Still live at
  `templates/admin/network_coverage.html:748,752`.
- **New CSS in the matrix templates silently not rendering**: bare classes (0,1,0) lose to the
  pre-existing `.cov-matrix thead/tbody th` rules (0,1,2), with no error anywhere. Scope new matrix
  cell styling under `.cov-matrix th.`. When verifying sticky-column behaviour, measure geometrically
  AND hit-test with `elementFromPoint` (it proves what is PAINTED, not merely positioned), at ~20 hubs
  / 4 countries / mixed name lengths so the matrix actually scrolls.
