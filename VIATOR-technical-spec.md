# VIATOR — Technical specification

_Detailed engineering specification for the VIATOR demonstrator: identity & RBAC, multi-session architecture, ingestion, comparison, APIs, data model._

_Companion to `VIATOR-strategy.md` — read the strategy first for the why._

_**Powered by TrackOnPath SAS** — patrick.heuguet@trackonpath.com_
_**© 2026 UIC — International Union of Railways. All rights reserved.**_
_Last updated: 2026-04-27_

---

## 1. System overview

VIATOR is a **multi-tenant, multi-session journey-planning demonstrator** built on OpenTripPlanner. A single VPS hosts:

- A **Python admin app** (FastAPI) for user management, session management, and feed ingestion.
- A **JavaScript journey UI** (static, MapLibre + OTP GraphQL) for end-user testing and side-by-side comparison.
- One **OTP container per session** — sessions are isolated routing instances fed by different upstream sources (NAP, MERITS, etc.).
- A **worker** that debounces uploads, runs OTP builds, and promotes graphs.
- **Postgres** as the authoritative store for users, sessions, uploads, jobs, and audit.
- **nginx** as the single TLS endpoint, routing per surface and per session.

### 1.1 Component architecture

```
                                          ┌─────────────────────────────────┐
   browsers ────────────────────────────► │  nginx — TLS, single endpoint   │
                                          └─────┬─────┬──────────┬──────────┘
                                                │     │          │
                          ┌─────────────────────┘     │          └─────────────────┐
                          │                           │                            │
                          ▼                           ▼                            ▼
              /admin     ─►                /journey  ─►                   /otp/<session>/
   ┌───────────────────────────┐ ┌───────────────────────────┐ ┌───────────────────────────┐
   │ FastAPI admin app (Python)│ │ Journey UI (static JS)    │ │ OTP container (Java)      │
   │ - users, roles, JWT       │ │ - MapLibre map            │ │ - one per session         │
   │ - sessions CRUD           │ │ - session selector        │ │ - graph.obj loaded        │
   │ - upload + dispatcher     │ │ - compare two sessions    │ │ - GraphQL endpoint        │
   │ - audit log               │ │ - direct calls to OTP     │ │ - real-time updaters      │
   └─────┬─────────────────────┘ └───────────────────────────┘ └───────────────────────────┘
         │
         ▼
   ┌───────────────────────────┐    ┌──────────────────────────────────────────────────────┐
   │ Worker (Python)           │    │ Postgres                                             │
   │ - per-session rebuild q   │    │ users, sessions, uploads, rebuild_jobs,              │
   │ - calls otp-build per     │    │ audit_events, password_reset_tokens,                 │
   │   session                 │    │ verification_tokens, mct_overrides, stations_xref    │
   └───────────────────────────┘    └──────────────────────────────────────────────────────┘
   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ Volumes:                                                                             │
   │   /data/inbox/<session_id>/{gtfs,osm,netex,archive,runtime}                          │
   │   /data/graphs/<session_id>/{<timestamp>/graph.obj, current → <timestamp>}           │
   │   /data/postgres/                                                                    │
   └──────────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Stack summary

| Layer | Technology |
|---|---|
| Reverse proxy | nginx 1.27 |
| Admin app | Python 3.12 + FastAPI 0.115 + Jinja2 + SQLAlchemy 2 |
| Worker | Python 3.12 (same image as admin app, different command) |
| Journey UI | Static HTML + MapLibre GL + vanilla JS, served by nginx |
| Routing engine | OpenTripPlanner 2.9.0 on Java 25 (Eclipse Temurin) |
| Database | Postgres 16 |
| Auth | JWT (`python-jose`) + bcrypt (`passlib[bcrypt]`) |
| Email | `aiosmtplib` over SMTP/STARTTLS |
| Rate limiting | `slowapi` |
| Container runtime | Docker Engine 29 + Compose plugin |

---

## 2. Surface map

| URL | Surface | Auth | Roles permitted |
|---|---|---|---|
| `/` | Journey search UI (default landing) | Public read; JWT cookie if logged in | end_user, content_manager, platform_admin |
| `/login` | Login page | Public | — |
| `/register` | Self-registration form | Public | — |
| `/confirm/<token>` | Email confirmation landing | Public (token-gated) | — |
| `/admin` | Admin app home | JWT required | content_manager, platform_admin |
| `/admin/users` | User management | JWT required | platform_admin |
| `/admin/sessions` | Session CRUD | JWT required | platform_admin |
| `/admin/sessions/<id>/feeds` | Upload feeds into session | JWT required | content_manager, platform_admin |
| `/admin/sessions/<id>/audit` | Per-session audit log | JWT required | platform_admin |
| `/api/auth/*` | Auth endpoints | varies | — |
| `/api/sessions` | Session metadata API (list, get) | JWT required | all logged-in roles |
| `/api/journey/plan?session=<id>` | Plan a journey on one session | JWT required | all logged-in roles |
| `/api/journey/compare?a=<id>&b=<id>` | Plan on two sessions and diff | JWT required | all logged-in roles |
| `/otp/<session_id>/...` | OTP GraphQL & debug client (per session) | JWT required (proxied) | all logged-in roles |

---

## 3. User management & authentication

### 3.1 Roles

```python
class Role(str, Enum):
    PLATFORM_ADMIN   = "platform_admin"
    CONTENT_MANAGER  = "content_manager"
    END_USER         = "end_user"
```

Permission matrix (high level):

| Capability | end_user | content_manager | platform_admin |
|---|:-:|:-:|:-:|
| Search / compare journeys | ✅ | ✅ | ✅ |
| Upload feeds to a session | ❌ | ✅ | ✅ |
| Wipe a session's data | ❌ | ✅ | ✅ |
| Trigger / cancel rebuild | ❌ | ✅ | ✅ |
| Edit master data (stations, aliases, carriers) | ❌ | ✅ | ✅ |
| Refresh Trainline / RICS + resolve drift | ❌ | ✅ | ✅ |
| Create / destroy sessions | ❌ | ❌ | ✅ |
| Manage users (CRUD, role changes) | ❌ | ❌ | ✅ |
| Configure SMTP / platform config | ❌ | ❌ | ✅ |
| View audit log | ❌ | own actions | all |

### 3.2 Self-registration flow

```
┌─────────────┐    POST /api/auth/register-request    ┌──────────────┐
│ visitor     │ ───────── { email, name } ──────────► │ FastAPI      │
└─────────────┘                                       │              │
       ▲                                              │ 1. INSERT    │
       │                                              │    verification_tokens
       │ link: /confirm/<token>                       │    (token, email, name,
       │                                              │     expires_at = now+24h)
       │                                              │ 2. SMTP send
       │                                              │    "Confirm your VIATOR account"
       │                                              └──────┬───────┘
       │                                                     │
       │ ◄─────────────── magic-link email ──────────────────┘
       │
       ▼
┌─────────────┐    GET  /api/auth/check-token?t=<token>      ┌──────────────┐
│ user clicks │ ─────────────────────────────────────────────► FastAPI      │
│   the link  │ ◄──────── { email, name, expires_at } ───────┤              │
└─────────────┘                                               │              │
       │                                                      │              │
       │ POST /api/auth/register-confirm                      │              │
       │  { token, password }                                 │              │
       └─────────────────────────────────────────────────────►│              │
                                                              │ 3. validate  │
                                                              │ 4. INSERT    │
                                                              │    users(role=end_user)
                                                              │ 5. DELETE token
                                                              │ 6. issue JWT │
                                                              │ 7. return    │
                                                              │    Set-Cookie│
                                                              └──────────────┘
```

Token: 32-byte URL-safe random (`secrets.token_urlsafe(32)`), stored hashed (sha256) in DB so a leaked DB doesn't yield usable tokens. Single-use, 24h TTL, deleted on consumption.

### 3.3 Authentication

- Issued JWT contains `sub` (user UUID), `role`, `email`, `iat`, `exp` (default 12h).
- Signed with HS256 using `JWT_SECRET` from environment.
- Stored as `Set-Cookie: viator_jwt=<token>; HttpOnly; Secure; SameSite=Lax; Path=/`.
- `Authorization: Bearer <token>` header accepted for API clients.
- A FastAPI dependency `current_user(min_role: Role)` decodes, verifies, and rejects with 401/403 as appropriate.

### 3.4 Password handling

- bcrypt via `passlib[bcrypt]`, work factor **12** (matches OSCAR).
- Minimum length 12 chars; no maximum, no composition rules (NIST 800-63B alignment).
- Passwords are never logged. Audit events store user IDs, not passwords.

### 3.5 Rate limiting

`slowapi` configured per endpoint:

| Endpoint | Limit |
|---|---|
| `POST /api/auth/login` | 20 / 15 min / IP |
| `POST /api/auth/register-request` | 5 / hour / IP |
| `POST /api/auth/password-reset-request` | 5 / hour / IP |
| `POST /api/auth/register-confirm` | 10 / hour / IP |

Hits beyond the limit return `429 Too Many Requests`.

### 3.6 Audit

Every state-changing request writes an `audit_events` row:

```
id UUID, ts TIMESTAMPTZ, actor_user_id UUID NULL, actor_ip INET,
action TEXT,                    -- e.g. login.success, login.fail, upload.dispatch, session.create
target_kind TEXT,               -- session, user, upload, job
target_id  TEXT,
metadata   JSONB                -- non-sensitive details
```

Indexed on `(ts DESC)` and `(actor_user_id, ts DESC)` for the admin views.

### 3.7 OSCAR pattern reuse — concrete mapping

| OSCAR (Node.js) | VIATOR (Python) |
|---|---|
| `jsonwebtoken` | `python-jose[cryptography]` |
| `bcrypt` (12 rounds) | `passlib[bcrypt]` (12 rounds) |
| `express-rate-limit` | `slowapi` |
| `sendVerificationEmail` util | `app.email.send_verification(user, token)` using `aiosmtplib` |
| `auth_event` table | `audit_events` table (same shape) |
| Email HTML templates (`/templates/*.html`) | Lifted verbatim, restyled with VIATOR palette |
| `/register/request`, `/register/confirm`, `/register/check-token`, `/login`, `/me` | Same paths under `/api/auth/` |
| `bootstrap-platform-user` (one-time admin) | Same; gated by `BOOTSTRAP_TOKEN` env var, single-use |

The Python code is new but the **flow shape is identical**, so behaviour (emails sent, tokens, redirects, error semantics) is consistent across OSCAR and VIATOR for any user who has used both.

---

## 4. Multi-session model

### 4.1 Concept

A **session** is an isolated OTP routing instance with:

- a stable string ID (e.g. `nap-fr-2026-q2`, slug-style, immutable after creation),
- a human-readable name,
- a category (`NAP`, `MERITS`, `MANUAL`, `EXPERIMENTAL`),
- its own inbox subtree, its own graph snapshots, its own OTP container,
- a configuration JSON (source URLs, real-time feed URLs, OTP heap, allowed standards).

Sessions are created and destroyed by **platform admins only**. Content managers operate **inside** existing sessions.

### 4.2 Storage layout

```
/data/
├── inbox/
│   ├── <session_id>/
│   │   ├── gtfs/         # active GTFS for the next build
│   │   ├── osm/          # OSM PBF for the next build
│   │   ├── netex/        # Nordic / EPIP NeTEx (loadable by OTP)
│   │   ├── archive/      # NeTEx-FR (stored only until converter exists)
│   │   ├── runtime/      # MCT and stations CSV (consumed at runtime, not by OTP)
│   │   └── _staging/     # transient per-upload directories
│   └── _shared/          # cross-session reference data (UIC stop registry, etc.)
└── graphs/
    └── <session_id>/
        ├── 20260427-090000/graph.obj
        ├── 20260427-150000/graph.obj
        ├── 20260428-090000/graph.obj
        └── current → 20260428-090000
```

Promotion of a freshly built graph is atomic: the worker writes `graph.obj` into a new timestamped directory, then re-points `current` via `ln -sfn`. The OTP container watches the symlink target via the entrypoint and reloads on swap.

### 4.3 Lifecycle

```
created ──► configured ──► populated ──► graph-built ──► serving ──┐
   ▲                                                                │
   │                                                                ▼
   └──────────────────────── reset / wipe ◄─────── archived / deleted
```

- **created** — row in `sessions`, no inbox, no OTP container yet.
- **configured** — source URLs, OTP heap, allowed standards set.
- **populated** — at least one of {GTFS, NeTEx-loadable, OSM-PBF} present.
- **graph-built** — `graphs/<id>/current/graph.obj` exists.
- **serving** — `otp-<id>` container is up and `/otp/<id>/actuator/health` returns 200.
- **archived** — read-only; OTP container stopped; data retained.
- **deleted** — row marked deleted, volumes wiped (irreversible).

### 4.4 Per-session OTP container

Compose generates an OTP service per session. Two implementation patterns:

**A. Compose with a generated file (recommended for ≤10 sessions)** — the admin app, on session creation, regenerates `docker-compose.sessions.yml` and runs `docker compose up -d` to add the new service. nginx config is regenerated similarly.

**B. Docker SDK directly (for >10 sessions)** — the admin app calls the Docker API to start/stop containers without compose; nginx is reconfigured via templated files + `nginx -s reload`.

For the demonstrator, **pattern A** is sufficient. Each generated OTP service:

```yaml
otp-<session_id>:
  build: ./otp
  restart: unless-stopped
  environment:
    OTP_MODE: serve
    OTP_GRAPH_DIR: /var/otp/graph
    OTP_HEAP: ${OTP_HEAP_<SESSION_ID>:-4g}
  volumes:
    - graphs-<session_id>:/var/otp/graph:ro
  labels:
    viator.session_id: <session_id>
```

### 4.5 Real-time updates per session

Each session's `router-config.json` is generated from its DB config row:

```json
{
  "updaters": [
    {"type": "real-time-alerts",   "feedId": "<feed_id>", "url": "<gtfs-rt-alerts-url>",  "frequency": "1m"},
    {"type": "stop-time-updater",  "feedId": "<feed_id>", "url": "<gtfs-rt-tu-url>",      "frequency": "1m"}
  ]
}
```

Sessions with no real-time configuration get an empty `updaters` array.

---

## 5. Data ingestion (per session)

The dispatch matrix from `docker/README.md` is now per-session. Every upload form, every audit row, every queued rebuild carries a `session_id`.

| Declared standard | Stored at | Triggers OTP rebuild for that session? |
|---|---|---|
| `GTFS` | `/data/inbox/<sid>/gtfs/` | yes |
| `OSM-PBF` | `/data/inbox/<sid>/osm/` | yes |
| `NeTEx-Nordic` | `/data/inbox/<sid>/netex/` | yes |
| `NeTEx-EPIP` | `/data/inbox/<sid>/netex/` | yes (best-effort) |
| `NeTEx-FR-Horaires` | `/data/inbox/<sid>/archive/NeTEx-FR-Horaires/` | **no — Phase 6** |
| `NeTEx-FR-Arrets` | `/data/inbox/<sid>/archive/NeTEx-FR-Arrets/` | **no — Phase 6** |
| `SNCF-MCT` | `/data/inbox/<sid>/runtime/SNCF-MCT/latest.zip` | no — runtime data |
| `SNCF-Stations` | `/data/inbox/<sid>/runtime/SNCF-Stations/latest.csv` | no — runtime data |

### 5.1 Auto-pull from configured sources

A session can declare an optional `sources` config — the operator-facing shape stored in `sessions.config.sources`:

```json
{
  "sources": {
    "gtfs": [
      {"id": "SNCF",       "url": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"},
      {"id": "IDFM",       "url": "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"},
      {"id": "TRENITALIA", "url": "https://www.example.com/trenitalia-gtfs.zip"}
    ],
    "osm_pbf": "https://download.geofabrik.de/europe/france-latest.osm.pbf",
    "mct":     "https://ressources.data.sncf.com/.../temps-correspondance-minimaux.csv",
    "stations":"https://ressources.data.sncf.com/.../gares-de-voyageurs.csv"
  }
}
```

**Multi-feed (since v0.1.4):** `sources.gtfs` is a list of `{id, url}` objects; one OTP graph contains all listed feeds. `id` becomes the OTP `feedId` namespace prefix on every stop_id from that feed (e.g. `SNCF:OCETrain-87271007`). Cross-operator transfers between physically-nearby stops are auto-generated by OTP at build time. Backward-compat: a string value is read as a single feed with `id="GTFS"`. Validation: `id` matches `/^[A-Z][A-Z0-9_-]{1,15}$/`, unique per session.

`sources.osm_pbf` is necessarily a single PBF (one street network per graph). It must cover every region the GTFS feeds reach. Mismatches surface as `LOCATION_NOT_FOUND` at journey-query time when a coordinate is outside the OSM coverage.

The build pipeline materializes this config:
- The `refresh_sources` endpoint downloads each feed → `inbox/<sid>/gtfs/<id_lower>.zip`.
- The `otp-build` entrypoint scans `gtfs/*.zip` and generates a `build-config.json` with one `transitFeeds` entry per file (feedId = filename stem, uppercased).

The worker has an APScheduler-style cron that pulls fresh files on the configured cadence and feeds them through the same dispatcher as a manual upload would. Audit rows mark the actor as `system:auto-pull`.

For MERITS (when wired), the same shape applies — `kind: "MERITS-..."` with auth headers stored in `sessions.config.merits_credentials_ref` (a Postgres-stored encrypted secret reference).

---

## 6. Journey search & comparison

### 6.1 Single-session plan

```
GET /api/journey/plan?session=<id>
    &from=lat,lon
    &to=lat,lon
    &date=YYYY-MM-DD
    &time=HH:MM
    &modes=TRANSIT,WALK
```

Implementation: the admin app proxies to `http://otp-<sid>:8080/otp/gtfs/v1` with a translated GraphQL query. Returns OTP's response, normalised into a stable VIATOR JSON envelope so the journey UI doesn't have to worry about OTP version drift. (OTP 2.x uses `/otp/gtfs/v1`, not the legacy `/otp/routers/default/index/graphql` from OTP 1.x.)

### 6.2 Comparison

```
GET /api/journey/compare?a=<id_a>&b=<id_b>&...same params
```

Backend behaviour:

```python
async def compare(a, b, params):
    plan_a, plan_b = await asyncio.gather(
        plan_one(a, params),
        plan_one(b, params),
    )
    diff = compute_diff(plan_a, plan_b)
    return {"a": plan_a, "b": plan_b, "diff": diff}
```

`compute_diff` returns:

- `count_a`, `count_b` — number of itineraries returned by each.
- `common_legs` — legs sharing identical (route, trip, fromStop, toStop, departure, arrival).
- `time_to_arrival_delta_seconds` — for the best itinerary on each side, signed delta.
- `services_only_in_a`, `services_only_in_b` — service IDs that appear in only one side.

The journey UI renders the two itinerary lists side-by-side with the diff summary at the top.

### 6.3 Search history — full request and response, version-anchored

The recording model has **three tables** working together, plus a **graph_snapshots** anchor that makes every recorded trip traceable to the exact timetable version that produced it.

```
                                              ┌─────────────────────┐
                                              │ graph_snapshots     │  one row per OTP build
                                              │  - session_id       │  per session, immutable.
                                              │  - built_at         │  Holds feed_signature
                                              │  - feed_signature   │  (hash of input uploads).
                                              │  - source_uploads   │
                                              └──────────▲──────────┘
                                                         │
                                                         │ FK: which version answered
                                                         │
   ┌────────────────────┐       ┌─────────────────────────┴────────────────────┐
   │ journey_searches   │ 1───* │ journey_search_executions                    │
   │  - origin/dest     │       │  - search_id                                 │
   │  - requested time  │       │  - session_id                                │
   │  - modes           │       │  - graph_snapshot_id                         │
   │  - total_response  │       │  - status, num_itineraries, response_ms     │
   │  - total_trips     │       │  - raw_response (JSONB, retention-pruned)   │
   └────────────────────┘       └──────────────────┬───────────────────────────┘
                                                   │ 1
                                                   │
                                                   │ *
                                              ┌────▼──────────────────────────┐
                                              │ journey_trips                 │
                                              │  - execution_id (FK)          │
                                              │  - trip_signature             │
                                              │  - duration, transfers        │
                                              │  - departure/arrival          │
                                              │  - legs (JSONB, structured)   │
                                              │  - fare (JSONB)               │
                                              └───────────────────────────────┘
```

#### What each table is for

- **`graph_snapshots`** — every successful OTP build is recorded as an immutable snapshot, carrying the list of source upload IDs and a deterministic `feed_signature` (SHA-256 of concatenated upload SHA-256s). This is **the timetable version** any later report or replay anchors on.
- **`journey_searches`** — what the user asked. One row per request, regardless of how many sessions get queried.
- **`journey_search_executions`** — what each session returned. One row per (search, session) pair, with the exact `graph_snapshot_id` that answered it. Pulls `raw_response` (the full OTP JSON) into a JSONB column for forensic drill-down — pruned at retention.
- **`journey_trips`** — every itinerary OTP returned, decomposed into structured columns + a JSONB `legs` array. Each trip carries a `trip_signature` for cross-session equivalence (see §6.5).

#### Why this shape

- **Storage is structured for reporting.** Every leg, mode, departure/arrival, transfer, fare placeholder is queryable — not buried in opaque OTP responses.
- **Provenance is derived, not stored.** "NAP only / MERITS only / both" comes from a view that aggregates `journey_trips` by `trip_signature` across executions of the same search. No write-side maintenance, no risk of stale flags.
- **Versioning is built in.** Every trip → execution → graph_snapshot → uploads. You can answer "which version of the timetable did this trip come from?" with one join, retroactively, forever.

### 6.4 Trip signature — how cross-session matching works

A `trip_signature` is a SHA-256 hash of a canonical, normalised string describing the itinerary. The construction is **defensive about cross-source identifier drift** (NAP and MERITS use different feed IDs, route IDs, and stop IDs).

```
canonical = "|".join(
  f"{leg.mode}:{uic_of(leg.from_stop)}-{uic_of(leg.to_stop)}@{leg.departure:%H:%M}-{leg.arrival:%H:%M}#{leg.route_short_name}"
  for leg in itinerary.legs
)
trip_signature = sha256(canonical).hexdigest()[:16]
```

Notes on the design:

- Stops are mapped to their **UIC code via `stations_xref`** before hashing — that's the only stable cross-source identifier.
- `route_short_name` (e.g. `"TGV INOUI 6107"`) is included because train numbers usually match across sources; route IDs do not.
- Times are rounded to the minute to absorb millisecond-level differences in scheduling.
- 16 hex chars (64 bits) is enough — collision space ≫ daily trip volume.

Limitations (acknowledged):

- **False negatives possible** if one source uses `"TGV 6107"` and the other `"TGV INOUI 6107"`. We can extend the normaliser with a known-aliases map.
- **Bus / coach legs** without UIC codes fall back to `(stop_lat, stop_lon)` rounded to 4 decimals (~11 m). Worse but workable.
- A trip seen in only one session may genuinely be a real difference, or just a missing stop in `stations_xref`. Reports flag this: "X of Y unmatched trips have non-UIC stops."

### 6.5 Fanout — single user request, multiple sessions, merged response

The default end-user endpoint is **fanout**, not single-session plan. The user issues one request; the backend queries every session marked `include_in_fanout = TRUE` in parallel, merges results by `trip_signature`, flags origin per trip, records everything.

```
POST /api/journey/fanout
{
  "from": {"lat": 48.8566, "lon": 2.3522, "label": "Paris"},
  "to":   {"lat": 45.7640, "lon": 4.8357, "label": "Lyon"},
  "depart_at": "2026-05-12T08:00:00",
  "modes":     ["TRANSIT","WALK"]
}
```

Backend behaviour:

```python
async def fanout(req):
    sessions = await active_fanout_sessions()           # WHERE state='serving' AND include_in_fanout
    search   = await record_search(req)

    executions = await asyncio.gather(*[
        plan_one(session, req, search.id) for session in sessions
    ])  # each writes one row in journey_search_executions + N rows in journey_trips

    merged = merge_by_signature(executions)
    return {
        "search_id": search.id,
        "trips":     merged,                            # each carries `found_in_sessions: [...]`
        "executions": [e.summary() for e in executions]
    }
```

Response shape (abridged):

```json
{
  "search_id": "0a82…",
  "trips": [
    {
      "signature": "8b7e3c1d…",
      "found_in_sessions": ["nap-fr-2026-q2", "merits-pilot"],
      "origin_flag": "BOTH",
      "best": { "duration_seconds": 7200, "departure_at": "...", "arrival_at": "...", "legs": [...] },
      "by_session": {
        "nap-fr-2026-q2":  { "duration_seconds": 7200, "departure_at": "...", "arrival_at": "..." },
        "merits-pilot":    { "duration_seconds": 7240, "departure_at": "...", "arrival_at": "..." }
      }
    },
    { "signature": "…", "found_in_sessions": ["nap-fr-2026-q2"], "origin_flag": "NAP_ONLY", "best": {...} },
    { "signature": "…", "found_in_sessions": ["merits-pilot"],   "origin_flag": "MERITS_ONLY", "best": {...} }
  ],
  "executions": [
    {"session_id": "nap-fr-2026-q2", "graph_snapshot_id": "…", "status": "ok", "num_itineraries": 5, "response_ms": 312},
    {"session_id": "merits-pilot",   "graph_snapshot_id": "…", "status": "ok", "num_itineraries": 4, "response_ms": 287}
  ]
}
```

`origin_flag` derivation:

| `found_in_sessions` content | flag |
|---|---|
| All fanout sessions | `ALL` (or `BOTH` if exactly two) |
| Subset, includes a NAP session | `NAP_ONLY` (or named subset for >2 sessions) |
| Subset, includes only MERITS | `MERITS_ONLY` |
| One session | `<session_id>_ONLY` |

### 6.6 Timetable versioning — two levels

A single linear "version" number for graph snapshots would be misleading. Replaying a March 2026 search against an October 2026 graph is meaningless: the **service calendar itself changed** (new routes, retired routes, different dates of operation). So VIATOR uses **two-level versioning**:

| Level | Field | Where it comes from | Example |
|---|---|---|---|
| **Main version** — the timetable calendar | `timetable_main_version` (text) + `service_period_start` + `service_period_end` (dates) | Auto-derived from `min(start_date)` / `max(end_date)` of the feed's `calendar.txt` (GTFS) or service-calendar frame (NeTEx). Encoded as **ISO-week range** `YYYY-Www_YYYY-Www` (start week → end week). Content manager can override during upload if the feed metadata is misleading. | `2026-W14_2026-W39` (covers 2026-W14 → 2026-W39 — Apr through Sep). |
| **Update version** — patches within a main version | `timetable_update_version` (int) | Auto-incremented per session within the same `timetable_main_version`. First import = 1, subsequent corrections = 2, 3, … | `2026-W14_2026-W39` rev 3 = third corrective build of that timetable. |

**Why ISO-week ranges and not season labels.** Different railways have different service-period boundaries; a German operator's "Sommerfahrplan" doesn't line up with SNCF's "service estival". An ISO-week range is unambiguous, multilingual, and tells the truth about each operator's actual service window without forcing a one-size-fits-all season name.

**Comparability rule:** version-diff and replay endpoints compare snapshots **only if `from.timetable_main_version == to.timetable_main_version`**. Cross-main-version diffs are rejected with an explanatory error and a link to the snapshots' service periods. The UI surfaces the rule explicitly so users don't waste effort on meaningless replays.

A new "main version transition" report (admin only) shows: this session moved from main `2026-S` to `2026-W` on date X — here are the high-level deltas (new services count, retired services count, average response time across a sample of canonical O&D pairs). It is purely descriptive and does not pretend to be a regression report.

### 6.7 Replay (regression detection)

Because every trip points at the `graph_snapshot` that produced it, two questions become easy — within the same main version:

**Q1 — "What changed between two updates of the same main version?"**

```
GET /api/reports/version-diff?session=<id>&from_snapshot=<a>&to_snapshot=<b>
```

Returns: trips present in `b` not in `a` (new), trips in `a` not in `b` (lost), trips in both with material delta (improved / regressed). Bucketed by O&D.

**Q2 — "Did this rebuild fix the no-route searches we had last month?"**

```
POST /api/admin/replay
{
  "filter": { "session_id": "nap-fr", "since": "2026-03-01", "until": "2026-03-31", "status": "no_route" },
  "against_graph_snapshot_id": "<current-snapshot>"
}
```

The system rejects searches whose original `graph_snapshot.timetable_main_version` differs from the target snapshot's main version (returned in the per-search outcome list as `skipped: main_version_mismatch`). For the rest, it re-issues each search and records new `journey_search_executions` rows tagged `replay_of_search_id`. The endpoint returns a comparison report: how many of the previously failing searches now succeed, how many still fail, how many were skipped for main-version mismatch.

### 6.8 Reports powered by this model

| Report | What it answers |
|---|---|
| Top O&D pairs | Where users are testing (per session, per period) |
| Volume per user / per session | Who and what is most exercised |
| Response-time percentiles per session | Health under load (p50/p95/p99) |
| Trip-source distribution | % of trips found in NAP only / MERITS only / both, per period |
| Trip stability across versions | For a given O&D, trip churn between two graph_snapshots |
| Comparison divergence | O&D pairs where best-itinerary delta > threshold between sessions |
| Replay outcomes | Did a new graph_snapshot resolve previously-failing searches? |
| Unmatched-trip diagnostic | Trips that didn't match across sessions and the reason (missing UIC, route name mismatch, etc.) |

### 6.9 Journey UI surfaces

| Element | Behaviour |
|---|---|
| Map | MapLibre GL, OSM raster fallback, draws origin/destination markers and itinerary polylines |
| From / To | **Station autocomplete from `master_stations`** — multilingual, UIC-keyed. The user picks a known station; lat/lon and UIC code are attached automatically. No address-level geocoding (rail-only demonstrator). |
| Search button | One click → `/api/journey/fanout` (the default UX). Power users can pick a single session via an "Advanced" disclosure. |
| Itinerary card | Departure, arrival, duration, transfers, modes, fare placeholder |
| **Origin badge per card** | `BOTH` (green), `NAP_ONLY` (orange), `MERITS_ONLY` (blue), `<session>_ONLY` (grey) |
| Per-session timing strip | Below the trip list: "NAP responded in 312 ms with 5 trips · MERITS responded in 287 ms with 4 trips · 2 trips matched, 4 unique" |
| Itinerary detail | Click a card → side-by-side panels showing the trip as each session reported it (legs, departures, arrivals); deltas highlighted |

---

## 7. Master data management

Cross-source comparison only works if VIATOR has a stable view of "the same station" and "the same service" across NAP, MERITS, Trenitalia France, and any future feed. Three master tables hold this reference data, all editable from the admin UI and seedable from open datasets.

### 7.1 Master stations

The canonical UIC-keyed registry of European passenger stations. Seeded from **Trainline-eu/stations** ([github.com/trainline-eu/stations](https://github.com/trainline-eu/stations)) — ~12k stations, ODbL, includes UIC + UIC8 SNCF + per-operator codes (SNCF, DB, Trenitalia, Renfe, ATOC) + coordinates + multilingual names + parent-station hierarchy for meta-stations.

Bootstrap on first install:

```bash
docker compose run --rm web python -m app.master.bootstrap_stations
```

Periodic refresh: monthly by default (`MASTER_STATIONS_REFRESH_DAYS`). The trip_signature canonicaliser (§6.4) resolves any source-specific stop_id to UIC by joining on the per-session `stations_xref` table → `master_stations` table.

#### Conflict resolution — "our edits prevail, but stay informed"

Local fixes must always win — but admins also need to know when Trainline later updates a row we've already touched, in case our fix becomes obsolete or Trainline's improvement is genuinely better. The policy is **row-level lock with drift surfacing**:

1. Each `master_stations` row carries a `source` column. Trainline-imported rows are `source='trainline'`. The moment a row is edited via the admin UI (PATCH or POST), `source` flips to `'manual'`.
2. The monthly refresh **never overwrites** rows where `source='manual'`. Trainline's incoming version of those rows is captured in a sibling table `master_stations_pending_drift`:

   ```sql
   CREATE TABLE master_stations_pending_drift (
     uic                  TEXT PRIMARY KEY REFERENCES master_stations(uic),
     trainline_snapshot   JSONB NOT NULL,        -- full Trainline row as of last refresh
     fields_differing     TEXT[] NOT NULL,       -- which columns differ from our `manual` value
     detected_at          TIMESTAMPTZ NOT NULL DEFAULT now()
   );
   ```

3. The admin UI shows a **"Trainline drift" badge** on master-stations rows where a pending-drift entry exists. Clicking it opens a side-by-side diff (our value vs Trainline's), with three actions:
   - **Keep ours** — clears the drift entry; nothing else changes.
   - **Adopt Trainline's value (full row)** — overwrites our row with Trainline's, flips `source` back to `'trainline'`, clears drift.
   - **Adopt selected fields only** — partial adoption; row stays `'manual'`; drift entry cleared.

4. A `GET /api/master/stations/drift` endpoint lists all pending-drift rows so the team can periodically reconcile.

The same pattern applies to `master_carriers` (RICS dictionary) — pending-drift table, three resolution actions.

This way our authority is preserved by default; we never silently lose a fix; but we never silently miss an upstream improvement either.

### 7.2 Route aliases

Maintains canonical service-name equivalences across sources and across time. `"TGV"` = `"TGV INOUI"` since rebrand. `"Eurostar"` = `"Thalys"` for cross-border services that were merged. Editable by content managers; bootstrapped empty.

The trip_signature canonicaliser looks up `route_short_name` in `route_aliases` before hashing — if a canonical form exists, it is substituted. Aliases are time-bounded (`applies_from`, `applies_until`) so historical signatures are not rewritten retroactively.

The unmatched-trips report (§6.8) is the operational input to this table: when admins see "X trips on Paris-Lyon were `'TGV 6107'` in NAP and `'TGV INOUI 6107'` in MERITS", they add the alias and re-run the report.

### 7.3 Master carriers (RICS dictionary)

The European Rail Identification Codes registry. Seeded from the public RICS code list (UIC publication). Used by the OJP adapter (Phase 5) to translate operator codes between sources, and by the MCT enforcement layer (legacy SN/DB/ES → modern RICS).

### 7.4 Where master data is consumed

| Consumer | Tables used |
|---|---|
| `trip_signature` canonicaliser | `master_stations`, `route_aliases` |
| OJP adapter | `master_stations`, `master_carriers` |
| MCT enforcement | `master_carriers` |
| Reports (geographic O&D bucketing) | `master_stations` (to label O&D pairs by station name instead of raw lat/lon) |
| Journey UI From/To autocomplete | `master_stations` — **the only station-name resolver**; no Nominatim, no Photon. Multilingual matching via `name` + `name_translations`. UIC code captured with the selection. |

The per-session `stations_xref` table (already in §8) maps each session's `stop_id` to a `master_stations.uic` — that's the bridge between feed-specific identifiers and the master registry.

---

## 8. Data model (Postgres schema sketch)

```sql
-- Identity
CREATE TABLE users (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email           CITEXT UNIQUE NOT NULL,
  name            TEXT NOT NULL,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('platform_admin','content_manager','end_user')),
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at   TIMESTAMPTZ
);

CREATE TABLE verification_tokens (
  token_hash      BYTEA PRIMARY KEY,
  email           CITEXT NOT NULL,
  name            TEXT NOT NULL,
  expires_at      TIMESTAMPTZ NOT NULL,
  consumed_at     TIMESTAMPTZ
);

CREATE TABLE password_reset_tokens (
  token_hash      BYTEA PRIMARY KEY,
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at      TIMESTAMPTZ NOT NULL,
  consumed_at     TIMESTAMPTZ
);

-- Sessions
CREATE TABLE sessions (
  id                  TEXT PRIMARY KEY,                     -- slug like 'nap-fr-2026-q2'
  name                TEXT NOT NULL,
  category            TEXT NOT NULL CHECK (category IN ('NAP','MERITS','MANUAL','EXPERIMENTAL')),
  state               TEXT NOT NULL CHECK (state IN ('created','configured','populated','graph_built','serving','archived','deleted')),
  config              JSONB NOT NULL DEFAULT '{}'::jsonb,   -- source URLs, heap, real-time, etc.
  include_in_fanout   BOOLEAN NOT NULL DEFAULT FALSE,       -- if TRUE, /api/journey/fanout queries this session
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by          UUID NOT NULL REFERENCES users(id),
  archived_at         TIMESTAMPTZ
);
CREATE INDEX sessions_fanout ON sessions(state, include_in_fanout) WHERE state='serving' AND include_in_fanout;

-- Ingestion
CREATE TABLE uploads (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      TEXT NOT NULL REFERENCES sessions(id),
  user_id         UUID NOT NULL REFERENCES users(id),
  filename        TEXT NOT NULL,
  declared_kind   TEXT NOT NULL,
  detected_kind   TEXT NOT NULL,
  sha256          TEXT NOT NULL,
  size_bytes      BIGINT NOT NULL,
  stored_path     TEXT NOT NULL,
  version_label   TEXT,
  triggered_rebuild BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX uploads_session_ts ON uploads(session_id, created_at DESC);

CREATE TABLE rebuild_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      TEXT NOT NULL REFERENCES sessions(id),
  status          TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','cancelled')),
  log             TEXT,
  graph_path      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ
);
CREATE INDEX rebuild_jobs_session_ts ON rebuild_jobs(session_id, created_at DESC);

-- Reference data consumed at runtime
CREATE TABLE mct_overrides (
  session_id      TEXT NOT NULL REFERENCES sessions(id),
  station_code    TEXT NOT NULL,
  carrier_a       TEXT NOT NULL,
  carrier_b       TEXT NOT NULL,
  min_minutes     INT NOT NULL,
  PRIMARY KEY (session_id, station_code, carrier_a, carrier_b)
);

CREATE TABLE stations_xref (
  session_id      TEXT NOT NULL REFERENCES sessions(id),
  stop_id         TEXT NOT NULL,
  uic             TEXT REFERENCES master_stations(uic),
  trigramme       TEXT,
  insee           TEXT,
  rics            TEXT,
  PRIMARY KEY (session_id, stop_id)
);
CREATE INDEX stations_xref_uic ON stations_xref(uic);

-- ─────────────────────────── Master data ───────────────────────────
-- Cross-session reference data. Seeded from open datasets, editable by admins.

-- Master stations: UIC-keyed registry of European passenger stations.
-- Bootstrap source: Trainline-eu/stations (ODbL).
CREATE TABLE master_stations (
  uic               TEXT PRIMARY KEY,                 -- 7-digit UIC code (or 8 with check digit)
  uic8_sncf         TEXT,                             -- SNCF variant if different
  name              TEXT NOT NULL,
  slug              TEXT,
  country_iso       CHAR(2),
  latitude          DOUBLE PRECISION,
  longitude         DOUBLE PRECISION,
  parent_uic        TEXT REFERENCES master_stations(uic),  -- meta-stations
  is_main_station   BOOLEAN NOT NULL DEFAULT FALSE,
  is_suggestable    BOOLEAN NOT NULL DEFAULT TRUE,
  -- Operator-specific identifiers
  trigramme_sncf    TEXT,
  db_code           TEXT,
  trenitalia_code   TEXT,
  renfe_code        TEXT,
  atoc_code         TEXT,
  other_codes       JSONB DEFAULT '{}'::jsonb,        -- forward-compatible bag for future operators
  -- Multilingual names
  name_translations JSONB DEFAULT '{}'::jsonb,        -- {fr: '...', de: '...', en: '...', it: '...'}
  -- Provenance
  source            TEXT NOT NULL DEFAULT 'trainline' -- 'trainline' | 'sncf' | 'manual' | 'merits'
                    CHECK (source IN ('trainline','sncf','manual','merits','other')),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX master_stations_country     ON master_stations(country_iso);
CREATE INDEX master_stations_trigramme   ON master_stations(trigramme_sncf);
CREATE INDEX master_stations_name_trgm   ON master_stations USING gin (name gin_trgm_ops);  -- requires pg_trgm

-- Route name aliases. Editable by content managers.
-- The trip_signature canonicaliser substitutes alias → canonical_name before hashing.
CREATE TABLE route_aliases (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name  TEXT NOT NULL,                      -- e.g. 'TGV INOUI'
  alias           TEXT NOT NULL,                      -- e.g. 'TGV'
  applies_from    DATE,                               -- NULL = always
  applies_until   DATE,
  scope_country   CHAR(2),                            -- NULL = global
  scope_carrier   TEXT,                               -- NULL = any
  notes           TEXT,
  created_by      UUID REFERENCES users(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX route_aliases_unique
  ON route_aliases (alias, canonical_name, COALESCE(scope_country,''), COALESCE(scope_carrier,''));

-- Master carriers: RICS code dictionary.
CREATE TABLE master_carriers (
  rics_code        TEXT PRIMARY KEY,
  short_name       TEXT NOT NULL,
  full_name        TEXT,
  country_iso      CHAR(2),
  legacy_codes     JSONB DEFAULT '{}'::jsonb,         -- {SN: 'SNCF', DB: 'DB', ES: 'Eurostar', ...}
  source           TEXT NOT NULL DEFAULT 'uic'       -- 'uic' | 'manual'
                   CHECK (source IN ('uic','manual')),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pending drift: Trainline (or RICS) values that differ from our manual edits.
-- Surfaced in admin UI so admins can periodically reconcile.
CREATE TABLE master_stations_pending_drift (
  uic                  TEXT PRIMARY KEY REFERENCES master_stations(uic),
  trainline_snapshot   JSONB NOT NULL,
  fields_differing     TEXT[] NOT NULL,
  detected_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE master_carriers_pending_drift (
  rics_code            TEXT PRIMARY KEY REFERENCES master_carriers(rics_code),
  upstream_snapshot    JSONB NOT NULL,
  fields_differing     TEXT[] NOT NULL,
  detected_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Timetable versioning anchor — every successful OTP build is a snapshot.
-- Two-level versioning:
--   timetable_main_version   = the calendar period (e.g. '2026-S' for Summer 2026)
--   timetable_update_version = sequential patch number within the same main version
-- Replay/version-diff endpoints REQUIRE both snapshots share the same main version.
CREATE TABLE graph_snapshots (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id                  TEXT NOT NULL REFERENCES sessions(id),
  rebuild_job_id              UUID REFERENCES rebuild_jobs(id),
  built_at                    TIMESTAMPTZ NOT NULL,
  graph_path                  TEXT NOT NULL,                  -- /data/graphs/<sid>/<ts>/graph.obj
  source_uploads              JSONB NOT NULL,                 -- [{upload_id, filename, sha256, kind}, …]
  feed_signature              TEXT NOT NULL,                  -- sha256 of concatenated source upload sha256s
  -- Two-level versioning
  timetable_main_version      TEXT NOT NULL,                  -- e.g. '2026-S', '2026-W'; auto-derived or admin-set
  timetable_update_version    INT  NOT NULL DEFAULT 1,        -- sequential within (session_id, timetable_main_version)
  service_period_start        DATE NOT NULL,                  -- min(calendar.start_date)
  service_period_end          DATE NOT NULL,                  -- max(calendar.end_date)
  main_version_source         TEXT NOT NULL DEFAULT 'auto'    -- 'auto' | 'manual_override'
                              CHECK (main_version_source IN ('auto','manual_override')),
  is_current                  BOOLEAN NOT NULL DEFAULT FALSE,
  archived_at                 TIMESTAMPTZ
);
CREATE INDEX graph_snapshots_session_built  ON graph_snapshots(session_id, built_at DESC);
CREATE INDEX graph_snapshots_main_version   ON graph_snapshots(session_id, timetable_main_version, timetable_update_version DESC);
CREATE UNIQUE INDEX graph_snapshots_one_current_per_session
  ON graph_snapshots(session_id) WHERE is_current;
CREATE UNIQUE INDEX graph_snapshots_unique_update_within_main
  ON graph_snapshots(session_id, timetable_main_version, timetable_update_version);

-- The user's request — one row regardless of how many sessions answer it.
CREATE TABLE journey_searches (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id             UUID REFERENCES users(id),
  ip                  INET,
  endpoint            TEXT NOT NULL CHECK (endpoint IN ('plan','compare','fanout')),
  origin_lat          DOUBLE PRECISION NOT NULL,
  origin_lon          DOUBLE PRECISION NOT NULL,
  origin_label        TEXT,
  dest_lat            DOUBLE PRECISION NOT NULL,
  dest_lon            DOUBLE PRECISION NOT NULL,
  dest_label          TEXT,
  requested_time_kind TEXT NOT NULL CHECK (requested_time_kind IN ('depart_at','arrive_by')),
  requested_time      TIMESTAMPTZ NOT NULL,
  modes               TEXT NOT NULL,
  total_response_ms   INT,                                    -- end-to-end including fanout
  total_trips_unique  INT,                                    -- after dedup by signature
  status              TEXT NOT NULL CHECK (status IN ('ok','partial','no_route','error','timeout')),
  replay_of_search_id UUID REFERENCES journey_searches(id)    -- set if this is a replay
);
CREATE INDEX journey_searches_ts      ON journey_searches(ts DESC);
CREATE INDEX journey_searches_user_ts ON journey_searches(user_id, ts DESC);

-- One row per (search, session) pair. Carries the EXACT graph version that answered.
CREATE TABLE journey_search_executions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  search_id           UUID NOT NULL REFERENCES journey_searches(id) ON DELETE CASCADE,
  session_id          TEXT NOT NULL REFERENCES sessions(id),
  graph_snapshot_id   UUID NOT NULL REFERENCES graph_snapshots(id),
  status              TEXT NOT NULL CHECK (status IN ('ok','no_route','error','timeout')),
  num_itineraries     INT NOT NULL DEFAULT 0,
  response_ms         INT,
  raw_response        JSONB,                                  -- full OTP body, pruned at retention
  error_message       TEXT
);
CREATE INDEX journey_executions_search       ON journey_search_executions(search_id);
CREATE INDEX journey_executions_session_snap ON journey_search_executions(session_id, graph_snapshot_id);
CREATE INDEX journey_executions_session_ts   ON journey_search_executions(session_id, id);

-- Each itinerary OTP returned, decomposed into structured columns.
CREATE TABLE journey_trips (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id        UUID NOT NULL REFERENCES journey_search_executions(id) ON DELETE CASCADE,
  trip_signature      TEXT NOT NULL,                          -- sha256[:16] canonical hash, see §6.4
  rank_in_response    INT NOT NULL,
  duration_seconds    INT NOT NULL,
  num_transfers       INT NOT NULL,
  departure_at        TIMESTAMPTZ NOT NULL,
  arrival_at          TIMESTAMPTZ NOT NULL,
  modes               TEXT NOT NULL,                          -- 'TRAIN,WALK', etc.
  legs                JSONB NOT NULL,                         -- [{mode, route_short_name, from_stop_id, from_uic, to_stop_id, to_uic, departure, arrival, …}, …]
  fare                JSONB                                   -- placeholder until OJP/OSDM fares wired
);
CREATE INDEX journey_trips_execution_rank ON journey_trips(execution_id, rank_in_response);
CREATE INDEX journey_trips_signature      ON journey_trips(trip_signature);

-- View: cross-session provenance per (search, trip_signature)
CREATE OR REPLACE VIEW journey_trip_provenance AS
SELECT
  e.search_id,
  t.trip_signature,
  array_agg(DISTINCT e.session_id ORDER BY e.session_id)        AS found_in_sessions,
  count(DISTINCT e.session_id)                                  AS num_sessions_with_trip,
  array_agg(DISTINCT e.graph_snapshot_id)                       AS graph_snapshot_ids,
  min(t.duration_seconds)                                       AS best_duration_seconds,
  min(t.departure_at)                                           AS earliest_departure_at,
  max(t.arrival_at)                                             AS latest_arrival_at
FROM journey_search_executions e
JOIN journey_trips t ON t.execution_id = e.id
GROUP BY e.search_id, t.trip_signature;

-- Platform configuration (OSCAR pattern)
CREATE TABLE platform_config (
  key             TEXT PRIMARY KEY,
  value           TEXT,                    -- nullable for unset / cleared values
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      UUID REFERENCES users(id)
);
-- Schema is enforced in Python (CONFIG_SCHEMA), not in SQL — see §12.

-- Audit
CREATE TABLE audit_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_user_id   UUID REFERENCES users(id),
  actor_ip        INET,
  action          TEXT NOT NULL,
  target_kind     TEXT,
  target_id       TEXT,
  metadata        JSONB
);
CREATE INDEX audit_ts ON audit_events(ts DESC);
CREATE INDEX audit_actor_ts ON audit_events(actor_user_id, ts DESC);
```

---

## 9. API specification (selected endpoints)

### 9.1 Auth

| Method & path | Body | Returns | Roles |
|---|---|---|---|
| `POST /api/auth/register-request` | `{ email, name }` | `204` (always, to prevent enumeration) | public |
| `GET /api/auth/check-token?t=` | — | `{ email, name, expires_at }` | public |
| `POST /api/auth/register-confirm` | `{ token, password }` | `200 { jwt }` + Set-Cookie | public |
| `POST /api/auth/login` | `{ email, password }` | `200 { jwt }` + Set-Cookie | public |
| `POST /api/auth/logout` | — | `204` | logged-in |
| `GET /api/auth/me` | — | `{ id, email, name, role }` | logged-in |
| `POST /api/auth/password-reset-request` | `{ email }` | `204` | public |
| `POST /api/auth/password-reset-confirm` | `{ token, password }` | `204` | public |

### 9.2 Users (admin)

| Method & path | Body | Returns | Roles |
|---|---|---|---|
| `GET /api/users` | — | `[ { id, email, name, role, is_active, last_login_at } ]` | platform_admin |
| `POST /api/users` | `{ email, name, role, password }` | `201 { id, email, ... }` | platform_admin |
| `PATCH /api/users/<id>` | `{ role?, is_active? }` | `200 { ... }` | platform_admin |

`POST /api/users` is the **direct-creation** path: the platform admin sets the initial password and shares it out-of-band. Used while SMTP isn't yet configured (or for accounts that bypass email). The new user is `is_active=true` immediately and can log in with the supplied password; they're encouraged to rotate it via the password-reset flow. Failure modes:

- `400` — `role` not in `{platform_admin, content_manager, end_user}`
- `409` — email already exists
- `422` — Pydantic validation: malformed email, password shorter than `MIN_PASSWORD_LENGTH` (12 chars)
- `403` — caller isn't a platform admin
- `401` — caller isn't authenticated

The audit row records `action=user.created` with `metadata={email, role, name}` — never the password.

### 9.3 Sessions

| Method & path | Body | Returns | Roles |
|---|---|---|---|
| `GET /api/sessions` | — | `[ { id, name, category, state, served_at } ]` | logged-in |
| `POST /api/sessions` | `{ id, name, category, config }` | `201 { ... }` | platform_admin |
| `PATCH /api/sessions/<id>` | `{ name?, config? }` | `200 { ... }` | platform_admin |
| `POST /api/sessions/<id>/archive` | — | `204` | platform_admin |
| `POST /api/sessions/<id>/wipe` | — | `204` | platform_admin or content_manager |
| `POST /api/sessions/<id>/uploads` | multipart `{ declared_standard, version_label, file }` | `201 { upload }` | content_manager, platform_admin |
| `GET /api/sessions/<id>/uploads` | — | `[ uploads ]` | logged-in |
| `POST /api/sessions/<id>/rebuilds` | — | `201 { job }` | content_manager, platform_admin |
| `GET /api/sessions/<id>/rebuilds` | — | `[ jobs ]` | logged-in |

### 9.4 Journey

| Method & path | Body / params | Returns | Roles |
|---|---|---|---|
| `POST /api/journey/fanout` | `{from, to, depart_at\|arrive_by, modes}` | merged trips with `origin_flag` + `executions` summary (see §6.5) | logged-in |
| `POST /api/journey/plan` | same + `session_id` | single-session plan, normalised | logged-in |
| `POST /api/journey/compare` | same + `session_id_a`, `session_id_b` | `{a, b, diff}` (kept for explicit two-session comparison) | logged-in |
| `GET /api/journey/searches/<search_id>` | — | full recorded search with executions and trips | search owner or platform_admin |

The journey UI's From/To autocomplete uses **`GET /api/master/stations?q=`** (§9.9) directly — no separate geocode endpoint exists.

### 9.5 Audit

| Method & path | Returns | Roles |
|---|---|---|
| `GET /api/audit?since=&actor=&action=` | event list | platform_admin |

### 9.6 Reports (search analytics + versioning)

| Method & path | Returns | Roles |
|---|---|---|
| `GET /api/reports/searches?since=&user=&session=&status=&page=` | paginated `journey_searches` rows | platform_admin |
| `GET /api/reports/od-pairs?session=&since=&precision=4&limit=50` | top O&D buckets (lat/lon rounded to N decimals) | platform_admin |
| `GET /api/reports/volume-per-user?since=` | `[ {user_id, email, count} ]` | platform_admin |
| `GET /api/reports/volume-per-session?since=` | `[ {session_id, count, p50_ms, p95_ms, p99_ms, error_rate} ]` | platform_admin |
| `GET /api/reports/trip-source-distribution?since=` | per-period % of trips found in NAP only / MERITS only / both / all | platform_admin |
| `GET /api/reports/compare-divergence?session_a=&session_b=&since=&threshold_min=5` | O&D pairs where best-itinerary delta exceeds threshold | platform_admin |
| `GET /api/reports/version-diff?session=&from_snapshot=&to_snapshot=` | trips new / lost / improved / regressed between two snapshots of one session | platform_admin |
| `GET /api/reports/unmatched-trips?since=` | trips that didn't match across sessions, with diagnostic (missing UIC, route name mismatch, …) | platform_admin |
| `GET /api/reports/searches.csv?...` | CSV export of searches | platform_admin |
| `GET /api/reports/trips.csv?since=&session=` | CSV export of trips with provenance | platform_admin |

### 9.7 Replay (regression detection)

| Method & path | Body | Returns | Roles |
|---|---|---|---|
| `POST /api/admin/replay` | `{ filter: {session_id, since, until, status?}, against_graph_snapshot_id }` | `{ replay_batch_id, count_queued, count_skipped_main_version_mismatch }` | platform_admin |
| `GET /api/admin/replay/<batch_id>` | — | per-search outcome: was failing, now succeeds / still fails / different result / skipped (main-version mismatch) | platform_admin |

### 9.8 Platform configuration (OSCAR pattern)

| Method & path | Body | Returns | Roles |
|---|---|---|---|
| `GET /api/admin/config` | — | full config with sensitive fields masked | platform_admin |
| `PATCH /api/admin/config` | partial JSON | updated config (re-masked) | platform_admin |
| `POST /api/admin/config/smtp/test` | `{ to }` | `{ ok, error? }` | platform_admin |

### 9.9 Master data

All write operations on master data are open to **content_manager and platform_admin** (no platform-admin-only endpoints in this block). Both refresh endpoints (Trainline / RICS) are also open to both roles — they're operational tasks.

| Method & path | Body / params | Returns | Roles |
|---|---|---|---|
| `GET /api/master/stations?q=&country=&page=` | — | paginated `master_stations` rows; rows with pending drift include a flag | logged-in |
| `POST /api/master/stations` | full row | `201 { ... }`, `source = 'manual'` | content_manager, platform_admin |
| `PATCH /api/master/stations/<uic>` | partial | updated row, `source = 'manual'` | content_manager, platform_admin |
| `POST /api/master/stations/refresh-trainline` | — | `{ added, updated, skipped_manual, pending_drift }` | content_manager, platform_admin |
| `GET /api/master/stations/drift` | — | list of pending-drift rows with field-level diffs | content_manager, platform_admin |
| `POST /api/master/stations/<uic>/drift/resolve` | `{ action: 'keep_ours' \| 'adopt_full' \| 'adopt_fields', fields?: [...] }` | updated row | content_manager, platform_admin |
| `GET /api/master/route-aliases?q=` | — | list | content_manager, platform_admin |
| `POST /api/master/route-aliases` | `{ canonical_name, alias, applies_from?, applies_until?, scope? }` | `201 { ... }` | content_manager, platform_admin |
| `DELETE /api/master/route-aliases/<id>` | — | `204` | content_manager, platform_admin |
| `GET /api/master/carriers` | — | list of RICS carriers; pending-drift flag where applicable | logged-in |
| `POST /api/master/carriers` | full row | `201 { ... }`, `source = 'manual'` | content_manager, platform_admin |
| `PATCH /api/master/carriers/<rics_code>` | partial | updated row | content_manager, platform_admin |
| `POST /api/master/carriers/refresh-rics` | — | `{ added, updated, skipped_manual, pending_drift }` | content_manager, platform_admin |
| `GET /api/master/carriers/drift` | — | list of pending-drift rows | content_manager, platform_admin |
| `POST /api/master/carriers/<rics_code>/drift/resolve` | `{ action, fields? }` | updated row | content_manager, platform_admin |

---

## 10. Security model

### 10.1 Threat model (high level)

| Threat | Mitigation |
|---|---|
| Credential stuffing | bcrypt 12 + login rate limit + audit |
| Email enumeration | Generic 204 responses on register/reset requests |
| Token theft | HttpOnly cookies; short JWT TTL (12 h); single-use verification tokens stored hashed |
| Privilege escalation | Role checks in every dependency; role transitions logged |
| Malicious upload | Format detection on bytes (not extension); size cap; reject mismatched declarations |
| Path traversal in upload | Filenames sanitized; staging dirs are random; never use user-supplied path components |
| OTP RCE / SSRF | OTP container is firewalled to internal network only; only updaters call out, with allow-listed URLs |
| Compose-from-DB injection | Compose YAML fragments are templated with strict slug validation on `session_id` |

### 10.2 Secrets

All in environment variables, never in DB schema:

```
JWT_SECRET, BOOTSTRAP_TOKEN, POSTGRES_PASSWORD,
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
MERITS_CLIENT_ID, MERITS_CLIENT_SECRET   # phase 7
```

For Phase-7 MERITS credentials, a Postgres `secret_refs` table holds references; actual values come from environment or HashiCorp Vault if scope grows.

---

## 11. Operations — runbook

This chapter is the **runbook for operators** — the people SSH'd into the VPS, not the developers writing code. Read it top-to-bottom for a first install; jump to a sub-section when something needs fixing.

For the developer-facing tooling (CI, local lint/test, GHCR, branch protection), see §15. For first-platform-admin creation specifically, see §11.4.

### 11.1 Runtime architecture (what runs where)

The VPS hosts a single Docker Compose stack. The stack is generated, not hand-edited — every time a session is created or archived, the admin app rewrites `docker-compose.generated.yml` and `nginx/conf.d/sessions.generated.conf`, then runs `docker compose up -d`.

```
                  ┌────────────────────────────────────────────────────┐
   :443 (TLS) ───►│  nginx                                             │
                  │  ─ /              → web (admin + journey UI)       │
                  │  ─ /api/          → web                            │
                  │  ─ /otp/<sid>/    → otp-<sid> (per-session)        │
                  └─┬───────────┬─────────────┬─────────────┬──────────┘
                    │           │             │             │
              ┌─────▼─────┐ ┌───▼─────┐ ┌─────▼──────┐ ┌────▼─────┐
              │  web      │ │ worker  │ │ otp-nap-q2 │ │ otp-…    │
              │  FastAPI  │ │ APSched │ │ JRE 25     │ │ (one per │
              │  Jinja UI │ │ +Docker │ │ +OTP 2.9   │ │  session)│
              │  Pydantic │ │  socket │ │            │ │          │
              └─┬─────────┘ └─┬───────┘ └─┬──────────┘ └─┬────────┘
                │             │           │              │
                ▼             ▼           ▼              ▼
              ┌────────────────────────────────────────────────┐
              │  postgres:16  (persisted volume: pgdata)       │
              └────────────────────────────────────────────────┘

              Persistent volumes per-session:
              ─ inbox-<sid>     uploaded raw feeds
              ─ graphs-<sid>    built OTP graphs (current = symlink)
```

Containers and what they do:

| Container | Role | Restart policy |
|---|---|---|
| `nginx` | Public TLS termination + routing to web/OTP. | `unless-stopped` |
| `web` | FastAPI admin app + journey UI + all `/api/*` routes. | `unless-stopped` |
| `worker` | APScheduler crons (retention, master-data refresh) + on-demand OTP builds. Mounts `/var/run/docker.sock` to spawn `otp-build` jobs. | `unless-stopped` |
| `postgres` | Single Postgres 16 with `pgcrypto`, `citext`, `pg_trgm`. | `unless-stopped` |
| `otp-<sid>` | One per session in state `serving`. JRE 25 + `otp-shaded-2.9.0.jar`. | `unless-stopped` |

### 11.2 First-time install on a fresh VPS

This collapses what's in `docker/INSTALL.md` into the spec. For full step-by-step (with download URLs and example outputs), see that file.

#### 11.2.1 VPS sizing

| Resource | Pilot (Île-de-France only) | National (France-wide) |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 16 GB | **32 GB** |
| SSD | 60 GB | 100 GB |
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |

The 32 GB floor for national isn't optional — OTP graph build for the full SNCF GTFS + France OSM PBF needs ~24 GB heap. Cheaper hosts that fit: OVH, Scaleway, Hetzner, Infomaniak.

Open inbound ports on the provider firewall: `22` (SSH, restricted to your IPs), `80` (HTTP, public during install), `443` (HTTPS, public after step 11.2.7).

#### 11.2.2 OS hardening

```bash
# As root
adduser viator && usermod -aG sudo viator
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
apt update && apt -y upgrade
```

Reconnect as `viator` for the rest.

#### 11.2.3 Install Docker Engine + Compose plugin

```bash
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker version && docker compose version
```

#### 11.2.4 Get the stack onto the VPS

```bash
sudo mkdir -p /opt/viator && sudo chown $USER:$USER /opt/viator
cd /opt/viator
git clone https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator.git .
cd docker
```

#### 11.2.5 Configure secrets

```bash
cp .env.example .env
nano .env
```

At minimum set:

```
POSTGRES_PASSWORD=<openssl rand -base64 32>
JWT_SECRET=<openssl rand -base64 64>
BOOTSTRAP_TOKEN=<openssl rand -base64 32>
PUBLIC_BASE_URL=https://viator.example.com
OTP_BUILD_HEAP=24g           # 8g for regional, 24g for France-wide
OTP_BUILD_MEM_LIMIT=28g      # heap + ~4 GB native headroom; cgroup cap on otp-build
OTP_BUILD_PHASES=two_phase   # 'one_shot' available as fallback (debug only)
OTP_SERVE_HEAP=8g
```

> **Save the `BOOTSTRAP_TOKEN` somewhere outside the VPS** (password manager). You need it once for §11.4. After consumption, set it to empty in `.env` and `docker compose restart web`.

#### 11.2.6 Pull images and start

If you've enabled GHCR pulls (15.7.4 made the packages public) and your `docker-compose.yml` references the GHCR image tags, pull instead of build:

```bash
docker compose pull web
docker compose up -d postgres web worker nginx
docker compose logs -f web        # Ctrl-C once you see "Application startup complete"
```

If you build locally instead:

```bash
docker compose build
docker compose up -d postgres web worker nginx
```

#### 11.2.7 Wire HTTPS

`docker/nginx/nginx.conf` already includes the `:443` server block, the `:80 → :443` redirect, and a Mozilla "intermediate" TLS profile (TLS 1.2 + 1.3, no weak ciphers). `docker/docker-compose.yml` already mounts `/etc/letsencrypt:/etc/nginx/certs:ro` on nginx and exposes port 443. Wiring HTTPS is therefore: issue the cert, force-recreate nginx, flip the cookie/URL settings, set up renewal hooks. Full step-by-step in `docker/INSTALL.md` §9.

**Prerequisites**
- Public DNS resolves the chosen hostname to the VPS IP. Verify with `dig @8.8.8.8 +short <hostname>` (force a public resolver — local `dig` checks `/etc/hosts` first and returns `127.0.1.1` for the VPS's own name).
- The Phase-A `nginx.conf` has `vmi3259514.contaboserver.net` hardcoded as both `server_name` and the cert path. **If your hostname differs**, edit `docker/nginx/nginx.conf` (search/replace) and commit before issuing the cert — otherwise nginx fails to start because the cert path doesn't exist. Templating via env var is Phase-B work (alongside the orchestrator auto-wire).

**Issue the cert and bring up TLS**

```bash
sudo apt install -y certbot
cd /opt/viator/docker
docker compose stop nginx                                          # free port 80 for the challenge
sudo certbot certonly --standalone -d <hostname> \
    --email <contact-email> --agree-tos --no-eff-email --non-interactive
docker compose up -d --force-recreate nginx                         # picks up the cert + 443 mapping
```

The `ps` PORTS column should now show `0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp`. If only `:80` shows, the running container was started before the HTTPS-aware compose file landed locally — `git pull` and re-`force-recreate`.

**Flip cookie + base URL to HTTPS**

```bash
sed -i 's/^JWT_COOKIE_SECURE=.*/JWT_COOKIE_SECURE=true/' /opt/viator/docker/.env
sed -i 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://<hostname>|' /opt/viator/docker/.env
docker compose up -d --force-recreate web                           # re-read .env (restart doesn't)
```

`JWT_COOKIE_SECURE=true` makes browsers drop the cookie on plain HTTP (intended). `PUBLIC_BASE_URL` builds absolute URLs in magic-link emails.

**Verify**

```bash
curl -s https://<hostname>/healthz                                  # → {"status":"ok"}
curl -sI -X GET http://<hostname>/healthz | head -3                 # → HTTP/1.1 301; location: https://<hostname>/healthz
```

In a browser: `https://<hostname>/login` → green padlock + Let's Encrypt cert.

