# VIATOR — Technical specification

_Detailed engineering specification for the VIATOR demonstrator: identity & RBAC, multi-session architecture, ingestion, comparison, APIs, data model._

_Companion to `VIATOR-strategy.md` — read the strategy first for the why._

_Author: Patrick Heuguet — TrackOnPath_
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

A session can declare an optional `sources` config:

```json
{
  "sources": [
    {"kind": "GTFS",          "url": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip", "schedule": "daily"},
    {"kind": "OSM-PBF",       "url": "https://download.geofabrik.de/europe/france-latest.osm.pbf",                            "schedule": "weekly"},
    {"kind": "SNCF-MCT",      "url": "https://ressources.data.sncf.com/.../temps-correspondance-minimaux.csv",                "schedule": "weekly"},
    {"kind": "SNCF-Stations", "url": "https://ressources.data.sncf.com/.../gares-de-voyageurs.csv",                           "schedule": "weekly"}
  ]
}
```

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

Implementation: the admin app proxies to `http://otp-<sid>:8080/otp/gtfs/v1/index/graphql` with a translated GraphQL query. Returns OTP's response, normalised into a stable VIATOR JSON envelope so the journey UI doesn't have to worry about OTP version drift.

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
| `PATCH /api/users/<id>` | `{ role?, is_active? }` | `200 { ... }` | platform_admin |

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

## 11. Operations

### 11.1 First-time admin bootstrap

On first deploy, the DB has no users. The platform admin is created by hitting `POST /api/auth/bootstrap-platform-user` with a one-time `BOOTSTRAP_TOKEN` from environment. After consumption, the token is invalidated and bootstrap is disabled.

### 11.2 Session creation

```
PA → POST /api/sessions {id:'nap-fr-2026-q2', ...}
  ↓ INSERT row, state='created'
PA → PATCH /api/sessions/<id> { config: {sources:[...]} }
  ↓ state='configured'
CM → upload feeds (or auto-pull triggers)
  ↓ state='populated'
worker → run otp-build for that session
  ↓ state='graph_built'
admin app → regenerate compose & nginx, docker compose up -d otp-<id>
  ↓ state='serving'
```

### 11.3 Backups

| Volume | Strategy |
|---|---|
| `pgdata` | Daily `pg_dump` to off-VPS storage |
| `inbox` (per session) | Re-downloadable from upstream → optional |
| `graphs` (per session) | Regenerable from inbox → no backup |

### 11.4 Observability

- `/api/healthz` — admin app liveness.
- `/otp/<id>/actuators/health` — per-session OTP liveness.
- Structured JSON logs to stdout; aggregated by Docker's default driver.
- Optional: Prometheus exporter on the admin app for `uploads_total`, `rebuilds_total`, `auth_failures_total`.

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

## 15. DevOps & CI/CD

### 15.1 Tooling matrix

| Concern | Python (admin app + worker) | JavaScript (journey UI) | Containers |
|---|---|---|---|
| Test runner | **pytest** + **pytest-asyncio** + **httpx** test client | **Vitest** (preferred over Jest — ESM-native, faster, drop-in API) | — |
| Coverage | **coverage.py** → lcov export | **c8** (vitest default) → lcov export | — |
| Lint / format | **ruff** (replaces flake8 + isort + pylint) + **black** | **eslint** + **prettier** | **hadolint** for Dockerfiles |
| Type check | **mypy** in strict mode for `app/` | TypeScript optional (consider for v2) | — |
| Security | **bandit** + **pip-audit** | **npm audit** + **eslint-plugin-security** | **Trivy** (image CVE scan) |
| DB migrations | **Alembic** | — | — |
| Pre-commit | **pre-commit** framework wrapping ruff, black, prettier, eslint, hadolint | — | — |
| Dep updates | **Renovate** (richer than Dependabot, free for OSS) | same | same |

The Python/JS split is a feature, not a bug: each language ecosystem has mature, focused tooling. Trying to force a single test runner across both languages produces worse outcomes than running both natively.

### 15.2 GitHub Actions workflows

Three workflows, kept independent so they can fail in isolation:

```
.github/workflows/
├── ci.yml              # on every PR: lint, type-check, unit + integration tests, coverage upload
├── docker.yml          # on push to main: build images, scan with Trivy, push to GHCR
└── deploy.yml          # manual or on tag: pull images on the VPS via SSH, docker compose up -d
```

#### `ci.yml` shape

