# VIATOR — Strategy

_UIC MERITS-aligned rail journey-planning demonstrator built on OpenTripPlanner._

_High-level strategy for an OpenTripPlanner-based journey planner that initially ingests SNCF open data via the French NAP (transport.data.gouv.fr), and is designed to migrate to ingesting the same dataset families directly from **MERITS** (the UIC central data platform) once available. Companion to `VIATOR-technical-spec.md`._

_**Powered by TrackOnPath SAS** — patrick.heuguet@trackonpath.com_
_**© 2026 UIC — International Union of Railways. All rights reserved.**_
_Last updated: 2026-04-27_

---

## Executive summary

Build a single-VPS journey-planning stack made of:

1. An **OpenTripPlanner** core, fed with **SNCF GTFS** as the production timetable source.
2. A **web UI + ingestion service** that lets an operator upload new feeds (timetables, stations, MCT) by **declaring the standard** of the file. The backend validates, stores, and routes the file to the right downstream consumer.
3. An **enrichment layer** holding station cross-references (UIC ↔ Trigramme ↔ INSEE ↔ RICS) and minimum connection times (MCT) — both consumed by the API façade, not by OTP itself.
4. (Phase 2) An **OJP adapter** in front of OTP, so external consumers (MERITS, OSDM, UIC partners) can query the planner using the CEN OJP standard rather than OTP's GraphQL.

The NeTEx file SNCF publishes follows the **French profile (Profil France NeTEx v2.4)**, which OTP cannot ingest natively. The pragmatic path is **GTFS into OTP, NeTEx-FR kept aside** for richer stop and interchange semantics until a NeTEx-FR → Nordic converter is built.

---

## 1. Phase-1 data sources (SNCF via French NAP), formats, and the legacy-vs-NeTEx comparison

> **Mid-term:** the same data families (timetables, stations, MCT) will be ingested from **MERITS** instead of the French NAP, once MERITS exposes them. The ingestion architecture (UI + dispatcher + standard-aware routing) is built so that swapping the source does not change the downstream pipeline. Section 4 (roadmap) contains the migration phase.

### 1.1 Source matrix