**Cert auto-renewal hooks**

Certbot's apt-installed systemd timer runs `certbot renew` daily, which uses the original method (`--standalone`) — that conflicts with running nginx on port 80. Add per-cert hooks so renewal stops nginx, renews, and starts nginx automatically:

```bash
sudo python3 - <<'PYEOF'
from pathlib import Path
import re
HOSTNAME = "<hostname>"          # substitute
f = Path(f"/etc/letsencrypt/renewal/{HOSTNAME}.conf")
text = f.read_text()
parts = text.split('[renewalparams]')
if len(parts) > 2:                              # de-duplicate any earlier mistakes
    text = parts[0] + '[renewalparams]' + parts[1].rstrip() + '\n'
text = re.sub(r'^pre_hook\s*=.*\n?',  '', text, flags=re.MULTILINE)
text = re.sub(r'^post_hook\s*=.*\n?', '', text, flags=re.MULTILINE)
text = text.rstrip() + (
    '\npre_hook = docker compose -f /opt/viator/docker/docker-compose.yml stop nginx'
    '\npost_hook = docker compose -f /opt/viator/docker/docker-compose.yml start nginx\n'
)
f.write_text(text)
PYEOF

sudo certbot renew --dry-run                                        # all simulated renewals should succeed
```

> **Don't use `tee -a [renewalparams]` for this.** Appending creates a duplicate `[renewalparams]` section, which Python's INI parser rejects with `parsefail`. The script above is idempotent and de-duplicates if you've already tried.

