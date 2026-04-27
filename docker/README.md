# OTP-MERITS docker stack

A small VPS-ready stack that:

1. Runs **OpenTripPlanner** (serve mode).
2. Exposes a **web UI** to upload transit feeds (GTFS, NeTEx, MCT CSV, stations CSV, OSM PBF).
3. **Detects** the file format, **verifies** it matches the standard the operator declared,
   **routes** it to the right destination, and **queues** an OTP rebuild when applicable.
4. A **worker** debounces queued rebuilds and runs OTP build in a one-shot container.

The Phase-1 data sources are SNCF feeds from `transport.data.gouv.fr` (GTFS/NeTEx-FR/MCT);
the mid-term target is to ingest the same dataset families directly from **MERITS**
(the UIC central platform), which is why the project is named OTP-MERITS rather than
OTP-SNCF.

This is a Phase-1 skeleton aligned with `../VIATOR-strategy.md` and `../VIATOR-technical-spec.md`. It does **not** yet
include the OJP adapter (Phase 2) or a NeTEx-FR → Nordic converter (Phase 3).

## Layout

```
docker/
├── docker-compose.yml
├── .env.example
├── nginx/
│   └── nginx.conf
├── web/                       # FastAPI upload service + worker (same image)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py            # HTTP API + UI
│       ├── settings.py
│       ├── db.py              # SQLAlchemy models
│       ├── detect.py          # format sniffing
│       ├── ingestion.py       # routing rules
│       ├── worker.py          # rebuild queue worker
│       └── templates/index.html
└── otp/
    ├── Dockerfile             # downloads OTP shaded jar
    ├── entrypoint.sh          # build / serve modes
    ├── build-config.json
    └── router-config.json
```

## First run on a VPS

```bash
git clone <this-repo>
cd docker
cp .env.example .env
# edit .env — set strong POSTGRES_PASSWORD and ADMIN_PASSWORD

# Pre-seed the inbox so the first OTP build has data:
docker compose up -d postgres web worker
docker compose exec web bash -c '
  mkdir -p /data/inbox/{gtfs,osm,netex,dem,archive,runtime,_staging}
'
# Then upload via the UI (default http://VPS_IP/), or copy files in directly:
#   /data/inbox/gtfs/<sncf-gtfs.zip>
#   /data/inbox/osm/<france-latest.osm.pbf>

# Trigger first build manually (blocks ~30-60 min for France):
docker compose run --rm otp-build

# Start the OTP server:
docker compose up -d otp nginx
```

After the first successful build:

- The upload UI is at `http://<vps>/`.
- OTP GraphQL is at `http://<vps>/otp/routers/default/index/graphql` (Transmodel) and
  `http://<vps>/otp/gtfs/v1/` (GTFS schema).
- OTP debug UI is at `http://<vps>/otp/debug-client/`.

## Format dispatch matrix

| Declared standard          | Stored at                              | Triggers OTP rebuild? |
|----------------------------|----------------------------------------|-----------------------|
| `GTFS`                     | `/data/inbox/gtfs/`                    | yes                   |
| `OSM-PBF`                  | `/data/inbox/osm/`                     | yes                   |
| `NeTEx-Nordic`             | `/data/inbox/netex/`                   | yes                   |
| `NeTEx-EPIP`               | `/data/inbox/netex/`                   | yes (best-effort)     |
| `NeTEx-FR-Horaires`        | `/data/inbox/archive/NeTEx-FR-Horaires/` | **no — Phase 3**    |
| `NeTEx-FR-Arrets`          | `/data/inbox/archive/NeTEx-FR-Arrets/`   | **no — Phase 3**    |
| `SNCF-MCT`                 | `/data/inbox/runtime/SNCF-MCT/latest.zip` | no — runtime data  |
| `SNCF-Stations`            | `/data/inbox/runtime/SNCF-Stations/latest.csv` | no — runtime data |

## Operational notes

- **Heap:** `OTP_BUILD_HEAP=12g` and `OTP_SERVE_HEAP=16g` in `.env`. France-wide builds
  routinely OOM below 10 GB.
- **Debounce:** uploads coalesce; one rebuild runs per `DEBOUNCE_SECONDS` window (30 min default).
- **Graph snapshots:** the worker keeps the last 3 in `/data/graphs/<timestamp>/` and flips a
  `current` symlink. `otp` serves the symlink so a graph swap is atomic.
- **Auth:** the upload UI is behind HTTP basic auth (`ADMIN_USER` / `ADMIN_PASSWORD`).
  In production, terminate TLS at nginx and consider OIDC or the per-tester credential
  pattern from `oscar-server`.
- **Docker socket:** the `worker` mounts `/var/run/docker.sock` so it can launch
  the `otp-build` one-shot container. Treat the worker container as privileged.
- **NeTEx-FR uploads** are accepted, validated, and archived — but they will **not**
  trigger a rebuild until the converter exists. The UI shows them in the recent-uploads
  table with `Triggered build = —`.