| Data | Primary source | Available formats | Update | License |
|---|---|---|---|---|
| **Station codes** (passenger stations, UIC, Trigramme, INSEE, lat/lon) | [transport.data.gouv.fr — Gares de voyageurs du RFN](https://transport.data.gouv.fr/datasets/gares-de-voyageurs-1) and [Liste des gares](https://transport.data.gouv.fr/datasets/liste-des-gares) | CSV, GeoJSON, JSON, ZIP | ~weekly | ODbL |
| **Timetables — national bundle** (TGV + Intercités + TER) | [transport.data.gouv.fr — Réseau SNCF TGV, Intercités et TER](https://transport.data.gouv.fr/datasets/horaires-sncf) | **GTFS**, **NeTEx (Profil France)**, GTFS-RT, SIRI Lite | Daily (static), 2 min (real-time) | ODbL |
| Timetables — Intercités only | `export-intercites-netex-last.zip` | GTFS, NeTEx-FR | Daily | ODbL |
| Timetables — TER only | `export-ter-netex-last.zip` | GTFS, NeTEx-FR | Daily | ODbL |
| **MCT** (minimum connection times) | [data.gouv.fr — Temps de correspondance minimaux SNCF Voyageurs](https://www.data.gouv.fr/en/datasets/temps-de-correspondance-minimaux-entre-operateurs-sncf-voyageurs/) | CSV (in ZIP) + JSON | Officially daily — freshness flagged as irregular on data.gouv.fr | ODbL |

**Direct download URLs to pin in configuration:**

```
GTFS national   : https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip
NeTEx national  : https://eu.ftp.opendatasoft.com/sncf/plandata/export-opendata-sncf-netex.zip
NeTEx Intercités: https://eu.ftp.opendatasoft.com/sncf/plandata/export-intercites-netex-last.zip
NeTEx TER       : https://eu.ftp.opendatasoft.com/sncf/horaires/export-ter-netex-last.zip
GTFS-RT TU      : https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates
SIRI Lite ET    : https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable
```

### 1.2 Availability of each dataset in NeTEx-FR

| Dataset | Available in NeTEx-FR? | Notes |
|---|---|---|
| Stations | **Yes** — covered by *Profil France NeTEx — Description des arrêts* (v2.4). The SNCF national NeTEx ZIP embeds StopPlaces. The standalone CSV "Gares de voyageurs" remains the canonical source for UIC ↔ Trigramme ↔ INSEE cross-walks. | French profile only; not Nordic, not EPIP. |
| Timetables | **Yes** — *Profil France NeTEx — Horaires* (v2.3). Daily ZIP `export-opendata-sncf-netex.zip`. | French profile. **OTP does not load it as-is** (see §1.3). |
| MCT | **No** — MCT is **not part of the NeTEx-FR profile**. It is an SNCF-specific PRR (Passenger Rights Regulation) compliance dataset, only published as CSV/JSON. | RICS-coded since 2024-06-27; legacy SN/DB/ES carrier codes are gone. |

The French NeTEx profile is published at [normes.transport.data.gouv.fr](https://normes.transport.data.gouv.fr/normes/netex/) and split into modules: arrêts, horaires, tarifs, réseaux, parkings, accessibilité, éléments communs. It was decreed by the order of 2022-03-04 (AFNOR BNTRA/CN03/GT7).

### 1.3 GTFS (legacy) vs NeTEx-FR — what changes

By "legacy" we mean **GTFS**, since that is the format that actually loads into OTP today.

| Dimension | GTFS | NeTEx-FR (Profil France v2.4) |
|---|---|---|
| Standard body | MobilityData (de-facto) | CEN/TS 16614, AFNOR profile, decreed in France since 2022-03-04 |
| Vocabulary | transit-operator (stops, trips, calendars) | Transmodel (StopPlace, Quay, ScheduledStopPoint, ServiceJourney, …) |
| Stations richness | flat `stops.txt`; no Quay / StopPlace hierarchy | full hierarchy, parent stop places, accessibility, equipment, multimodal links |
| Fares | very limited (GTFS-Fares v1, partial v2) | first-class FareFrame, products, validity, distribution channels |
| Connections | `transfers.txt` only | `Interchange`, `ServiceJourneyInterchange`, with `MinimumTransferTime` |
| Real-time pairing | GTFS-RT | SIRI |
| **OTP support** | **First-class.** SNCF GTFS loads cleanly. | **Not directly supported.** OTP only ingests Nordic profile and partial EPIP. The French profile uses the same XSD as Nordic but diverges on identifiers, codespaces, framing, and several mandatory elements. |
| Effort to use today | hours | weeks (need a NeTEx-FR → Nordic converter, or an OTP fork patching the French codespace) |
| Future-proofing | medium — GTFS-FR efforts exist but EU rail interop is going NeTEx | high — aligned with EU MMTIS regulation, OSDM, MERITS |

**Strategic recommendation:**

- **Phase 1 (now):** ingest **GTFS** into OTP. It is production-ready and SNCF publishes it daily.
- **Phase 2:** keep the **NeTEx-FR ZIP** archived; consume it only at the **OJP-output** layer for richer StopPlace/Interchange semantics that GTFS cannot express. The OJP adapter can read NeTEx-FR for stop metadata even though OTP doesn't.
- **Phase 3 (optional):** invest in a NeTEx-FR → Nordic converter once cross-border, fare integration, or stricter UIC-aligned semantics demand it.

---

## 2. Feeding OTP with the three SNCF datasets

OTP has exactly **one** ingestion surface: a build directory whose contents are scanned and merged into a `graph.obj`. Everything else is rules around that.

### 2.1 Stations

- **GTFS path:** stations are already in the GTFS feed's `stops.txt`. Nothing extra to load into OTP.
- **Cross-reference layer:** keep the SNCF "Gares de voyageurs" CSV outside OTP, in a small lookup table (`stop_id` → `UIC` / `Trigramme` / `INSEE` / `RICS`). This is what the OJP adapter uses to translate `stop_id` to UIC stop-place codes that MERITS / OSDM downstream consumers expect. **Do not try to re-inject this into OTP** — it is enrichment, not routing input.

### 2.2 Timetables

1. Drop `Export_OpenData_SNCF_GTFS_NewTripId.zip` into the OTP build directory.
2. Drop a France OSM PBF (e.g. Geofabrik `france-latest.osm.pbf`) in the same directory.
3. Optional: `france-dem.tif` for elevation if cycling/walking elevation costs are needed.
4. Build the graph: `java -Xmx12G -jar otp.jar --build --save /var/otp/graph` (one-shot batch, ~20–60 min for France depending on hardware).
5. Start the server: `java -Xmx16G -jar otp.jar --load --serve /var/otp/graph`.
6. For real-time, configure `router-config.json` with the GTFS-RT and SIRI Lite URLs above; OTP polls them.

### 2.3 Minimum connection times

OTP has **no native concept of regulatory MCT**. It computes transfer time from walking distance plus a small slack. MCT must be applied **outside** OTP.

Two options, in order of effort:

1. **Pre-build injection (cheap, brittle):** generate a `transfers.txt` from `Export_CONNECTION_TIMES` and merge it into the GTFS feed before OTP build. OTP honours `transfers.txt` `min_transfer_time`. Caveats: per-train overrides (`Export_TRAIN_CONNECTION_TIMES`) don't fit GTFS `transfers.txt` cleanly; `-1` (forbidden) maps to `transfer_type=3`.
2. **Post-routing enforcement (clean, recommended):** keep MCT in a separate service. The OJP adapter reads OTP results, validates each interchange against the MCT table, and either re-queries OTP with a larger `transferSlack` or filters invalid itineraries. This matches PRR semantics and keeps the OTP graph stable across MCT publications.

**Recommendation: build option (2) and skip (1) unless quick demo results are needed.**

---

## 3. VPS deployment — OTP + upload UI + format-aware ingestion

### 3.1 Hardware and OS

- **VPS sizing for France-wide OTP:**
  - 8 vCPU, **32 GB RAM** (24 GB Java heap during build, 16 GB during serve), 100 GB SSD.
  - For a single-region pilot (e.g. Île-de-France only), 16 GB RAM is enough.
- **OS:** Ubuntu 24.04 LTS or Debian 12.
- **Runtime:** Docker + Docker Compose. Avoid bare-metal Java — reproducible rebuilds matter.

### 3.2 Component diagram

```
                  ┌──────────────────────────────────────────────┐
   browser ─────► │  nginx (TLS, basic auth or OIDC)             │
                  └────────────┬──────────────────┬──────────────┘
                               │                  │
                  ┌────────────▼──────┐  ┌────────▼──────────────┐
                  │ Upload UI (web)   │  │ OJP adapter (Phase 2) │
                  │ FastAPI + HTMX    │  │                       │
                  └────────────┬──────┘  └───────────────────────┘
                               │
                  ┌────────────▼─────────────────────────────────┐
                  │ Ingestion service                            │
                  │  - detects standard (GTFS / NeTEx-FR / MCT)  │
                  │  - validates                                 │
                  │  - stores into /data/inbox/<kind>/<version>/ │
                  │  - triggers OTP rebuild when applicable      │
                  └────────────┬─────────────────────────────────┘
                               │
                  ┌────────────▼─────────────────────────────────┐
                  │ OTP container (build + serve modes)          │
                  │  Volume: /data/otp/graph                     │
                  └──────────────────────────────────────────────┘
                  ┌──────────────────────────────────────────────┐
                  │ Postgres (uploads metadata, MCT, stations)   │
                  └──────────────────────────────────────────────┘
                  ┌──────────────────────────────────────────────┐
                  │ MinIO or local FS for raw uploads + graphs   │
                  └──────────────────────────────────────────────┘
```

### 3.3 Upload UI — declared standard before processing

The UI is intentionally simple: one upload form, one **mandatory `standard` selector**, plus a free-text version label.

```
[ Choose file ]   *.zip / *.csv / *.json
[ Standard ▼ ]    GTFS
                  NeTEx (Profil France v2.4 — Horaires)
                  NeTEx (Profil France v2.4 — Arrêts)
                  NeTEx (Nordic profile)
                  NeTEx (EPIP)
                  SNCF MCT (Export_CONNECTION_TIMES CSV)
                  SNCF Stations CSV (Gares de voyageurs)
                  OSM PBF
[ Description ]   free text
[ Upload & validate ]
```

The user-declared `standard` is the routing key. The backend must still **verify** that the file matches it (size + magic-byte + a quick schema sniff) and reject mismatches — never trust the dropdown alone.

### 3.4 Ingestion business logic

Pseudocode for the central dispatcher:

```python
def on_upload(file, declared_standard, version_label):
    saved = store_raw(file)
    sniff = detect_actual_format(saved)         # peek inside zip / csv header
    if sniff != declared_standard:
        return reject("File does not match declared standard")

    match declared_standard:
        case "GTFS":
            validate_gtfs(saved)
            stage_into("/data/otp/inbox/gtfs/", saved)
            schedule_rebuild()

        case "NeTEx-FR-Horaires":
            # Phase 3: convert to Nordic before staging.
            # Phase 1/2: store but do not trigger rebuild.
            archive_only(saved, kind="netex-fr-horaires")

        case "NeTEx-FR-Arrets":
            archive_only(saved, kind="netex-fr-arrets")

        case "NeTEx-Nordic" | "NeTEx-EPIP":
            stage_into("/data/otp/inbox/netex/", saved)
            schedule_rebuild()

        case "SNCF-MCT":
            load_csv_into_db(saved, table="mct")        # consumed by OJP adapter
            # no OTP rebuild

        case "SNCF-Stations":
            load_csv_into_db(saved, table="stations_xref")
            # no OTP rebuild

        case "OSM-PBF":
            stage_into("/data/otp/inbox/osm/", saved)
            schedule_rebuild()

        case _:
            return reject("Unsupported standard")

    audit_log(user, declared_standard, sniff, saved.checksum, version_label)
```

Two important properties:

- **Only timetables and OSM trigger an OTP rebuild.** MCT and station cross-references are runtime data, not graph data — they're consumed by the OJP adapter or gateway, never by OTP.
- **Rebuilds are queued.** OTP build is single-threaded and CPU/RAM-heavy; debounce uploads (e.g. one rebuild per 30 min window) and keep the previous `graph.obj` until the new one is verified, then hot-swap.

### 3.5 Scheduled rebuild flow

```
inbox change ──► debounce 30 min ──► docker run otp --build /data/inbox
                                          │
                                          ▼
                                    /data/graphs/<timestamp>/graph.obj
                                          │
                                          ▼
                              smoke test (sample TripRequest)
                                          │
                              symlink /data/graphs/current ─► <timestamp>
                                          │
                                          ▼
                              docker compose restart otp-server
```

### 3.6 Initial install — concrete step list

1. Provision VPS, harden SSH, install Docker + Compose.
2. `docker-compose.yml` with services: `nginx`, `web` (FastAPI), `otp`, `postgres`, `worker` (rebuild queue), optional `minio`.
3. Volumes: `/data/inbox`, `/data/graphs`, `/data/postgres`, `/etc/letsencrypt`.
4. Bootstrap content: download Geofabrik France PBF, current SNCF GTFS, current MCT CSV, current Stations CSV — drop into the inbox to seed the first build.
5. First OTP build (manual): ~30–60 min.
6. Configure `router-config.json` for GTFS-RT + SIRI Lite polling.
7. Wire nginx with TLS (Let's Encrypt) and an auth layer — the per-tester credentials pattern from `oscar-server` is a good fit for consistency across the stack.
8. Smoke-test a `plan` query against the GraphQL endpoint, then a stub OJP `TripRequest` once the adapter is in place.

### 3.7 Operational caveats

- **Disk filling up** with old graph snapshots — keep the last 3 only.
- **NeTEx-FR uploads will fail OTP build** unless the converter is shipped. Until then, the UI should mark NeTEx-FR as "stored, not yet routable" rather than triggering a rebuild.
- **MCT freshness** — the SNCF dataset has a flagged irregular update cadence; show last-update timestamps in the UI to make staleness visible to operators.
- **Build memory** is the #1 source of failures: pin `-Xmx` explicitly and don't let OOM-killer hide it.
- **Real-time feeds rate limits**: SIRI Lite and GTFS-RT proxies on transport.data.gouv.fr are throttled. Set polling intervals conservatively (≥ 60 s).

---

## 4. User management, roles, and self-registration

VIATOR introduces three user roles. Each surface of the system filters by role.

| Role | Can do | Cannot do |
|---|---|---|
| **Platform admin** | Everything: manage users, create/destroy sessions, configure SMTP, view audit log, run rebuilds, query journeys. | — |
| **Content manager** | Upload feeds into a chosen session, trigger/cancel rebuilds, wipe a session's data, configure source URLs. Query journeys. | Manage users; create/destroy sessions; change platform config. |
| **End user** | Search journeys; pick a session to query; compare two sessions side-by-side. | Anything write-related on data or users. |

### 4.1 Self-registration flow

Registration is **open** — anyone with a valid email can self-register as `end_user`. Platform admins keep two compensating controls:

- **Full activity tracking**: every journey search a user runs is recorded (origin, destination, time, session, response time). See §5.5 and the technical spec for the analytics model.
- **Admin-driven role promotion**: there is no self-service promotion. To make someone a content manager or platform admin, an existing platform admin changes the role in the admin console.

```
1. Visitor enters email + name on /register
2. Server creates a pending invitation, sends a magic-link email
3. User clicks the link (token in URL, 24h TTL, single-use)
4. User sets a password
5. Account is created with role = end_user
6. JWT issued, user redirected to journey search UI
```

### 4.2 OSCAR pattern reuse

OSCAR ([TOP-PHE/OSCAR-OSdm-Compliance-Automation-Runner](https://github.com/TOP-PHE/OSCAR-OSdm-Compliance-Automation-Runner)) already implements a comparable flow in Node.js: JWT (`jsonwebtoken`), bcrypt 12-rounds, verification-token email confirmation, role enum, rate limiting (20 attempts / 15 min), audit logging. Because VIATOR's admin app is Python/FastAPI, we **reimplement the same flow** with the equivalent Python libraries (`passlib[bcrypt]`, `python-jose`, `aiosmtplib`, `slowapi`). The HTML email templates can be lifted from OSCAR verbatim, restyled with the VIATOR palette.

Concrete spec lives in `VIATOR-technical-spec.md` §3.

---

## 5. Multi-session architecture — running NAP and MERITS in parallel

### 5.1 What a "session" is

A **session** is a named, isolated OTP instance with its own data sources, inbox, graph, and lifecycle. Examples:

- `nap-fr-2026-q2` — fed by SNCF GTFS via French NAP.
- `merits-pilot` — fed by MERITS pulls (when MERITS is wired in).
- `nap-it-trenitalia` — fed by Trenitalia France data.
- `experimental-netex-fr` — fed by raw NeTEx-FR once the converter exists.

Sessions run **side-by-side**. Content managers select which session to upload into; end users select which session to query, and may compare two sessions on the same journey request.

### 5.2 Why this shape

The user-facing requirement is *"compare what NAP says vs. what MERITS says"*. That requires both planners reachable simultaneously. OTP 2.x dropped the multi-router model, so the only honest answer is **one OTP container per session**, behind nginx routes.

```
nginx
├── /otp/nap-fr-2026-q2/    → otp-nap-fr-2026-q2:8080
├── /otp/merits-pilot/      → otp-merits-pilot:8080
└── /otp/nap-it-trenitalia/ → otp-nap-it-trenitalia:8080
```

Each session gets its own inbox subtree, its own graph snapshots, its own rebuild queue, and (optionally) its own real-time updaters. The **dispatcher and admin UI become session-aware**: every upload form, every rebuild button, every audit row carries a `session_id`.

### 5.3 Comparison feature

End users can issue a `compare` request against two session IDs. The journey-search backend fans out, normalises results, and returns:

- itineraries from session A,
- itineraries from session B,
- a diff summary (number of itineraries, common legs, time-to-arrival deltas, missing services).

This is precisely the workflow that justifies running two ingestion pipelines in parallel during the MERITS migration.

### 5.4 Resource implications

Each running OTP serves a graph in heap. France-wide = 16 GB heap. Two parallel France-wide sessions therefore want a 64 GB VPS minimum. For a demonstrator, **scope sessions down to a region** (e.g. Île-de-France or Grand Est) to keep heap to 4 GB each — three sessions then fit comfortably on a 32 GB VPS.

Detailed lifecycle, storage layout, and API in `VIATOR-technical-spec.md` §4.

### 5.5 Search analytics — full request and response, version-anchored

The default end-user UX is **one click → fanout**: the user issues a single search; the backend queries every fanout-enabled session in parallel, merges results, flags each trip with its origin (`NAP_ONLY` / `MERITS_ONLY` / `BOTH`), and records everything for reporting.

Storage is structured for analytics, not opaque OTP blobs:

- **`journey_searches`** — one row per user request (origin, destination, time, modes, status).
- **`journey_search_executions`** — one row per (search × session) pair, anchored to the **exact `graph_snapshot_id`** that answered. This is how timetable versioning works: every recorded trip is traceable to the precise build, fed by the precise input files, dated to the minute.
- **`journey_trips`** — every itinerary OTP returned, decomposed into structured columns plus a JSONB `legs` array. Carries a `trip_signature` (UIC-stop-based canonical hash) for cross-session equivalence.
- **`journey_trip_provenance`** — derived view: which sessions returned which `trip_signature`. Origin flags (`NAP_ONLY` / `MERITS_ONLY` / `BOTH` / `ALL`) come from this view, never stored, never stale.

Reports this enables (all from one model):

- **Top O&D pairs** per session and period.
- **Per-user / per-session volume** and response-time percentiles (p50/p95/p99).
- **Trip-source distribution** — what % of trips appear in NAP only / MERITS only / both, and how that ratio evolves over time.
- **Comparison divergence** — O&D pairs where the best-itinerary delta between sessions exceeds a threshold.
- **Version-diff** — between two `graph_snapshots` of the same session: which trips are new / lost / improved / regressed.
- **Replay** — re-issue a batch of historical searches against the current `graph_snapshot` to detect "did this rebuild actually fix the no-route searches we logged last month?".
- **Unmatched-trips diagnostic** — trips that didn't match across sessions and why (missing UIC code, route-name format drift, etc.).

The replay feature is the **explicit timetable-versioning loop**: feed → build → snapshot → searches → replay → confirm an upgrade actually fixed something.

#### Two-level timetable versioning

Replay only makes sense **within the same timetable calendar**. Replaying a March 2026 search against an October 2026 graph is meaningless — the service calendar itself changed. Versioning is therefore split:

- **Main version** — the calendar period, encoded as an **ISO-week range** (e.g. `2026-W14_2026-W39`). Auto-derived from the feed's calendar bounds; content manager can override. ISO-week is honest across operators with non-aligned service periods (a German "Sommerfahrplan" and a French "service estival" don't have to share a label).
- **Update version** — sequential patch number within a main version (1, 2, 3, …).

Replay and version-diff endpoints **enforce same-main-version**: snapshots from different main versions are rejected with an explanation. A separate "main-version transition" report describes high-level deltas across calendars but does not pretend to be a regression report.

#### Master data — the cross-source spine

Cross-session comparison only works if VIATOR has a stable view of "the same station" and "the same service" across NAP, MERITS, Trenitalia, and future feeds. Three master tables hold this reference data:

- **`master_stations`** — UIC-keyed registry, seeded from **[Trainline-eu/stations](https://github.com/trainline-eu/stations)** (~12k stations, ODbL, includes UIC + UIC8 SNCF + per-operator codes + multilingual names + meta-station hierarchy). Periodic refresh (monthly default). **Conflict policy: row-level lock with drift surfacing** — local edits flip a row's `source` to `manual` and prevail over future Trainline updates; upstream changes to those rows are captured in a `master_stations_pending_drift` table and surfaced in the admin UI for periodic reconciliation. Our authority is preserved, but we never silently miss a useful upstream improvement.
- **`route_aliases`** — name equivalences across sources and across time (e.g. `"TGV"` ⇄ `"TGV INOUI"` since rebrand). Editable by content managers; bootstrapped empty; populated as the unmatched-trips report flags drift.
- **`master_carriers`** — RICS code dictionary; used by the OJP adapter and MCT enforcement.

The trip_signature canonicaliser consults `master_stations` (to resolve any source's stop_id to UIC) and `route_aliases` (to canonicalise names) before hashing. So adding an alias retroactively helps **future** searches without rewriting historical signatures.

Master-data write access (stations, aliases, carriers, drift resolution, refresh triggers) is open to **content managers and platform admins**. It's operational, frequent work — too much friction if locked to platform admins.

#### Bootstrap iteration — twin-NAP validation

Until real MERITS feeds are available, we validate the multi-session and fanout machinery by running **two sessions with byte-identical NAP inputs** (`nap-fr-control` and `nap-fr-as-merits`). Both included in fanout.

Because the inputs are identical, both sessions should compute the same `feed_signature` and produce equivalent graphs. Expected outcome: **every trip flagged `BOTH`, version-diff empty, trip-source-distribution at 100% "both"**. Any deviation is a bug in our plumbing — not a real comparison signal — to fix before any real second source is wired in.

When MERITS is wired later, `nap-fr-as-merits` is dropped from fanout (kept for history) and `merits-pilot` takes its place. The earlier "perfect mirror" reports become the baseline that proves any subsequent divergence is real, not noise.

Detail in `VIATOR-technical-spec.md` §§6, 7, 8, and 14.

### 5.6 Platform configuration via admin UI

Mirroring the OSCAR pattern (`server_config` key-value table + schema-validated PATCH endpoint), VIATOR exposes runtime-editable settings to platform admins. No env-file edits needed for day-2 operations:

- **SMTP** — host, port, secure mode, user, password, from-address. Test-send button.
- **Concurrency limits** — max concurrent journey requests, max concurrent rebuilds, max concurrent uploads, journey timeout. These are the levers to keep the VPS from collapsing under demo load.
- **Registration** — whether registration is open, default role assignment.
- **Audit retention** — days before audit rows are pruned.

Sensitive values (SMTP password) are masked in `GET` responses; PATCH skips masked sentinels. All changes are written to the audit log with the actor's email.

Detail in `VIATOR-technical-spec.md` §12.

---

## 6. Roadmap summary

| Phase | Scope | Outcome |
|---|---|---|
| **0 — Setup** | VPS, Docker, nginx, TLS, base auth | Empty stack, ready to receive feeds |
| **1 — MVP routing** | OTP + GTFS SNCF + OSM France + upload UI for GTFS/CSV | Working journey planner via OTP GraphQL; MCT and stations stored separately |
| **2 — Identity & RBAC** | Three roles (platform admin / content manager / end user), open self-registration with email confirmation, audit log | Multi-user-safe demonstrator; OSCAR pattern reused (re-implemented in Python) |
| **2b — Platform config UI** | DB-backed runtime config (SMTP, concurrency limits, registration policy, retention), schema-validated, sensitive-field masking | Day-2 operations without redeploying or editing env files |
| **3 — Multi-session** | Sessions become first-class; inbox, graph, OTP container per session; admin UI to create/destroy sessions; comparison endpoint | Run NAP and MERITS in parallel; compare planner outputs |
| **3a — Master data foundation** | `master_stations` (Trainline-seeded), `route_aliases`, `master_carriers` (RICS); admin UI to edit; periodic refresh | Stable cross-source spine for trip-signature, OJP, MCT, geocoding |
| **3b — Search analytics + versioning** | Multi-table recording (`journey_searches` + executions + trips), graph snapshots with two-level versioning (main + update), trip-signature provenance, fanout endpoint, replay & version-diff reports | Activity tracking + the report that proves running NAP+MERITS side-by-side is worth it + ability to prove timetable bug fixes |
| **4 — Branded journey UI** | Static MapLibre + GraphQL frontend at `/`, session selector, side-by-side comparison view | Public-facing demonstrator surface for UIC presentations |
| **5 — OJP exposure** | OJP adapter in front of each session, MCT enforcement, UIC code translation via stations table | External consumers can query OJP `TripRequest` with PRR-correct connections |
| **6 — NeTEx-FR ingestion** | NeTEx-FR → Nordic converter, OTP rebuild from NeTEx | Full alignment with French regulatory format; richer fare and interchange data |
| **7 — MERITS as primary source** | Switch a "merits-prod" session to pull directly from MERITS (timetables, stations, MCT). Decommission direct NAP polling once coverage is sufficient. | Single, UIC-aligned upstream replaces N national NAPs |
| **8 — Multi-feed / multi-region** | Add IDFM, regional TER, cross-border feeds (e.g. Trenitalia France); per-tenant config | Production-grade EU-facing planner |

---

## 7. Source references

- [OpenTripPlanner — GitHub](https://github.com/opentripplanner/OpenTripPlanner)
- [OpenTripPlanner — NeTEx and SIRI compatibility](https://docs.opentripplanner.org/en/latest/features-explained/Netex-Siri-Compatibility/)
- [OpenTripPlanner — APIs overview](https://docs.opentripplanner.org/en/latest/apis/Apis/)
- [Issue #3640 — EPIP support in OTP](https://github.com/opentripplanner/OpenTripPlanner/issues/3640)
- [Issue #2753 — Implement OJP in OTP](https://github.com/opentripplanner/OpenTripPlanner/issues/2753)
- [Issue #4896 — OJP support in OTP](https://github.com/opentripplanner/OpenTripPlanner/issues/4896)
- [openmove/ojp-middleware](https://github.com/openmove/ojp-middleware)
- [VDV OJP specification](https://github.com/VDVde/OJP)
- [Profil France NeTEx — index](https://normes.transport.data.gouv.fr/normes/netex/)
- [transport.data.gouv.fr — Réseau SNCF TGV, Intercités et TER](https://transport.data.gouv.fr/datasets/horaires-sncf)
- [transport.data.gouv.fr — Gares de voyageurs du RFN](https://transport.data.gouv.fr/datasets/gares-de-voyageurs-1)
- [transport.data.gouv.fr — Liste des gares](https://transport.data.gouv.fr/datasets/liste-des-gares)
- [data.gouv.fr — Temps de correspondance minimaux SNCF Voyageurs](https://www.data.gouv.fr/en/datasets/temps-de-correspondance-minimaux-entre-operateurs-sncf-voyageurs/)
- [SNCF Open Data — Temps correspondance minimaux](https://ressources.data.sncf.com/explore/dataset/temps-correspondance-minimaux/)
