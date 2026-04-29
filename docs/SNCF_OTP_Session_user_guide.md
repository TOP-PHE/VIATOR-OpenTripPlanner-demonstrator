# VIATOR — SNCF / NAP OTP session user guide

A practical, click-through walkthrough for setting up a VIATOR session that
pulls SNCF rail timetables via the French National Access Point
(`transport.data.gouv.fr` mirrors), builds an OpenTripPlanner graph, and
serves journey queries.

The same pattern works for other NAPs (Deutsche Bahn `mobilithek.info`,
Trenitalia `dati.mit.gov.it`, etc.) — just substitute the source URLs.

> **Audience:** platform admins or content managers operating an installed
> VIATOR stack. Assumes you've completed `docker/INSTALL.md` through §10
> (admin UI live under HTTPS).

---

## 1. What an OTP session needs

An OpenTripPlanner instance routes journeys by combining two kinds of data:

| Kind | Why OTP needs it | How it's used |
|---|---|---|
| **Public-transport timetable** (GTFS or NeTEx) | Stops, lines, trips, calendars, fares | The transit graph — what trains run, when, between which stops |
| **Street network** (OSM PBF) | Walking / cycling paths, roads, sidewalks | First-mile and last-mile routing, transfers between stops |

Without both, OTP either can't load a graph (no timetable) or can only do
"transit-only" routing without realistic walk legs (no street data).

VIATOR also stores two **runtime** files for SNCF that aren't yet fed into
OTP — they're staged for the OJP adapter (Phase-3 milestone):

- **MCT** (minimum connection times) — how long it takes to change trains
  at a given station
- **Stations CSV** — enrichment data (station amenities, accessibility flags,
  exact platform coords)

These four file types are the SNCF-NAP "set." The session ingests them, the
build pipeline bakes them into a graph, and the promote step exposes the
graph behind a per-session URL.

---

## 2. The four SNCF data sources

### 2.1 GTFS — the timetable (required)

| Property | Value |
|---|---|
| URL | `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip` |
| Size | ~50 MB compressed, ~600 MB uncompressed |
| Refresh cadence on the SNCF side | Daily (typically refreshed overnight) |
| Format | GTFS Schedule (RFC 4180 CSVs in a ZIP), unmodified Google standard |
| Coverage | All SNCF rail services nationwide — TGV, Intercités, TER, Transilien |
| What's inside | `agency.txt`, `routes.txt`, `stops.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt`, `transfers.txt` |
| VIATOR config key | `gtfs` |
| Inbox subfolder after dispatch | `inbox/<sid>/gtfs/` |
| Triggers OTP rebuild? | **Yes** |

This is the **non-negotiable** input for a SNCF session. Everything else can
be optional or deferred; without GTFS, OTP has no timetable to route against.

### 2.2 OSM PBF — the street network (required)

| Property | Value |
|---|---|
| URL — Île-de-France only | `https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf` |
| URL — full France | `https://download.geofabrik.de/europe/france-latest.osm.pbf` |
| Size | IDF: ~250 MB · France: ~4 GB |
| Refresh cadence | Geofabrik rebuilds nightly |
| Format | OpenStreetMap Protocol Buffer (PBF) |
| Coverage | Streets, footpaths, roads, sidewalks, level crossings — anything OSM-mapped |
| What VIATOR uses it for | First/last-mile walking + cycling routing; computes the geographic shape of stop-to-stop transfers |
| VIATOR config key | `osm_pbf` |
| Inbox subfolder after dispatch | `inbox/<sid>/osm/` |
| Triggers OTP rebuild? | **Yes** |

> **Recommendation for a first session: use the IDF-only PBF.** The full
> France PBF is 16× larger and pushes graph build to ~30–60 min instead of
> ~10 min. You can switch to France-wide later by saving the new config URL,
> clicking "Refresh sources now", then "Rebuild graph". The session keeps
> serving the old graph until the new build completes — atomic swap via the
> `current` symlink.

The two files **must agree on geography**: a France-wide GTFS with an IDF-
only PBF means OTP can route in IDF but not in the rest of France
(walk-leg gaps mean trips outside IDF degrade to transit-only or fail).
For a Paris demo this is perfectly fine — just be aware.

### 2.3 SNCF MCT — minimum connection times (optional, Phase-3)

| Property | Value |
|---|---|
| Source page | https://ressources.data.sncf.com/explore/dataset/temps-correspondance-minimaux/ |
| Format | CSV inside a ZIP, refreshed monthly |
| Coverage | Every SNCF station with multi-platform configurations |
| What it tells you | "At Lyon Part-Dieu, allow at least 6 minutes to change between TGV platforms 1–4 and TER platforms F–G" |
| VIATOR config key | `mct` |
| Inbox subfolder after dispatch | `inbox/<sid>/runtime/SNCF-MCT/latest.zip` |
| Triggers OTP rebuild? | **No** (stored only) |
| Currently used by OTP? | **No** — Phase-3 OJP adapter milestone |