The "ran with error output" lines that `--dry-run` prints around the hooks are misleading — `docker compose stop`/`start` writes progress to stderr; the hooks succeeded. Verify with `docker compose ps nginx` showing nginx back up.

### 11.3 Database initialisation

Postgres comes up empty on first boot. The web container runs `alembic upgrade head` automatically at startup (entrypoint), so by the time the `web` container reports healthy, all 19 tables and the `journey_trip_provenance` view exist.

If the migration ever needs to be re-run by hand:

```bash
docker compose exec web alembic upgrade head
docker compose exec web alembic current   # confirm head
```

### 11.4 First-time platform-admin bootstrap

On a fresh DB, no users exist — including no admin. The bootstrap endpoint creates the first one:

```bash
curl -X POST https://viator.example.com/api/auth/bootstrap-platform-user \
  -H 'Content-Type: application/json' \
  -d '{
    "token":    "<the BOOTSTRAP_TOKEN you set in .env>",
    "email":    "you@example.com",
    "name":     "Patrick Heuguet",
    "password": "<a strong password, ≥12 chars>"
  }'
```

Response: `200 OK` with a JWT and a `viator_session` cookie set. The endpoint then refuses subsequent calls (returns 403) — it checks for any existing `platform_admin` user before allowing.

**Immediately after** a successful bootstrap:

1. Edit `.env` → set `BOOTSTRAP_TOKEN=` (empty).
2. `docker compose restart web` so the empty value takes effect.
3. Log into the admin UI at `https://viator.example.com/login`.

You're now an authenticated platform admin and can invite the rest of the team via Admin → Users.

### 11.5 Session lifecycle (creating a new comparison session)

A "session" is one OTP instance with its own data, independent from any other session. The fanout endpoint queries all `serving` sessions in parallel and merges results.

```
PA → Admin → Sessions → New
   POST /api/sessions {id:"nap-fr-2026-q2", label:"NAP Q2 2026", ...}
   ↓ INSERT row, state='created'

PA → Configure sources (URLs, schedules)
   PATCH /api/sessions/<sid> {config: {...}}
   ↓ state='configured'

CM → Upload feeds (UI or via API)
   POST /api/sessions/<sid>/uploads (multipart)
   ↓ state='populated' once required artefacts present

worker → otp-build job (debounced or manual trigger)
   docker run --rm -v inbox-<sid>:/inbox -v graphs-<sid>:/graphs otp-build
   ↓ state='graph_built'

admin app → regenerate compose + nginx + reload
   ↓ state='serving' (now in fanout pool)

PA → eventually: Archive
   POST /api/sessions/<sid>/archive
   ↓ state='archived', removed from compose, kept for replay
```