```yaml
name: CI
on: [pull_request, push]

jobs:
  python:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: ci, POSTGRES_DB: viator_ci }
        ports: [5432:5432]
        options: --health-cmd pg_isready
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: pip install -r docker/web/requirements.txt -r requirements-dev.txt
      - run: ruff check .
      - run: black --check .
      - run: mypy app/
      - run: pytest --cov=app --cov-report=xml
      - uses: codecov/codecov-action@v4   # or sonarcloud upload step
        if: always()

  js:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "22", cache: npm, cache-dependency-path: journey-ui/package-lock.json }
      - run: npm ci
        working-directory: journey-ui
      - run: npm run lint
        working-directory: journey-ui
      - run: npm test -- --coverage
        working-directory: journey-ui

  sonar:
    needs: [python, js]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }   # SonarCloud needs full history for blame
      - uses: SonarSource/sonarcloud-github-action@v2
        env:
          SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

#### `docker.yml` shape

```yaml
name: Docker
on:
  push:
    branches: [main]
    tags: ["v*"]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { packages: write, contents: read }
    strategy:
      matrix:
        image: [web, otp]
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - uses: docker/build-push-action@v6
        with:
          context: docker/${{ matrix.image }}
          push: true
          tags: ghcr.io/${{ github.repository }}/${{ matrix.image }}:${{ github.sha }}
      - uses: aquasecurity/trivy-action@master
        with:
          image-ref: ghcr.io/${{ github.repository }}/${{ matrix.image }}:${{ github.sha }}
          severity: CRITICAL,HIGH
          exit-code: 1
          ignore-unfixed: true
```

### 15.3 SonarCloud configuration

`sonar-project.properties` at repo root:

```properties
sonar.projectKey=trackonpath_viator
sonar.organization=trackonpath
sonar.sources=app,journey-ui/src
sonar.tests=tests,journey-ui/tests
sonar.python.version=3.12
sonar.python.coverage.reportPaths=coverage.xml
sonar.javascript.lcov.reportPaths=journey-ui/coverage/lcov.info
sonar.coverage.exclusions=**/tests/**,**/migrations/**
sonar.exclusions=docker/otp/**,**/__pycache__/**,**/node_modules/**
```

Quality gate: default SonarCloud "Sonar way" gate is fine for v1 (new-code coverage ≥ 80%, new-code maintainability A, no new security hotspots). Tighten later.

### 15.4 Test layout

```
tests/                                      # Python tests
├── unit/
│   ├── test_detect.py                      # format-detection rules
│   ├── test_trip_signature.py              # canonicaliser + alias resolution
│   ├── test_dispatch.py                    # ingestion routing
│   └── test_config_schema.py               # platform_config validation + masking
├── integration/
│   ├── test_auth_flow.py                   # register → confirm → login → me
│   ├── test_session_lifecycle.py           # create → upload → build → serve
│   ├── test_fanout.py                      # twin-session: 100% BOTH expected
│   └── test_replay.py                      # main-version mismatch handling
└── conftest.py                             # pytest fixtures: db, client, sample feeds

journey-ui/tests/                           # JS tests
├── unit/
│   └── results-merging.test.js             # frontend dedup of fanout results
└── e2e/                                    # optional Playwright later
```

Pytest fixtures use **testcontainers-python** for ephemeral Postgres and (for the integration tests that need it) a stub OTP container exposing canned GraphQL responses. This keeps integration tests hermetic and fast.

### 15.5 Pre-commit configuration

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.4
    hooks: [{id: ruff, args: [--fix]}, {id: ruff-format}]
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks: [{id: mypy, additional_dependencies: [pydantic, sqlalchemy]}]
  - repo: https://github.com/hadolint/hadolint
    rev: v2.13.1-beta
    hooks: [{id: hadolint-docker}]
  - repo: https://github.com/pre-commit/mirrors-prettier
    rev: v4.0.0-alpha.8
    hooks: [{id: prettier, types: [javascript, html, css]}]
```

### 15.6 Architectural notes for CI

- **Worker mounts `/var/run/docker.sock`** — Trivy will flag the worker container with high-severity findings related to root-equivalent host access. This is a **known accepted trade-off** documented in §10.1; CI suppresses it via a `trivy-ignore.rego` policy file. Don't blanket-ignore severity HIGH.
- **OTP image** is a downstream rebuild of `eclipse-temurin:25-jre-noble` plus an OTP shaded jar. Trivy findings on the JRE base are not actionable by us — we depend on Temurin's own update cadence. Configure `--ignore-unfixed` to suppress unactionable noise.
- **Multi-language coverage reports** — both `coverage.xml` (Python) and `lcov.info` (JS) must be uploaded **before** the SonarCloud job runs. The example workflow above has the right ordering.
- **Postgres-as-a-service in CI** is sufficient for integration tests. No need for testcontainers in CI itself; testcontainers is the local-developer convenience for running tests outside CI.

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