**Why we still ingest it now:** the OJP adapter (Phase-3) will use these
when stitching multi-leg itineraries. Storing them today means the day
the OJP layer lands, the MCT data is already there. No retrospective
download needed.

If you skip this for the first session, nothing breaks. The session won't
respect MCT during transfers — OTP uses its own default `transferTime`
heuristic (usually 60 s).

### 2.4 SNCF Stations CSV — station enrichment (optional, Phase-3)

| Property | Value |
|---|---|
| Source page | https://ressources.data.sncf.com/explore/dataset/gares-de-voyageurs/ |
| Direct CSV | `https://ressources.data.sncf.com/explore/dataset/gares-de-voyageurs/download/?format=csv` |
| Format | UTF-8 CSV |
| Coverage | Every SNCF passenger station — codes, names, postal codes, platform counts, accessibility, parking, services |
| What it tells you | "Gare de Lyon — UIC 8768603, 4 train types served, 28 platforms, accessible, taxi rank, parking spaces 1750" |
| VIATOR config key | `stations` |
| Inbox subfolder after dispatch | `inbox/<sid>/runtime/SNCF-Stations/latest.csv` |
| Triggers OTP rebuild? | **No** (stored only) |
| Currently used by OTP? | **No** — used by the master_stations enrichment job (Phase-3) |

The **`master_stations` table** (see §4) is the canonical European station
registry used by VIATOR's trip-signature canonicaliser, journey UI
autocomplete, and reports. The SNCF Stations CSV will be cross-referenced
with it during the Phase-3 enrichment step to fill in trigramme codes,
accessibility flags, etc. for French stations specifically.

For now, it's stored but unused. Same logic as MCT — ingest today, exploit
when the layer lands.

---

## 3. How VIATOR turns these files into an OTP graph

```
┌────────────────────────────┐
│ Admin UI: Configure        │  PATCH /api/sessions/<sid> { config: { sources: { gtfs, osm_pbf, ... } } }
│ — saves URLs to            │
│   session.config.sources   │
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│ Refresh sources now        │  POST /api/sessions/<sid>/sources/refresh
│ — httpx-streams each URL   │
│ — runs ingestion.dispatch  │  (or use Upload form for manual files)
└────────────┬───────────────┘
             │
             ▼ files placed in inbox/<sid>/<kind>/, rebuild queued
┌────────────────────────────┐
│ RebuildJob row             │  POST /api/sessions/<sid>/rebuilds (manual) or auto-queued by dispatch
│ status='pending'           │
└────────────┬───────────────┘
             │  (worker polls every 15 s, debounces 30 min by default)
             ▼
┌────────────────────────────┐
│ docker compose run         │  worker shells out to one-shot otp-build container
│   --rm otp-build           │  reads inbox/<sid>/{gtfs,osm}/  → writes graph.obj
└────────────┬───────────────┘
             │
             ▼ on success
┌────────────────────────────┐
│ Graph promoted             │  worker mv graph.obj → graphs/<sid>/<timestamp>/graph.obj
│ symlink current →          │  ln -sfn <timestamp> graphs/<sid>/current
│   <timestamp>/             │
└────────────┬───────────────┘
             │
             ▼ session state auto-advances populated → graph_built
┌────────────────────────────┐
│ Admin UI: Promote          │  POST /api/sessions/<sid>/promote
│ — sets state='serving'     │
│ — regenerates compose +    │  app.sessions_orchestrator.regenerate
│   nginx fragments          │
│ — touches reload trigger   │
└────────────┬───────────────┘
             │
             ▼ worker on next tick (≤15 s)
┌────────────────────────────┐
│ docker compose -p viator   │  brings up otp-<sid> container
│ up -d                      │
│ + docker exec nginx        │  picks up /otp/<sid>/ route
│   nginx -s reload          │
└────────────────────────────┘
             │
             ▼ session is now in fanout pool
       Journey queries work.
       /otp/<sid>/actuators/health → UP
```

### 3.1 Upload / Refresh sources

**Refresh sources** is the typical path: configure once, click refresh
whenever upstream has new data. The endpoint streams each URL into a
staging file, runs `ingestion.dispatch`, which:

1. Verifies the detected file format matches what the config key implies
   (e.g. `gtfs` should produce a file detected as `GTFS`).
