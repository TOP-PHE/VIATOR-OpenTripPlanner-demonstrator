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
git clone https://github.com/TOP-PHE/VIATOR-OpenTripPlanner-demonstrator.git .
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
VIATOR_VERSION=v0.1.0       # pin to a release tag for reproducible deploys

POSTGRES_USER=viator
POSTGRES_PASSWORD=<openssl rand -base64 32>
POSTGRES_DB=viator

JWT_SECRET=<openssl rand -base64 64>
JWT_COOKIE_SECURE=false     # set to true after step 9 (HTTPS)
BOOTSTRAP_TOKEN=<openssl rand -base64 32>

PUBLIC_BASE_URL=https://<your-hostname>     # NOT viator.example.com — use your real hostname

OTP_BUILD_HEAP=24g          # 8g for regional, 24g for France-wide
OTP_BUILD_MEM_LIMIT=28g     # heap + ~4 GB native headroom; cgroup cap on otp-build
OTP_BUILD_PHASES=two_phase  # 'one_shot' available as fallback (debug only)

DOCKER_GID=<verify on host> # see note below — not always 999
```

> **`DOCKER_GID` matters.** The worker container runs as a non-root user that needs read access to `/var/run/docker.sock` (it spawns per-session OTP containers). The host's `docker` group GID is distribution-dependent — don't trust the `999` default in `.env.example`. Verify on the host first:
> ```bash
> getent group docker | cut -d: -f3
> ```
> Mismatched GID = silent worker failure (`Unable to find group …: no matching entries in group file`) on first `docker compose up`. Common values: 999 (Debian/Ubuntu apt-installed), 998 (some RHEL variants), 988 (newer Ubuntu installs where 999 was already taken).

> **`OTP_SERVE_HEAP` is NOT an `.env` variable.** Per-session OTP serve heap is configured per-session in the `platform_config` table (platform default) or in the session config (override). Don't try to set it in `.env` — it's a no-op. After step 8 (bootstrap), you'll set the platform default via `Admin → Configuration` in the UI, and override it per session via `Admin → Sessions → <session> → Edit`.

> **`PUBLIC_BASE_URL` must match the hostname certbot will issue against.** It's used to build absolute URLs in magic-link emails (registration confirm, password reset). The placeholder `viator.example.com` will leak into emails and break click-through if you forget to change it.

> **Save the `BOOTSTRAP_TOKEN` somewhere outside the VPS** (password manager). You need it once for step 8. Once consumed, set it to empty in `.env` and `docker compose restart web`.

> All other operational config (SMTP credentials, concurrency limits, retention windows, fanout timeouts) lives in the `platform_config` table inside Postgres — not in `.env`. You'll edit it from the admin UI after the bootstrap.

---

## 5.5 Bootstrap the runtime stub files (one-time, fresh-install only)

The web container's `_startup` hook regenerates the per-session compose +
nginx fragments in `docker/generated/` from current DB state on every boot.
But compose's `include:` directive (in `docker-compose.yml`) is parse-time
strict — `docker-compose.sessions.yml` must exist as valid YAML *before*
the web container is started. On a fresh clone, neither file exists yet.

Run from the repo root (`/opt/viator`, not `/opt/viator/docker`):

```bash
cd /opt/viator
./bin/viator-bootstrap-stubs.sh
```

Output:
```
created /opt/viator/docker/generated/docker-compose.sessions.yml
created /opt/viator/docker/generated/nginx-sessions.conf
```

The script is idempotent — running it twice is a no-op. See
`docker/generated/README.md` for the full lifecycle.

> **Known issue (≤ v0.1.32.15) — orchestrator empty-services regression.**
> With zero serving sessions, `app/sessions_orchestrator.render_compose`
> writes `services:` followed only by a comment, which YAML parses as
> `services: null` and compose then rejects with `services must be a
> mapping` on the *next* `docker compose up`. The bug surfaces on a fresh
> install on the second `up` (after web has booted once and overwritten
> the bootstrap stub the script just wrote).
>
> **Workaround until the fix lands:** before *every* `docker compose up`,
> overwrite the broken regen with valid YAML:
> ```bash
> cat > /opt/viator/docker/generated/docker-compose.sessions.yml <<'EOF'
> services: {}
> volumes: {}
> EOF
> ```
> Once you have at least one session in `state='serving'`, the bug
> becomes invisible — the renderer outputs real entries instead of the
> bare comment. Day-2 operations are unaffected; only fresh-install /
> empty-DB scenarios hit it.

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

## 7. Bring up the DB-backed services

We start everything **except** `nginx` and per-session OTP containers. nginx is held back because its HTTPS server block in `nginx.conf` references TLS cert files that don't exist yet on a fresh VPS — bringing it up now would crash-loop it on `cannot load certificate "/etc/nginx/certs/live/<hostname>/fullchain.pem"`. The cert is issued in step 9, after which nginx comes up cleanly.

```bash
docker compose up -d postgres web worker          # NOT nginx — comes up in step 9
docker compose ps
docker compose logs -f web
# wait for "Application startup complete." then Ctrl-C to detach
```

The web container's entrypoint runs `alembic upgrade head` automatically, so the schema is created on first start.

> If you accidentally start nginx alongside the others (e.g. by running `docker compose up -d` with no service list), you'll see it in a `Restarting` loop. Just `docker compose stop nginx` and continue with step 8 / 9 — no harm done.

---

## 8. Bootstrap the first platform admin

> **Order of operations on a fresh VPS: do step 9 (HTTPS) first, then come back here.**
> The bootstrap endpoint and the admin UI live behind nginx, which isn't running until step 9 issues the TLS cert. Trying the curl below before step 9 returns `Failed to connect … port 443: Couldn't connect to server`. Skip ahead to §9 now; this section assumes HTTPS is live.

