# VIATOR — Docker stack

VPS-ready stack for the **VIATOR** rail journey-planning demonstrator. One Docker Compose project provisions:

1. **`web`** — FastAPI admin app + journey UI + REST API (Jinja2 templates, JWT auth).
2. **`worker`** — APScheduler crons (retention, master-data refresh) + on-demand OTP graph builds (mounts `/var/run/docker.sock` to spawn `otp-build` jobs).
3. **`postgres`** — Postgres 16 with `pgcrypto`, `citext`, `pg_trgm` extensions.
4. **`nginx`** — public TLS termination + routing to web and per-session OTP containers.
5. **`otp-<session_id>`** — one container per active session (JRE 25 + `otp-shaded-2.9.0.jar`). Generated on demand by the admin app, not hand-edited.

The fanout endpoint queries every session in state `serving` in parallel and merges results — the multi-session shape lives in the spec at `../VIATOR-technical-spec.md` §4.

## Layout

```
docker/
├── docker-compose.yml          # base services (postgres, web, worker, nginx)
├── docker-compose.generated.yml  # per-session otp-<sid> entries (auto-generated)
├── .env.example
├── nginx/
│   ├── nginx.conf
│   └── conf.d/
│       └── sessions.generated.conf  # per-session routing (auto-generated)
├── web/
│   └── Dockerfile              # python:3.12-slim + docker CLI + app code
└── otp/
    ├── Dockerfile              # eclipse-temurin:25-jre-noble + otp-shaded jar
    ├── entrypoint.sh           # build / serve modes
    ├── build-config.json
    └── router-config.json
```

`docker-compose.generated.yml` and `nginx/conf.d/sessions.generated.conf` are rewritten by the admin app whenever a session is created, configured, started, or archived. Do not edit them by hand.

## First run on a VPS

The complete VPS install runbook lives in **`./INSTALL.md`** (URLs, sizing, hardening, certbot, etc.) and is mirrored in the spec at `../VIATOR-technical-spec.md` §11.2. The short version:

```bash
# Clone + configure
git clone https://github.com/TOP-PHE/VIATOR-OpenTripPlanner-demonstrator.git /opt/viator
cd /opt/viator/docker
cp .env.example .env && nano .env

# Bring up the platform services
docker compose pull web                    # or: docker compose build
docker compose up -d postgres web worker nginx
docker compose logs -f web                 # wait for "Application startup complete"

# Bootstrap the first platform admin (one-shot, see spec §11.4)
curl -X POST https://viator.example.com/api/auth/bootstrap-platform-user \
  -H 'Content-Type: application/json' \
  -d '{"token":"<BOOTSTRAP_TOKEN>","email":"you@example.com","name":"You","password":"…"}'

# Then clear BOOTSTRAP_TOKEN in .env and `docker compose restart web`
```

After that, sessions, uploads, builds and rebuilds are all driven from the admin UI at `https://viator.example.com/admin/sessions`.

## Volume map

The compose project is named `viator`, so volume names are `viator_<volume>`:

| Volume | Mounted in | Purpose | Backup? |
|---|---|---|---|
| `viator_pgdata` | `postgres` | Identity, config, audit, search history, master data | **Daily `pg_dump`** |
| `viator_inbox-<sid>` | `web`, `worker`, `otp-<sid>` | Uploaded raw feeds for one session | optional (re-downloadable) |
| `viator_graphs-<sid>` | `worker`, `otp-<sid>` | Built OTP graphs (`current` symlink + dated dirs) | regenerable, no backup |
| `nginx/certs` (bind mount) | `nginx` | Let's Encrypt certs | optional (certbot re-issues) |

## Authentication

VIATOR uses **JWT in an httponly cookie**, set by `/api/auth/login` after password verification. The first platform admin is created via the one-shot bootstrap endpoint (see spec §11.4); subsequent users are invited via the admin UI which sends a magic-link email through the SMTP credentials in `platform_config`.

There is **no basic auth** anywhere in the runtime stack. `/api/healthz` and `/api/readyz` are public; everything else requires a valid JWT (or, for HTML page routes, redirects to `/login`).

## Configuration

All operational tunables (SMTP credentials, concurrency limits, retention windows, fanout timeouts, etc.) live in the `platform_config` Postgres table — **not in `.env`**. Edit them via Admin → Configuration in the UI; changes are validated against `app/config_schema.py`, persisted, audited, and hot-swap into the running process. See spec §12 for the full schema.

The `.env` file is reserved for **bootstrap-only** secrets that need to exist before the database does:

| Var | Why it's in env, not DB |
|---|---|
| `POSTGRES_PASSWORD` | Postgres needs this at first start |
| `JWT_SECRET` | Required to mint the very first JWT (incl. the bootstrap one) |
| `BOOTSTRAP_TOKEN` | One-time platform-admin creation gate |
| `PUBLIC_BASE_URL` | Used in magic-link email URLs |
| `OTP_BUILD_HEAP` / `OTP_SERVE_HEAP` | Read at container start; per-session JVM heap |

## Operational notes

- **Heap sizing.** `OTP_BUILD_HEAP=24g` for France-wide bundles (lower OOMs); `OTP_SERVE_HEAP=8g` is enough for a regional graph. See spec §11.10 for capacity planning thresholds.
- **Debounce.** APScheduler in the worker coalesces rebuild requests per session (default 30 min window).
- **Graph snapshots.** Each session keeps the last 3 snapshots in `viator_graphs-<sid>/<timestamp>/` and flips a `current` symlink. OTP serves the symlink so swaps are atomic.
- **Docker socket.** The `worker` mounts `/var/run/docker.sock` so it can launch the `otp-build` one-shot container (and write per-session compose fragments). Treat the worker container as privileged. Trivy flags this; it's a documented accepted trade-off (spec §10.1).
- **OTP image.** Downstream rebuild of `eclipse-temurin:25-jre-noble` plus the OTP shaded jar (`https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar`). To bump versions, change `ARG OTP_VERSION` in `otp/Dockerfile`.
- **Multi-stage build.** The `web` image copies the docker CLI from `docker:29-cli` rather than installing Debian's `docker.io` package — same functionality, much smaller CVE surface.

## Logs and health

```bash
docker compose logs -f web                  # admin app + journey UI
docker compose logs -f worker               # cron output + spawned otp-build runs
docker compose logs -f otp-<session_id>     # per-session OTP routing engine

curl https://viator.example.com/api/readyz   # web + DB ready
curl https://viator.example.com/otp/<sid>/actuators/health  # per-session OTP
```

## What this stack does NOT include

- An OJP (CEN/TS 17118) adapter — OTP only natively understands Nordic NeTEx + GTFS. OJP is a CEN spec we may layer on later (spec §1, §13).
- A NeTEx-FR → Nordic profile converter — French Profil-France files are accepted into the inbox and archived, but they don't feed OTP yet (this is the Phase-3 milestone of the strategy doc).
- An MCT (minimum connection time) enforcement layer — MCT files are stored under `runtime/` waiting for the OJP layer.

These three are the next architectural milestones; until they exist, MERITS data flowing through the same fanout pipeline as NAP data is the realistic Phase-2 demo.