2. Moves the staged file into the right per-kind inbox subfolder, **renamed
   to a canonical name** so the OTP build config can reference it
   predictably:

   | Upstream URL filename | Stored as |
   |---|---|
   | `Export_OpenData_SNCF_GTFS_NewTripId.zip` | `inbox/<sid>/gtfs/gtfs.zip` |
   | `ile-de-france-latest.osm.pbf` | `inbox/<sid>/osm/osm.pbf` |
   | `france-latest.osm.pbf` | `inbox/<sid>/osm/osm.pbf` |
   | (any NeTEx zip) | `inbox/<sid>/netex/netex.zip` |
   | MCT/Stations CSVs | `inbox/<sid>/runtime/SNCF-{MCT,Stations}/latest.csv` |

   The mapping is the `STAGE_INTO_OTP_INBOX_FILENAME` dict in
   `app/ingestion.py` — kept in sync with `docker/otp/build-config.json`'s
   `transitFeeds` and `osm` entries.
3. Rotates any prior file of the same kind (`.old` suffix) so the new
   build sees only the fresh one. The entrypoint's `compgen -G "*.zip"`
   glob excludes `.zip.old` / `.pbf.old` rotated copies.
4. Enqueues a `RebuildJob` for kinds that warrant one (GTFS, OSM-PBF,
   NeTEx-Nordic, NeTEx-EPIP).

**Upload** is the manual fallback: drag a file in via the per-session
"Upload" form when you don't have a URL (e.g. a custom test feed or a
file from a partner that isn't on a public CDN). The same canonical-
rename happens — declared standard `GTFS` writes to
`inbox/<sid>/gtfs/gtfs.zip`, no matter what the upload's original
filename was.

#### File reference — what each path is and who owns it

| Path | Owner | Purpose |
|---|---|---|
| `inbox/<sid>/gtfs/gtfs.zip` | dispatcher | the GTFS feed OTP reads |
| `inbox/<sid>/osm/osm.pbf` | dispatcher | the OSM PBF OTP reads |
| `inbox/<sid>/netex/netex.zip` | dispatcher | NeTEx alternative (not used for SNCF) |
| `inbox/<sid>/runtime/SNCF-MCT/latest.csv` | dispatcher | stored, not yet read by OTP (Phase-3) |
| `inbox/<sid>/runtime/SNCF-Stations/latest.csv` | dispatcher | stored, not yet read by OTP (Phase-3) |
| `inbox/<sid>/<kind>/*.old` | rotation | previous file, kept for one cycle |
| `inbox/<sid>/_staging/` | refresh-sources | partial downloads in flight, cleaned up after dispatch |
| `docker/otp/build-config.json` | repo | tells OTP which files to read by name |
| `docker/otp/router-config.json` | repo | runtime config for the otp-`<sid>` serving container (timeouts, etc.) |
| `docker/otp/entrypoint.sh` | repo | copies inbox → tmp build dir, runs `java -jar otp.jar --build`, moves graph.obj into the volume |
| `app/ingestion.py` `STAGE_INTO_OTP_INBOX_FILENAME` | repo | the canonical-name map — the source of truth for filenames |
| `graphs/<sid>/<timestamp>/graph.obj` | worker | a built graph snapshot; `current` symlink points at the most recent successful one |

### 3.2 The graph build (`otp-build` one-shot container)

Triggered automatically when dispatch enqueues a job, or manually via the
"Rebuild graph" button. Either way, the worker picks up the pending
`RebuildJob`, waits out the debounce window (30 min default — configurable
via `DEBOUNCE_SECONDS`), then shells out to:

```bash
docker compose -p viator run --rm \
    -e OTP_HEAP=8g \
    -e OTP_INBOX_DIR=/var/otp/inbox/<sid> \
    otp-build
```

`otp-build` is a one-shot container (Eclipse Temurin JRE 25 + the OTP
shaded jar) that:

1. Reads `gtfs/*.zip` and `osm/*.osm.pbf` from the inbox.
2. Runs OTP's graph builder (`org.opentripplanner.standalone.OtpMain --build`).
3. Writes `graph.obj` to the graphs volume.

Build time depends on bundle size — ~10 min for IDF, ~30–60 min for
France-wide. Tail the logs from the admin UI's "Refresh job list" button
or `docker compose logs -f worker`.

On success the worker:
- Moves `graph.obj` into a timestamped directory: `graphs/<sid>/<timestamp>/graph.obj`
- Updates the `current` symlink to point at the new timestamp dir
- Prunes all but the most recent 3 timestamp dirs
- Auto-advances the session state to `graph_built`

The atomic symlink swap means a re-build of an already-serving session
takes effect on the next request without dropping any in-flight ones.

### 3.3 Promote to serving

**Promote** is the operator's deliberate "this graph is good, go live" step.
It does three things:

1. Sets `session.state = 'serving'`.
2. Calls `app.sessions_orchestrator.regenerate(db)`, which writes:
   - `docker/generated/docker-compose.sessions.yml` — adds an `otp-<sid>`
     service entry for every session in `serving` state
   - `docker/generated/nginx-sessions.conf` — adds a `location /otp/<sid>/`
     block proxying to `otp-<sid>:8080`
