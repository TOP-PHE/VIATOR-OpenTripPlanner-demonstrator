# Installation guide — OTP-MERITS stack on a fresh VPS

Step-by-step procedure to bring the stack from "blank Linux VPS" to
"OTP serving SNCF itineraries with an upload UI in front".

Estimated time: **45–90 minutes**, dominated by the first OTP graph build.

---

## 1. Choose and provision the VPS

| Resource | Pilot (Île-de-France only) | National (France-wide) |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 16 GB | **32 GB** |
| SSD | 60 GB | 100 GB |
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |

Providers that fit (any will do): OVH, Scaleway, Hetzner, Infomaniak, AWS Lightsail.

Open the following inbound ports on the provider's firewall:

- `22` (SSH) — restricted to your IPs.
- `80` (HTTP) — public during install.
- `443` (HTTPS) — public once TLS is wired (step 9).

---

## 2. Initial OS hardening

SSH in as root, then:

```bash
# Create a non-root user
adduser otpadmin
usermod -aG sudo otpadmin

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

Reconnect as `otpadmin` for the rest.

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

# Let otpadmin run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# Sanity check
docker version
docker compose version
```

---

## 4. Get the stack onto the VPS

You have three options; pick one.

### Option A — git clone (preferred)

If this `docker/` folder lives in a git repo:

```bash
sudo mkdir -p /opt/otp-merits
sudo chown $USER:$USER /opt/otp-merits
cd /opt/otp-merits
git clone <your-repo-url> .
cd docker
```

### Option B — scp from your laptop

From your Windows laptop in PowerShell:

```powershell
$src = "C:\Users\patri\OneDrive\Documents\TrackOnPath\Contract_execution\UIC_New_Revenue_Management project\projets\MERITS\Journey Planning\OpenJourneyPlanner\docker"
scp -r $src otpadmin@<VPS_IP>:/opt/otp-merits/
```

Then on the VPS:

```bash
cd /opt/otp-merits/docker
```

### Option C — rsync (faster for re-deploys)

```bash
rsync -avz --delete ./docker/ otpadmin@<VPS_IP>:/opt/otp-merits/docker/
```

---

## 5. Configure secrets (`.env`)

```bash
cd /opt/otp-merits/docker
cp .env.example .env
nano .env
```

Set, at minimum:

```
POSTGRES_PASSWORD=<long random string>
ADMIN_PASSWORD=<long random string>
OTP_BUILD_HEAP=12g     # or 24g for the national bundle
OTP_SERVE_HEAP=16g
```

Generate random passwords with `openssl rand -base64 32`.

---

## 6. Build the images

```bash
docker compose build
```

Two images are built:

- `otp-merits-web` (FastAPI + worker share this image; ~250 MB)
- `otp-merits-otp` (Eclipse Temurin JRE 25 + OTP shaded jar; ~400 MB, includes a one-time download of OTP from Maven Central)

If the OTP download fails, the version pin is in [otp/Dockerfile](otp/Dockerfile) (`ARG OTP_VERSION=2.6.0`) — bump to a version that exists on Maven Central.

---

## 7. Bring up the platform services

We start everything **except** OTP itself, since OTP needs a graph first.

```bash
docker compose up -d postgres web worker nginx
docker compose ps
docker compose logs -f web   # Ctrl-C to detach; check it boots cleanly
```

The upload UI is now reachable at `http://<VPS_IP>/`. Log in with `ADMIN_USER` / `ADMIN_PASSWORD`.

---

## 8. Pre-seed the inbox (first graph build)

You can either upload via the UI or drop files directly into the volume. Direct drop is faster for the first time.

### 8a. Find the inbox path on the host

```bash
docker volume inspect otp-merits_inbox -f '{{ .Mountpoint }}'
# typically: /var/lib/docker/volumes/otp-merits_inbox/_data
```

Export it for convenience:

```bash
INBOX=$(docker volume inspect otp-merits_inbox -f '{{ .Mountpoint }}')
sudo mkdir -p $INBOX/{gtfs,osm,netex,dem,archive,runtime,_staging}
```

### 8b. Download SNCF + OSM data

```bash
# SNCF national GTFS
sudo curl -L -o $INBOX/gtfs/sncf.zip \
  https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip

# France OSM PBF (~4 GB)
sudo curl -L -o $INBOX/osm/france-latest.osm.pbf \
  https://download.geofabrik.de/europe/france-latest.osm.pbf

# (Optional, runtime data — does not affect the graph)
sudo curl -L -o /tmp/mct.zip \
  https://ressources.data.sncf.com/explore/dataset/temps-correspondance-minimaux/files/<id>/download/
sudo curl -L -o /tmp/stations.csv \
  https://ressources.data.sncf.com/explore/dataset/gares-de-voyageurs/download/?format=csv
```

(The MCT and Stations CSVs are best uploaded through the UI later, so they get logged and dispatched into `runtime/`.)

### 8c. Run the first OTP build

This is a one-shot, foreground command. France-wide build = 30–60 min, RAM-bound.

```bash
docker compose run --rm \
  -e OTP_HEAP=$(grep OTP_BUILD_HEAP .env | cut -d= -f2) \
  otp-build
```

Watch for `Graph written.` near the end. The graph lands in the `graphs` volume.