> **Phase-A vs Phase-B status (April 2026).** The `state='graph_built' → 'serving'` transition currently requires an operator to run three commands by hand (regenerate fragments, `docker compose up -d`, `nginx -s reload`) — see `docker/INSTALL.md` step 10. Phase-B will wire `app/sessions_orchestrator.regenerate()` into the admin sessions API state transitions, have the worker shell out to compose-up after fragment regeneration, and trigger nginx reload via the docker socket. Tracked as the first post-deploy task; ~half a day of work, validated against real traffic during Phase-A operation.

### 11.6 Day-2 operations

#### 11.6.1 Updating the application

```bash
cd /opt/viator
git pull
cd docker
docker compose pull web                # if pulling from GHCR
# OR: docker compose build web         # if building locally
docker compose up -d web worker
docker compose exec web alembic upgrade head   # apply any new migrations
```

The `nginx`/`postgres` containers don't restart unless their image changed.

#### 11.6.2 Bumping the OTP version

```bash
nano docker/otp/Dockerfile          # change ARG OTP_VERSION=2.9.0 → newer
docker compose build otp otp-build
# For each session in 'serving' state, trigger a rebuild via the admin UI
# (Admin → Sessions → <sid> → Rebuild graph). Worker rebuilds and promotes.
```

#### 11.6.3 Changing platform configuration

All operational config (SMTP credentials, concurrency limits, retention windows, fanout timeouts) lives in the `platform_config` Postgres table — never in `.env`. See §12 for the schema.

To change something:

> Admin UI → Configuration → edit field → Save.

Behind the scenes: validated against `CONFIG_SCHEMA`, persisted, audited, in-process cache invalidated, concurrency semaphores hot-swapped (no restart needed).

If the UI is broken and you need an emergency override:

```bash
docker compose exec postgres psql -U viator -d viator -c \
  "UPDATE platform_config SET value='\"60\"' WHERE key='FANOUT_TIMEOUT_MS';"
docker compose restart web    # forces cache reload
```

#### 11.6.4 Inviting another user

> Admin UI → Users → Invite — pick a role (`platform_admin`, `content_manager`, `end_user`).

The invitee gets a magic-link email (sent through the SMTP credentials in `platform_config`). Token TTL is 24h.

If SMTP isn't wired yet, you can manually fetch the verification token:

```sql
docker compose exec postgres psql -U viator -d viator -c \
  "SELECT email, token_hash, expires_at FROM verification_tokens ORDER BY expires_at DESC LIMIT 5;"
```

…but the raw token is only known to the email; you can't reconstruct it from the hash. Easier: configure SMTP first (Admin → Configuration → SMTP_*), test with the "Send test email" button, then invite users.

#### 11.6.5 Triggering a manual graph rebuild

Three ways:

1. **UI** — Admin → Sessions → `<sid>` → "Rebuild graph". Queues a worker job.
2. **API** — `POST /api/sessions/<sid>/rebuild`.
3. **Direct compose** (last resort, bypasses bookkeeping):
   ```bash
   docker compose run --rm \
     -e SESSION_ID=<sid> \
     -e OTP_HEAP=$(grep OTP_BUILD_HEAP .env | cut -d= -f2) \
     otp-build
   ```

After the build, the worker promotes the new graph (`graphs-<sid>/<ts>/graph.obj` + symlink update) and triggers `docker compose up -d otp-<sid>`.

#### 11.6.6 Replaying a historical search

> Admin UI → Replay → pick a date range or search filter → "Replay against current `serving` sessions".

Each replay records as a new `JourneySearch` with `replay_of_search_id` pointing at the original. The UI shows a side-by-side diff of original vs current trips, flagging stations/routes that have moved.

If a session's `main_version` differs from the original search's `main_version`, replay is **skipped for that session** (not silently re-run on a different timetable) — the UI badges this clearly.

### 11.7 Backups & disaster recovery

#### 11.7.1 What to back up

| Volume | Why | Strategy |
|---|---|---|
| `pgdata` | All identity, config, audit, search history, master data | **Mandatory.** Daily `pg_dump`, off-VPS storage. |
| `inbox-<sid>` | Raw uploaded feeds | Optional — re-downloadable from upstream. Back up only if you accept manual uploads not re-fetchable from any URL. |
| `graphs-<sid>` | Built OTP graphs | **Don't back up.** Regenerable from `inbox-<sid>` in 30–60 min. |
| `nginx/certs` | Let's Encrypt certs | Optional — certbot will re-issue on a clean VPS in minutes. |

#### 11.7.2 Daily Postgres dump

```bash
# Add to /etc/cron.daily/viator-pg-dump
#!/bin/bash
set -euo pipefail
TS=$(date -u +%Y%m%d-%H%M%S)
docker compose -f /opt/viator/docker/docker-compose.yml exec -T postgres \
  pg_dump -U viator -d viator --format=custom --compress=9 \
  > /opt/backups/viator-${TS}.pgdump
# Push off-VPS:
rclone copy /opt/backups/ remote:viator-backups/ --max-age 24h
# Local rotation:
find /opt/backups -name 'viator-*.pgdump' -mtime +30 -delete
```