3. Touches `/data/generated/.reload-trigger`.

The worker, on its next tick (≤15 s), notices the trigger file and runs:

```bash
docker compose -p viator up -d            # picks up the new otp-<sid> service
docker exec viator-nginx-1 nginx -s reload # picks up the new /otp/<sid>/ route
```

After this the session is in the fanout pool — `POST /api/journey/fanout`
queries it in parallel with every other `serving` session.

There's a brief (≤15 s) window after promote where `state='serving'` but
the otp container isn't routable yet. Phase-B will close this gap by
having the worker watch DB state instead of the trigger file.

### 3.4 Tracking build progress

OTP graph builds run inside a one-shot `otp-build` container that the
worker spawns via `docker compose run`. The worker captures stdout +
stderr (via `subprocess.run(capture_output=True)`), so OTP's chatter
**doesn't stream to `docker compose logs`** — it lands in the
`rebuild_jobs.log` column once the process completes. During the
build, you have to use external signals (CPU, memory, I/O) to track
progress.

#### Five ways to verify the build is alive

```bash
# 1. The otp-build container is currently up
docker ps | grep otp-build
# Expected line:
#   <CID>  ghcr.io/top-phe/viator-otp:latest  ...  Up X minutes  ...
#   viator-otp-build-run-XXX
```

```bash
# 2. CPU + memory + I/O — most informative single command
docker stats --no-stream
# Healthy build: otp-build at 100-700% CPU + multi-GB MEM + several GB BLOCK I/O read
```

CPU >100% means OTP is using multiple cores (it parallelises OSM
parsing and transit-graph construction). Memory should grow steadily
through the build then plateau. Block-I/O *read* climbs as PBF + GTFS
are streamed; block-I/O *write* spikes only at the very end when
`graph.obj` is serialised.

```bash
# 3. Worker process state — confirms it's blocked on subprocess.run
docker compose top worker
# Expected: a single python -m app.worker process, sleeping
```

```bash
# 4. Latest rebuild_jobs row — definitive "is it done yet?"
docker compose exec postgres psql -U viator -d viator -c \
  "SELECT id, status, started_at, finished_at,
          EXTRACT(EPOCH FROM (COALESCE(finished_at, NOW()) - started_at)) AS seconds_elapsed,
          length(log) AS log_chars
   FROM rebuild_jobs ORDER BY created_at DESC LIMIT 1;"
```

| `status` | `finished_at` | meaning |
|---|---|---|
| `pending` | NULL | worker hasn't picked it up yet — debounce window not elapsed, or worker stopped |
| `running` | NULL | OTP is actively building, `seconds_elapsed` keeps growing |
| `done` | populated | success — graph.obj written, session auto-advanced to `graph_built` |
| `failed` | populated | OTP crashed — read the `log` column for details |

```bash
# 5. Read the OTP build log after completion (success or failure)
docker compose exec postgres psql -U viator -d viator -t -c \
  "SELECT log FROM rebuild_jobs ORDER BY created_at DESC LIMIT 1;"
```

The log contains both stdout and stderr from the otp-build container
(separated by `--- stderr ---`). It's truncated to the last ~32 KB
to bound row size — sufficient for the OTP build's tail output.

#### Phase timing — what to expect at each minute

Numbers are rough but representative. Bigger bundles scale roughly
linearly.

| Phase | IDF (~250 MB PBF + ~50 MB GTFS) | France-wide (~4 GB PBF + ~50 MB GTFS) | What's happening |
|---|---|---|---|
| Staging | <30 s | <2 min | `entrypoint.sh` copies inbox files to a tmp build dir |
| GTFS parsing | 1–3 min | 1–3 min | reading `agency.txt`, `routes.txt`, `stops.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt` |
| **OSM PBF parsing** | 3–5 min | **15–30 min** ← longest single phase | iterating every way + node in the PBF |
| Street graph build | 1–2 min | 5–10 min | snapping stops to the nearest streets, building walk edges |
| Transit graph | 1–2 min | 3–5 min | indexing trips by stop, computing departure tables |
| Transfer computation | 1–3 min | 5–10 min | for each pair of nearby stops, compute the walking transfer |
| Serialisation | <30 s | 1–2 min | binary-write `graph.obj` to disk; block-I/O write spikes |
| **Total** | **~10–15 min** | **~30–60 min** | |

#### Symptom-by-symptom progress reading