### 8d. Promote the graph

The host-side worker normally does this; for the very first build, do it by hand:

```bash
GRAPHS=$(docker volume inspect otp-merits_graphs -f '{{ .Mountpoint }}')
TS=$(date -u +%Y%m%d-%H%M%S)
sudo mkdir -p $GRAPHS/$TS
sudo mv $GRAPHS/graph.obj $GRAPHS/$TS/graph.obj
sudo ln -sfn $TS $GRAPHS/current
ls -l $GRAPHS
```

---

## 9. Start OTP and verify

```bash
docker compose up -d otp
docker compose logs -f otp
# wait for: "Grizzly server running."
```

Smoke tests from your laptop:

```bash
# Health
curl http://<VPS_IP>/otp/actuators/health

# A simple GraphQL ping (GTFS schema)
curl -s http://<VPS_IP>/otp/gtfs/v1/index/graphql \
  -H 'content-type: application/json' \
  -d '{"query":"{ feeds { feedId } }"}'
```

OTP debug map (interactive trip planning) at:

```
http://<VPS_IP>/otp/debug-client/
```

---

## 10. Add HTTPS (recommended)

Once the stack is reachable on a domain (`otp.example.com`):

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d otp.example.com \
  --pre-hook "docker compose -f /opt/otp-merits/docker/docker-compose.yml stop nginx" \
  --post-hook "docker compose -f /opt/otp-merits/docker/docker-compose.yml start nginx"
```

Then in [nginx/nginx.conf](nginx/nginx.conf): uncomment the `443` server, mount the cert
directory in [docker-compose.yml](docker-compose.yml) (`./nginx/certs:/etc/nginx/certs:ro`),
and `docker compose restart nginx`.

A renewal cron is auto-installed by certbot; verify with `systemctl list-timers | grep certbot`.

---

## 11. Day-2 operations

### Update SNCF feeds

Use the UI (`http://<VPS_IP>/`) — pick the file, declare the standard, submit. The dispatcher rules in [web/app/ingestion.py](web/app/ingestion.py) decide whether a rebuild is needed.

The worker debounces uploads by `DEBOUNCE_SECONDS` (default 30 min) so multiple uploads in a short window cause only one rebuild.

### Watch a rebuild in progress

```bash
docker compose logs -f worker
```

The home page also shows recent jobs with status `pending|running|done|failed`.

### Update the stack itself

```bash
cd /opt/otp-merits
git pull               # or rsync from your laptop
cd docker
docker compose build
docker compose up -d
```

### Bump the OTP version

Edit `ARG OTP_VERSION` in [otp/Dockerfile](otp/Dockerfile), then:

```bash
docker compose build otp otp-build
docker compose run --rm otp-build      # rebuild graph with the new version
docker compose up -d otp
```

### Backups

Two volumes matter:

- `pgdata` — uploads metadata + audit log. Daily `pg_dump` is plenty.
- `inbox` — current input feeds. Re-downloadable from data.gouv.fr, so optional.

The `graphs` volume is regenerated on every build; no need to back up.

```bash
# Daily pg_dump example
docker compose exec -T postgres \
  pg_dump -U otp otp | gzip > /opt/backups/otp-$(date +%F).sql.gz
```

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `otp` container restart-loops with `OutOfMemoryError` | `OTP_SERVE_HEAP` < graph size | Raise `OTP_SERVE_HEAP` in `.env`, `docker compose up -d otp` |
| `otp-build` killed by OOM-killer (`exit code 137`) | VPS RAM too small | Upgrade VPS or use a regional GTFS only |
| First request to OTP returns 503 | Graph not loaded yet | Wait for "Grizzly server running." in logs |
| Upload returns "File looks like X but you declared Y" | Dropdown wrong, **or** SNCF changed their export schema | Check the file with `unzip -l file.zip`; update [web/app/detect.py](web/app/detect.py) if SNCF changed schema |
| GTFS-RT updaters log 401/403 | transport.data.gouv.fr proxy expects an API key | Add header in [otp/router-config.json](otp/router-config.json) |
| `docker compose run otp-build` says "no graph.obj written" | Inbox missing required files (no PBF, no GTFS) | Verify `$INBOX/gtfs/*.zip` and `$INBOX/osm/*.pbf` |
| Worker can't run `otp-build` (`permission denied … docker.sock`) | Socket permissions | `sudo chmod 666 /var/run/docker.sock` (or run worker as root) |

---

## 13. What is NOT in this install

This installs Phase 1 of the strategy in [../VIATOR-strategy.md](../VIATOR-strategy.md):

- ✅ OTP routing engine fed by SNCF GTFS + France OSM
- ✅ Upload UI with declared-standard validation and format dispatch
- ✅ Real-time updates (GTFS-RT, SIRI Lite)
- ⛔ OJP adapter in front of OTP (Phase 2)
- ⛔ NeTEx-FR → Nordic converter (Phase 3) — NeTEx-FR uploads are accepted and archived but do not feed OTP
- ⛔ MCT enforcement at the OJP layer (Phase 2) — MCT files are stored in `runtime/` waiting for the adapter

The next milestone is the OJP adapter. Once it's in place, the same upload UI starts feeding it the MCT and Stations data automatically.