The `platform_admin` role can do everything (create sessions, manage users, reconfigure SMTP, etc.). On a fresh DB no users exist, so we create the first one via the bootstrap endpoint:

```bash
curl -X POST https://<your-hostname>/api/auth/bootstrap-platform-user \
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
3. Log in at `https://<your-hostname>/login`.

You can now invite the rest of the team via Admin → Users.

---

## 9. Add HTTPS

Let's Encrypt issues free certs for any hostname with public DNS. The repo ships with a Phase-A nginx config that already includes the `:443` server block and HTTP→HTTPS redirect (`docker/nginx/nginx.conf`); you just need to issue the cert and trigger nginx to re-read its config.

### 9.1 Verify the public hostname resolves to your VPS

The Contabo auto-assigned name (e.g. `vmi3259514.contaboserver.net`), an `sslip.io` derivative, or a domain you control — any will do. Verify against a public resolver, **not** the VPS's own DNS (which checks `/etc/hosts` first and returns `127.0.1.1` for the local hostname):

```bash
dig @8.8.8.8 +short <your-hostname>
# Expected: <your-VPS-public-IP>
```

If it returns the wrong IP or nothing, fix DNS first. Let's Encrypt won't issue against IPs or unresolvable names.

### 9.2 Install certbot and issue the certificate

The Phase-A nginx config has `vmi3259514.contaboserver.net` hardcoded as the `server_name` and cert path. **If your hostname is different**, edit `docker/nginx/nginx.conf` first (search/replace) and `git commit` before issuing the cert — otherwise nginx will fail to start because the cert path doesn't exist.

```bash
sudo apt update && sudo apt install -y certbot

# Make sure nothing is bound to port 80 — certbot's --standalone mode
# binds it itself for the HTTP-01 challenge. If you came from step 7
# without starting nginx, port 80 is already free; the stop is a no-op.
cd /opt/viator/docker
docker compose stop nginx

# Issue the cert.
sudo certbot certonly --standalone \
  -d <your-hostname> \
  --email <your-contact-email> \
  --agree-tos --no-eff-email --non-interactive
```

Success leaves four symlinks at `/etc/letsencrypt/live/<hostname>/`:
`cert.pem`, `chain.pem`, `fullchain.pem`, `privkey.pem`.

### 9.3 Bring nginx back up with HTTPS

The compose file already mounts `/etc/letsencrypt:/etc/nginx/certs:ro` on the nginx service, so the cert is reachable inside the container. Force-recreate so the new port mapping takes effect:

```bash
docker compose up -d --force-recreate nginx
docker compose ps nginx
```

The `PORTS` column should now show `0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp`. If only `:80` is listed, the running container is on the pre-HTTPS config — `git pull` to be sure the local repo has the right `docker-compose.yml`, then re-run `--force-recreate`.

### 9.4 Flip the cookie + base URL to HTTPS

Edit `.env`:

```bash
sed -i 's/^JWT_COOKIE_SECURE=.*/JWT_COOKIE_SECURE=true/' /opt/viator/docker/.env
sed -i 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://<your-hostname>|' /opt/viator/docker/.env

# Force re-read of .env on web (restart doesn't pick up env changes)
cd /opt/viator/docker
docker compose up -d --force-recreate web
```

`JWT_COOKIE_SECURE=true` makes browsers drop the `viator_jwt` cookie on plain HTTP (intended). `PUBLIC_BASE_URL` is used to build absolute URLs in magic-link emails, so it must match the public HTTPS hostname.

### 9.5 Verify the TLS surface