| Observation | Likely phase | Healthy signal |
|---|---|---|
| CPU 200–700%, MEM growing fast (1→8 GB), I/O read climbing | OSM PBF parsing | yes — single most CPU-intensive phase |
| CPU 100–200%, MEM growing slowly, I/O read flat | Transit graph or transfer computation | yes — less parallel, more memory churn |
| CPU 100%, MEM plateaued, I/O write jumping from 0 to GB | Serialisation (final phase) | yes — almost done |
| CPU drops to 0%, container still in `docker ps` | Container teardown / worker copying graph.obj into final volume | yes |
| Container disappeared from `docker ps`, `rebuild_jobs.status` flips to `done` or `failed` | Build complete | check status to know which |
| CPU 0%, MEM stuck, no disk I/O for >5 min | **Stuck** | unhealthy — see troubleshooting §6 |
| `OutOfMemoryError: Java heap space` in logs | Heap too small | bump `OTP_BUILD_HEAP` in `.env` |
| `OOM killed` (container exits with code 137) | Host RAM exhausted | upgrade VPS, raise swap, or use a smaller bundle |

#### Reading the log live (best-effort)

The worker doesn't stream output to host docker logs, but you can
peek at the otp-build container's stderr/stdout via:

```bash
docker logs --follow $(docker ps --format '{{.Names}}' | grep otp-build)
```

(returns nothing if no otp-build container is currently up.) This works
for the *currently-running* otp-build process. Once it exits and the
worker captures+stores the log, this stream goes away — at that point
you read the final log via the SQL query above.

#### After the build finishes

`rebuild_jobs.status='done'` triggers the worker to:

1. Move `graph.obj` from the tmp build dir into
   `graphs/<sid>/<timestamp>/graph.obj`
2. Update the `current` symlink: `graphs/<sid>/current → <timestamp>/`
3. Prune all but the most recent 3 timestamped graph dirs
4. Auto-advance `session.state` from `populated` → `graph_built`

The state badge on the admin Sessions page flips from amber `populated`
to amber `graph_built`. Click **Promote to serving** to start the
otp-`<sid>` container loading the graph and add it to the fanout pool.

---

## 4. The station code list (`master_stations`)

### 4.1 What it contains

The `master_stations` table is VIATOR's canonical European passenger-
station registry. One row per station, keyed by the **UIC code**
(7-digit identifier per UIC Leaflet 920-14):

| Column | Meaning | Example |
|---|---|---|
| `uic` | UIC code (PK) | `8727100` (Paris-Gare-de-Lyon) |
| `name` | Display name | `Paris Gare de Lyon` |
| `country_iso` | ISO 3166-1 alpha-2 | `FR` |
| `latitude`, `longitude` | WGS84 | `48.84432, 2.37408` |
| `trigramme_sncf` | French 3-letter code | `FRPLY` (full) or `PLY` |
| `db_code` | Deutsche Bahn IBNR | (null for FR-only stations) |
| `trenitalia_code` | Trenitalia code | (null for FR-only) |
| `is_main_station` | Heuristic for "primary" station in a city | `true` for Paris-Lyon, `false` for Vert-de-Maisons |
| `source` | Where this row came from: `trainline` / `manual` / `sncf` | `trainline` |
| `updated_at` | Last touched | timestamp |

### 4.2 What it's used for

Three places in VIATOR rely on `master_stations`:

1. **`journey/signature.py`** — when recording a search execution, each
   trip's leg stops are looked up against `master_stations` (via
   `stations_xref`) so the canonical UIC code is used in the trip
   signature instead of the per-feed local stop ID. This makes
   "same trip" recognisable across different feeds (NAP vs MERITS).
2. **Journey UI From/To autocomplete** — `/journey` issues
   `GET /api/master/stations?q=<query>` to populate the search dropdown.
   Empty `master_stations` → empty autocomplete → users have to know lat/lon
   coordinates by heart.
3. **Reports** — `unmatched-trips` and `compare-divergence` reports use
   UIC codes from `master_stations` to bucket results.

### 4.3 Where the rows come from

Three sources, in order of precedence:

| Source | When it runs | What it loads |
|---|---|---|
| **Trainline-eu/stations** (ODbL) | `MASTER_REFRESH_CRON` schedule (default weekly) + manual button | ~3000 European passenger stations with UIC, name, country, lat/lon, trigramme/IBNR/Trenitalia codes |
| **SNCF Stations CSV** (Phase-3) | Same cron, after Trainline | Enriches French rows with platform counts, accessibility, etc. |
| **Manual edits** | Admin UI Edit button | Highest-precedence — flips `source` to `manual`, never overwritten by automatic refresh |

### 4.4 Why your `master_stations` table is empty right now

On a fresh VIATOR install:

- The `MASTER_REFRESH_CRON` schedule (default `0 3 * * 1` — 3am every Monday)
  has been registered with APScheduler at process startup
- But APScheduler's cron triggers only fire **at the configured time**, not
  immediately on startup
- So until the first scheduled run, the table is empty

