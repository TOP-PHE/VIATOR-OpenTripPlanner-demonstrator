# VIATOR — `nap-fr-rail` operator guide

A practical, click-through walkthrough for setting up the canonical
**France-wide rail demonstrator session** in VIATOR — the one we call
`nap-fr-rail` throughout this doc — and extending it with additional
operators (IDFM, Trenitalia France, Eurostar) over time.

The session pulls timetables via the French National Access Point
(`transport.data.gouv.fr`) and other operators' open-data feeds, builds
an OpenTripPlanner graph, and serves journey queries. Same pattern works
for non-French NAPs (Deutsche Bahn `mobilithek.info`, Trafiklab Sweden,
etc.) — substitute the URLs.

> **Audience:** platform admins or content managers operating an installed
> VIATOR stack at v0.1.7 or newer. Assumes you've completed
> `docker/INSTALL.md` through §10 (admin UI live under HTTPS).

---

## 1. What an OTP session needs

An OpenTripPlanner instance routes journeys by combining two kinds of data:

| Kind | Why OTP needs it | How it's used |
|---|---|---|
| **Public-transport timetable** (GTFS or NeTEx) | Stops, lines, trips, calendars, fares | The transit graph — what trains run, when, between which stops |
| **Street network** (OSM PBF) | Walking / cycling paths, roads, sidewalks | First-mile and last-mile routing, transfers between stops |

Without both, OTP either can't load a graph (no timetable) or can only do
"transit-only" routing without realistic walk legs (no street data).

VIATOR's **provider-bundle model** (since v0.1.6) lets one session contain
many timetable feeds — one per railway / transit operator. They get merged
into a single OTP graph at build time, with cross-operator transfers
auto-generated between physically-close stops. So a single `nap-fr-rail`
session can carry SNCF Trains + Île-de-France Mobilités + Trenitalia France
+ Eurostar all at once, and the journey UI returns multi-operator
itineraries (TGV → RER → walk; Frecciarossa as alternative to TGV INOUI;
Eurostar continuations).

The OSM PBF stays at session level (one street network per graph) and must
cover every region your providers reach into.

---

## 2. The provider-bundle model

### 2.1 What a provider is

A provider is one railway / transit operator that publishes timetable data.
In `session.config.sources.providers[]`, each entry has:

| Field | Required? | Purpose |
|---|---|---|
| `id` | yes | OTP `feedId` namespace prefix on every stop_id from this feed (e.g. `SNCF:OCETrain-87271007`). Format `^[A-Z][A-Z0-9_-]{1,15}$`. Unique per session. |
| `label` | yes | Operator-facing display name (often the railway's brand, e.g. "SNCF Trains") |
| `country_iso` | yes for routing | ISO-2 code (FR, DE, IT…). Triggers the **country-gate**: at session-save time, the API checks `master_stations` has at least one row for that country. |
| `timetable.format` | yes | `gtfs`, `netex_nordic`, or `netex_epip`. NeTEx-FR is intentionally absent — OTP can't read it. |
| `timetable.url` | required for routing | http(s) URL to the GTFS / NeTEx ZIP archive |
| `gtfs_rt.alerts_url` | optional | GTFS-RT service-alerts endpoint, polled every 1 min by OTP |
| `gtfs_rt.trip_updates_url` | optional | GTFS-RT trip-updates endpoint, same polling |
| `gtfs_rt.vehicle_positions_url` | optional | GTFS-RT vehicle-positions endpoint |
| `mct_url` | optional | Minimum-connection-times CSV (Phase-3 OJP integration; stored but not yet wired) |
| `stations_csv_url` | optional | Provider's own station list — augments `master_stations` cross-references |

The UI presents each provider as a collapsible card with all of these
fields. Clicking **+ Add provider** creates a blank card; the trash icon
removes one.

### 2.2 Country gate

When you save a session whose providers declare countries that have **zero
rows** in `master_stations`, the API returns:

```
HTTP 409
{
  "error": "missing_master_stations_for_countries",
  "missing_countries": ["IT"],
  "message": "Cannot save: no master_stations rows for ['IT']..."
}
```

The UI surfaces this as a toast pointing at `/admin/master/stations`.
Click **Refresh from Trainline** there — Trainline's CSV covers most of
Europe — then retry the save. (Trainline's ODbL data covers FR, DE, IT,
ES, PT, NL, BE, LU, CH, AT, GB, IE, the Nordics, plus several Eastern
European countries.)

### 2.3 Per-provider refresh

Each card has its own **⤴ Refresh this provider** button. Use it when you
want to pull only that provider's timetable + MCT + stations CSV without
re-downloading every other provider's data. The session-wide
**Refresh all sources** button still pulls everything (and the OSM PBF).

### 2.4 NeTEx-FR is archive-only

OTP doesn't read NeTEx-FR (the SNCF-specific NeTEx profile). If you have
NeTEx-FR archives for compliance reasons, use the **Upload a file** form
in the session detail with declared standard `NeTEx-FR-Horaires` or
`NeTEx-FR-Arrets`. Files land in `inbox/<sid>/archive/` and never touch
OTP. They're preserved for audit; routing uses GTFS / NeTEx-Nordic /
NeTEx-EPIP.

### 2.5 Bulk-import providers from a National Access Point (since v0.1.8)

For a "France-wide every-rail-operator" demonstrator, manually adding
each provider URL through the UI gets tedious quickly. The **⇪ Import
from NAP** button on the Configure section opens a modal that:

1. Fetches the chosen NAP catalogue (default: `https://transport.data.gouv.fr/api/datasets`)
2. Filters by country + modes (rail / urban / bus / bike) + optional
   publisher whitelist + optional dataset-id exclude list
3. For each matching dataset, picks the best timetable resource:
   - **GTFS preferred** (best for OTP)
   - **NeTEx-Nordic / NeTEx-EPIP** when GTFS missing but a profile-tagged
     NeTEx is available
   - **NeTEx-FR datasets are surfaced as warnings** (OTP can't read them)
4. **Preview first** — table of proposed providers shown for review
5. **Confirm** — providers persisted to `session.config.sources.providers[]`,
   country-gate runs, staleness banner appears

Same machinery works against any DCAT-AP-style NAP — just paste the
endpoint URL (e.g. German `mobilithek.info`, Trafiklab Sweden). API
responses cached 5 min in-process to keep preview→confirm cycles snappy.

After import: click **Refresh all sources** then **Rebuild graph** to
materialise the new providers' files and rebuild the OTP graph.

---

## 3. Data sources for `nap-fr-rail`

A curated catalogue of every rail dataset on `transport.data.gouv.fr` (and
a few off-NAP sources) relevant to a France-wide rail demonstrator. The
**SNCF national feed already includes TER**, so most regional rail is
already covered by one provider — this is the surprising-but-true thing
to know.

### 3.1 Recommended set for a France-only demonstrator

These three providers cover intercity France + the only domestic non-SNCF
operator + Paris urban transit. About 15 minutes to set up; rebuild from
fresh inputs in ~15-20 min, ~10-12 min on subsequent rebuilds thanks to
the streetGraph cache.

| Provider ID | Country | Format | Timetable URL | What it adds |
|---|---|---|---|---|
| `SNCF` | FR | GTFS | `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip` | TGV INOUI, OUIGO, Intercités, **TER (every region SNCF operates)** |
| `IDFM` | FR | GTFS | `https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip` | Île-de-France: Transilien + RER A/B + Metro + bus + tram |
| `TRENITALIA` | FR | GTFS | `https://thello.axelor.com/public/gtfs/gtfs.zip` | Frecciarossa Paris-Lyon-Milan (the only non-SNCF intercity competitor) |

GTFS-RT URLs you can attach to each provider's card (1-min polling once
the graph rebuilds + promotes):

| Provider | Service alerts | Trip updates |
|---|---|---|
| `SNCF` | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts` | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates` |
| `IDFM` | (none — IDFM uses SIRI, requires a free PRIM account; not yet wired) | (none) |
| `TRENITALIA` | (none yet) | `https://proxy.transport.data.gouv.fr/resource/trenitalia-gtfs-rt` |

> **Authenticated GTFS-RT (v0.1.10):** if you'd rather use SNCF's
> first-party `https://api.sncf.com/v1/coverage/...` endpoints (more
> stable than the proxy and lets you set your own polling SLA), they
> require an API key. Save it once at **Top nav → My credentials**
> (auth scheme: `query`, param name: `apikey`), then pick that
> credential from the dropdown next to the GTFS-RT URL on the SNCF
> provider card. The same credential covers all three GTFS-RT URLs
> (alerts + trip updates + vehicle positions). See `docs/admin-guide.md`
> §9 for the full credential-management workflow.

### 3.2 Cross-border extensions

Add when you want continuations into the UK, Spain, or Italy:

| Provider ID | Country | Format | Timetable URL | What it adds |
|---|---|---|---|---|
| `EUROSTAR` | FR | GTFS | `https://integration-storage.dm.eurostar.com/gtfs-prod/gtfs_static_commercial_v2.zip` | Paris-London / Paris-Brussels-Amsterdam-Cologne |
| `RENFE` | FR | GTFS | (find on `transport.data.gouv.fr/datasets/horaires-ave-espagne-france`) | AVE Madrid-Barcelona extensions into Lyon/Marseille |

GTFS-RT for Eurostar: `https://integration-storage.dm.eurostar.com/gtfs-prod/gtfs_rt_v2.bin`

**OSM-coverage caveat:** France-wide PBF only covers FR. Eurostar London
stops, Trenitalia Italian stops, and Renfe Spanish stops will fail
coordinate-snap with `LOCATION_NOT_FOUND` — OTP's access/egress bound
(20 min walk by default; see §4.4) refuses to silently truncate. To
demo cross-border coordinate searches you'd need a wider PBF
(`europe-latest.osm.pbf` ~30 GB) or merge `france + great-britain +
italy + spain` with `osmium merge` before upload. Stop-id-based queries
(via the GraphQL API directly) work either way.

### 3.3 What you'll see in the "Import from NAP" preview *(reading the UI)*

When you open a French session and use **Import from NAP** with the
filter `country=FR` + `mode=rail`, the preview shows roughly **12
rail-tagged datasets**. Not the per-region list of TER operators most
people expect. Here's how to read it (v0.1.35.07+):

| What appears | What to do |
|---|---|
| **Réseau SNCF TGV, Intercités et TER** | **Pick this.** It's the SNCF national GTFS containing TGV, Intercités, OUIGO, **AND every region's TER**. One provider covers all France-wide rail (see §3.1). |
| Trains régionaux Hauts-de-France mobilités | Optional. Duplicates the Hauts-de-France TER already inside the SNCF national above. Pick only if you want higher-frequency regional updates. (See §3.4 — these are listed as "deliberately omitted" duplicates.) |
| Trains régionaux ZOU! (+ Transdev Rail Sud) | Same — PACA region TER, duplicate of SNCF national. |
| Réseau interurbain BreizhGo TER | Same — Bretagne region TER. Title is misleading ("interurbain" = bus-like) but it IS rail-tagged because of the trailing "TER". |
| SNCF Transilien | Île-de-France suburban rail. Strict subset of IDFM (#3.3). Skip. |
| Réseau européen AVE Renfe / Eurostar / Trenitalia France / FlixBus+FlixTrain | Cross-border operators — pick per §3.2. |
| Gares + Passages à niveau du réseau ferré national | Station / level-crossing inventories. No timetable, no GTFS resource → automatically rejected by `select_resource` (you'll see them flagged "no usable format" in the skipped list). |

**Key surprise**: there are no per-region "TER Auvergne", "TER Grand Est",
"TER Nouvelle-Aquitaine", etc. as separate datasets. SNCF publishes
**one consolidated GTFS** for all France, not per-region. The 3 regions
that DO publish separately (Hauts-de-France, PACA via ZOU, Bretagne via
BreizhGo) are exceptions, and their data is redundant with the SNCF
national feed.

**Side note**: pre-v0.1.35.07 the preview was polluted by ~36
"Navette" (shuttle) datasets falsely tagged as rail. The substring match
on `"ave"` (Renfe AVE keyword) matched inside `"navette"` — fixed in
v0.1.35.07 by switching `classify_modes()` to word-boundary regex on
ambiguous-short keywords.

**Important nuance — `urban`-tagged datasets can include rail.** IDFM,
TCL (Lyon), and most large urban-network datasets bundle bus + tram +
metro + RER + Transilien into one big multi-modal GTFS. They get tagged
`urban` (sometimes also `bus`) by `classify_modes()` because that's the
dominant mode by title. They do **not** get tagged `rail` even though
they carry RER / Transilien / Metro trains. This is intentional:

- The classifier reflects "dominant mode by title", not "all modes
  present" (which would require parsing every GTFS during preview).
- The `mode=rail` filter is meant to surface **intercity / regional
  rail operators** for focused-purpose rail sessions, not all-modes
  urban aggregates.
- Once an operator picks IDFM (or any urban-tagged dataset), OTP routes
  through every mode the GTFS contains — RER and Transilien included.
  The tag doesn't gate routing capability; it only affects what shows
  up in the preview UI.

**So**: for Île-de-France rail, pick IDFM (under `mode=urban`) — its
RER and Transilien come along automatically. For nation-wide rail, pick
the SNCF national GTFS (under `mode=rail`). Together they cover all
metropolitan rail in France.

### 3.4 Deliberately omitted — why

| Source | Why we skip |
|---|---|
| **SNCF Transilien** (`https://eu.ftp.opendatasoft.com/sncf/gtfs/transilien-gtfs.zip`) | Strict subset of IDFM. Use IDFM (#3.1) for Île-de-France. |
| **TER Hauts-de-France regional** (`transport.data.gouv.fr/datasets/horaires-theoriques-de-loffre-du-reseau-ferre-regional-de-transport`) | Verified to be the same SNCF TER data already in #3.1's national feed. Duplicate. |
| **AURA OURA aggregator** | Mostly buses + duplicate TER. Not worth the noise for a rail demo. |
| **Per-region TER feeds (Bretagne, Bourgogne, etc.)** | All covered by #3.1's SNCF national feed. |

### 3.5 Master-stations countries to import before saving

Each provider's `country_iso` triggers the country-gate. For the
recommended-set sessions:

| Session covering | Countries you must have in `master_stations` |
|---|---|
| France only (#3.1) | `FR` |
| France + UK (Eurostar) | `FR`, `GB` |
| France + Spain (Renfe) | `FR`, `ES` |
| France + UK + Spain + Italy (everything) | `FR`, `GB`, `ES`, `IT` |

**Refresh from Trainline** once on the Master Stations page imports all of
these in one go — Trainline's CSV is pan-European.

---

## 4. How VIATOR builds the graph

```
┌────────────────────────────┐
│ Admin UI: + Add provider   │  PATCH /api/sessions/<sid>
│ — saves URLs to            │      { config: { sources: { providers: [...] } } }
│   session.config.sources   │
│ — country-gate runs        │
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│ ⤴ Refresh this provider    │  POST /api/sessions/<sid>/providers/<id>/refresh
│   (or Refresh all sources) │  — httpx-streams each URL
│ — runs ingestion.dispatch  │  — stages timetable as inbox/<sid>/<gtfs|netex>/<id>.zip
└────────────┬───────────────┘
             │
             ▼  (worker debounces, then)
┌────────────────────────────┐
│ Rebuild graph button       │  POST /api/sessions/<sid>/rebuilds
│ — enqueues RebuildJob      │  — worker picks it up after debounce (default 30 min)
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│ otp-build container        │  docker compose run otp-build
│   1. osmium tags-filter    │  — applies session.config.osm_scope (transit-focused
│   2. generate              │    drops 84% of OSM ways)
│      build-config.json     │  — one transitFeeds entry per provider's zip
│   3. phase 1 — buildStreet │  — SKIPPED if streetGraph cache hits (OSM unchanged)
│   4. phase 2 — loadStreet  │  — always runs, layers transit on the cached street
│      + transit overlay     │    graph
│   5. write graph.obj +     │  — graph.obj moved to graphs/<sid>/<timestamp>/
│      router-config.json    │
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│ Promote to serving         │  POST /api/sessions/<sid>/promote
│ — orchestrator regen       │  — adds otp-<sid> service to compose fragment
│ — worker spawns container  │  — adds nginx /otp/<sid>/ proxy block
│ — OTP loads graph (~60-90s │  — OTP picks up router-config GTFS-RT updaters
│   for 1.8 GB France graph) │    at load time
└────────────────────────────┘
```

### 4.1 Two-phase build (since v0.1.3)

Phase 1 reads the OSM PBF and constructs the street graph. Phase 2 layers
the transit data on top. Two separate JVMs — peak heap is ~30% lower than
the legacy `--build` one-shot because raw OSM nodes are released between
phases.

For France-wide with the transit-focused OSM filter (see 4.2):

| Stage | Wall time | Heap peak |
|---|---|---|
| osmium tags-filter | ~30-60 s | (tool-side, ~500 MB) |
| Phase 1 — Parse OSM Relations / Ways / Nodes | ~3 min | ~10 GB |
| Phase 1 — Build street graph | ~5-7 min | **peaks ~14-18 GB** |
| Phase 1 — Save streetGraph.obj | ~3-4 min | dropping |
| streetGraph.obj cache write | <1 s | — |
| Phase 2 — Load streetGraph + transit | ~5-8 min | ~12-16 GB |
| Promote graph.obj | <1 s | — |
| **Total wall time** | **~17-25 min** | **~18 GB peak** |

`OTP_BUILD_HEAP=24g` and `OTP_BUILD_MEM_LIMIT=32g` in `.env` is the
sweet spot — gives the JVM room without wasting host RAM.

### 4.2 OSM scope filter (since v0.1.5)

`session.config.osm_scope` selects an `osmium tags-filter` preset that
runs against the PBF before OTP parses it. Three presets:

| Scope | What it keeps | Use case | France-wide phase 1 heap |
|---|---|---|---|
| `transit-focused` (default) | All highway types except `service`/`track` + railway + public_transport + parking | journey planning to/from stations | ~14-18 GB |
| `multi-modal` | + service roads, all foot/bike paths | dense-urban last-mile detail | ~22-28 GB |
| `comprehensive` | original PBF unchanged | car routing, OSM debugging | ~38-44 GB |

`transit-focused` cuts ~84 % of France's OSM data (5 GB → 800 MB) without
losing any transit-station accessibility. Use it unless you have a
specific reason not to.

### 4.3 streetGraph.obj cache (since v0.1.7)

Phase 1's streetGraph.obj is cached at
`graphs/.cache/<sid>/streetGraph.obj`, keyed by `sha256(osm.pbf):<scope>`.
On the next rebuild, if the OSM input AND the OSM scope are unchanged,
**phase 1 is skipped entirely** and the cached file is copied into
BUILD_DIR. Phase 2 (transit overlay) always runs.

Effect: adding a new GTFS provider to a France-wide session goes from
~30 min (full rebuild) → **~10-12 min** (phase 2 only). The cache
invalidates automatically when the OSM URL changes, the PBF is
re-uploaded, or the scope changes.

### 4.4 Access/egress walking bound (since v0.1.7)

`router-config.json` includes
`maxAccessEgressDurationForMode = { WALK: "20m" }`. OTP refuses to route
when either endpoint is more than ~1.5 km walk from any transit stop —
returns a clean `LOCATION_NOT_FOUND` instead of silently truncating to
the closest reachable stop with a fake long walk. (Pre-v0.1.7, asking
"Paris → Cagnes-sur-Mer" against a TGV-only feed returned trips ending
at Marseille without warning.)

### 4.5 Per-session router-config.json (since v0.1.7)

The worker generates a fresh `router-config.json` per session before each
build, containing one updater entry per provider's GTFS-RT URL:

```json
{
  "server": { "apiProcessingTimeout": "10s" },
  "routingDefaults": { ..., "maxAccessEgressDurationForMode": { "WALK": "20m" } },
  "updaters": [
    { "type": "real-time-alerts",  "feedId": "SNCF",  "url": "...", "frequency": "1m" },
    { "type": "stop-time-updater", "feedId": "SNCF",  "url": "...", "frequency": "1m" },
    { "type": "real-time-alerts",  "feedId": "IDFM",  "url": "...", "frequency": "1m" }
  ]
}
```

The `feedId` on each updater matches the provider's id from
`build-config.json`'s `transitFeeds.feedId`, so OTP routes the live
updates to the right feed at runtime. The serving otp-`<sid>` container
loads the same file from `graphs/<sid>/current/router-config.json`
alongside `graph.obj`.

### 4.6 Tracking build progress

Builds run inside the one-shot `otp-build` container. The worker captures
stdout + stderr into `rebuild_jobs.log` only when the subprocess exits —
so during a build, the worker log shows just the lifecycle entries.

To watch live OTP output:

```bash
# Find the running build container, tail its logs:
docker logs -f $(docker ps -q -f name=viator-otp-build-run)

# Live memory + CPU on the build container (separate shell):
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' \
  | grep -E 'NAME|otp-build|otp-nap'
```

What different log lines mean:

| Last log line | Stage |
|---|---|
| `OSM filter: transit-focused — running osmium tags-filter` | osmium pre-filter (~30-60 s) |
| `Parse OSM Relations / Ways / Nodes progress: X%` | OSM parse phase |
| `Build street graph progress: X of Y` | The heaviest phase 1 step |
| `Save streetGraph.obj progress: X.X GB done` | Phase 1 finalising, memory drops |
| `streetGraph.obj cache updated (key=...)` | Cache write, <1 s |
| `Phase 2/2 — overlaying transit (heap=...)` | Phase 2 starts, fresh JVM |
| `Saving graph.obj` | Final serialization |
| `Done building graph. Exiting.` | Phase 2 complete |
| `Promoting graph to /var/otp/graph` | Worker takes over |

---

## 5. Master stations

The `master_stations` table is the canonical European station registry
used by VIATOR for journey-UI autocomplete, trip-signature canonicalisation,
and the country-gate.

### 5.1 Bootstrap source

[Trainline-eu/stations](https://github.com/trainline-eu/stations) — an
ODbL-licensed CSV of every European passenger station with UIC code,
multilingual names, lat/lon, country, parent station, and **operator-
specific identifiers** for SNCF, DB, Trenitalia, Renfe, ATOC, ÖBB, SBB,
NTV, Trenord, Cercanías, Entur, Westbahn, Flixbus, Benerail, Busbud,
Distribusion, plus IATA airport codes for stations co-located with
airports.

Trainline refreshes the CSV monthly. VIATOR mirrors with
`POST /api/master/stations/refresh-trainline` — ~30 s for the full ~7000
station import. Manual edits (`source='manual'`) are never overwritten;
upstream changes to those rows are surfaced in the **Pending drift**
queue with per-key granularity (e.g. `other_codes.sbb` rather than just
`other_codes`).

### 5.2 Browsing the table (since v0.1.7)

The Master Stations page (`/admin/master/stations`) supports:

- **Scrollable table with sticky header** — vertical scroll inside a fixed
  container, column titles stay visible
- **Pagination footer** — `« First · ‹ Prev · page [N] of M · Next › · Last »`
  + jump-to-page input
- **Context-mode search** (default on) — typing "Paris" jumps to the
  alphabetical page where Paris stations live, highlights matching rows
  in yellow with a brief flash, scrolls the first match into view. Other
  stations on the page remain visible for context. Toggle off for
  classic filter behaviour.
- **Operator-code badges** — every station shows a row of compact badges
  for each operator code present (SNCF, DB, Trenitalia, ÖBB, SBB, etc.).

### 5.3 Refresh recipe

```bash
# UI: /admin/master/stations → "Refresh from Trainline" button
# API equivalent:
curl -X POST -H "Authorization: Bearer <jwt>" \
  https://<your-host>/api/master/stations/refresh-trainline
# returns: {"added": N, "updated": N, "skipped_manual": N, "pending_drift": N}
```

---

## 6. End-to-end walkthrough — building `nap-fr-rail`

Total time: ~25-30 minutes wall, ~10 minutes attention.

### 6.1 Prerequisites

Before any session work:
- VIATOR stack live under HTTPS (per `docker/INSTALL.md`)
- `master_stations` populated with FR stations: hit `/admin/master/stations`
  → **Refresh from Trainline**

### 6.2 Create the session

`/admin/sessions` → expand **+ Create a new session**:

| Field | Value |
|---|---|
| ID | `nap-fr-rail` |
| Name | `NAP France — rail demonstrator` |
| Category | `NAP` |
| Include in fanout immediately | ✅ |

Submit. Row appears with state `created`.

### 6.3 Configure providers

Two paths, pick one:

**Manual** — click **+ Add provider** for each, fill in the fields.
Useful when you want fine control or you only need 1-2 providers.

**Bulk import (since v0.1.8)** — click **⇪ Import from NAP**, the
modal pre-fills with `transport.data.gouv.fr/api/datasets`, country=FR,
mode=rail. Click **▸ Preview** → table of ~5-15 rail providers detected
on the French NAP. Review, untick anything you don't want via the
publisher-whitelist field (e.g. `SNCF, IDFM, TRENITALIA` to limit to
those three). Click **✓ Confirm import** → providers added in one shot.

Either way, the SNCF provider needs at minimum:

| Field | Value |
|---|---|
| Provider ID | `SNCF` |
| Label | `SNCF Trains` |
| Country | `FR` |
| Timetable format | `GTFS` |
| Timetable URL | `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip` |
| GTFS-RT alerts URL | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts` |
| GTFS-RT trip updates URL | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates` |
| MCT URL / Stations CSV | (leave blank — Phase-3) |

Below the providers list:

| Field | Value |
|---|---|
| OSM PBF URL | `https://download.geofabrik.de/europe/france-latest.osm.pbf` |
| OSM scope | `Transit-focused (recommended)` |

Click **Save config**. Toast: "Config saved for nap-fr-rail — 1 GTFS feed".

> **No country gate?** You'd see HTTP 409 if `master_stations` had no FR
> rows. Since you ran the Trainline refresh in 6.1, you have ~2500 FR
> stations and the gate passes silently.

### 6.4 Refresh sources

Click **Refresh all sources** (or **⤴ Refresh this provider** on the
SNCF card — same outcome since it's the only provider). Toast confirms
fetch ~3-5 min later: "Fetched: provider[SNCF].timetable(gtfs),
provider[SNCF].gtfs_rt(alerts), provider[SNCF].gtfs_rt(trip_updates),
osm_pbf".

State badge auto-advances to `populated`. A `RebuildJob` is queued.

### 6.5 Trigger the build

```bash
# Optional — lower debounce for fast iteration during setup:
sudo sed -i 's/^DEBOUNCE_SECONDS=.*/DEBOUNCE_SECONDS=30/' /opt/viator/docker/.env
docker compose -f /opt/viator/docker/docker-compose.yml up -d --force-recreate worker
```

Click **Rebuild graph** → status `pending` → after debounce, `running` →
build runs ~17-25 min total. Watch progress in another shell:

```bash
docker logs -f $(docker ps -q -f name=viator-otp-build-run)
```

Look for the v0.1.7-feature lines:

```
OSM filter: transit-focused — running osmium tags-filter on osm.pbf
  osm.pbf: 5001159701 → 804835801 bytes (16% of original)        ← OSM scope working
Using per-session router-config.json from /var/otp/inbox/nap-fr-rail   ← v0.1.7-C
Generating build-config.json with feeds: {"feedId":"SNCF",...}        ← v0.1.6 multi-feed gen
streetGraph.obj cache empty — building from scratch                    ← first build, expected
Phase 1/2 — building street graph (heap=24g) ...
[OTP STARTING UP — Build Street Graph]
[Build street graph progress: X of ~8M ways]                           ← filtered count, not 13.6M
Saving streetGraph.obj
Done building graph. Exiting.
streetGraph.obj cache updated (key=<sha256>:transit-focused) at /var/otp/graph/.cache/nap-fr-rail
Phase 2/2 — overlaying transit (heap=24g) ...
[OTP STARTING UP — Load Street Graph + Build Transit]
Done building graph. Exiting.
Promoting graph to /var/otp/graph
```

State auto-advances to `graph_built`. Restore production debounce after:

```bash
sudo sed -i 's/^DEBOUNCE_SECONDS=.*/DEBOUNCE_SECONDS=1800/' /opt/viator/docker/.env
docker compose -f /opt/viator/docker/docker-compose.yml up -d --force-recreate worker
```

### 6.6 Promote to serving

Click **Promote to serving** → confirm dialog → toast: "Promoted nap-fr-rail
(state=serving). Worker will reload within ~15 s." Page refreshes; state
badge now `serving`, fanout checkbox enabled.

The worker spawns the otp-`nap-fr-rail` container (~30 s) and reloads
nginx. The serving container then loads the 1.8 GB graph.obj into
memory (~60-90 s on 12 GB serving heap).

> **OTP_HEAP for serving**: the orchestrator template defaults to 8g.
> A France-wide graph is ~1.8 GB on disk and needs ~10 GB to deserialize.
> If you see `Terminating due to java.lang.OutOfMemoryError` in
> `viator-otp-nap-fr-rail-1` logs, bump the per-session heap to 12g via
> SQL (no UI hook yet):
> ```sql
> UPDATE sessions SET config = jsonb_set(config, '{otp_heap}', '"12g"')
>   WHERE id='nap-fr-rail';
> ```
> Then regenerate compose fragment + recreate container:
> ```bash
> docker compose exec web python -c "
> from app.db import SessionLocal; from app import sessions_orchestrator as orch
> with SessionLocal() as db: orch.regenerate(db)
> "
> docker compose up -d --no-deps --force-recreate otp-nap-fr-rail
> ```

### 6.7 Smoke check

Watch for `Grizzly server running.` in the otp container's logs:

```bash
docker logs -f viator-otp-nap-fr-rail-1 | grep -E 'Grizzly|Updaters|UPDATERS'
# Expected:
#   ... INFO ... OTP UPDATERS INITIALIZED (2 updaters) - OTP 2.9.0 is ready for routing!
#   ... INFO ... Grizzly server running.
```

Then test routing:

```bash
# GraphQL ping — confirms OTP is reachable and the feed loaded
curl -sk -X POST -H 'content-type: application/json' \
  -d '{"query":"{ feeds { feedId } serviceTimeRange { start end } }"}' \
  https://<your-host>/otp/nap-fr-rail/gtfs/v1 | python3 -m json.tool
# Expected: feedId="SNCF", serviceTimeRange spanning today

# Real query — Paris Gare de Lyon → Lyon Part-Dieu via TGV
curl -sk -X POST -H 'content-type: application/json' \
  -d '{"query":"{ plan(from: {lat: 48.8443, lon: 2.3744}, to: {lat: 45.7605, lon: 4.8595}, date: \"2026-05-15\", time: \"08:30\", numItineraries: 5, searchWindow: 14400) { itineraries { duration legs { mode route { shortName longName } from { name } to { name } } } } }"}' \
  https://<your-host>/otp/nap-fr-rail/gtfs/v1 | python3 -m json.tool
# Expected: 5 itineraries, RAIL legs with route shortName like "601A"
```

Then visit `/journey` in the browser. Type "Paris Gare de Lyon" in
**From** (pick from dropdown), "Lyon Part-Dieu" in **To** (pick from
dropdown), Search. Real TGV itineraries should render — click any card
to expand per-leg detail.

### 6.8 Add IDFM as a second provider (optional but recommended)

Once the SNCF-only session is live, layering IDFM is fast — phase 1 is
cached so the rebuild only runs phase 2.

1. Expand `nap-fr-rail` → **+ Add provider**:
   - ID: `IDFM`
   - Label: `Île-de-France Mobilités`
   - Country: `FR`
   - Format: `GTFS`
   - Timetable URL: `https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip`
2. **Save config** → toast: "Config saved for nap-fr-rail — 2 GTFS feeds"
3. **⤴ Refresh this provider** on IDFM (~2 min, ~200 MB)
4. **Rebuild graph** → ~10-12 min total (cache hit on phase 1)
5. **Promote to serving** → otp container restarts to load the new graph
   (~60-90 s)

Now journey searches like "Notre-Dame de Paris → CDG Airport" return
RER B itineraries via Châtelet-Les Halles. And "Paris Gare du Nord →
Paris Gare de Lyon" — which previously returned `WALKING_BETTER_THAN_TRANSIT`
because the SNCF-only feed has no urban service connecting the two — now
returns RER D / Métro 14 / bus alternatives.

### 6.9 Add Trenitalia France (optional cross-operator demo)

Same pattern:

1. **+ Add provider**:
   - ID: `TRENITALIA`
   - Label: `Trenitalia France`
   - Country: `FR`
   - Format: `GTFS`
   - Timetable URL: `https://thello.axelor.com/public/gtfs/gtfs.zip`
   - GTFS-RT trip updates URL: `https://proxy.transport.data.gouv.fr/resource/trenitalia-gtfs-rt`
2. Save → Refresh this provider → Rebuild graph → Promote (same ~10-12 min).

Now Paris → Lyon and Paris → Marseille searches show **Frecciarossa as an
alternative to TGV INOUI** with the `origin_flag=ALL` badge in the journey
UI's fanout view (since both providers serve the route).

---

## 7. Operating

### 7.1 Refreshing data

| Trigger | Use |
|---|---|
| Daily/weekly auto-refresh | Phase-2.1 (cron, deferred) — for now, manual or operator-script |
| Operator changed a single URL | **⤴ Refresh this provider** on that card |
| Operator changed multiple URLs / OSM | **Refresh all sources** at the bottom of the form |
| OSM PBF rolled over (Geofabrik nightly) | Refresh all sources, then Rebuild — streetGraph cache invalidates automatically |
| GTFS feed rolled over (SNCF daily) | Refresh just the SNCF provider, then Rebuild — phase 1 skipped, ~10 min |

The **staleness banner** (yellow box above Build & Promote, since
v0.1.7.1) appears whenever URLs in `config.sources` were edited after
the last successful refresh. Clicking Rebuild while it's showing pops a
confirm dialog reminding you the build will use OLD data.

### 7.2 Adding more providers later

Same recipe as 6.8 / 6.9. The streetGraph cache means each new provider
adds ~10 min to the rebuild + ~60-90 s to the otp container's reload after
promote. Keep going until you've layered every operator you want.

### 7.3 Removing a provider

In the provider card, click the **×** button → Save config → Refresh all
sources → Rebuild. The orchestrator regen drops the removed provider's
files from `inbox/<sid>/<gtfs|netex>/` rotation; the next phase 2 builds
without it.

### 7.4 Deleting a session entirely (since v0.1.7)

Each session row has a red **Delete** button next to Archive. Two-step
confirmation (yes/no, then type-the-id-to-confirm). Wipes:

- The session row + every child table referencing it (rebuild_jobs,
  uploads, graph_snapshots, journey_search_executions + trips,
  mct_overrides, stations_xref)
- On-disk `inbox/<sid>/` and `graphs/<sid>/`
- The streetGraph cache `graphs/.cache/<sid>/`
- The otp-`<sid>` container if currently serving (orchestrator regen +
  worker orphan cleanup)

Audit row preserved with a snapshot of what was deleted (name, providers,
state) for forensics.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Save config returns `409 missing_master_stations_for_countries` | A provider declares `country_iso=X` but `master_stations` has zero rows for X | Operator-driven prerequisite: go to `/admin/master/stations` → click **Refresh from Trainline**. Then retry **Save config**. |
| "Refresh sources now" returns `{"skipped": [{"reason": "unknown source key 'foo'"}]}` | You configured a key not in the recognised list (`gtfs`, `osm_pbf`, `netex_nordic`, `netex_epip`, `mct`, `stations`, or providers[]) | Use one of the recognised keys; the v0.1.6 UI emits providers[] form by default. |
| Refresh succeeds for OSM PBF but build fails with "no GTFS found" | Provider's timetable URL returned a 404 / login page, not a real GTFS | `curl -sIL <url>` from the VPS to verify; some operators require a free account or have moved their endpoint. |
| OTP build crashes with `OutOfMemoryError` (with `OTP_BUILD_PHASES=two_phase`, default) | `OTP_BUILD_HEAP` < what the bundle needs even after splitting phases | First check session's **OSM scope** — `transit-focused` cuts heap ~40%; bump down from `comprehensive` if currently set higher. If already `transit-focused` and still OOMs: bump `.env` `OTP_BUILD_HEAP=24g` + `OTP_BUILD_MEM_LIMIT=32g` for France-wide, `OTP_BUILD_HEAP=8g` + `OTP_BUILD_MEM_LIMIT=12g` for IDF. `docker compose up -d --force-recreate worker`. |
| Build OOMKilled (exit 137) without an OOM stack in the OTP log | Container `mem_limit` is tight relative to `-Xmx` | Raise `OTP_BUILD_MEM_LIMIT` so `mem_limit ≥ Xmx + 4 GB` |
| `viator-otp-<sid>-1` restart-loops with `OutOfMemoryError` while loading graph | `session.config.otp_heap` too small for serving (default 8g). France-wide 1.8 GB graph needs ~12 GB | SQL: `UPDATE sessions SET config = jsonb_set(config, '{otp_heap}', '"12g"') WHERE id='<sid>'`. Then orchestrator regen + recreate (recipe in 6.6). |
| Build is slower than expected on a host with plenty of RAM | `OTP_BUILD_PHASES=two_phase` adds one streetGraph.obj serialize + deserialize (~30-90 s for France-wide) | Acceptable. Set `OTP_BUILD_PHASES=one_shot` only if you've measured. |
| Build stuck at status `pending` for >30 min | Debounce window not yet elapsed | `grep DEBOUNCE_SECONDS /opt/viator/docker/.env` — lower temporarily if iterating |
| Promote returns `400 Session must be in state 'graph_built'` | Build hasn't completed | Wait for state badge to hit `graph_built`. Check job logs in the Build & Promote section. |
| `/otp/<sid>/...` returns 502 | The otp container is loading the graph | Wait ~60-90 s for the JVM to deserialize graph.obj. `docker logs -f viator-otp-<sid>-1` should show `Grizzly server running.` once ready. |
| `/otp/<sid>/...` returns 502 after 3 min | OTP container died (OOM during load, etc.) | Check `docker logs --tail=20 viator-otp-<sid>-1` — likely `OutOfMemoryError`; bump session.config.otp_heap (recipe above). |
| `/otp/<sid>/...` returns 404 | nginx hasn't picked up the new location block | `docker compose exec nginx cat /etc/nginx/conf.d/sessions/nginx-sessions.conf` — does the location block exist? If not, regenerate didn't run; check web logs for orchestrator errors. |
| Journey UI From/To autocomplete is empty | `master_stations` empty | Click "Refresh from Trainline" on `/admin/master/stations`. |
| Master stations refresh button does nothing or 401 | Your JWT cookie expired or you're not logged in as content_manager / platform_admin | Re-log in. |
| Pending drift count keeps growing | Trainline's upstream is changing rows you've manually edited | Walk through the drift queue — adopt or keep — to keep it manageable. |
| Build appears stuck — no log output for >5 min | Normal during OSM parsing or transit-graph phase | Run `docker stats --no-stream` and check `otp-build` CPU. >100% CPU = healthy, just slow. |
| Toast says all sources "Skipped: ... [Errno -2] Name or service not known" | DNS failure inside the container, OR malformed URL in the config form | Check the URLs in the Configure form — pasted URLs sometimes get concatenated (`https://rehttps://...`). Clear with Ctrl+A, paste fresh, click Save config, retry Refresh. |
| Journey UI search returns "no itinerary found" with `routingErrors: [{code: "LOCATION_NOT_FOUND"}]` | The destination is more than ~1.5 km walk from any transit stop OR outside the OSM PBF coverage | This is **correct** behaviour — the v0.1.7 access/egress bound. To extend reach: pick an OSM PBF that covers the destination region, OR stage the destination as a stop_id-based query via the GraphQL API. |
| `plan(...)` returns one WALK-only itinerary with `routingErrors: [{code: "WALKING_BETTER_THAN_TRANSIT"}]` for two stations clearly served by transit | The provider you loaded doesn't contain the line that connects them (e.g. SNCF intercity GTFS for two Paris terminals) | Add IDFM as a second provider for urban Paris (recipe in 6.8). |
| "Save config" toast says `Feed id "..." must be uppercase…` | Feed ID didn't match `^[A-Z][A-Z0-9_-]{1,15}$` | Use uppercase only, no spaces, 2-16 chars. `SNCF`, `IDFM`, `TRENITALIA`, `FR-SNCF`. |
| Refresh sources skips a feed with `invalid gtfs config: feed id "X" appears twice` | Two providers in the list have the same ID | Rename one — provider IDs must be unique within a session. |
| Build log shows `Generating build-config.json with feeds: {…SNCF…}{…IDFM…}` and finishes ok, but a journey query returns no itineraries | Routing across feeds requires the connecting stops to be within OTP's `maxTransferDistance` (default ~200 m) — far apart and OTP doesn't generate the walking transfer | Check coordinates: e.g. SNCF and IDFM versions of "Paris Gare du Nord" must be in the same place. If GTFS lat/lons disagree, OTP won't connect them. The fix is operator-side (correct GTFS data). |
| Save config toast says "OSM URL doesn't appear to cover [IT]" (since v0.1.7) | A provider declares `country_iso=X` but the OSM URL doesn't mention X (heuristic check) | Soft warning only — save still succeeds. If the URL is correct (e.g. you merged regions offline), ignore. Otherwise switch to a wider PBF (e.g. `europe-latest.osm.pbf`) or merge the country PBFs with `osmium merge` before upload. |
| Real-time alerts / trip updates don't show in OTP (since v0.1.7) | GTFS-RT URLs configured but session hasn't rebuilt + been promoted yet | Click **Rebuild graph** then **Promote to serving**. The new graph carries the per-session `router-config.json` containing each provider's updaters; the per-session `otp-<sid>` container picks them up at load time. |
| Need to fully reset a session and start over | Archive only flips state to `archived` — preserves data | Click the red **Delete** button next to Archive (since v0.1.7). Two-step confirmation removes everything: DB rows, on-disk inbox/graphs, the otp container. |
| Need to refresh just one provider's data without re-downloading the others (since v0.1.6) | Clicking "Refresh all sources" pulls every provider | Each provider card has a **⤴ Refresh this provider** button — downloads only that provider's timetable + MCT + stations CSV. OSM PBF stays untouched. |
| Bulk-import preview returns 0 providers despite obvious matches existing on the NAP website (since v0.1.8) | Mode classifier didn't match — the NAP API doesn't expose modes structurally; the importer guesses from title/tags | Add the publisher name to the **Publishers (opt., comma-sep)** field in the import modal as a fallback whitelist. Or paste a wider mode set (rail + urban). The classifier is permissive but can miss novel naming — flag the operator name and we can add to `_MODE_KEYWORDS` in the importer. |
| Bulk-import succeeds but `Confirm` fails with `409 missing_master_stations_for_countries` | An imported provider declares a country with no master_stations rows | Same as the manual save case: go to `/admin/master/stations` → **Refresh from Trainline** to import the missing country, then re-run the bulk-import (the dedupe pass means already-imported providers are silently skipped). |
| NAP fetch fails with `502` from our API | NAP catalogue endpoint is down or unreachable from the web container | Check transport.data.gouv.fr's status page. The 5-min cache means the next preview also fails until the cache expires; you can force-clear by restarting the web container (`docker compose restart web`). |
| NeTEx-FR-only datasets keep appearing as warnings on every re-import | OTP can't read NeTEx-FR (see §2.4); these datasets have no GTFS | Add their dataset IDs to the **Exclude IDs** field in the import modal to suppress the warning, OR find their GTFS-publishing alternative. Exclusion is per-import; for permanent suppression, add to a future operator-level exclude list (not yet wired). |
| Staleness banner showing despite recent refresh | The refresh fetched zero files (every URL was 404 / network error) | `last_refresh_completed_at` is only bumped on at least one successful fetch. Inspect the most recent refresh response or web logs; fix the failing URL and re-refresh. |
| Journey UI search returns trips ending at the wrong destination, e.g. shows "Lyon Part-Dieu" when To field said "Cagnes-sur-Mer" (since v0.1.7.1, this is fixed) | Operator typed a new station name in From/To without picking from the dropdown — the previous station's lat/lon were still in hidden form fields | Now refused at form-submit time with a yellow toast. Always **pick from the dropdown** so the lat/lon hidden fields update. |

---

## 9. Where each file ends up — disk layout cheatsheet

After a successful refresh + build + promote on session id `nap-fr-rail`
with two providers (SNCF + IDFM):

```
/var/lib/docker/volumes/
├── viator_inbox/_data/nap-fr-rail/
│   ├── gtfs/sncf.zip                              # SNCF feed (canonical name = provider id)
│   ├── gtfs/sncf.zip.old                          # previous SNCF, rotated by dispatch
│   ├── gtfs/idfm.zip                              # IDFM feed
│   ├── osm/osm.pbf                                # current OSM (canonical name)
│   ├── osm/osm.pbf.old                            # previous OSM
│   ├── runtime/SNCF-MCT/latest.csv                # if MCT URL configured (Phase-3, stored only)
│   ├── runtime/SNCF-Stations/latest.csv           # if stations CSV configured (Phase-3)
│   └── router-config.json                         # generated by worker per build
│
└── viator_graphs/_data/
    ├── nap-fr-rail/
    │   ├── 20260501-073220/
    │   │   ├── graph.obj                          # most recent build (~1.8 GB for France-wide)
    │   │   └── router-config.json                 # carried with the graph for serve mode
    │   ├── 20260430-091422/                       # one back
    │   ├── 20260429-152007/                       # two back (worker keeps N=3)
    │   └── current → 20260501-073220/             # symlink the otp-nap-fr-rail container serves from
    └── .cache/nap-fr-rail/
        ├── streetGraph.obj                        # phase 1 cache (~600-900 MB)
        └── streetGraph.key                        # `<sha256>:<scope>`
```

The host paths above resolve to:

| Volume name | Host path | Mounted in |
|---|---|---|
| `viator_inbox` | `/var/lib/docker/volumes/viator_inbox/_data/` | `web`, `worker` (rw); `otp-build` (ro) |
| `viator_graphs` | `/var/lib/docker/volumes/viator_graphs/_data/` | `worker` (rw); `otp-build` (rw — for cache); `otp-<sid>` (ro) |
| `viator_pgdata` | `/var/lib/docker/volumes/viator_pgdata/_data/` | `postgres` (rw) |

Inside containers the same files are at:

| Container | Inbox path | Graphs path |
|---|---|---|
| `web`, `worker` | `/data/inbox/<sid>/` | `/data/graphs/<sid>/` |
| `otp-build` | `/var/otp/inbox/<sid>/` (ro) | `/var/otp/graph/` (rw) |
| `otp-<sid>` (per-session serving) | (none) | `/var/otp/graph/<sid>/current/` (ro) |

`postgres` separately stores the metadata: `sessions`, `uploads`,
`rebuild_jobs`, `master_stations`, `audit_events`, `journey_searches`, etc.
Re-deploys can nuke the inbox and graph volumes and rebuild from URLs;
the Postgres volume is the only must-back-up data store.

---

## 10. Where to go next

- **Add Eurostar / Renfe AVE for cross-border.** Section 3.2 lists URLs.
  Note the OSM-coverage caveat: France-wide PBF only reaches French
  borders. For coordinate searches into the UK/Spain/Italy you'd need
  `europe-latest.osm.pbf` or merged regional PBFs.
- **Set up an MCT pipeline (Phase-3).** Currently `mct_url` per provider
  stages the file but it's not yet consumed by OTP. The OJP adapter
  (Phase-3 milestone) will wire it.
- **Add a NeTEx-FR converter (Phase-4).** OTP doesn't read NeTEx-FR
  natively. Operators wanting NeTEx-FR routing today have to use the
  GTFS export of the same data.
- **Create comparison sessions.** `nap-fr-rail` could be paired with a
  `merits-fr-rail` (when MERITS goes live) or a `twin-nap-fr-rail` (per
  spec §14 — same data, different code path) for fanout-style A/B
  comparison.

---

## 11. Reference — relevant spec sections

- §4 Multi-session model — sessions, fanout, lifecycle
- §5 Data ingestion — `sources.providers[]` shape, NeTEx-FR archive-only
- §6 Journey search & comparison — fanout, trip signature, replay
- §7 Master data management — master_stations, route_aliases, RICS

For implementation details: `app/ingestion.py` (provider normalisation),
`app/router_config.py` (per-session GTFS-RT generation),
`app/osm_filter.py` (scope presets), `docker/otp/entrypoint.sh`
(two-phase build + cache + filter pipeline).
