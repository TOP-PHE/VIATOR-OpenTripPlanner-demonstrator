# CLAUDE.md — VIATOR journey-planner setup

Working notes for AI sessions resuming this project. Concise + structured.
Companion to: `README.md`, `VIATOR-strategy.md`, `VIATOR-technical-spec.md`, `docs/admin-guide.md`.

**Start with `docs/architecture.md` (#242)** if you are new to the code — chapter 2 is the map
(module map, runtime topology, the three principal flows) and every chapter ends with *Invariants &
traps*. This file is the operator's log: state, incidents and recipes. That one is the reference.

Last updated: 2026-09-04, after PRs #242–#245 (docs set + two CI fixes).

**Coverage caveat for this file:** §2/§5/§7 were last curated at #205. PRs **#206–#227** merged after
that and are listed in §6 by commit subject only — they have not been triaged into the decisions
table, the key-files map, or the priorities list. Treat those three sections as accurate up to #205
and provisional after it. Nothing here is stale as of #245; it is incomplete, which is different.

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
| Cross-engine alignment scorer: exact `transit_fingerprint` match (1.0) + train-number-guarded ±5min fuzzy fallback (0.7) | Avoids false-positives on high-frequency corridors (same endpoints/minute, different trains) | `app/network_coverage/alignment.py` |
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
  - HAFAS form: `A=1@L=8503000`
  - Canonical: `UIC:8503000`
  - Fallback: lat/lon rounded to ~110 m when no UIC available
  - **Coverage hubs (`network_coverage_hubs` table) carry NO UIC column today** — no FK/join to `master_stations`. The Re-run link's `&from_uic=&to_uic=` (PR-202, merged) is wired but always resolves to empty string until a follow-up adds the column + backfill.
- **Timezones**: IANA names everywhere (`Europe/Zurich`, never `CET`/`CEST`)
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
│   ├── admin/network_coverage.py        Coverage matrix API (runs, results, cell-trips, verify-external, export, stop)
│   ├── admin/config.py                  Platform config CRUD
│   └── journey.py                       /plan + /fanout (live UI); semaphores.journey gate (limit 20)
├── network_coverage/
│   ├── runner.py                        execute_run + cancel registry + K-slot fan-out + alignment persistence
│   ├── external_verify.py               ÖBB HAFAS adapter (LocGeoPos→TripSearch two-step) + VerifyItinerary/VerifyLeg + extract_uic
│   ├── alignment.py                     Cross-engine alignment scorer (exact fingerprint + train-guarded fuzzy fallback)
│   └── hubs.py                          Static fallback hub list (DB takes precedence); no UIC field yet
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
├── static/
│   ├── css/compare_grid.css             Shared N-column grid CSS + alignment-tier-pill palette (modal variant)
│   └── js/compare_grid.js               window.CompareGrid.{renderGrid, tierPill, escHTML}
└── templates/
    ├── journey.html                     Search UI + side-by-side compare-grid; URL-param prefill (from_lat/lon/name/uic, depart_at)
    └── admin/
        ├── network_coverage.html        Coverage matrix UI + heatmap + VIATOR/ÖBB side-by-side cell modal + Re-run link
        └── network_coverage_export.html Self-contained downloadable HTML report; mirrors the heatmap, all CSS/JS inlined
docker/
├── docker-compose.yml                   Main stack incl. autoheal service (opt-in via viator.autoheal label)
└── prometheus/
    ├── prometheus.yml                   Scrape config + rule_files stanza (added by #203)
    └── rules/autoheal.yml               AutohealExcessiveRestarts alert (PR-203, merged; no notification channel wired yet)
alembic/versions/                        Migrations (YYYYMMDD_HHMM_descriptor.py pattern)
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

**Merged #206→#227, not yet triaged into §2/§5/§7 — commit subjects only:**

| PR | Subject |
|---|---|
| #227 | fix(coverage): recompute trip header from remaining legs when walks are hidden |
| #225 | fix(coverage): address adversarial review of the reference_date fix |
| #224 | fix(coverage): search the day the operator asked for, not "tomorrow" |
| #223 | feat(journey): paginate ÖBB HAFAS to VIATOR's window, flag possible-duplicate trips |
| #222 | feat(journey): align VIATOR and ÖBB HAFAS trips by first-transit departure in SBS view |
| #221 | feat(coverage): align VIATOR trips shown/scored to ÖBB's depart_at window |
| #220 | fix(coverage): normalize ÖBB timestamps to UTC before alignment scoring |
| #219, #218 | dependabot: actions + python-runtime patch/minor groups |
| #217 | fix(coverage): stop scrolled data cells bleeding through sticky matrix columns |
| #216 | feat(coverage): VIATOR-vs-ÖBB side-by-side on the share page + mode-chip overlap fix |
| #215 | feat(coverage): lazy-load share-page cell detail + slim large downloads |
| #214 | fix(coverage): stop export/share page ballooning memory on large runs |
| #213 | fix(hafas): don't crash cat-to-mode mapping when HAFAS sends an int |
| #212 | feat(coverage): country + transport-mode header bands on the matrix |
| #211 | feat(coverage): unauthenticated share link for a run's HTML report |
| #210 | fix(journey): allow any-precision coords in Promote-to-Hub form |
| #209 | fix(coverage): also retry on RemoteProtocolError, not just ConnectError |
| #208 | fix(sonar): clear the failing quality gate |
| #207 | fix(coverage): widen MOTIS healthcheck timeout + retry ConnectError with backoff |
| #206 | feat(coverage): Delete button for coverage runs in the admin sidebar |

**None currently open.**

**Incident #1 (2026-06-30/07-01)**: `motis-eu19-transit-motis` silent-death (~10h at 99% CPU, undetected) during a coverage run. Root-cause confirmed via direct MOTIS curl post-recovery: **not** a walk-graph/coord problem (Brussels-Midi routes correctly once MOTIS is healthy) — it was purely the zombie process. Fixed operationally with `docker compose down/up`; PR-199 + PR-203 are the structural fix so it self-heals + eventually pages next time.

**Incident #2 (2026-07-01), same alarm, different cause**: with PR-199+203 live, a fresh eu19 sweep hit the exact same "autoheal keeps restarting the container" symptom (8 restarts in ~43 min) — but this time it wasn't a zombie, it was a **stale `platform_config.COVERAGE_SLOT_COUNT=2` override** (should be the code default `6`) tripling each K-slot query's RAPTOR search window to 12h instead of the documented-safe 4h, pegging MOTIS's CPU at 199.91% until it missed its own healthcheck. See §2 MOTIS-quirks bullet and §8 recipe below for the full diagnostic trail and fix. **Lesson**: "autoheal is restarting this container repeatedly" is a symptom with (at least) two different root causes — always check `platform_config` for a stale `COVERAGE_*` override before assuming it's a repeat of incident #1.

**Data gap discovered**: eu19 MOTIS session's Dutch (NS) GTFS appears stale/incomplete — Amsterdam↔Rotterdam and Amsterdam↔Leiden return `no_route` from VIATOR while ÖBB HAFAS confirms real trains exist. Needs an NS GTFS re-import into the eu19 graph (not yet actioned).

---

## 7. Next steps (priorities)

0. **Triage #206→#227 into this file** (new): those 22 PRs are listed in §6 by commit subject only.
   The decisions table (§2), key-files map (§5) and this priorities list have not absorbed them, so
   several entries below may already be done or obsolete. Worth one pass before trusting §7 to plan
   from — in particular #220–#224 reworked ÖBB/VIATOR time alignment, which touches the same
   machinery as items 4 and 7.
1. **Guard against a stale/unsafe `COVERAGE_SLOT_COUNT`** (new, from incident #2): nothing today stops `platform_config` from holding a slot count that implies a search window wide enough to overload MOTIS and trigger an autoheal restart-loop. Add a cheap safeguard — e.g. warn (admin UI + startup log) if the effective per-slot window (`day_window / slot_count`) exceeds ~6h, or clamp it. Also worth a one-time audit of every `COVERAGE_*` value in `platform_config` for other leftover overrides from past incident tuning (this project's psql-fallback recipe makes it easy to set a knob and easy to forget to unset it).
2. **Decide a notification channel** for #203's alert: Alertmanager (new subsystem) vs. Grafana-provisioned alerting (fits the existing dashboards/datasources-as-code pattern better) — either needs a real SMTP/webhook contact point that doesn't exist today. Incident #2 reinforces this: the alert would have fired (8 restarts/hour ≫ the >3 threshold) but nobody was paged — only caught by someone watching the live matrix.
3. **Re-import NS (Netherlands) GTFS** into the eu19 MOTIS session — confirmed data gap, not a code bug (see incident #1 above)
4. **Run a full eu19 validation sweep** with `verify_externally=true` now that the heatmap + both autoheal-restart-loop incidents are resolved — this is the first "real" alignment-heatmap dataset. The 2026-07-01 run that surfaced incident #2 needs re-doing; its error cells are restart-loop collateral, not genuine `no_route` findings.
5. **Hub trim for eu19**: 94 hubs × both directions = 8742 pairs runs ~14h at current knob defaults. Recommendation: 3 hubs/country ≈ 42 hubs = 1722 pairs ≈ 90-150 min. Operational decision (toggle `is_active` in Manage Hubs), not a code change — ask before building an automated top-3-picker script
6. **UIC backfill for coverage hubs** (Scope B of PR-202): nullable `uic` column + FK to `master_stations` + backfill by name/coord match + surface in `HubInfo`/manage-hubs UI. Activates PR-202's passthrough.
7. **Reconcile the 3 near-duplicate viridis hex palettes** (compare_grid.css modal-pill, network_coverage.html live matrix, network_coverage_export.html) — PR-201 aligned the export to the modal-pill (WCAG-AA-safe) values; the live matrix's own palette in `network_coverage.html` still has the old, lower-contrast hex and wasn't flagged by Sonar because those specific lines predate this round of new-code scanning
8. **PKP Intercity GTFS** (Polish national rail) not in eu19 — Warsaw missing from station typeahead. Needs auth FTP credentials per `docs/eu19-compliance-summary.md`
9. **Counter race** on `completed_pairs`: observed a mismatch in a cancelled run, suggests multi-write race. Investigate before the next major coverage feature
10. **More reference engines**: OJP + HAFAS pattern is proven twice now; DB Navigator / SBB CFF / SNCF could be added by mirroring `hafas_client.py`

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
