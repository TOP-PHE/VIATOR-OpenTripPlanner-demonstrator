# VIATOR — Installation guide for a fresh VPS

Step-by-step procedure to bring the VIATOR stack from "blank Linux VPS" to "admin UI live with at least one OTP session serving SNCF itineraries."

This file is the long-form version. The same procedure is summarised in `../VIATOR-technical-spec.md` §11.2 — read whichever you prefer.

Estimated time: **45–90 minutes**, dominated by the first OTP graph build (30–60 min on the national bundle).

---

## 1. Provision the VPS

| Resource | Pilot (Île-de-France only) | National (France-wide) |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 16 GB | **32 GB** |
| SSD | 60 GB | 100 GB |
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |

The 32 GB floor is real: a France-wide OTP graph build needs ~24 GB heap. Hosts that fit: OVH, Scaleway, Hetzner, Infomaniak, AWS Lightsail.

Open inbound ports on the provider firewall:

- `22` (SSH) — restricted to your IPs.
- `80` (HTTP) — public during install.
- `443` (HTTPS) — public after step 9.

---

## 2. OS hardening

SSH in as root, then:

```bash
# Create a non-root user
adduser viator
usermod -aG sudo viator

# Lock down SSH
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/'    /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Basic firewall (ufw)
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# OS updates
apt update && apt -y upgrade
```

Reconnect as `viator` for the rest.

---

## 3. Install Docker Engine + Compose plugin

```bash
sudo apt install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Let viator run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# Sanity check
docker version
docker compose version
```

---

## 4. Get the stack onto the VPS

```bash
sudo mkdir -p /opt/viator
sudo chown $USER:$USER /opt/viator
cd /opt/viator
git clone https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator.git .
cd docker
```

---

## 5. Configure secrets (`.env`)

```bash
cp .env.example .env
nano .env
```

At minimum set:

```
POSTGRES_USER=viator
POSTGRES_PASSWORD=<openssl rand -base64 32>
POSTGRES_DB=viator

JWT_SECRET=<openssl rand -base64 64>
BOOTSTRAP_TOKEN=<openssl rand -base64 32>

PUBLIC_BASE_URL=https://viator.example.com

OTP_BUILD_HEAP=24g     # 12g if you're only doing a regional bundle
OTP_SERVE_HEAP=8g
```

> **Save the `BOOTSTRAP_TOKEN` somewhere outside the VPS** (password manager). You need it once for step 8. Once consumed, set it to empty in `.env` and `docker compose restart web`.

> All other operational config (SMTP credentials, concurrency limits, retention windows, fanout timeouts) lives in the `platform_config` table inside Postgres — not in `.env`. You'll edit it from the admin UI after the bootstrap.

---

## 6. Pull or build the images

If you've enabled GHCR pulls (the Trivy gate already runs in CI, so the published images are scanned), pull instead of build:

```bash
docker compose pull web
```

Or build locally:

```bash
docker compose build
```

Two images are produced/pulled:

- `viator-web` (FastAPI admin app + worker share this image; ~280 MB) — `python:3.12-slim` + the docker CLI from `docker:29-cli` (multi-stage) + `requirements.txt` + `app/`
- `viator-otp` (Eclipse Temurin JRE 25 + OTP shaded jar; ~420 MB)

If the OTP image build fails on the Maven download, the version pin is in `otp/Dockerfile` (`ARG OTP_VERSION=2.9.0`) — bump to a version that exists on Maven Central.

---

## 7. Bring up the platform services

We start everything **except** per-session OTP containers — those are spawned by the admin app on demand once a session is configured.

```bash
docker compose up -d postgres web worker nginx
docker compose ps
docker compose logs -f web
# wait for "Application startup complete." then Ctrl-C to detach
```

The web container's entrypoint runs `alembic upgrade head` automatically, so the schema is created on first start.

---

## 8. Bootstrap the first platform admin

The `platform_admin` role can do everything (create sessions, manage users, reconfigure SMTP, etc.). On a fresh DB no users exist, so we create the first one via the bootstrap endpoint:

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
3. Log in at `https://viator.example.com/login`.

You can now invite the rest of the team via Admin → Users.

---

## 9. Add HTTPS

Once the domain is pointing at the VPS:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d viator.example.com \
  --pre-hook  "docker compose -f /opt/viator/docker/docker-compose.yml stop nginx" \
  --post-hook "docker compose -f /opt/viator/docker/docker-compose.yml start nginx"