Restore on a new VPS (after running 11.2.1–11.2.6 to bring up an empty stack, then **stopping web** so it doesn't run migrations on top of the restore):

```bash
docker compose stop web worker
docker compose exec -T postgres pg_restore -U viator -d viator --clean --if-exists < /path/to/dump.pgdump
docker compose start web worker
```

#### 11.7.3 Configuration drift protection

Master-data rows with `source='manual'` are **never** overwritten by the Trainline CSV bootstrap or any automatic refresh. If you've curated the master_stations table by hand, those edits survive forever — no backup needed. Same rule for `route_aliases`: hand-entered rows are sacred.

### 11.8 Observability

#### 11.8.1 Health endpoints

| Endpoint | What it reports |
|---|---|
| `GET /api/healthz` | Web liveness — returns `200 {"ok": true}` if the process is alive |
| `GET /api/readyz` | Web + DB readiness — checks Postgres connectivity |
| `GET /otp/<sid>/actuators/health` | Per-session OTP — `{"status":"UP"}` once graph loaded |

External monitoring (UptimeRobot, Better Stack, etc.) should hit `readyz`, not `healthz` — readyz fails closed if the DB is down, healthz only fails if the process crashed.

#### 11.8.2 Logs

All containers log structured JSON to stdout. Docker's default `json-file` driver retains them on disk:

```bash
docker compose logs -f web                  # tail
docker compose logs --since 1h worker       # last hour
docker compose logs --tail 200 otp-nap-q2   # last 200 lines from one session
```

For long-term log shipping, point Docker at a remote log driver (e.g. `loki` or `awslogs`) — out of scope here.

#### 11.8.3 Optional Prometheus exporter

The web container exposes `/api/metrics` (disabled by default). Enable with platform_config key `METRICS_ENABLED=true`. Exported counters:

- `viator_uploads_total{session_id, format}`
- `viator_rebuilds_total{session_id, status}`
- `viator_auth_failures_total{reason}`
- `viator_fanout_executions_total{session_id, status}`
- `viator_fanout_latency_ms_bucket{session_id, le}`

Scrape from a Prometheus running outside the VPS — don't run Prometheus on the same VPS, that defeats the alerting.

### 11.9 Routine maintenance

| Task | Cadence | How |
|---|---|---|
| OS security updates | Weekly | `sudo apt update && sudo apt upgrade -y && sudo reboot` (off-hours) |
| Docker engine upgrade | Quarterly | `sudo apt upgrade docker-ce docker-ce-cli containerd.io` then `docker compose down && up -d` |
| Postgres minor version | When Postgres releases a patch | `docker compose pull postgres && docker compose up -d postgres` (pgdata persists across minor versions) |
| Postgres major version | Yearly | Manual: `pg_dump` from old, `pg_restore` into new. Don't trust Postgres to in-place upgrade across majors in a Docker volume. |
| Let's Encrypt renewal | Auto every 60 days | `systemctl list-timers \| grep certbot` to verify the timer is alive. **Hooks must be configured** (`pre_hook`/`post_hook` in `/etc/letsencrypt/renewal/<host>.conf` — see §11.2.7) or renewal will fail because nginx still owns port 80. Test once with `sudo certbot renew --dry-run`. |
| Retention pruning (raw 30d / trips 180d / searches 365d) | Daily, automatic | APScheduler in the `worker` container — see §6 |
| Master-data refresh (Trainline pull) | Monthly | Same scheduler — adjustable via `MASTER_REFRESH_CRON` in platform_config |
| Audit log archive | Yearly | `pg_dump audit_events` to cold storage, then `DELETE FROM audit_events WHERE ts < now() - interval '1 year'` |
| Secret rotation (`JWT_SECRET`, `BOOTSTRAP_TOKEN`, SMTP password) | Annually or on staff change | Edit `.env` (for JWT) or platform_config (for SMTP), `docker compose restart web`. Rotating `JWT_SECRET` invalidates all sessions — users must log in again. |

### 11.10 Incident triage runbook

| Symptom | Likely cause | First-response fix |
|---|---|---|
| `web` container restart-loops | Migration failure on startup | `docker compose logs web` — look for alembic error. If "table already exists" mismatch, manually run `alembic stamp head` and investigate. |
| `otp-<sid>` restart-loops with `OutOfMemoryError` | `OTP_SERVE_HEAP` < graph size | Raise `OTP_SERVE_HEAP` in `.env`, `docker compose up -d otp-<sid>`. Long-term: that session's graph has grown — consider splitting it. |
| `otp-build` killed by OOM-killer (exit 137) | VPS RAM too small for that bundle | Either upgrade VPS, use a regional GTFS only, or temporarily raise swap (`fallocate -l 16G /swapfile && mkswap /swapfile && swapon /swapfile`). |
| First request to OTP returns 503 | Graph not loaded yet | Wait for `Grizzly server running.` in `docker compose logs otp-<sid>`. Cold load takes 30–90 s for national. |
| All sessions return `partial` from fanout | One slow session pushing past `FANOUT_TIMEOUT_MS` | Check per-session p95 in Admin → Reports → Volume per session. Either raise the timeout in platform_config or investigate the slow session. |
| `worker` can't run `otp-build` (`permission denied … docker.sock`) | Socket perms | `sudo chmod 666 /var/run/docker.sock` (or run worker as root). On Linux distros that drop suid, may need `setfacl -m u:<uid>:rw /var/run/docker.sock`. |
| Login fails for everyone after a JWT secret rotation | Existing JWTs no longer valid (expected) | Tell users to log in again. If `BOOTSTRAP_TOKEN` is also empty, you can't bootstrap a new admin — restore old `JWT_SECRET` temporarily. |
| `bootstrap-platform-user` returns 403 | A platform admin already exists, or `BOOTSTRAP_TOKEN` is empty | Both correct behaviours. To create another admin, log in as the existing one and use the Users UI. |
| SMTP test email fails with auth error | Bad creds or expired app password | Re-enter in Admin → Configuration → SMTP_*. For Gmail/Workspace, generate a fresh app password. |
| Disk filling up with old graphs | Failed rebuilds left orphan graph dirs | `find /var/lib/docker/volumes/viator_graphs-*/_data -mindepth 1 -maxdepth 1 -type d -mtime +30 -name '20*' | head`. Verify they're not the current symlink target before deleting. |
| `pg_dump` fails with "out of memory" | Audit log too big | Run with `--exclude-table=audit_events` for the bulk dump, then a separate `--table=audit_events --jobs=4` dump. Or archive old audit rows first (see §11.9). |
| TLS cert expired | Certbot timer broken (often: `parsefail` on `/etc/letsencrypt/renewal/<host>.conf` from a duplicate `[renewalparams]` section, or missing `pre_hook`/`post_hook` so renewal can't free port 80) | `sudo certbot renew --force-renewal --pre-hook "..." --post-hook "..."` to recover the cert immediately, then fix the renewal config per §11.2.7 so the timer works unattended next time. Verify with `sudo certbot renew --dry-run`. |
| Trainline CSV refresh fails | Upstream URL changed or rate-limited | Admin → Master data → check last-refresh log. If URL changed, edit `app/master/trainline.py`. Manual rows (source='manual') are unaffected. |
| Fanout returns no results from a known-good session | Session's OTP graph not loaded, or session toggled `include_in_fanout=false` | Check `GET /api/sessions/<sid>` for state + flag. Toggle back via Admin → Sessions. |

### 11.11 Capacity planning thresholds

Watch these numbers; act when crossed:

| Metric | Yellow | Red | Action |
|---|---|---|---|
| pgdata size | 20 GB | 50 GB | Tighten retention windows (§12), or scale up disk |
| postgres `audit_events` row count | 10M | 100M | Archive + delete old rows (§11.9) |
| Per-session graph size | 6 GB | 12 GB | Raise `OTP_SERVE_HEAP`; consider splitting the session |
| Fanout p95 latency | 1500 ms | 3000 ms | Raise `FANOUT_TIMEOUT_MS`; investigate slow session |
| RAM headroom on VPS | 4 GB free | 1 GB free | Stop one session, or upgrade VPS |
| Disk headroom | 20% free | 10% free | Prune old graph dirs, archive audit, upgrade disk |

---

## 12. Platform configuration (DB-managed, OSCAR pattern)

### 12.1 Storage and schema

Configuration lives in the `platform_config` key-value table (§7) and is enforced in Python by a `CONFIG_SCHEMA` dict — exactly the OSCAR pattern. Each entry declares its type, bounds, default, and whether it's sensitive.

```python
class FieldSpec(TypedDict):
    type: Literal["str","int","bool","secret"]
    default: Any
    min: NotRequired[int]
    max: NotRequired[int]
    sensitive: NotRequired[bool]   # if True, masked in GET responses

CONFIG_SCHEMA: dict[str, FieldSpec] = {
    # ── SMTP ──────────────────────────────────────────────────────────
    "SMTP_HOST":     {"type": "str",    "default": ""},
    "SMTP_PORT":     {"type": "int",    "default": 587, "min": 1, "max": 65535},
    "SMTP_SECURE":   {"type": "str",    "default": "starttls"},  # 'none' | 'starttls' | 'tls'
    "SMTP_USER":     {"type": "str",    "default": ""},
    "SMTP_PASS":     {"type": "secret", "default": "", "sensitive": True},
    "SMTP_FROM":     {"type": "str",    "default": "no-reply@viator.local"},

    # ── Concurrency / server protection ───────────────────────────────
    "MAX_CONCURRENT_JOURNEYS": {"type": "int", "default": 20, "min": 1, "max": 200},
    "MAX_CONCURRENT_REBUILDS": {"type": "int", "default": 1,  "min": 1, "max": 4},
    "MAX_CONCURRENT_UPLOADS":  {"type": "int", "default": 3,  "min": 1, "max": 20},
    "JOURNEY_TIMEOUT_MS":      {"type": "int", "default": 8000, "min": 1000, "max": 60000},

    # ── Fanout behaviour ──────────────────────────────────────────────
    "FANOUT_TIMEOUT_MS":       {"type": "int",  "default": 10000, "min": 1000, "max": 60000},
    "FANOUT_PARTIAL_OK":       {"type": "bool", "default": True},   # if a session times out, return remaining; mark search status='partial'
    "STORE_RAW_RESPONSE":      {"type": "bool", "default": True},   # whether journey_search_executions.raw_response is populated

    # ── Master data refresh ───────────────────────────────────────────
    "MASTER_STATIONS_REFRESH_DAYS":  {"type": "int", "default": 30, "min": 1, "max": 365},
    "MASTER_CARRIERS_REFRESH_DAYS":  {"type": "int", "default": 90, "min": 1, "max": 365},

    # ── Replay safety caps ────────────────────────────────────────────
    "REPLAY_MAX_BATCH_SIZE":         {"type": "int", "default": 1000, "min": 10, "max": 10000},
    "REPLAY_MAX_RPS":                {"type": "int", "default": 5, "min": 1, "max": 50},

    # ── Registration policy ──────────────────────────────────────────
    "REGISTRATION_OPEN":         {"type": "bool", "default": True},
    "REGISTRATION_DEFAULT_ROLE": {"type": "str",  "default": "end_user"},

    # ── Retention ────────────────────────────────────────────────────
    # Three levels: full responses, structured trips, search summaries.
    # Pruning higher levels is cheaper than pruning summaries.
    "AUDIT_RETENTION_DAYS":              {"type": "int", "default": 365, "min": 30, "max": 3650},
    "JOURNEY_SEARCH_RETENTION_DAYS":     {"type": "int", "default": 365, "min": 30, "max": 3650},
    "JOURNEY_TRIPS_RETENTION_DAYS":      {"type": "int", "default": 180, "min": 30, "max": 3650},
    "JOURNEY_RAW_RESPONSE_RETENTION_DAYS": {"type": "int", "default": 30,  "min": 7,  "max": 365},
}
```

### 12.2 GET / PATCH semantics (mirroring OSCAR)

- **GET** returns every key, current value (or default if unset). `secret` fields are returned as `"********"` if non-empty, `""` if empty.
- **PATCH** accepts a partial object. For `secret` fields, the masked sentinel `"********"` is **skipped** (so a no-change PATCH on the SMTP screen doesn't blank the password). Anything else is validated against `CONFIG_SCHEMA`.
- Every successful PATCH writes one `audit_events` row per changed key, with `metadata = {"key": <key>, "from": <masked>, "to": <masked>}`.
- A read-through cache in the admin app refreshes from `platform_config` every 30 s, plus on PATCH.

### 12.3 Concurrency enforcement

Limits are enforced via in-process `asyncio.Semaphore` instances created at app start from the cached config:

```python
journey_sem  = asyncio.Semaphore(config.MAX_CONCURRENT_JOURNEYS)
upload_sem   = asyncio.Semaphore(config.MAX_CONCURRENT_UPLOADS)
rebuild_sem  = asyncio.Semaphore(config.MAX_CONCURRENT_REBUILDS)
```

Exceeded → endpoint returns `503 Service Unavailable` with `Retry-After: 5`. Excess hits beyond the semaphore are themselves recorded in `audit_events` so platform admins can see when the system was under-provisioned.

When the admin PATCHes a limit, the semaphores are recreated atomically (existing in-flight requests are unaffected; new ones see the new limit).

### 12.4 SMTP test endpoint

`POST /api/admin/config/smtp/test {to: "..."}` builds an `aiosmtplib.SMTP` connection from the **current cached config** (not the DB — so the admin can PATCH then test in the same session) and sends a small "VIATOR SMTP test from <user> at <ts>" email. Returns `{ok: true}` or `{ok: false, error: "..."}` with the SMTP error class for diagnostics.

---

## 13. Open decisions (need a steer before implementation)

1. ~~**SMTP provider**~~ — **resolved 2026-04-27**: SMTP is configured at runtime via the admin UI (`platform_config` table, OSCAR pattern).
2. ~~**Geocoder**~~ — **resolved 2026-04-27**: no external geocoder. The journey UI's From/To fields autocomplete against `master_stations` (returning UIC + lat/lon + multilingual names). VIATOR is a rail timetable demonstrator, not a multimodal door-to-door planner — station-to-station is the only mode in scope. Address-level geocoding (Nominatim/Photon) deferred until door-to-door enters scope.
3. ~~**Domain whitelist on registration**~~ — **resolved 2026-04-27**: registration is **open**, with full search-history tracking as compensating control. Admin-driven role promotion only.
4. **Session compose generation** — Pattern A (regenerate compose file) vs. Pattern B (Docker SDK) — confirm Pattern A is fine for ≤10 sessions.
5. ~~**OJP adapter timing**~~ — **resolved 2026-04-27**: deferred. The comparison value (fanout + origin flags + replay) ships first; OJP is added as the export surface for UIC partners only after the journey-UI loop is stable and the twin-NAP validation has produced its mirror reports. Stays at Phase 5 in the roadmap; no stub in earlier phases.
6. ~~**Comparison normalisation**~~ — **resolved 2026-04-27**: trip-signature based on UIC stops + route_short_name + minute-rounded times (see §6.4). Limitations acknowledged; aliases map can be added incrementally.
7. ~~**Spatial index**~~ — **resolved 2026-04-27**: lat/lon rounded to 4 decimals for O&D grouping. PostGIS deferred unless we need geographic radius queries.
8. ~~**Search retention**~~ — **resolved 2026-04-27**: three-tier retention (raw responses 30d, trips 180d, search summaries 365d). All editable from admin UI.
9. ~~**Replay scope**~~ — **resolved 2026-04-27**: capped via `REPLAY_MAX_BATCH_SIZE` (default 1000) and `REPLAY_MAX_RPS` (default 5), both editable from admin UI. Both also enforce the same-main-version rule (§6.6).
10. ~~**Trip-signature aliases**~~ — **resolved 2026-04-27**: `route_aliases` table is in the schema from day 1, bootstrapped empty. Content managers populate it as the unmatched-trips report (§6.8) surfaces drift like `"TGV"` vs `"TGV INOUI"`. The canonicaliser checks the alias table at signature time, so additions retroactively help future searches without rewriting historical signatures.
11. ~~**Main-version naming convention**~~ — **resolved 2026-04-27**: ISO-week range `YYYY-Www_YYYY-Www`. Universally honest across operators with non-aligned service periods.
12. ~~**Trainline refresh strategy**~~ — **resolved 2026-04-27**: monthly CSV pull (`MASTER_STATIONS_REFRESH_DAYS=30`). Conflict resolution = row-level lock + drift-surfacing table (§7.1 Conflict resolution). Local edits prevail; upstream changes never silently override; admins reconcile via the drift screen.
13. ~~**Master-data permission boundary**~~ — **resolved 2026-04-27**: all master data writes (stations, aliases, carriers, drift resolution, refresh triggers) open to **content_manager and platform_admin**. Role table updated in §3.1.

---

## 14. Bootstrap iteration — twin-NAP validation

Until MERITS feeds are actually available, the multi-session and fanout/comparison machinery has nothing real to compare. The iteration-1 strategy is to **stand up two sessions with identical NAP inputs** and use the divergence (or lack of it) as the smoke test for the entire comparison stack.

### 14.1 Setup

Create two sessions, identically configured:

| Session | id | category | inputs |
|---|---|---|---|
| Control | `nap-fr-control` | `NAP` | SNCF GTFS + France OSM PBF (the real NAP files) |
| Twin | `nap-fr-as-merits` | `MERITS` | **the same SNCF GTFS + the same OSM PBF** — file-for-file identical |

Both have `include_in_fanout = TRUE`. The journey UI's default search hits both via `/api/journey/fanout`.

### 14.2 What this validates

Because the input files are byte-identical, both sessions will compute **identical `feed_signature`s** and (modulo any non-determinism in OTP build) produce equivalent graphs. Expected behaviour:

| Surface | Expected outcome | If different → |
|---|---|---|
| `/api/journey/fanout` | Every trip flagged `BOTH` | trip_signature canonicaliser bug |
| `version-diff` between the two sessions' snapshots | Empty diff | snapshot derivation bug |
| `trip-source-distribution` report | 100% in "both" bucket | provenance view bug |
| Per-session response times | Comparable; minor variance OK | OTP/JVM warmup or networking issue |
| Replay — same search, both sessions | Identical itineraries | recording / proxy bug |

In other words: **the twin should look like a perfect mirror.** Any cell that doesn't is a bug in our plumbing, not a real comparison signal — fix before a real second source is wired in.

### 14.3 Transition to a real second source

When real MERITS feeds become available:

1. Create a third session `merits-pilot` (category `MERITS`), point it at the MERITS pulls.
2. Drop `nap-fr-as-merits` from fanout (`include_in_fanout = FALSE`) but keep its data for historical comparison.
3. The twin-validation reports become the **before** state of a "real difference vs noise" baseline — the divergence we now see between `nap-fr-control` and `merits-pilot` is meaningful precisely because we previously demonstrated zero divergence on identical inputs.

Optionally archive `nap-fr-as-merits` after the cut-over.

### 14.4 What it does NOT validate

- Cross-source identifier mapping (UIC ↔ trigramme, route name aliases) — both sessions use identical IDs, so this layer is exercised but not stressed.
- MERITS-specific format quirks.
- Real-time updaters from a different feed.

These need real MERITS data to test.

---

## 15. DevOps — operational user guide

This chapter is a **how-to**, not a design document: it tells you exactly what to install, what commands to run, and what to do when CI goes red. The pipeline is mostly hands-off — the manual interventions are documented at the end.

### 15.1 What's automated end-to-end

Every push to `main` (and every PR) runs the following without human action:

```
┌──────────────────────────────────────────────────────────────────────┐
│  GitHub push / PR                                                     │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
   ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
   │  CI workflow   │ │ Pre-commit job │ │ Docker workflow│
   │  ────────────  │ │ ────────────── │ │ ────────────── │
   │  • ruff        │ │  • runs all    │ │  • build web   │
   │  • black       │ │    pre-commit  │ │  • build otp   │
   │  • mypy strict │ │    hooks fresh │ │  • hadolint    │
   │  • bandit      │ │    in CI to    │ │  • Trivy scan  │
   │  • pytest      │ │    catch drift │ │  • push GHCR   │
   │  • coverage.xml│ │                │ │                │
   │  • SonarCloud* │ │                │ │                │
   └────────────────┘ └────────────────┘ └────────────────┘
                             │
                             ▼
              ┌────────────────────────────┐
              │  Status checks on commit   │
              │  PR cannot merge until ✅  │
              └────────────────────────────┘
```

`*` SonarCloud only runs if the repository variable `SONARCLOUD_ENABLED=true` and the secret `SONAR_TOKEN` are set — see 15.7.

The deploy workflow (`deploy.yml`) is **manual on purpose** — see 15.10.

### 15.2 Local development environment — one-time setup

#### 15.2.1 Required toolchain

| Tool | Version | Why this version | Where to get it |
|---|---|---|---|
| **Python** | **3.12.x** | CI uses 3.12; **3.14 has a known pip 26.1 incompatibility** (`AttributeError: module 'warnings' has no attribute '_add_filter'`). Stick to 3.12 locally. | https://www.python.org/downloads/release/python-3128/ |
| Docker Desktop | latest | For local OTP + Postgres + the per-session compose stack | docker.com |
| Git | 2.40+ | — | git-scm.com |
| GitHub CLI (`gh`) | optional | Lets you tail CI logs without opening a browser. Useful but not required. | https://cli.github.com/ |
| Node.js 22 | optional | Only if you'll touch the journey UI build (vanilla JS works fine without) | nodejs.org |

> **Windows specific:** if the Python installer asks "Add Python to PATH", say yes. Otherwise the `Scripts/` folder containing `uvicorn.exe`, `black.exe`, etc. won't be reachable. You can fix it after the fact via System Properties → Environment Variables, adding `C:\Users\<you>\AppData\Local\Programs\Python\Python312\Scripts` to the user PATH.

#### 15.2.2 Install the project deps

From the repo root:

```bash
# Pick the right Python explicitly (Windows: py -3.12; macOS/Linux: python3.12)
py -3.12 -m pip install -r requirements.txt          # runtime
py -3.12 -m pip install -r requirements-dev.txt      # adds black, ruff, mypy, pytest, bandit, pre-commit
```

> **Don't upgrade pip if it warns you to.** The notice "A new release of pip is available: 24.3.1 -> 26.1" is **harmless on Python 3.12**, but pip 26.1 is broken on Python 3.14 — keep pip 24.x to be safe across machines.

#### 15.2.3 Verify the install

A 5-second smoke check that doesn't need a database:

```bash
py -3.12 -c "from app import main; print('OK')"
```

If this prints `OK`, the import chain is healthy. If it raises, paste the traceback and fix before going further — CI will hit the same import error.

#### 15.2.4 Install pre-commit hooks (recommended)

```bash
py -3.12 -m pre_commit install
```

This wires `.pre-commit-config.yaml` into `.git/hooks/pre-commit`. From now on, ruff/black/mypy/hadolint run automatically on staged files at commit time. **Skip this if you prefer to lint manually** — the CI pre-commit job will catch any drift anyway.

### 15.3 Local quality gates — the same checks CI runs

Before you push, run these in order. Each takes seconds. CI will run identical commands.

```bash
# 1. Lint
py -3.12 -m ruff check .

# 2. Format check (do not auto-fix; just verify)
py -3.12 -m black --check .

# 3. Type check (strict — every function must be fully typed)
py -3.12 -m mypy --strict app

# 4. Security scan
py -3.12 -m bandit -r app -ll

# 5. Tests (needs a Postgres on localhost:5432, see 15.4)
py -3.12 -m pytest --cov=app --cov-report=xml
```

If any of those fail, CI will fail in the same way. Fixing locally is faster than push-wait-fix-push loops.

**Auto-fix shortcuts:**

```bash
py -3.12 -m ruff check . --fix      # fixes most lint issues automatically
py -3.12 -m black .                 # rewrites files in place to satisfy black --check
```

### 15.4 Running tests against Postgres locally

The integration tests need a real Postgres (the tests touch CITEXT, JSONB, GIN trigram indexes — sqlite isn't equivalent).

```bash
# Bring up the same image CI uses
docker run --rm -d --name viator-pg \
    -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=viator_ci \
    -p 5432:5432 postgres:16

# Run alembic migrations against it
DATABASE_URL=postgresql+psycopg://postgres:ci@localhost:5432/viator_ci \
    py -3.12 -m alembic upgrade head

# Run tests
DATABASE_URL=postgresql+psycopg://postgres:ci@localhost:5432/viator_ci \
    py -3.12 -m pytest -v

# Tear down when done
docker rm -f viator-pg
```

### 15.5 Repository structure for CI

```
.github/workflows/
├── ci.yml              # ruff + black + mypy + bandit + pytest + coverage + (optional) SonarCloud
└── docker.yml          # web + otp image build, hadolint, Trivy, GHCR push (matrix)

.pre-commit-config.yaml # local + CI pre-commit hooks
pyproject.toml          # ruff/black/mypy/pytest/coverage config (single source of truth)
sonar-project.properties # SonarCloud project metadata
.trivyignore            # CVE allow-list (see 15.8)
ci/trivy-config-ignore.rego  # OPA policy for Trivy config-mode (Dockerfile) findings
```

### 15.6 GitHub Actions workflows — what each does

#### 15.6.1 `ci.yml` — Python lint + type + test

Triggers: every PR + every push to `main`.

Two jobs run in parallel:

1. **`python`** — installs Python 3.12, brings up a Postgres 16 service container, runs ruff → black --check → mypy --strict → bandit → pytest → uploads `coverage.xml` as an artifact. If `SONARCLOUD_ENABLED=true`, also runs the SonarCloud scanner.
2. **`pre-commit`** — installs pre-commit and runs `pre-commit run --all-files`. This is the belt-and-braces job: it catches the case where `.pre-commit-config.yaml` and the standalone tool versions have drifted.

A failure in either job makes the PR un-mergeable (assuming branch protection is enabled per 15.7.3).

#### 15.6.2 `docker.yml` — image build + security scan + GHCR push

Triggers: push to `main` and tags matching `v*`.

Matrix build over `[web, otp]`:

1. **hadolint** lints the Dockerfile (DL3008, DL3015, etc.).
2. **`docker buildx build`** builds the image, with build cache stored in GitHub Actions cache.
3. **Trivy diagnostic pass** — prints findings as a table to stdout at severity CRITICAL,HIGH. Never fails (exit-code: 0). This is your window into "what's there" even when the gate passes — the table appears inline in the job log. **Important:** do not configure `output:` on this step; that redirects the table to a file and the log goes silent.
4. **Trivy gating pass** — same scan, fails on any unignored finding (exit-code: 1, SARIF output).
5. **SARIF upload to GitHub Security tab** — `continue-on-error: true` so a repo without Code Scanning enabled doesn't break CI. **Also gated to `push` events on `main`** — fork PRs can't write security-events to the upstream repo anyway, so skipping there reduces noise. To enable Code Scanning: Settings → Code security and analysis → Code scanning → Set up → Default (free for public repos).
6. **SARIF artifact upload** — always saves the scan result as a 14-day workflow artifact (`trivy-<image>-sarif`). Useful for offline grep with `jq` or for re-uploading to Code Scanning later if it's enabled after the run.
7. **Push** to `ghcr.io/<owner>/<repo>/<web|otp>:<sha>` and `:latest`.

> **Per-image Trivy scope** — the matrix sets `vuln-type` differently for each image:
> - **`web`** (FastAPI app): `os,library` — full scan, including all Python deps. We control everything in this image.
> - **`otp`** (`eclipse-temurin:25-jre-noble` + `otp-shaded-2.9.0.jar`): `os` only. The shaded jar bundles ~80 transitive Java deps that come from upstream OpenTripPlanner; we can't update them without forking OTP. Java CVEs are tracked at https://github.com/opentripplanner/OpenTripPlanner/issues, not blocked at our CI gate.
>
> Both images run with `--ignore-unfixed` so unactionable findings in base layers (no upstream fix yet) don't block the build.

### 15.7 GitHub repository setup checklist

This is the **once-per-repository** configuration. After this, everything is automatic.

#### 15.7.1 Secrets (Settings → Secrets and variables → Actions → Secrets)

| Secret | Required for | How to obtain |
|---|---|---|
| `SONAR_TOKEN` | SonarCloud upload | sonarcloud.io → My Account → Security → Generate Tokens |
| `GITHUB_TOKEN` | GHCR push | **auto-provided by GitHub** — no action needed |

#### 15.7.2 Variables (Settings → Secrets and variables → Actions → Variables)

| Variable | Effect | Default if absent |
|---|---|---|
| `SONARCLOUD_ENABLED` | Set to `true` to enable the SonarCloud scanner step | scanner is skipped (CI still passes) |

The `SONARCLOUD_ENABLED` gate exists so that contributors who fork the repo can run CI without needing a Sonar token of their own. Set it to `true` in the upstream repo only.

#### 15.7.3 Branch protection (Settings → Branches → Add rule for `main`)

Recommended:

- ✅ Require a pull request before merging
- ✅ Require status checks to pass — select `python`, `pre-commit`, and (if used) `sonar` and `docker`
- ✅ Require branches to be up to date before merging
- ✅ Require conversation resolution before merging
- ✅ Do not allow bypassing the above settings

#### 15.7.4 Packages (GHCR) visibility

After the first successful `docker.yml` run, two packages appear under the repo:

- `ghcr.io/<owner>/<repo>/web`
- `ghcr.io/<owner>/<repo>/otp`

Both are **private by default**. Make them public if you want the demonstrator pulls to be anonymous (recommended for an OSS demonstrator):

> Repo → Packages → click each package → Package settings → Change visibility → Public.

#### 15.7.5 Code Scanning (for Trivy SARIF uploads)

The `docker.yml` workflow uploads Trivy SARIF reports to the repo's Security tab. This requires Code Scanning to be enabled. The upload step is `continue-on-error: true`, so CI will pass either way — but if you want the findings visible in the GitHub UI:

> Repo → Settings → Code security and analysis → Code scanning → Set up → **Default**.

Free for public repos. For private repos, requires GitHub Advanced Security ($).

If you skip this, the SARIF reports are still uploaded as workflow artifacts (14-day retention) — download from the workflow run page if you need them.

#### 15.7.6 SonarCloud project setup (optional but recommended)

1. https://sonarcloud.io → log in with GitHub.
2. **+** → Analyze new project → import the GitHub repo.
3. Choose **GitHub Actions** as the analysis method.
4. Copy the `SONAR_TOKEN` it generates → paste into repo Secrets (15.7.1).
5. Set repo Variable `SONARCLOUD_ENABLED=true`.
6. Verify `sonar-project.properties` has the right `sonar.projectKey` and `sonar.organization` (set during creation).
7. Default "Sonar way" quality gate is appropriate: new-code coverage ≥ 80%, new-code maintainability A, no new security hotspots.

### 15.8 Trivy & security scanning — what to do with findings

The `docker.yml` workflow fails if Trivy reports any CRITICAL or HIGH CVE that has a fix available upstream **and is in scope for that image** (see the per-image scope rules in §15.6.2).

**See findings even when the gate passes** — the workflow runs Trivy twice on each image: first as a non-fatal diagnostic pass (table output, exit-code 0) printed to the job log, then as a gating pass (SARIF, exit-code 1). The first pass means you always see the CVE list; the second pass enforces it.

Six escape hatches, in order of preference:

1. **Apply pending OS patches at build time.** Both Dockerfiles run `apt-get update && apt-get upgrade -y` because the base image tags (`eclipse-temurin:25-jre-noble`, `python:3.12-slim`) are rebuilt on a slower cadence than `debian-security-announce` / `ubuntu-security-announce` post fixes. **Most OS-package CVE findings clear with a clean rebuild** (no code change needed). If a build runs from cache and skips the apt steps, force a rebuild: in CI, push an empty commit; locally, `docker compose build --no-cache <service>`.
2. **Reduce the dependency surface.** If a package brings transitive deps you don't need, swap to a leaner equivalent. Example: the web image previously installed Debian's `docker.io` (daemon + CLI + tools, ~50 transitive deps) just so the worker could shell out to `docker` to spawn otp-build jobs. Replaced with a multi-stage `COPY --from=docker:27-cli /usr/local/bin/docker` — single static go binary, no apt deps. Big CVE surface reduction for the same functionality.
3. **Update the base image.** If `eclipse-temurin:25-jre-noble` or `python:3.12-slim` has a newer revision, pin to it.
4. **Update the dependency** that triggered the finding (web image only — `requirements.txt`).
5. **Add to `.trivyignore`** — only if the finding is genuinely not exploitable in our context (e.g. the worker mounts `/var/run/docker.sock`, which Trivy flags but is documented as accepted in §10.1):
   ```
   # .trivyignore — one CVE per line, with a justifying comment above each
   # CVE-2024-XXXXX: jdwp debug port flag — not enabled in our JRE config
   CVE-2024-XXXXX
   ```
6. **For Dockerfile config findings** (e.g. "USER not set"), use `ci/trivy-config-ignore.rego` to suppress with rationale.

**Never blanket-ignore severity HIGH.** Each suppression must have a comment explaining why it's safe.

#### 15.8.1 Java CVEs in the OTP image — handled out-of-band

Java CVEs in the bundled `otp-shaded-2.9.0.jar` deps are intentionally **excluded from CI's Trivy gate** (see §15.6.2). Track them as follows:

- **SARIF still uploads** — even though `vuln-type: os` skips library findings during the gate, GitHub's Security tab receives whatever Trivy did report. Findings on OS packages show up there for triage.
- **For the Java-side**: file an upstream issue at https://github.com/opentripplanner/OpenTripPlanner/issues for any CRITICAL CVE in the shaded jar. When OTP releases a new version with the fix, bump `ARG OTP_VERSION` in `docker/otp/Dockerfile` (see §11.6.2).
- **If a Java CVE is being actively exploited** and OTP hasn't released a fix: temporarily change `vuln-type: os` → `vuln-type: os,library` for the OTP image to gate on it, then revert once OTP ships the fix. Or fork OTP. Both are heavy actions — reserved for genuine emergencies.

### 15.9 Pre-commit framework — what runs and when

`.pre-commit-config.yaml` configures these hooks (versions match the standalone tools in `requirements-dev.txt`):

| Hook | What it does | Auto-fix? |
|---|---|---|
| `ruff` | Lint | Yes (`--fix`) |
| `ruff-format` | Equivalent of black, faster | Yes |
| `black` | Format check | Yes (rewrites files) |
| `mypy` (strict) | Type check on `app/` | No — manual fix required |
| `hadolint` | Dockerfile lint | No |

After installing hooks (see 15.2.4), they run on every `git commit`. A failed hook **aborts the commit** and prints the diff. Re-stage the auto-fixed files and commit again.

To run them on the whole repo without committing:

```bash
py -3.12 -m pre_commit run --all-files
```

### 15.10 Deployment to the VPS — manual on purpose

There is **no auto-deploy on push to main**. The `deploy.yml` workflow exists as a workflow_dispatch (manual trigger) so you choose when production rolls forward.

The deploy step is:

1. **Tag the release locally:**
   ```bash
   git tag -a v0.3.0 -m "Step-21 complete; first end-to-end demonstrator"
   git push origin v0.3.0
   ```
   This triggers `docker.yml` to build images tagged `v0.3.0`.
2. **SSH to the VPS:**
   ```bash
   ssh viator@vps.trackonpath.com
   cd /opt/viator
   ```
3. **Pull the new images and restart:**
   ```bash
   docker compose pull web         # pulls ghcr.io/.../web:latest
   docker compose up -d web worker # rolling restart
   ```
4. **Run any pending Alembic migrations:**
   ```bash
   docker compose exec web alembic upgrade head
   ```
5. **Smoke check:**
   ```bash
   curl -fsS https://viator.trackonpath.com/api/health | jq
   ```

Full VPS bring-up (cold install on a new server) is documented separately in `docker/INSTALL.md`.

### 15.11 Manual interventions — when CI is red

This table captures every failure mode hit during initial bring-up. Use it as a triage runbook.

| Failing step | Symptom | Fix |
|---|---|---|
| **`black --check`** | `would reformat path/to/file.py` | Run `py -3.12 -m black .` locally, commit, push. Black is opinionated by design — never argue with it. |
| **`ruff check`** | Rule code + file:line + suggested fix | Most rules are auto-fixable: `py -3.12 -m ruff check . --fix`. For the rest, edit per the rule's docs at https://docs.astral.sh/ruff/rules/. |
| **`mypy --strict`** "Library stubs not installed for X" | A third-party library lacks type info | First try `types-X` on PyPI (e.g. `types-passlib`, `types-python-jose`). Add to `requirements-dev.txt`, reinstall, re-run. If no stubs exist, mark the import: `# type: ignore[import-untyped]` (with a comment explaining why). |
| **`mypy --strict`** "Returning Any from function declared to return X" | A typed function calls an untyped one | Wrap the return in an explicit cast: `result: str = untyped_call(...); return result`. Don't blanket-ignore. |
| **`mypy --strict`** "Function 'count' could always be true in boolean context" | SQLAlchemy Row attribute clashes with `tuple.count` / `tuple.index` method | Rename the column label: `func.count().label("count")` → `func.count().label("n_executions")` and update the consumer. |
| **`mypy --strict`** "Unused 'type: ignore' comment" | Mypy got smarter and no longer needs the suppression | Just delete the `# type: ignore[...]` comment. |
| **FastAPI startup** `AssertionError: Status code 204 must not have a response body` | Endpoint declared `status_code=204` with default JSON response class | Type the function as `-> Response` and return `Response(status_code=status.HTTP_204_NO_CONTENT)` explicitly. See the auth routes for the canonical pattern. |
| **`pytest`** Postgres connection refused | No Postgres on `localhost:5432` | Start one per 15.4, set `DATABASE_URL`. |
| **`bandit`** new MEDIUM/HIGH finding | A real security issue | Fix the code. If genuinely a false positive, mark the line with `# nosec BXXX` + a comment. |
| **`Trivy`** CRITICAL on the OTP image | Almost always a JRE base CVE | Bump `eclipse-temurin:25-jre-noble` digest in `docker/otp/Dockerfile`. If no fix available, use `--ignore-unfixed` (already on) — the build still passes. |
| **`pip install`** `AttributeError: module 'warnings' has no attribute '_add_filter'` | You're on Python 3.14 with pip 26.1 | Install Python **3.12** alongside 3.14: download from python.org, then use `py -3.12 -m pip` everywhere. |
| **`ruff` / `black` / `pytest` not found** after `pip install` | Scripts directory not on PATH | Either add `…\Python312\Scripts` to PATH (15.2.1), or always invoke as `py -3.12 -m <tool>`. |
| **PowerShell** `&&` parser error | You're on Windows PowerShell 5.1 (no `&&` support) | Use `;` to chain unconditionally, or `; if ($?) { ... }` for "run B only if A succeeded". Or upgrade to PowerShell 7. |
| **Git** "LF will be replaced by CRLF" warnings on Windows | Git's `core.autocrlf=true` rewriting line endings | **Harmless** — files in the repo stay LF, only the working copy gets CRLF. To silence: add a `.gitattributes` with `* text=auto eol=lf`. |
| **GitHub Actions** "Node.js 20 actions are deprecated" | The action's runner uses Node 20 internally; June 2026 makes Node 24 the default, September 2026 removes Node 20. | **Already addressed.** All actions bumped to versions that ship Node 24 runners: `actions/checkout@v5`, `actions/setup-python@v6`, `actions/upload-artifact@v5`, `actions/download-artifact@v5`, `docker/setup-buildx-action@v4`, `docker/login-action@v4`, `docker/metadata-action@v6`, `docker/build-push-action@v7`, `github/codeql-action/upload-sarif@v4`, `SonarSource/sonarcloud-github-action@v5`, `hadolint/hadolint-action@v3.3.0`. If a future warning lists a different action, run `curl -fsSL https://api.github.com/repos/<owner>/<repo>/releases?per_page=3` to find the latest published tag and bump to it. |
| **Coverage upload** "No files were found with the provided path: coverage.xml" | pytest didn't run (an earlier step failed) | Look at the previous step's logs — fix the upstream failure and the artifact will appear. |
| **GHCR push** "denied: permission_denied" | Workflow lacks `packages: write` permission | Already set in `docker.yml`. If you fork, you may need to enable Workflow permissions: Settings → Actions → General → Workflow permissions → "Read and write permissions". |
| **GitHub Actions** "Unable to resolve action `<owner>/<repo>@<version>`" | Three common causes: (1) the ref doesn't exist; (2) tags use `v` prefix but the pin omits it; (3) the action's tag is real, but its **internal** sub-action pin points at a deleted tag. | (1)+(2): `curl -fsSL "https://api.github.com/repos/<owner>/<repo>/tags?per_page=20"` and pick a real ref. For `aquasecurity/trivy-action`, tags use `v` prefix — `@v0.36.0`. (3) is sneakier: the failure message names the *sub*-action. Inspect `action.yaml` at the version you pinned (`curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/<ref>/action.yaml | grep uses:`) — if you see another action pinned by tag, check that tag exists too. **Bumping to a recent action version usually fixes this** (modern releases pin sub-actions by SHA). For trivy-action, releases ≤ v0.29.0 pin `setup-trivy@v0.2.2` (deleted); v0.36.0 pins by SHA. |
| **`upload-sarif`** "Resource not accessible by integration" | `github/codeql-action/upload-sarif` needs `actions: read` to fetch run metadata, in addition to `security-events: write`. The default `GITHUB_TOKEN` doesn't get this implicitly. | Add `actions: read` to the job's `permissions:` block. Already set in `docker.yml`. |
| **Trivy** scan exits 1 on the OTP image with Java CVE findings | OTP's shaded jar bundles transitive Java deps that we don't control | The workflow scopes the OTP image scan to `vuln-type: os` (OS packages only) for this reason. If you see this failure, it means someone changed the matrix entry to `os,library`. Either revert, or address the upstream Java CVE per §15.8.1. |
| **Trivy** scan exits 1 on **OS packages** (Ubuntu/Debian) | The base image is shipping with un-patched CVEs | First, check the diagnostic step's table output (immediately above the failed gating step) to see the CVE list. Then: (1) push an empty commit to force a fresh build that re-runs `apt-get upgrade`; (2) if that doesn't clear it, the patch isn't in the distro yet — bump the base image tag in `docker/<svc>/Dockerfile`; (3) if no fix exists upstream, add the CVE to `.trivyignore` with a one-line rationale. |
| **`certbot renew --dry-run`** "Renewal configuration file is broken … (parsefail)" | A duplicate `[renewalparams]` section in `/etc/letsencrypt/renewal/<host>.conf`. Usually caused by `tee -a` appending the section instead of editing inside the existing one. | Run the idempotent fix script in spec §11.2.7 — splits on `[renewalparams]`, keeps only the first body, strips any prior `pre_hook`/`post_hook`, then appends fresh hooks inside the (single) section. `--dry-run` after that should show "all simulated renewals succeeded." |
| **HTTPS** browser shows `ERR_TOO_MANY_REDIRECTS` | The login page redirects non-admins to `/`, and `/` redirects back to `/login` in Phase-2 mode. | Fixed in commit `7838f59` — login destination is now `/journey` for non-admins. Pull + rebuild web. If the issue persists, check `app/templates/auth/login.html` and `app/api/pages.py::login_page` — both should send non-admins to `/journey`. |
| **HTTPS** "ERR_CONNECTION_REFUSED" on `:443` after wiring HTTPS | Nginx container started before the HTTPS compose changes were applied. `docker compose ps nginx` shows only `:80` mapped. | `docker compose up -d --force-recreate nginx` to apply the new ports + cert mount. Plain `restart` doesn't re-read the compose file. |
| Browser shows native **basic-auth dialog** when hitting bare hostname | Phase-1 upload UI is reached at `/`. `ADMIN_USER` is set, or the redirect-to-login fix isn't deployed yet. | Two fixes: (1) `ADMIN_USER=` empty in `.env` puts you in Phase-2 mode where `/` redirects to `/login`; (2) ensure commit `f8706c6` or later is on the running web container (`docker compose ps web` should show recent `Up`). |
| **`upload-sarif`** "Code scanning is not enabled for this repository" | Code Scanning hasn't been turned on in repo settings | Two options: (a) Enable Code Scanning per §15.7.5 — free for public repos, gives you the Security tab UI; (b) ignore — the `upload-sarif` step is `continue-on-error: true`, and SARIFs are still saved as 14-day workflow artifacts. |
| **SonarCloud** "Project not found" | Repo Variable `SONARCLOUD_ENABLED=true` but project not yet imported on sonarcloud.io | Either import the project (15.7.5) or unset the Variable to skip the step. |

### 15.12 Tooling matrix (reference)

| Concern | Tool | Pinned version | Config location |
|---|---|---|---|
| Test runner | pytest + pytest-asyncio + pytest-cov + httpx | 8.3.4 / 0.24.0 / 6.0.0 / 0.28.1 | `pyproject.toml` `[tool.pytest.ini_options]` |
| Coverage | coverage.py (via pytest-cov) → `coverage.xml` | 7.13.5 | `pyproject.toml` `[tool.coverage.*]` |
| Lint | ruff | 0.7.4 | `pyproject.toml` `[tool.ruff]` |
| Format | black | 24.10.0 | `pyproject.toml` `[tool.black]` |
| Type | mypy (strict) | 1.13.0 | `pyproject.toml` `[tool.mypy]` |
| Security (code) | bandit | 1.7.10 | `pyproject.toml` `[tool.bandit]` |
| Security (deps) | pip-audit | 2.7.3 | command-line flags |
| Container scan | Trivy (via aquasecurity/trivy-action) | v0.36.0 | `.trivyignore`, `ci/trivy-config-ignore.rego` |
| Dockerfile lint | hadolint | 2.13.1-beta | command-line flags |
| Pre-commit | pre-commit | 4.0.1 | `.pre-commit-config.yaml` |
| Migrations | Alembic | 1.14.0 | `alembic.ini` + `alembic/env.py` |
| Code quality (cloud) | SonarCloud | n/a | `sonar-project.properties` |
| Type stubs | types-requests, types-passlib, types-python-jose, sqlalchemy[mypy] | various | `requirements-dev.txt` |

### 15.13 What the JS toolchain looks like (when we add it)

The journey UI is currently vanilla HTML+JS+CSS served by FastAPI/Jinja — no build step. If/when it grows to need a bundler:

| Concern | Tool |
|---|---|
| Test runner | Vitest (preferred over Jest — ESM-native, faster) |
| Coverage | c8 (vitest default) → lcov export |
| Lint | eslint + prettier |
| Type | TypeScript (optional) |
| Security | npm audit + eslint-plugin-security |

A new job in `ci.yml` would mirror the `python` job: `npm ci → npm run lint → npm test -- --coverage → upload lcov.info → SonarCloud`.

### 15.14 Architectural notes

- **Worker mounts `/var/run/docker.sock`** — Trivy will flag the worker container with high-severity findings related to root-equivalent host access. This is a **known accepted trade-off** documented in §10.1; CI suppresses it via `ci/trivy-config-ignore.rego`. Do not blanket-ignore severity HIGH.
- **OTP image** is a downstream rebuild of `eclipse-temurin:25-jre-noble` plus `otp-shaded-2.9.0.jar`. Trivy findings on the JRE base are not actionable by us — we depend on Temurin's update cadence. `--ignore-unfixed` is on.
- **Postgres-as-a-service in CI** is sufficient for integration tests. No need for testcontainers in CI itself; testcontainers is a local-developer convenience for running tests outside CI.
- **Coverage report ordering** — `coverage.xml` (Python) must exist on disk before the SonarCloud step runs. The `python` job emits it; the SonarCloud step `needs: python`.
- **Pip cache** — `actions/setup-python@v5` with `cache: pip` keys on `requirements*.txt` hashes. Bumping a single dep invalidates the cache for that key only.

---

## 16. Implementation order (proposed)

| Step | Deliverable | Effort estimate |
|---|---|---|
| **0a** | **CI scaffolding** — `pyproject.toml` (ruff/black/mypy/pytest config), `requirements-dev.txt`, `pre-commit-config.yaml`, `sonar-project.properties`, `.github/workflows/ci.yml`. Empty test that asserts the import chain so CI is green from day 1. | 0.5 day |
| **0b** | **Docker CI workflow** — `.github/workflows/docker.yml` (build + Trivy + push to GHCR with the `trivy-ignore` policy) | 0.5 day |
| 1 | Postgres schema migration scripts (Alembic) — all tables incl. `journey_searches` and `platform_config` | 0.5 day |
| 2 | `platform_config` module (CONFIG_SCHEMA, GET/PATCH, masking, audit, semaphore wiring) | 1 day |
| 3 | Auth module: register/confirm/login/me/reset, JWT, rate limit, audit. SMTP read from platform_config. | 2 days |
| 4 | OSCAR email-template port + SMTP test endpoint | 0.5 day |
| 5 | Admin user-management UI | 1 day |
| 6 | Admin platform-config UI (SMTP form + concurrency form + test-send button) | 1 day |
| 7 | Sessions table + admin CRUD UI | 1 day |
| 8 | Compose generator + nginx generator + per-session OTP | 1.5 days |
| 9 | Per-session ingestion (refactor current dispatcher) | 1 day |
| 10 | Per-session worker (rebuild queue keyed by session) | 1 day |
| 11 | **Master data tables** — schemas + Trainline CSV bootstrap importer + RICS dictionary loader | 1 day |
| 12 | **Graph snapshots** — record one row per OTP build, with main+update version derivation from feed metadata, validation rules | 1 day |
| 13 | **Search recording layer** — `journey_searches` + executions + trips writers; trip_signature canonicaliser using master_stations + route_aliases; provenance view | 2 days |
| 14 | **Fanout endpoint** — parallel OTP fan-out, merge by signature, partial-OK handling, response normalisation | 1.5 days |
| 15 | Three-tier retention cron (raw 30d → trips 180d → searches 365d) | 0.5 day |
| 16 | Reports endpoints (searches, O&D, volume, trip-source-distribution, version-diff, unmatched-trips, divergence, CSV) | 2 days |
| 17 | Admin reports UI (tables + filters + CSV export buttons) | 1.5 days |
| 18 | Journey UI v1 — fanout-default with origin badges + per-session timing strip + click-through detail panels | 2.5 days |
| 19 | Replay endpoint + admin UI (filter builder, batch outcome view, main-version-mismatch handling) | 1.5 days |
| 20 | Master-data admin UI (stations table + alias editor + Trainline refresh button) | 1.5 days |
| 21 | Bootstrap flow + docs | 0.5 day |

Total: **~26 person-days** for Phases 2–4 of the strategy roadmap (was 25; +1 for CI/CD scaffolding upfront).

The two CI scaffolding steps (0a, 0b) come first deliberately: every later step then lands with lint, type-check, tests, image scan, and SonarCloud quality gate already enforced. Cheaper to set up on day one than to retrofit on day twenty.