This is a deliberate "no surprise downloads at boot" design. It does mean
the very first session you create has empty autocomplete and no trip-
signature canonicalisation until you trigger the refresh.

### 4.5 How to populate it now

Two equivalent paths:

**Option A — UI button (recommended):**

> Browse to **`https://<your-host>/admin/master/stations`** → click
> **"Refresh from Trainline"** at the top of the search form → confirm.

The button hits `POST /api/master/stations/refresh-trainline`, which:

1. Downloads `https://github.com/trainline-eu/stations/raw/master/stations.csv` (~5 MB)
2. Parses it (UIC codes, names, country flags, lat/lon, trigramme/IBNR/Trenitalia codes)
3. Inserts rows where the UIC is new
4. Updates rows where `source != 'manual'` AND fields differ
5. Detects "drift" on rows where `source == 'manual'` AND fields differ —
   adds a `master_station_pending_drift` entry for operator review
6. Returns `{added: N, updated: N, skipped_manual: N, pending_drift: N}`

After ~5 s the toast tells you the counts. The search box now returns
results; the journey UI's From/To autocomplete works.

**Option B — API:**

```bash
curl -X POST https://<your-host>/api/master/stations/refresh-trainline \
  -H "Authorization: Bearer <jwt-of-content_manager-or-platform_admin>"
# returns: {"added": 3127, "updated": 0, "skipped_manual": 0, "pending_drift": 0}
```

### 4.6 Drift management — when Trainline updates conflict with your edits

The flow:

1. You edit `Paris Gare de Lyon` in the UI to change its `name` to
   `Paris-Gare-de-Lyon (Hall 1)`. Row `source` flips to `manual`.
2. A week later, Trainline pushes a global rename, e.g. the canonical
   name is now `Paris Gare de Lyon Hall 2`.
3. The next scheduled refresh sees: row exists, `source=manual`, fields
   differ. It does **not** overwrite your edit.
4. Instead, it inserts a row in `master_station_pending_drift` capturing
   what Trainline now says.
5. The Master Stations admin UI shows the count: "Pending drift (1)".
   Click the section to expand and decide:
   - **Keep ours** → drift is dismissed; your manual value stays
   - **Adopt Trainline** → all fields adopt the new Trainline snapshot;
     `source` flips back to `trainline`
   - (API also supports `adopt_fields` for partial adoption)

Drift rows are audit-logged so you have a paper trail of what
adopted/dismissed which Trainline change.

---

## 5. End-to-end walkthrough — first SNCF Île-de-France session

This is the click-by-click I recommend for your first session. ~15 min
total (10 min for the OTP build).

### 5.1 — Populate master_stations first

Go to **`https://<your-host>/admin/master/stations`** → click
**"Refresh from Trainline"** → confirm. After the toast appears, search
"Paris" — you should see ~10–20 rows (Paris stations). This will let the
journey UI work later.

### 5.2 — Create the session

Go to **`https://<your-host>/admin/sessions`** → expand **"+ Create a new session"**:

| Field | Value |
|---|---|
| ID | `nap-fr-sncf-idf` |
| Name | `NAP France — SNCF Île-de-France` |
| Category | **NAP** |
| Include in fanout immediately | ✅ |

Submit. Row appears with state `created`.

### 5.3 — Configure sources

Click the **▸** next to the new row → expand details → in the **Configure
sources** form:

| Field | Value |
|---|---|
| GTFS URL | `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip` |
| OSM PBF URL | `https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf` |
| MCT URL | (leave blank for now) |
| Stations CSV URL | (leave blank for now) |

Click **"Save config"** → toast: "Config saved for nap-fr-sncf-idf".

### 5.4 — Refresh sources (download)

Click **"Refresh sources now"**. The button shows "Downloading…" for
2–5 minutes (depends on VPS bandwidth — ~300 MB total). Toast confirms
when done: "Fetched: gtfs, osm_pbf".

State badge auto-advances to `populated`. A pending `RebuildJob` is
queued.

### 5.5 — Trigger the build

Two choices:

- **Wait** ~30 minutes — the worker's debounce window passes and the queued
  job auto-runs. Less attention required.