```bash
# 1. HTTPS direct (use GET, not HEAD — /healthz is GET-only)
curl -s https://<your-hostname>/healthz
# Expected: {"status":"ok"}

# 2. HTTP redirects to HTTPS
curl -sI -X GET http://<your-hostname>/healthz | head -3
# Expected:
#   HTTP/1.1 301 Moved Permanently
#   location: https://<your-hostname>/healthz
```

In a browser: open `https://<your-hostname>/login` — green padlock, valid Let's Encrypt cert.

### 9.6 Wire cert auto-renewal hooks

Apt-installed certbot ships a systemd timer that runs `certbot renew` daily. Once the cert is within 30 days of expiry, the timer renews — but it uses the original method (`--standalone`), which conflicts with running nginx on port 80. Add `pre_hook` / `post_hook` to the per-cert renewal config so renewal stops nginx, renews, and starts nginx automatically:

```bash
sudo python3 - <<'PYEOF'
from pathlib import Path
import re

# Substitute your hostname in the next line.
HOSTNAME = "<your-hostname>"

f = Path(f"/etc/letsencrypt/renewal/{HOSTNAME}.conf")
text = f.read_text()

# Drop any duplicate [renewalparams] sections from earlier mistakes.
parts = text.split('[renewalparams]')
if len(parts) > 2:
    text = parts[0] + '[renewalparams]' + parts[1].rstrip() + '\n'

# Strip any prior pre_hook / post_hook lines so re-running is idempotent.
text = re.sub(r'^pre_hook\s*=.*\n?',  '', text, flags=re.MULTILINE)
text = re.sub(r'^post_hook\s*=.*\n?', '', text, flags=re.MULTILINE)

# Append the hooks inside the (single) [renewalparams] section.
text = text.rstrip() + (
    '\npre_hook = docker compose -f /opt/viator/docker/docker-compose.yml stop nginx'
    '\npost_hook = docker compose -f /opt/viator/docker/docker-compose.yml start nginx\n'
)

f.write_text(text)
print('Updated', f)
PYEOF
```

> **Don't use `tee -a [renewalparams]` to do this.** Appending creates a *second* `[renewalparams]` section, which Python's INI parser rejects with `parsefail`. The script above handles deduplication + idempotency.

Verify:

```bash
sudo certbot renew --dry-run
```

Expected near the end:

```
Hook 'pre-hook' ran with error output:
 Container viator-nginx-1 Stopping
  Container viator-nginx-1 Stopped
Simulating renewal of an existing certificate for <hostname>
Congratulations, all simulated renewals succeeded:
  /etc/letsencrypt/live/<hostname>/fullchain.pem (success)
Hook 'post-hook' ran with error output:
  Container viator-nginx-1 Started
```

The "ran with error output" lines are misleading — `docker compose stop/start` writes progress to stderr; the hooks succeeded. Verify with `docker compose ps nginx` showing nginx back up.

After this, real renewals in ~60 days happen unattended.

---

## 10. Create the first OTP session

From the admin UI:

> **Admin → Sessions → New** → fill `id` (e.g. `nap-fr-2026-q2`), `category` (`NAP`), `label`, save.

Then:

> Click into the session → **Configure** → set source URLs (SNCF GTFS feed URL, France OSM PBF URL, optional MCT/Stations CSVs) → save.

Once configured, the worker starts auto-pulling at the schedule you defined (or you can upload manually via the session's Uploads tab). When the inbox has the required files, the session moves to `populated` and you can:

> **Rebuild graph** — kicks off `otp-build` in a one-shot container; takes 30–60 min for national.

After the build completes the session moves to `graph_built`. The next step (going to `serving` with a live `otp-<sid>` container) requires regenerating the per-session compose + nginx fragments and reloading.

> **Phase-A note (current):** the orchestrator that writes those fragments is implemented but not yet auto-triggered (Phase-B work). After a session reaches `graph_built`, run from the VPS:
>
> ```bash
> cd /opt/viator/docker
> docker compose exec web python -c "
> from app import sessions_orchestrator
> from app.db import SessionLocal
> with SessionLocal() as db:
>     sessions_orchestrator.regenerate(db)
> "
> docker compose up -d                           # picks up the new otp-<sid> service
> docker compose exec nginx nginx -s reload      # picks up the new /otp/<sid>/ route
> ```
>
> After this three-command dance, the session shows `serving` and the fanout endpoint includes it. Phase-B will hook this into the session state machine so it happens automatically.

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
| `otp-build` killed by OOM-killer (exit 137) | `OTP_BUILD_MEM_LIMIT` is tight relative to `-Xmx`, OR VPS RAM is genuinely too small | First check `OTP_BUILD_MEM_LIMIT ≥ OTP_BUILD_HEAP + 4 GB` in `.env`. If already correct, upgrade VPS, use a regional bundle, or raise swap. (Two-phase build, default since v0.1.3, already keeps peak ~30% lower than one-shot.) |
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