```

Then in `nginx/nginx.conf`: uncomment the `:443` server, mount the cert directory in `docker-compose.yml` (`./nginx/certs:/etc/nginx/certs:ro`), and `docker compose restart nginx`.

A renewal cron is auto-installed by certbot; verify with `systemctl list-timers | grep certbot`.

---

## 10. Create the first OTP session

From the admin UI:

> **Admin → Sessions → New** → fill `id` (e.g. `nap-fr-2026-q2`), `category` (`NAP`), `label`, save.

Then:

> Click into the session → **Configure** → set source URLs (SNCF GTFS feed URL, France OSM PBF URL, optional MCT/Stations CSVs) → save.

Once configured, the worker starts auto-pulling at the schedule you defined (or you can upload manually via the session's Uploads tab). When the inbox has the required files, the session moves to `populated` and you can:

> **Rebuild graph** — kicks off `otp-build` in a one-shot container; takes 30–60 min for national.

After the build completes the session moves to `serving` and is automatically added to `docker-compose.generated.yml` and `nginx/conf.d/sessions.generated.conf`. The fanout endpoint starts including it.

Smoke test from your laptop:

```bash
# Web
curl https://viator.example.com/api/readyz

# Per-session OTP
curl https://viator.example.com/otp/nap-fr-2026-q2/actuators/health
```

---

## 11. Day-2 operations

For routine operations — updates, backups, capacity planning, incident triage — see **`../VIATOR-technical-spec.md` §11**. Brief pointers:

- **Update the app**: `git pull` in `/opt/viator`, then `docker compose pull web && docker compose up -d web worker && docker compose exec web alembic upgrade head`. Spec §11.6.1.
- **Bump OTP version**: edit `ARG OTP_VERSION` in `otp/Dockerfile`, `docker compose build otp`, then trigger rebuild for each session via the admin UI. Spec §11.6.2.
- **Change platform config** (SMTP, concurrency, retention): Admin → Configuration. Spec §11.6.3.
- **Daily Postgres backup**: cron with `pg_dump --format=custom`, push off-VPS via rclone. Spec §11.7.
- **Capacity watermarks**: monitor `pgdata` size, fanout p95, RAM headroom. Spec §11.11.

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `web` container restart-loops | Migration failure on startup | `docker compose logs web` — look for alembic error. If "table already exists", manually `alembic stamp head`. |
| `otp-<sid>` restart-loops with `OutOfMemoryError` | `OTP_SERVE_HEAP` < graph size | Raise `OTP_SERVE_HEAP` in `.env`, restart. |
| `otp-build` killed by OOM-killer (exit 137) | VPS RAM too small | Upgrade VPS, use a regional GTFS, or raise swap. |
| First request to OTP returns 503 | Graph not loaded yet | Wait for `Grizzly server running.` in `otp-<sid>` logs. |
| `worker` can't run `otp-build` (`permission denied … docker.sock`) | Socket perms | `sudo chmod 666 /var/run/docker.sock` (or run worker as root). |
| `bootstrap-platform-user` returns 403 | A platform admin already exists, or `BOOTSTRAP_TOKEN` is empty | Both correct. To create another admin, log in as the existing one and use the Users UI. |
| SMTP test email fails | Bad creds in `platform_config` | Re-enter under Admin → Configuration → SMTP_*. For Gmail/Workspace, use an app password. |

A wider runbook (13 incident types) lives in spec §11.10.

---

## 13. What's NOT in this install

This brings up VIATOR Phase-2 (multi-session NAP comparison, identity/RBAC, search history, master data, replay):

- ✅ FastAPI admin app + JWT auth + admin UI
- ✅ Multi-session OTP (one container per session)
- ✅ Search recording + version-anchored fanout + replay
- ✅ Master stations + route-aliases + Trainline bootstrap
- ✅ Platform configuration (DB-managed, OSCAR pattern)
- ✅ Three-tier retention pruning + APScheduler crons
- ✅ Reports + CSV exports
- ⛔ OJP adapter in front of OTP — Phase 3 milestone
- ⛔ NeTEx-FR → Nordic profile converter — Phase 3 milestone (NeTEx-FR uploads are accepted and archived but don't feed OTP yet)
- ⛔ MCT enforcement at the OJP layer — Phase 3 milestone (MCT files are stored under `runtime/` waiting for the adapter)

The next milestone is the OJP adapter. Once it's in place, the same admin UI starts feeding it the MCT and Stations data automatically.