- **Click "Rebuild graph"** — re-enqueues (coalesces if one's already pending),
  but **does not bypass the debounce**. Same effect.

For the first build, click "Rebuild graph" to confirm the row is queued.
Wait the debounce then watch the jobs list expand the **Build & Promote**
section → click **"Refresh job list"** every minute or so. Status flips
`pending → running`. After ~10 min for IDF, `running → done`. State
badge auto-flips to `graph_built`.

> **Tip:** if you want builds to start immediately for testing, set
> `DEBOUNCE_SECONDS=30` in `.env` and `docker compose up -d --force-recreate worker`.
> Don't leave it at 30 in production — debounce protects against
> thundering-herd uploads.

### 5.6 — Promote to serving

Once state is `graph_built`, click **"Promote to serving"** → confirm
dialog → toast: "Promoted nap-fr-sncf-idf (state=serving). Worker will
reload within ~15 s." Page refreshes after 1.5 s — state badge now
`serving`, fanout checkbox now enabled.

The worker on its next tick runs `docker compose up` (otp-nap-fr-sncf-idf
container starts, ~30 s) and `nginx reload` (route activates).

### 5.7 — Smoke check

```bash
# Per-session OTP health
curl https://<your-host>/otp/nap-fr-sncf-idf/actuators/health
# Expected: {"status":"UP"}

# Tiny GraphQL ping
# Note: OTP 2.x exposes the GTFS GraphQL endpoint at /otp/gtfs/v1
# (NOT /otp/gtfs/v1/index/graphql — that's the legacy OTP 1.x form
# at /otp/routers/default/index/graphql).
curl -s "https://<your-host>/otp/nap-fr-sncf-idf/gtfs/v1" \
  -H 'content-type: application/json' \
  -d '{"query":"{ feeds { feedId } }"}' | python3 -m json.tool

# Real journey query (Notre Dame → Louvre, Paris)
curl -X POST https://<your-host>/api/journey/fanout \
  -H "Authorization: Bearer <your-jwt>" \
  -H 'Content-Type: application/json' \
  -d '{
    "from_lat": 48.8566, "from_lon": 2.3522,
    "to_lat":   48.8606, "to_lon": 2.3376,
    "depart_at": "2026-04-29T08:30:00+02:00",
    "modes": ["TRANSIT", "WALK"]
  }' | python3 -m json.tool
```

If you get a JSON response with `trips: [...]`, congratulations — your
SNCF session is live and serving real journeys.

### 5.8 — Try the journey UI

Browse to **`https://<your-host>/journey`**. The From / To autocomplete
should now work (master_stations populated in 5.1). Type "Paris" → pick
a station. Type "Lyon" → pick a station. Submit. Trips render with the
session's origin flag.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Refresh sources now" returns `{"skipped": [{"reason": "unknown source key 'foo'"}]}` | You configured a key not in the recognised list (`gtfs`, `osm_pbf`, `netex_nordic`, `netex_epip`, `mct`, `stations`) | Use one of the recognised keys |
| Refresh succeeds for OSM PBF but build fails with "no GTFS found" | Inbox layout has `osm/` populated but `gtfs/` empty | Check the staging dir for the GTFS download. Was the URL right? Did the upstream server return a redirect to a login page? `curl -sIL <gtfs-url>` from the VPS to verify. |
| OTP build crashes with `OutOfMemoryError` | `OTP_BUILD_HEAP` < what the bundle needs | Bump in `.env`: `OTP_BUILD_HEAP=24g` for France-wide, `OTP_BUILD_HEAP=4g` for IDF. `docker compose up -d --force-recreate worker`. |
| Build stuck at status `pending` for >30 min | Debounce window not yet elapsed | Check the worker debounce: `grep DEBOUNCE_SECONDS /opt/viator/docker/.env`. If too high, lower temporarily. |
| Promote returns 400 "Session must be in state 'graph_built'" | Build hasn't completed (still `populated` or `running`) — or you tried to promote a session that was never built | Wait for state badge to hit `graph_built`. Check job logs in the Build & Promote section. |
| `/otp/<sid>/actuators/health` returns 502 after promote | The worker hasn't run its tick yet | Wait ≤15 s. If still 502, check `docker compose ps` → is `otp-<sid>` Up? `docker compose logs otp-<sid>` will show OTP startup or its error. |
| `/otp/<sid>/...` returns 404 | nginx hasn't picked up the new location block | `docker compose exec nginx cat /etc/nginx/conf.d/sessions/nginx-sessions.conf` — does the location block exist? If not, regenerate didn't run; check web logs for orchestrator errors. |
| Journey UI From/To autocomplete is empty | `master_stations` empty | Click "Refresh from Trainline" on `/admin/master/stations` (§4.5). |
| Master stations refresh button does nothing or 401 | Your JWT cookie expired or you're not logged in as content_manager / platform_admin | Re-log in as a privileged user. |
| Pending drift count keeps growing | Trainline's upstream is changing rows you've manually edited | Walk through the drift queue periodically — adopt or keep — to keep it manageable. Consider whether your manual edits should become canonical via PRs to trainline-eu/stations. |
| Build appears stuck — no log output for >5 min | Normal during OSM parsing or transit-graph phase | See §3.4. Run `docker stats --no-stream` and check `otp-build` CPU. >100% CPU = healthy, just slow; <1% with no I/O = actually stuck (rare; check container status with `docker logs $(docker ps -q -f name=otp-build)`). |
| `rebuild_jobs.log` shows OTP error like `java.io.FileNotFoundException` for a file you uploaded | File didn't get the canonical name (Phase-2 ingestion bug, or pre-`e526d95` deploy) | Verify `inbox/<sid>/gtfs/gtfs.zip` and `inbox/<sid>/osm/osm.pbf` exist. If they have the original upstream name (e.g. `Export_OpenData_SNCF_GTFS_NewTripId.zip`), pull the latest code, rebuild web+worker images, click "Refresh sources now" again. |
| Toast says all sources "Skipped: ... [Errno -2] Name or service not known" | DNS failure inside the container, OR malformed URL in the config form | Check the URLs displayed in the Configure form — pasted URLs sometimes get concatenated (`https://rehttps://...`). Clear each field with Ctrl+A, paste fresh, click Save config, retry Refresh. If URLs look right, check `docker compose exec web getent hosts <hostname>`. If that fails, see the DNS pin in `docker-compose.yml` (8.8.8.8 / 1.1.1.1 on web + worker). |

---

## 7. Where each file ends up — disk layout cheatsheet

After a successful refresh + build + promote on session id `nap-fr-sncf-idf`:

```
/var/lib/docker/volumes/
├── viator_inbox/_data/nap-fr-sncf-idf/
│   ├── gtfs/gtfs.zip                              # current (canonical name — see §3.1)
│   ├── gtfs/gtfs.zip.old                          # previous, rotated by dispatcher
│   ├── osm/osm.pbf                                # current
│   ├── osm/osm.pbf.old                            # previous
│   ├── runtime/SNCF-MCT/latest.csv                # if configured
│   └── runtime/SNCF-Stations/latest.csv           # if configured
│
└── viator_graphs/_data/nap-fr-sncf-idf/
    ├── 20260429-103214/graph.obj                  # most recent build
    ├── 20260427-091122/graph.obj                  # one back
    ├── 20260424-152007/graph.obj                  # two back (worker keeps N=3)
    └── current → 20260429-103214/                 # symlink the otp-<sid> container serves from
```

The host paths above resolve to:

| Volume name | Host path | Mounted in |
|---|---|---|
| `viator_inbox` | `/var/lib/docker/volumes/viator_inbox/_data/` | `web`, `worker` (rw), `otp-build` (ro), `otp-<sid>` (none — graphs only) |
| `viator_graphs` | `/var/lib/docker/volumes/viator_graphs/_data/` | `worker` (rw), `otp-<sid>` (ro) |
| `viator_pgdata` | `/var/lib/docker/volumes/viator_pgdata/_data/` | `postgres` (rw) |

Inside containers the same files are at:

| Container | Inbox path | Graphs path |
|---|---|---|
| `web`, `worker` | `/data/inbox/<sid>/` | `/data/graphs/<sid>/` |
| `otp-build` | `/var/otp/inbox/<sid>/` (ro) | `/var/otp/graph/` (rw, written) |
| `otp-<sid>` (per-session serving) | (none) | `/var/otp/graph/<sid>/current/` (ro) |

`postgres` separately stores the metadata: `sessions`, `uploads`,
`rebuild_jobs`, `master_stations`, `audit_events`, etc. Re-deploys can
nuke the inbox and graph volumes and rebuild from URLs; the Postgres
volume is the only must-back-up data store.

---

## 8. Where to go next

- **Add a France-wide build** — same session, different OSM PBF URL,
  another `Refresh + Rebuild + Promote`. Old graph stays serving until
  the new one is promoted.
- **Add a second comparison session** (MERITS once available, or twin-NAP
  for validation per spec §14) — same workflow, different ID. Toggle
  fanout on both → journey queries hit both in parallel and origin-flag
  the trips (NAP_ONLY / MERITS_ONLY / BOTH).
- **Configure SMTP** in Admin → Configuration so you can use
  email-based registration / password reset (currently admin-create
  only).
- **Schedule auto-refresh** via cron — Phase-3 will read each session's
  `config.sources` automatically. Until then, the manual "Refresh sources
  now" button covers the use case.

---

## 9. Reference — relevant spec sections

- `VIATOR-technical-spec.md` §4 — multi-session model (per-session OTP behind nginx)
- `VIATOR-technical-spec.md` §5 — data ingestion (per-session inbox, dispatch rules)
- `VIATOR-technical-spec.md` §7 — master data (stations, route aliases)
- `VIATOR-technical-spec.md` §9.3 — sessions API (the endpoints this guide drives)
- `VIATOR-technical-spec.md` §9.9 — master data API (`/api/master/stations/*`)
- `VIATOR-technical-spec.md` §11.5 — session lifecycle state machine
- `VIATOR-technical-spec.md` §11.6.5 — rebuilds and promote
- `docker/INSTALL.md` §10 — install context, where this guide picks up
