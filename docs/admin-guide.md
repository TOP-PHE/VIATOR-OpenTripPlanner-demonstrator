# VIATOR — Admin Guide

How to release new versions of VIATOR on GitHub, install the stack on a fresh
VPS, and keep it healthy day-to-day. Written from the perspective of the
person who owns the GitHub repo, the GHCR container registry, and the VPS
running the stack.

If you're an end user (running journey searches in the browser) or a
content manager (curating master stations / aliases), this is **not** the
doc you want — see `docs/nap-fr-rail.md` for an operator walkthrough and
the in-app help in Admin → Configuration for runtime knobs.

---

## 0. Doc map

This guide covers the **lifecycle**: write code → release a version →
deploy to the VPS → keep it running. For depth in any one area:

| Topic | Source of truth |
|---|---|
| First-time VPS install (provisioning, hardening, certbot) | `docker/INSTALL.md` |
| Architecture, data model, API surface | `VIATOR-technical-spec.md` |
| Why VIATOR exists, multi-session strategy | `VIATOR-strategy.md` |
| Container stack details (volumes, ports, generated files) | `docker/README.md` |
| Operator walkthrough — France rail demo | `docs/nap-fr-rail.md` |
| Brand identity (icon, palette) | `branding/VIATOR-brand-brief.md` |
| **This guide** | Release workflow + day-2 ops + troubleshooting |

---

## 1. The VIATOR pipeline at a glance

```
   ┌────────────────┐    git push    ┌─────────────────┐
   │ Local dev      │ ─────────────► │ GitHub repo     │
   │  ruff, mypy,   │  + git tag     │  main + v0.1.x  │
   │  pytest        │                └────────┬────────┘
   └────────────────┘                         │
                                              ▼
                                     ┌─────────────────┐
                                     │ GitHub Actions  │
                                     │  CI: lint/type  │
                                     │      test       │
                                     │  Docker: build  │
                                     │  Trivy scan     │
                                     │  Push to GHCR   │
                                     └────────┬────────┘
                                              │
                                              ▼
                          ┌────────────────────────────────────┐
                          │ ghcr.io/top-phe/viator-web:v0.1.x  │
                          │ ghcr.io/top-phe/viator-otp:v0.1.x  │
                          └────────────────┬───────────────────┘
                                           │ docker compose pull
                                           ▼
                                ┌─────────────────────────┐
                                │ VPS (Ubuntu 24.04)      │
                                │ /opt/viator/docker/.env │
                                │   VIATOR_VERSION=v0.1.x │
                                │                         │
                                │ docker compose up -d    │
                                │ ├── nginx (TLS)         │
                                │ ├── web (FastAPI + UI)  │
                                │ ├── worker (OTP build)  │
                                │ ├── postgres            │
                                │ └── otp-<sid>×N         │
                                └─────────────────────────┘
```

Key invariant: **the version baked into the web image at build time is
what shows in the UI badge and `/healthz/version`** (since v0.1.9). That's
your source of truth for "what is actually running."

---

## 2. Version management on GitHub

### 2.1 What gets a version

Both Docker images are tagged together from the same git commit:

| Tag pushed | What CI publishes |
|---|---|
| Branch push to `main` | `viator-web:latest`, `viator-otp:latest`, `:main`, `:sha-<short>` |
| Tag push `v0.1.9` | `viator-web:v0.1.9`, `:0.1.9`, `:0.1`, plus the same on `viator-otp` |
| Pull request | `viator-web:pr-NN` (build + scan, **no push**) |

The UI badge value comes from a Docker `ARG VIATOR_VERSION` set by the GHA
workflow's "Compute VIATOR_VERSION label" step — `v0.1.9` for tag pushes,
`main` for branch pushes, `pr-NN` for PR builds. Local builds without the
arg default to `dev`.

### 2.2 Semver in this project

We use **strict 3-part semver**: `vMAJOR.MINOR.PATCH`.

| When you bump | Examples |
|---|---|
| **PATCH** (`v0.1.9 → v0.1.10`) — bug fix only, no new feature, no schema change, no .env change | OTP version pin update; UI bug fix; CI gate fix |
| **MINOR** (`v0.1.9 → v0.2.0`) — new feature, backward-compatible | Multi-feed GTFS (v0.1.4); OSM scope filter (v0.1.5); provider-bundle model (v0.1.6); UI version badge (v0.1.9) |
| **MAJOR** (`v0.x.y → v1.0.0`) — breaking change requiring operator action | OJP adapter ships; provider-bundle replaces flat `sources.gtfs` (operator must migrate config) |

> **Don't use 4-part versions** like `v0.1.8.1`. The `metadata-action` in our
> Docker workflow rejects them as non-semver and the build fails. If you
> need a hotfix on top of `v0.1.8`, ship it as `v0.1.9`. Pre-release
> suffixes (`v0.1.9-rc1`) are valid semver if you want a candidate tag.

Pre-1.0 caveat: we're in `v0.x.y` territory, so MINOR bumps are routine.
Until `v1.0.0`, treat any operator-config change as deserving a MINOR not
a PATCH.

### 2.3 Cutting a release

The full procedure, with one real example you can replay any time:

```bash
# 0. Make sure you're on main with everything committed.
cd /path/to/OpenJourneyPlanner
git status                    # working tree clean
git log --oneline -5          # confirm what you're tagging

# 1. (Optional but recommended) Run the same gates CI runs, locally.
ruff check . && ruff format --check .
mypy app/
pytest tests/unit/            # integration tests need Postgres

# 2. Tag (annotated, with a short message).
git tag -a v0.1.10 -m "v0.1.10 — short description of the release"

# 3. Push commits + tag.
git push origin main
git push origin v0.1.10

# 4. Watch CI.
gh run list --limit 5 --workflow=docker.yml
gh run watch <run-id> --exit-status
gh run watch <ci-run-id> --exit-status   # the lint/type/test workflow

# 5. Confirm the image landed on GHCR.
gh api orgs/TOP-PHE/packages/container/viator-web/versions \
  --jq '.[0:3] | .[] | {tags: .metadata.container.tags, created: .created_at}'
```

Both workflows must complete green:

- **Docker workflow** — builds + Trivy-scans + pushes to GHCR. This is the
  one that gates publishing the image.
- **CI workflow** — lint (ruff), format (black), type (mypy --strict),
  security (bandit), test (pytest with coverage). This is the quality gate;
  it doesn't gate publishing but red CI on a release tag is technical debt.

### 2.4 Recovering from a broken CI on a fresh tag

The v0.1.9 release went through **five iterations** before landing.
Every time CI fails on a tag, the procedure is:

```bash
# 1. Diagnose the failure.
gh run view <failing-run-id> --log-failed | grep -E "error|FAILED" | head -20

# 2. Fix locally + commit.
# ... make edits ...
git add <changed files>
git commit -m "fix(...): explain what + why"

# 3. Move the tag forward to point at the fix commit.
git tag -d v0.1.10                                 # delete locally
git push origin :refs/tags/v0.1.10                 # delete remotely
git tag -a v0.1.10 -m "v0.1.10 — same message"     # re-create at HEAD
git push origin main
git push origin v0.1.10

# 4. CI re-runs automatically on the tag push. Watch.
gh run watch <new-run-id> --exit-status
```

This pattern (delete tag, re-create at new HEAD) is fine **as long as
nobody has pulled the broken image**. Once a tag has been pulled in
production, treat it as immutable and bump to the next patch.

### 2.5 Hotfix workflow

When something is broken in production and you need to ship NOW:

```bash
# 1. Branch from the broken release tag, not main.
git checkout -b hotfix/v0.1.10 v0.1.9

# 2. Cherry-pick or write the fix.
git cherry-pick <commit-sha-from-main>            # if already on main
# ... or just edit + commit fresh ...

# 3. Tag + push.
git tag -a v0.1.10 -m "v0.1.10 — hotfix: short reason"
git push origin hotfix/v0.1.10
git push origin v0.1.10

# 4. Once CI is green, merge the hotfix branch back into main.
git checkout main
git merge hotfix/v0.1.10                           # or PR + review if you prefer
git push origin main
```

The point of the branch is to avoid coupling the hotfix to whatever
unrelated work is on `main`. If `main` is clean and you trust it, you can
tag directly there.

---

## 3. Knowing what's running

Three places to look, in order of trustworthiness:

### 3.1 The UI badge (most trustworthy)

The pill badge next to the **VIATOR** word in the header on every page
shows the value baked into the running web image's `ENV VIATOR_VERSION`.
Hover for a tooltip explaining the source.

`dev` means a local build that didn't pass `--build-arg VIATOR_VERSION=…`.
Anything else is whatever CI stamped in.

### 3.2 The /healthz endpoint (scriptable)

```bash
curl -s https://your-host/healthz/version
# → {"version": "v0.1.9"}
```

No auth. Use in monitoring, deploy scripts, alerting.

### 3.3 docker / .env (what was *intended*)

```bash
# What the .env says we're pulling:
grep '^VIATOR_VERSION' /opt/viator/docker/.env
# → VIATOR_VERSION=v0.1.9

# What docker actually pulled:
sudo docker compose -p viator images | grep viator
# Shows resolved tags + image IDs

# What the running container's metadata says:
sudo docker inspect viator-web-1 \
  --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
# → 0.1.9   (from the OCI label)
```

If the badge says one thing and `.env` says another, **trust the badge**.
The most common cause is `.env` got edited after the last `docker compose up`
but before the container was recreated. Fix:

```bash
sudo docker compose pull web
sudo docker compose up -d --force-recreate web
```

---

## 4. Initial install

The full step-by-step procedure (provisioning, OS hardening, Docker, .env,
HTTPS via Let's Encrypt, first OTP session) lives in
[`docker/INSTALL.md`](../docker/INSTALL.md). Read it once and follow it.

After the install completes:

1. **Make GHCR packages public** — required for `docker compose pull` to
   work without a PAT. Go to
   `https://github.com/orgs/TOP-PHE/packages` (or the user-level packages
   page) and flip both `viator-web` and `viator-otp` to "Public" in their
   "Danger Zone" settings. One-time fix.
2. **Bootstrap the platform admin** — see INSTALL.md §8.
3. **Pin `.env` to a release tag** — never deploy `latest` to production:
   ```bash
   sudo sed -i 's/^VIATOR_VERSION=.*/VIATOR_VERSION=v0.1.9/' /opt/viator/docker/.env
   ```
4. **Create your first session** — see `docs/nap-fr-rail.md` for a
   walkthrough using the French NAP catalogue.

---

## 5. Routine maintenance

### 5.1 Deploying a new version

When CI is green for `v0.1.x` and you want it on the VPS:

```bash
ssh otpadmin@vmi3259514.contaboserver.net
cd /opt/viator/docker

# 1. Pin the .env (single source of truth for what version this VPS runs).
sudo sed -i 's/^VIATOR_VERSION=.*/VIATOR_VERSION=v0.1.x/' .env
grep '^VIATOR_VERSION' .env

# 2. Pull the new images. This is fast (only changed layers) — usually
#    under a minute on a decent connection.
sudo docker compose pull web worker otp-build

# 3. Recreate web + worker. Postgres and nginx stay up; running
#    per-session OTP containers stay up too (they'll pick up the new
#    OTP image only on their next graph rebuild).
sudo docker compose up -d web worker

# 4. Verify.
curl -s http://localhost/healthz/version    # should match VIATOR_VERSION
sudo docker compose logs --tail 30 web      # no migration errors
sudo docker compose logs --tail 30 worker   # picks up first tick

# 5. Browser smoke: load /admin/sessions, confirm the badge says the new
#    version, click into a session and verify journey search works.
```

If a release ships a database migration, the web container's entrypoint
runs `alembic upgrade head` automatically on startup. Watch the logs to
confirm it succeeded.

### 5.2 Backups

The only data that **can't be regenerated** is the Postgres database
(users, sessions, search history, master data, audit log, platform_config).
Everything else (inboxes, OTP graphs, nginx certs) is rebuildable.

Daily Postgres dump, push off-VPS:

```bash
# Add to crontab as root, runs at 02:00 UTC daily.
0 2 * * *  /usr/local/bin/viator-backup.sh >>/var/log/viator-backup.log 2>&1
```

Where `/usr/local/bin/viator-backup.sh` is roughly:

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%d-%H%M)
DUMP=/var/backups/viator/viator-$TS.dump

mkdir -p /var/backups/viator
docker compose -p viator exec -T postgres \
  pg_dump -U viator viator --format=custom --compress=9 > "$DUMP"

# Push off-VPS — pick one:
rclone copy "$DUMP" remote:viator-backups/   # rclone w/ S3/Backblaze/etc.
# OR
aws s3 cp "$DUMP" s3://viator-backups/        # awscli

# Keep 14 days locally
find /var/backups/viator -name 'viator-*.dump' -mtime +14 -delete
```

Restore drill (do this once on a staging VM to know the procedure works):

```bash
# Drop schema and restore.
docker compose -p viator exec -T postgres \
  psql -U viator -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
docker compose -p viator exec -T postgres \
  pg_restore -U viator -d viator < viator-20260101-0200.dump
```

### 5.3 SSL/TLS renewal

Certbot is wired with pre/post hooks (see INSTALL.md §9.6) that stop nginx,
renew, restart. Verify weekly that it would succeed:

```bash
sudo certbot renew --dry-run | tail -10
```

The systemd timer `certbot.timer` runs `renew` daily; actual renewal only
happens when the cert is within 30 days of expiry. If a renewal fails,
certbot emails the address you registered with (check inbox, not the VPS).

### 5.4 Logs & health

```bash
# Live tail of all platform services
docker compose -p viator logs -f web worker nginx postgres

# One-shot health check
curl -s https://your-host/healthz             # web reachable
curl -s https://your-host/healthz/version     # version baked in
curl -s https://your-host/api/readyz          # web + DB ready
curl -s https://your-host/otp/<sid>/actuators/health  # per-session OTP

# Disk usage (the easiest thing to neglect)
df -h /var/lib/docker
sudo du -sh /var/lib/docker/volumes/viator_*
```

### 5.5 Capacity planning

Three things to watch as you onboard more sessions:

| Watermark | Why it matters | When to act |
|---|---|---|
| `viator_pgdata` size | Search history grows; audit log grows | At 50% disk free, set up retention pruning more aggressively (Admin → Configuration) |
| `viator_graphs-<sid>` count | Each `serving` session keeps `current` + 2 historical graphs | At 80% disk free, drop unused sessions (Admin → Sessions → Delete) |
| Free RAM during a build | OTP build needs `OTP_BUILD_HEAP + ~4 GB` headroom | If a build OOMs, raise `OTP_BUILD_MEM_LIMIT` (≥ heap + 4) OR upgrade VPS RAM. France-wide builds need 32 GB minimum. |

---

## 6. Troubleshooting

### 6.1 `docker compose pull` returns "unauthorized"

Cause: GHCR package(s) are private, your local docker login expired, or
both.

```bash
# 1. Check what GHCR thinks (no auth, public packages return a token):
curl -s "https://ghcr.io/token?scope=repository:top-phe/viator-web:pull&service=ghcr.io"
# Public  → {"token":"..."}
# Private → {"errors":[{"code":"UNAUTHORIZED","message":"authentication required"}]}
```

If private, flip to public in the package settings (one-time, see §4
above). If you *want* it private, login with a PAT:

```bash
# Create at https://github.com/settings/tokens with `read:packages` scope.
echo 'ghp_YOUR_TOKEN' | sudo docker login ghcr.io -u TOP-PHE --password-stdin
```

### 6.2 OTP build OOM-killed (exit 137)

Sequence of checks:

```bash
# 1. Confirm the host actually has the RAM you think it does.
free -h
# If "Mem:  total" is less than what you provisioned, you need to
# downsize OTP_BUILD_HEAP + OTP_BUILD_MEM_LIMIT in .env.

# 2. Confirm OTP_BUILD_MEM_LIMIT is at least heap + 4 GB.
grep -E '^OTP_BUILD_(HEAP|MEM_LIMIT)' /opt/viator/docker/.env

# 3. Confirm the streetGraph cache is being used (avoids re-doing the
#    expensive OSM parse phase every time).
sudo ls -la /var/lib/docker/volumes/viator_graphs/_data/.cache/<sid>/
# Should show streetGraph.obj + streetGraph.key
# If missing or stale, the OSM PBF was probably re-fetched from
# Geofabrik (cache key = sha256(osm.pbf):scope).
```

If headroom is genuinely tight on France-wide:
- Switch to `OSM_SCOPE=transit-focused` (5 GB → 800 MB filtered, ~40%
  less heap pressure).
- Or split into regional sessions (Île-de-France only, etc.).

The "Intersecting unconnected areas..." OTP step is the single biggest
heap moment in a France-wide build. If it survives that, the rest is
downhill.

#### 6.2.1 Multi-country sessions (v0.1.30 — `rail-focused` scope)

For sessions covering more than one country (e.g. an "EU rail" session
with FR + UK + BE + NL + DE + CH + AT + IT + ES merged into one PBF):
the default `transit-focused` scope still keeps every drivable road in
every country, which blows past commodity VPS heap budgets. A 10-country
merge at `transit-focused` needs **60–80 GB heap** — too much for the
47 GB Contabo box.

**Use `OSM_SCOPE=rail-focused` instead.** It drops *all* driving
infrastructure (motorway, primary, residential, service, cycleway) and
keeps only:
- All `railway=*` (tracks, stations, halts, tram stops, signals)
- All `public_transport=*` (platforms, stop areas, station polygons)
- Walking-only highway types: `footway / path / steps / pedestrian /
  corridor / elevator`
- `amenity=parking_entrance` (station forecourts — used by OTP to snap
  city-centre coords onto a station entrance)

Result: a 10-country merged PBF (~17 GB raw → ~3-4 GB filtered) builds
comfortably at **24-28 GB heap** — the same heap your French build uses
today.

**Trade-off**: OTP can't compute walking from arbitrary addresses
(driveable roads are gone). The journey UI's free-text address search
loses precision; the city/station dropdown and the network-coverage
matrix work normally because both submit station coordinates.

**Sourcing the merged PBF (10-country EU example)**:

```bash
# On the VPS, in /opt/viator/inbox-staging/ (~22 GB free disk needed)
for c in france great-britain belgium netherlands luxembourg \
         germany austria italy switzerland spain ; do
  wget "https://download.geofabrik.de/europe/${c}-latest.osm.pbf"
done

# Merge into one (osmium-tool handles overlaps cleanly)
osmium merge \
  france-latest.osm.pbf great-britain-latest.osm.pbf \
  belgium-latest.osm.pbf netherlands-latest.osm.pbf \
  luxembourg-latest.osm.pbf germany-latest.osm.pbf \
  austria-latest.osm.pbf italy-latest.osm.pbf \
  switzerland-latest.osm.pbf spain-latest.osm.pbf \
  -o eu-rail-10c.osm.pbf
```

Then upload `eu-rail-10c.osm.pbf` to the new EU session via the
session-creation UI with **OSM scope = Rail-focused (multi-country)**.
The entrypoint runs the rail-focused tags-filter automatically before
OTP sees the file.

### 6.3 Per-session OTP container 503s on first request

Cause: graph not loaded yet. Check the logs:

```bash
docker compose -p viator logs --tail 50 otp-<sid>
```

Wait for `Grizzly server running.` (the OTP "ready to serve" signal).
This typically takes 30s–2min for regional graphs, longer for big ones.

### 6.4 Worker can't spawn `otp-build` ("permission denied … docker.sock")

```bash
# Quick fix:
sudo chmod 666 /var/run/docker.sock

# Persistent fix — add the worker container's UID to the docker group.
# This requires bumping the UID in docker/web/Dockerfile (currently runs
# as root for simplicity) and is on the v0.2.0 hardening list.
```

### 6.5 CI lint/type/test gates failing on push

The most common pitfalls (all surfaced in v0.1.9):

| Gate | Common cause | Fix |
|---|---|---|
| ruff | New file added without running format | `ruff check --fix . && ruff format .` |
| mypy | Bare `dict` type annotation | Use `dict[str, Any]` (import `Any` from typing) |
| mypy | `# noqa` directive that's no longer needed | Just delete it; ruff RUF100 detects unused noqa |
| bandit | `subprocess.run(["docker", ...])` | Use absolute path constant `_DOCKER = "/usr/local/bin/docker"` |
| pytest | `tmp_path / "x"; x.mkdir()` raising FileExistsError on GHA Linux | `x.mkdir(exist_ok=True)` |

To iterate faster on CI failures, install the same versions locally:

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
mypy app/
pytest tests/unit/                  # integration tests need Postgres
```

### 6.6 The full troubleshooting matrix

`docker/INSTALL.md` §12 has 13 incident types with diagnosis + fix.
`VIATOR-technical-spec.md` §11.10 has the wider operational runbook.

### 6.7 Journey search returns "0 trips in Xms (timeout)"

This is **not** the same as "no service found". The search hit one of
the timeout layers in the request stack — there are four, and which one
fired determines the fix.

**Step 1 — read X carefully.** The number tells you which timeout fired:

| X is roughly | Fired | Fix |
|---|---|---|
| `~10 000 ms` | `FANOUT_TIMEOUT_MS` (web app's httpx → OTP) | Bump `FANOUT_TIMEOUT_MS` in `/admin/config` or platform_config (see §6.10) |
| matches `otp_api_timeout` (e.g. 30 000, 60 000) | OTP's `server.apiProcessingTimeout` (per-session) | Bump `otp_api_timeout` in the session's Configure form, then Rebuild + Promote |
| `< 5 000 ms` | Connection error / OTP container down | `docker compose ps`, `docker logs viator-otp-<sid>-1` |
| `> 600 000 ms` | nginx `proxy_read_timeout` (very rare) | Edit `docker/nginx/nginx.conf`, `docker compose restart nginx` |

**The most common case in v0.1.24-v0.1.25 deployments** is the first
one: operator bumped `otp_api_timeout` to 30s/60s via the new UI
dropdown but didn't realise `FANOUT_TIMEOUT_MS` (the web app's
HTTP-client cap) defaults to 10 000 ms. The web app gives up before
OTP returns, surfacing a 10 041 ms timeout regardless of how generous
the OTP-side budget is. **Both must be aligned**: `FANOUT_TIMEOUT_MS ≥
otp_api_timeout + 2 000 ms` (the +2 000 covers connection + transit).

**Step 2 — diagnose by direct OTP query** to bypass the fanout layer:

```bash
# Substitute your actual stop ids from /admin/master/stations
curl -s "https://vmi3259514.contaboserver.net/otp/nap-fr-rail/otp/gtfs/v1" \
  -H "Content-Type: application/json" \
  -d '{"query":"{plan(from:{lat:48.8443,lon:2.3739} to:{lat:43.3026,lon:5.3801} date:\"2026-05-19\" time:\"07:39\" numItineraries:5){itineraries{startTime endTime legs{mode startTime endTime}}}}"}' \
  | head -50
```

If OTP returns itineraries directly: the layer above (web app) is the
problem → bump `FANOUT_TIMEOUT_MS`. If OTP itself returns no
itineraries or its own timeout: the per-session `otp_api_timeout`
needs bumping (and a rebuild to apply).

**Step 3 — track v0.1.26+** which is queued to make the two timeouts
auto-coordinate (the fanout will inherit `max(default, session_otp_timeout
+ 2s)` so this footgun goes away).

### 6.8 router-config.json edit doesn't take effect after `docker restart`

Three things confuse this diagnosis. All three trip up the same way:

**1. `docker logs --tail N | head` shows the OLDEST logs, not the newest.**
Per-session OTP containers were created at the last Promote (often days
or weeks ago) and their initial startup logs are at the *top* of the
log stream. To see logs from a fresh `docker restart`, use:

```bash
docker logs --since=5m viator-otp-<sid>-1 2>&1 | head -50
# or:
docker logs --tail=100 viator-otp-<sid>-1 2>&1
```

If you see "OTP STARTING UP" with a timestamp from minutes ago (not
hours/days), the restart took effect.

**2. JVM startup is ~30 s for a 2.3 GB graph.** Searches issued during
the reload return whatever-was-cached or 503. Wait for `Grizzly server
running.` in the logs before retrying.

**3. The fast-path recipe for in-place router-config edits without
a 30-minute Rebuild + Promote cycle**:

```bash
# Identify the file (host-side path under the docker volume):
ROUTER_CFG="/var/lib/docker/volumes/viator_graphs/_data/<sid>/current/router-config.json"
sudo cat "$ROUTER_CFG"

# Edit in place (e.g. bump the per-session timeout from 30s to 60s):
sudo sed -i 's/"apiProcessingTimeout": "30s"/"apiProcessingTimeout": "60s"/' "$ROUTER_CFG"

# Restart the serving container so OTP re-reads the file at JVM startup:
docker restart viator-otp-<sid>-1

# Watch the reload finish (~30s):
docker logs --since=1m -f viator-otp-<sid>-1 2>&1 | grep -E "STARTING UP|Grizzly server|router-config"
```

**Important caveat**: this in-place edit **does not survive the next
Promote**, because Promote regenerates `router-config.json` from the
session's saved `config.otp_api_timeout`. To make the change permanent,
also save the value in the UI's Configure form, OR set it via SQL on
the `sessions` table's JSONB config column.

### 6.9 CI Trivy gate fails on a release that should pass

Lesson learned in v0.1.24 → v0.1.25.

**Symptom**: Trivy diagnostic step shows `0 vulnerabilities` in its
table, but the gating step exits 1 anyway with no findings printed.
SARIF artifact uploads but contains MEDIUM/LOW findings only.

**Root cause** (specific to `aquasecurity/trivy-action@v0.36.0`): when
`format: sarif` and `severity: CRITICAL,HIGH` are both set on the same
step, the action filters HIGH/CRITICAL for the SARIF *output* but
applies `exit-code: 1` to the **unfiltered** finding count. So a fresh
batch of MEDIUM curl/libcurl/sed CVEs blocks the gate even though our
gate is supposed to be CRITICAL,HIGH only.

**Diagnose**:

```bash
# 1. Identify which CVEs the gating SARIF actually contains:
gh run download <RUN_ID> -n trivy-otp-sarif -D ./sarif-tmp
python -c "
import json
data = json.loads(open('sarif-tmp/trivy-otp.sarif').read())
for run in data.get('runs', []):
    for r in run.get('results', []):
        msg = r.get('message', {}).get('text', '')[:200]
        print(f'  {r.get(\"level\"):8s} {r.get(\"ruleId\")}: {msg[:80]}')
"
```

If all findings are `note` or `warning` (= LOW/MEDIUM), the gate fired
on noise — see fix below. If anything is `error` (= HIGH/CRITICAL),
that's a real CVE to address.

**Fix** (in v0.1.25 onwards): the `.github/workflows/docker.yml` gating
step uses `format: table` (which honours severity for exit-code) and a
separate non-fatal step generates the SARIF for the GitHub Security
tab. If a future workflow change reintroduces `format: sarif` on the
gating step, expect this footgun to come back.

### 6.10 Timeout stack — full reference

Every timeout in the request and build paths, where it lives, and what
it caps. Use this table when "0 trips in Xms (timeout)" or build-side
hangs need diagnosing.

| Timeout | Path / surface | Default | Tunable via | Caps |
|---|---|---|---|---|
| `proxy_read_timeout` | `docker/nginx/nginx.conf` | `600s` | edit + `docker compose restart nginx` | how long nginx waits for the web/otp upstream to respond |
| `FANOUT_TIMEOUT_MS` | `platform_config` table | `10 000` ms | `/admin/config` UI (Fanout section) or SQL | the web app's httpx client when calling OTP from the journey-search endpoint |
| `otp_api_timeout` → `server.apiProcessingTimeout` | session config (v0.1.24+) → `router-config.json` | `30s` | per-session Configure form | OTP's per-request compute budget |
| `REBUILD_DEBOUNCE_SECONDS` | `platform_config` table | `1800s` | `/admin/config` or SQL | how long the worker waits between job-enqueue and pick-up |
| `OTP_BUILD_TIMEOUT_MINUTES` | (queued, not yet implemented) | — | — | future: hard cap on a single build's wallclock |
| JVM `-Xmx` (heap) | `OTP_HEAP` env passed to otp-build | session config `otp_build_heap` (v0.1.23+) | per-session Configure form | not a timeout, but related — OOM looks like a hang in logs |

**The two coordination rules every operator should know**:

1. **`FANOUT_TIMEOUT_MS ≥ otp_api_timeout + 2 000 ms`.** Otherwise the
   web app gives up before OTP returns and the per-session
   `otp_api_timeout` knob has no operator-visible effect. v0.1.26+ is
   queued to auto-coordinate these.

2. **Changing `otp_api_timeout` only affects future searches AFTER
   Rebuild + Promote.** The running otp-`<sid>` container holds
   `router-config.json` in JVM memory from when it was last loaded.
   See §6.8 for a fast-path recipe that skips the 30-minute rebuild.

### 6.11 Post-deploy verification checklist

Run through this after every `docker compose pull && up -d`. Catches
~80% of "deployed but didn't actually take effect" classes of bug
documented in §6.7-§6.10.

**1. Server-side: image actually picked up**

```bash
# /healthz/version returns the new tag (this hits FastAPI, not nginx cache)
curl -fsS https://vmi3259514.contaboserver.net/healthz/version
# → {"version":"v0.1.X"}

# OCI label on the running container matches
docker inspect viator-web-1 \
  --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
# → 0.1.X

# Worker is on the same version (it's the same image, different command)
docker inspect viator-worker-1 \
  --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

If any of these show the old version, the `up -d` didn't recreate the
container — usually because compose decided "no changes needed" when
the image tag didn't actually shift. Force-recreate:
`docker compose up -d --force-recreate web worker`.

**2. Browser-side: hard-refresh required**

```
Ctrl+Shift+R   (or Cmd+Shift+R on Mac)
```

Without this, the cached JS bundle doesn't reload and new UI fields
(post-v0.1.21 `otp_timezone` dropdown, post-v0.1.23 `otp_build_heap`,
post-v0.1.24 `otp_api_timeout`, post-v0.1.20 rebuild panel) silently
don't appear. The version badge in the page header is the cheapest
sanity check — if it shows the new version, JS reloaded.

**3. Per-session config: explicitly save the new defaults**

Sessions don't auto-pick up new defaults — `worker.py` falls back to
the module-level default only when `session.config.<field>` is None.
After a release that adds a new config field:

- Expand the session in `/admin/sessions`
- Verify the new dropdown shows the expected default (e.g. `30s` for
  `otp_api_timeout` post-v0.1.24)
- **Click "Save config"** to persist the value into JSONB

Without saving, two operators looking at the UI see the same dropdown
value but one is "unset → falls back to default" and the other is
"explicitly saved" — and a future release that changes the default
will only affect the unsaved one. **Always Save explicitly.**

**4. Running OTP serving containers don't auto-pick-up router-config
changes**

The orphan `otp-<sid>-1` containers from previous Promotes keep running
with whatever `router-config.json` they loaded at JVM startup. New
defaults / per-session knobs in v0.1.21+ only take effect after either:

- **Full path**: Rebuild graph + Promote (regenerates router-config,
  recreates the OTP container) — ~30 min for a France-wide multi-NAP
  session.
- **Fast path**: edit `router-config.json` in place + `docker restart
  viator-otp-<sid>-1` — see §6.8. Doesn't survive next Promote.

**5. Smoke-test a journey search**

The integration test for "everything's wired up". For a France-anchored
session:

- Paris GdL → Lyon Part-Dieu (short-range, hits SNCF TGV directly)
- Paris GdL → Marseille St-Charles (long-range, exercises
  `apiProcessingTimeout` budget)
- For multi-tz sessions: Paris GdN → London St Pancras (Eurostar,
  exercises `transitModelTimeZone`)

If any of these return `0 trips in Xms (timeout)`, jump to §6.7.
If they return `No itineraries found.` without a timeout, suspect data
quality (see also §8 v0.1.20's "service window" caveat).

**6. Browser DevTools console clean**

`F12` → Console tab. **No red errors.** Specific things that have
broken at various releases and are worth verifying still work:

- `escHTML is not defined` (v0.1.16 fix) — NAP catalogue dropdown
- `Uncaught (in promise) ReferenceError` of any flavour
- Any 4xx/5xx in the Network tab during page load (other than the
  expected 404 on `/static/branding/sentry-logo.svg` which we don't
  ship)

A clean console + filled provider status pills (v0.1.19) + populated
"Current build" card (v0.1.20) means the v0.1.X+ feature stack is
working end-to-end.

---

## 7. Rollback

If a deploy breaks production, roll back in under a minute:

```bash
ssh otpadmin@vmi3259514.contaboserver.net
cd /opt/viator/docker

# 1. Pin to the previous known-good version.
sudo sed -i 's/^VIATOR_VERSION=.*/VIATOR_VERSION=v0.1.8/' .env

# 2. Pull (probably already in local cache, instant).
sudo docker compose pull web worker

# 3. Recreate.
sudo docker compose up -d --force-recreate web worker

# 4. Verify.
curl -s http://localhost/healthz/version    # → {"version": "v0.1.8"}
```

**About database migrations on rollback**: Alembic migrations are forward-
only by default. If `v0.1.9 → v0.1.10` added a column, rolling back to
`v0.1.9` leaves the column orphaned but harmless (the v0.1.9 code just
won't read it). If a release adds a NOT-NULL constraint or drops a
column, you must restore from a Postgres backup taken **before** the
upgrade. Always take a backup before applying a release that touches
the schema (`grep alembic /path/to/release-notes` or check
`alembic/versions/` for new migration files).

---

## 8. Recent versions — what shipped, what's still queued

**v0.1.29 (latest)**: Network coverage — full-day search + trip-count view.

Two operator questions about v0.1.27/28's coverage matrix:

**Q1 — can we test the full day instead of just one departure time?**
**Yes.** Coverage runs now ask OTP for `numItineraries=50` over a
`searchWindow=86400` (24h) per pair, instead of the live-UI's `8 / 4h`.
RAPTOR returns the full day's worth of trains in a single call; we
get every alternative OTP can find for that A→B on that day.

Trade-off: per-call latency goes from ~0.5-1s up to ~2-5s (OTP runs
its loop until the window or numItineraries is exhausted). At
concurrency=5 the full 650-pair run grows from ~10-15min to
**~25-35min**. Acceptable — coverage is a deliberate weekly /
release-time tool, not interactive.

The two new constants live in `app/network_coverage/runner.py`:
`_COVERAGE_NUM_ITINERARIES` and `_COVERAGE_SEARCH_WINDOW_SECONDS`.
Bump them per-deployment if your graphs are dense enough that 50
trips truncate (Île-de-France RER pairs might).

**Q2 — can we see trip count per day per cell, not just the shortest?**
**Yes** — once Q1 is in, the count comes for free. Each result row
already has `num_itineraries` from v0.1.27. The matrix now offers a
**view toggle** (top-right of the result panel):

  * **Min duration** — original v0.1.27 behaviour (3h12 per cell)
  * **Trips/day** — count per cell, with a green heatmap that darkens
    as the trip count grows (1-2 = light, 30+ = dark green)
  * **Both** (default for new operators) — `3h12·25` per cell, the
    best of both worlds

The toggle persists in `localStorage` so it survives reloads. Tooltips
always show both numbers + transfers + operators + response_ms,
regardless of view.

**Q3 — does the data support cross-session comparison later?**
**Yes — already supported** via the existing schema chain that v0.1.27
wired in:

  * `network_coverage_results.journey_search_id` FK →
  * `journey_searches.id` (one row per pair search) →
  * `journey_search_executions.raw_response` (full OTP JSON, JSONB) +
  * `journey_trips.legs` (per-itinerary v0.1.26 operator info)

So when you have a second session populated by MERITS data, you can:
1. Promote the new session
2. Run the coverage matrix against it (same 26 hubs)
3. Eyeball both matrices side-by-side via the sidebar (manual diff)

A proper **side-by-side auto-diff view** — pick run A vs run B, render
a delta matrix coloured by agreement (green = both ok / blue = B
better / orange = A better / red = disagree fundamentally) with a
click-cell for full per-itinerary diff — is queued for v0.1.30 once a
second session exists to compare against. Building it before the test
data arrives risks fitting the UI to the wrong comparison story.

**v0.1.28**: Network coverage hub-list expansion (23 → 26).

Operator review of the v0.1.27 hub list flagged two missing Paris
terminals — **Gare d'Austerlitz** (gateway to south-central France via
the historic POLT line, plus Orléans / Bordeaux historic-route services)
and **Saint-Lazare** (Normandie services + dense Île-de-France suburbs).
Both added.

Also added **Batz-sur-Mer** as a small-TER-halt coverage stress test —
it's a local stop on the Le Croisic branch in the Guérande peninsula,
the kind of regional terminal where TGV-anchored sessions often fall
back to "Paris transfer + 2h TER" itineraries that exercise different
parts of the routing graph than direct big-hub pairs do.

New totals:
- **26 hubs** (up from 23): 6 Paris terminals + 19 regional capitals + 1
  small TER halt
- **650 directional pairs** (up from 506) — `n × (n-1)` matrix size
- **325 unordered pairs** (single-direction mode) — `n × (n-1) / 2`
- Wallclock estimate: **~13-18 min** for both-directions on a France-wide
  multi-NAP session at parallelism=5 (up from ~10-15 min for 23 hubs)

The `hub_set` field on each run is now stored as `"fr-major-26"` so
v0.1.27 runs (`"fr-major-23"`) and v0.1.28+ runs are distinguishable in
the database — important for cross-version comparison sanity. Older
runs will still render in the matrix (the UI overlays cells onto
whatever hub list the run was generated against), so no historical
data is lost.

**v0.1.27**: Network coverage matrix admin page.

A new feature for systematically testing how well a session covers the
French rail network. Runs all-pairs A→B journey searches across 23
curated major hubs (4 Paris terminals + 19 regional capitals chosen
from SNCF's "Le Réseau Ferré en France" March-2026 map) and renders a
colour-coded coverage matrix.

**Curated 23-hub list** (in `app/network_coverage/hubs.py`):
- **Paris terminals (4)**: Gare de Lyon, Nord, Est, Montparnasse
- **North/NE (4)**: Lille Flandres, Reims, Strasbourg, Nancy
- **Center-East (3)**: Dijon, Lyon Part-Dieu, Clermont-Ferrand
- **Mediterranean / SE (6)**: Avignon TGV, Aix-en-Provence TGV,
  Marseille Saint-Charles, Nice Ville, Montpellier Saint-Roch, Narbonne
- **South-West (2)**: Toulouse Matabiau, Bordeaux Saint-Jean
- **West / Atlantic (4)**: Le Mans, Nantes, Rennes, Brest

23 × 22 = **506 directional pairs** per run (or 253 if "single-direction"
mode is picked — half the work, but loses asymmetric-data detection).

**Why directional matters**: SNCF declares Paris→Marseille TGV but not
the return on some Sundays; Eurostar timetable order differs A→B vs B→A
on some service days; GTFS-RT delays can affect one direction's
connectivity but not the other. Running both directions surfaces these
asymmetries which would otherwise be silent bugs.

**Mechanism**:
- New admin page at **`/admin/network-coverage`** (platform_admin only,
  shows up in the top nav as "Coverage")
- Operator picks: session, departure datetime (defaults to next Monday
  08:00 — densest weekday TGV service), direction mode
- POST `/api/admin/network-coverage/runs` creates the run, kicks off
  `runner.execute_run` as a FastAPI BackgroundTask
- Bounded parallelism — 5 pairs run simultaneously to keep wallclock
  ~10-15 min for a full 506-pair run while staying gentle on OTP
- UI polls `/api/admin/network-coverage/runs/<id>` every 5 s for live
  progress; matrix renders incrementally as cells fill in

**Schema** (alembic 20260505_1500_network_coverage):
- `network_coverage_runs` — one row per Run click (session, depart_at,
  state, counters, summary JSON)
- `network_coverage_results` — one row per (run, origin, dest)
  (status, response_ms, num_itineraries, best_duration, best_transfers,
  best_operators, FK to journey_searches for click-cell drilldown)

The journey_search FK reuses the existing infrastructure — every
coverage pair also lands a row in `journey_searches` /
`journey_search_executions` / `journey_trips`, so the v0.1.26 trip-card
UI works as the click-cell drilldown for free.

**Multi-session comparison** (the original use case): since every run
is keyed to a session, running the same matrix against multiple
sessions creates a comparison record. Sidebar shows past runs newest-
first across ALL sessions; clicking any past run renders its matrix.
v0.1.28+ will add side-by-side diff view ("which session finds more
Marseille→Bordeaux trips") and time-of-day sweeps.

**Visual conventions on the matrix**:
- Rows = origin, columns = destination (read down then across)
- Cell content = shortest itinerary's duration ("3h12") or status icon
- Green = itinerary returned · amber ∅ = OTP found no route · red ⏱ =
  timeout · red ✗ = error · grey diagonal = same-station no-op
- Region-coloured row/column headers (Paris blue, NE amber, SE pink,
  SW sky, Atlantic green) for visual grouping
- Hover tooltip: "Origin → Destination · 3h12, 0 transfers · SNCF · 412ms"
- Click any populated cell → modal with operator badges + JSON
  inspector + link to re-run live in the journey UI

**API endpoints** (under `/api/admin/network-coverage/`, platform_admin):
- `GET /hubs` — return the 23-hub preset
- `GET /runs?limit=N` — list past runs (for the sidebar)
- `POST /runs` — start a new run (returns run id immediately, work
  proceeds in background)
- `GET /runs/{id}` — fetch run + all results (poll target for the UI)

**Limitations / queued for v0.1.28+**:
- Operator-editable hub list (add Eurostar London / Brussels / Frankfurt
  for cross-border tests)
- Side-by-side diff between two runs (the killer comparison feature)
- Time-of-day sweep (run the same hub set at 06:00 / 08:00 / 14:00 /
  18:00 / 22:00 to catch off-peak service gaps)
- CSV export
- Cancel-running-run button (today the operator has to wait or
  restart the worker)

**v0.1.26**: operator visibility + JSON inspector on journey results.

Operator feedback after v0.1.25's timeout fix unblocked Paris→Marseille
searches: "I can see the route number `631B` and the long name
`Paris-Marseille-Toulon TGV` but I can't tell at a glance whether each
trip is SNCF, Trenitalia, Eurostar etc. — and I can't tell whether
Trenitalia is even appearing in the result set."

Three changes:

1. **Operator/agency visible on every leg** — coloured pill next to
   the route number. Derived from OTP's `route.agency.name` (when the
   feed populates `agency.txt`) with a fallback to the feed_id prefix
   extracted from `trip.gtfsId`. Walk legs skip the badge (no
   operator). Pill colour-coded per feed: SNCF red, Trenitalia red,
   Eurostar amber, RENFE red, IDFM blue, regional rails sky blue,
   ZenBus green — matches operator brand conventions where reasonable.

2. **Per-result-set operator summary** — at the top of the journey
   results, a chip line "Operators in results: SNCF ×8 · TRENITALIA ×1
   · IDFM ×3" (counts = legs, not trips). Answers "is Trenitalia in
   my results?" without expanding every card. Hidden when no transit
   legs are returned.

3. **JSON inspector** — `{}` button top-right of every trip card →
   modal with formatted JSON of the raw OTP itinerary slice. Useful
   for "where did this leg get its operator from", "why is the
   tripHeadsign weird", general transparency. ESC or click-outside
   to close. The full multi-itinerary OTP response is also still
   stored at the execution level in `journey_search_executions.
   raw_response` for SQL-side audit (controlled by
   `STORE_RAW_RESPONSE`).

**Backend changes**: `app/journey/otp_client.py` GraphQL query now
fetches `route { agency { gtfsId name url } gtfsId }` and
`trip { gtfsId tripHeadsign }` in addition to existing fields.
`_normalise` extracts these into per-leg keys: `agency_name`,
`agency_id`, `agency_url`, `feed_id` (derived from `trip.gtfsId`
prefix), `trip_id`, `trip_headsign`, `route_id`. Each trip also
carries an `_raw_itinerary` slice for the JSON inspector — underscore-
prefixed so the recorder skips it when persisting to
`journey_trips.legs`.

**Schema impact**: the new keys are added to `journey_trips.legs`
JSONB on every successful search going forward. Existing rows in
the table predate v0.1.26 and don't have these keys; the UI's leg
renderer treats them all as optional so historical replay works.

**Diagnostic value for the "Trenitalia missing" question**: with
v0.1.26 in place, the result-set operator summary at the top
immediately answers "is Trenitalia in my results?". If it's missing
from the badge line, click `{}` on the closest-time trip card and
look at the legs' `feed_id` / `agency_name` to confirm. If still
missing, root cause is one of:
  * Trenitalia GTFS service-calendar window doesn't cover the search
    date (check the build log's `ServiceCalendar(s) removed` line for
    TRENITALIA — see §6.7).
  * No Paris→Marseille service in their feed (their
    Paris-Lyon-Marseille extension may not be in the version on NAP).
  * The feed's `route_type` is something OTP doesn't classify as RAIL
    and the search modes filter cuts it out.

**v0.1.25**: CI Trivy gate fix — unblocks v0.1.24 OTP image release.

v0.1.24's CI ran into a `trivy-action@v0.36.0` bug: when both
`format: sarif` and `severity: CRITICAL,HIGH` are set on the same step,
the action correctly filters HIGH/CRITICAL for the SARIF output but
applies the `exit-code: 1` to the **unfiltered** finding count. So a
fresh batch of MEDIUM curl/libcurl/sed patches published on 2026-05-05
caused the OTP build to fail the gate even though the diagnostic
table (same image, same config, table format) reported 0 HIGH/CRITICAL
findings. v0.1.24's web image successfully shipped to GHCR; the OTP
image was blocked.

v0.1.25 simplifies `.github/workflows/docker.yml` so the gating step
uses **table format** (which honors severity for exit-code) and the
SARIF generation moves to a separate non-fatal step that purely
feeds the GitHub Security tab + downloadable artifact. The table
output doubles as the human-readable diagnostic — when the gate
fails, the blocking CVEs are visible right above the error in the
job log.

No app code changes; pure CI/build-system fix. Carries the full
v0.1.24 payload (per-session `otp_api_timeout`, OTP 2.9
`accessEgress.maxDurationForMode` schema fix) plus this workflow
change. Deploy v0.1.25 instead of v0.1.24 — same operator-visible
behaviour, plus a working OTP image on GHCR.

**v0.1.24**: per-session OTP API timeout + OTP 2.9 schema fix.

Two related fixes touching `router-config.json`:

1. **Per-session `otp_api_timeout`** — same UI pattern as v0.1.21+
   knobs (osm_scope / otp_timezone / otp_build_heap). Default bumped
   from the pre-v0.1.24 hardcoded `10s` to `30s`. Operator can dial up
   to `60s` / `120s` via the Configure form when bigger graphs need it.

   Why: Paris GdL → Marseille on a 13-provider France-wide graph
   (43,673 stops, 1.18M walk transfers) was returning `0 trips in
   10036ms (timeout)` even though direct TGV exists. OTP wasn't failing
   to find a route — it was running out of its 10s budget while
   exploring TGV+TER+Trenitalia+Eurostar alternatives. 30s comfortably
   accommodates that exploration on the largest sessions; 10s is
   retained as a dropdown option for small single-feed sessions where
   tighter feedback is preferred.

   Caveat documented in OTP 2.9 docs and worth knowing: when the
   `ParallelRouting` feature flag is on, OTP **bypasses
   apiProcessingTimeout entirely**. We don't enable that flag; if a
   future operator does, the knob becomes a no-op.

2. **`maxAccessEgressDurationForMode` rename** — fixes the OTP 2.9
   warning on every build:

   ```
   WARN (NodeAdapter.java:169) Unexpected config parameter:
   'routingDefaults.maxAccessEgressDurationForMode:{"WALK":"20m"}'
   ```

   The field moved in OTP 2.9 (verified against
   <https://docs.opentripplanner.org/en/v2.9.0/RouteRequest/#rd_accessEgress_maxDurationForMode>):

   - **Old**: `routingDefaults.maxAccessEgressDurationForMode` (flat,
     uppercase mode keys like `WALK`).
   - **New**: `routingDefaults.accessEgress.maxDurationForMode` (nested,
     lowercase mode keys like `walk`).

   This wasn't just a cosmetic warning — OTP was silently falling back
   to its **default** access/egress duration (45 min) instead of the
   20 min we'd set. So the v0.1.7 commit message about
   "Cagnes-sur-Mer-style misrouting" was technically not protecting
   anything until v0.1.24. The bundled fallback
   `docker/otp/router-config.json` is updated to match.

**Migration**: existing sessions without `otp_api_timeout` set inherit
the new 30s default — no operator action required, but the value will
appear blank in the UI dropdown until the operator clicks Save (which
records the explicit choice in `session.config.otp_api_timeout`).
Existing graphs keep using whatever timeout was in their already-loaded
router-config; click **Rebuild graph** to regenerate with the new value.

**Footgun discovered post-release** (operator hit it 2026-05-05): the
v0.1.24 `otp_api_timeout` knob has no operator-visible effect unless
the web-app-side `FANOUT_TIMEOUT_MS` (default `10 000` ms in
`platform_config`) is also bumped to ≥ `otp_api_timeout + 2 000 ms`.
The web app's httpx client gives up before OTP returns, surfacing as
`0 trips in 10 041 ms (timeout)` regardless of the OTP-side budget.
Until v0.1.26 ships the auto-coordination fix, **operators bumping
`otp_api_timeout` to 60s must also bump `FANOUT_TIMEOUT_MS` to 60000
via `/admin/config`**. Full diagnostic flowchart in §6.7; reference
table of every timeout in the system in §6.10.

**v0.1.23**: complete rebuild inputs inventory + per-session heap + live elapsed ticker.

Three improvements driven by feedback from the v0.1.20 rebuild panel
landing in operator hands:

1. **The Inputs list now shows every file OTP actually built from**, not
   just the `Upload` table subset. Pre-v0.1.23, refresh-from-URL didn't
   write `Upload` rows, so a NAP-imported session like `nap-fr-rail`
   would record a wildly incomplete `source_uploads` list (operator saw
   "2 files" when the build actually consumed 13). v0.1.23 walks the
   inbox subdirs (`gtfs/`, `netex/`, `osm/`) at snapshot-write time and
   computes SHA-256 of each file directly from disk. Cross-references
   the `Upload` table by sha256 — manually-uploaded files keep their
   `upload_id` and get a green `⤴ uploaded` pill; refresh-fetched files
   get a `⤓ refreshed` pill. Each entry now also surfaces `size_bytes`
   and `stored_path`.

2. **Per-session `otp_build_heap` field** with a UI dropdown next to
   OSM scope and Timezone. Same pattern as v0.1.21's `otp_timezone`:
   top-level config, validated at save via `app/otp_heap.validate_heap`,
   read by the worker each tick (no worker restart needed). Lifts the
   "SSH and edit `.env` and restart worker" workaround that operators
   hit on `nap-fr-rail` after the OOM-during-Phase-2.
   Curated dropdown values: 12g / 16g / 20g / 24g / 28g / 32g / 36g
   with labels indicating typical session shapes ("light: single
   provider, regional OSM" → "heavy: 10+ providers, cross-border").
   Default `12g` keeps legacy sessions building unchanged.
   **Rule of thumb baked into the hint**: ~2 GB per provider on a
   France-wide PBF + 6 GB for the street graph + ~10-15% JVM overhead;
   stay under VPS RAM minus 8 GB headroom.

3. **Live elapsed-time ticker** on the running rebuild card. Pre-v0.1.23
   the card showed "Started 18:46 · Finished: —" with no indication of
   elapsed time — operators couldn't tell a healthy 50-min build from a
   stuck one. The new ticker updates every second client-side
   (independent of the 5s job-list poll) so the duration cell counts
   up smoothly: "running… · 56m 9s". Stops automatically when no
   running jobs are visible to avoid burning CPU on idle pages.

**Side observation surfaced during v0.1.23 testing**: the v0.1.20
panel was reporting `2026-W19_2026-W19 #1` for the `nap-fr-rail`
build — i.e. the GTFS calendars only declared service for ISO week 19
(2026-05-04 → 2026-05-10). That's because the `source_uploads` list
was incomplete: `derive_main_version_from_gtfs` was reading from the
single ZOU GTFS that happened to be in the Upload table, not from
SNCF's nation-wide calendar. v0.1.23 fixes this implicitly by
including every GTFS in the snapshot — the next rebuild will derive
the version from whatever GTFS is alphabetically first in the inbox
(typically `sncf.zip` for FR sessions), so expect a much wider
period like `2026-W14_2026-W39`. Worth a fresh Rebuild to repopulate.

**Migration note**: existing `graph_snapshots` rows from v0.1.20-v0.1.22
keep working; they just don't have `size_bytes` or `source` on their
inputs. The renderer treats those fields as optional. To upgrade a
session's snapshot data, click Rebuild graph once — the v0.1.23 worker
records a fresh row with the complete inputs list.

**v0.1.21**: per-session OTP timezone — fixes Eurostar / multi-tz rebuilds.

OTP 2.9 refuses to build a graph that mixes agencies declaring different
IANA timezones — SNCF's GTFS says `Europe/Paris`, Eurostar's says
`Europe/Brussels`, Trenitalia France says `Europe/Rome`, etc. The build
aborts with:

> The graph contains agencies with different time zones:
> Europe/Paris != Europe/Brussels. Please configure the one to be used
> in the build-config.json

v0.1.21 plumbs the `transitModelTimeZone` field through:

- New session config field **`otp_timezone`** (default `Europe/Paris`).
  Validated at save-time via stdlib `zoneinfo`; bad values rejected with
  a 400 toast next to the dropdown.
- New **Timezone dropdown** in the Configure form, sitting just below
  OSM scope (both are build-time settings stored at the top level of
  `config`). 17 curated entries covering every European country whose
  rail data has appeared in a session — operators can also type any
  IANA name for non-European demos.
- Worker passes `OTP_TIMEZONE` as a docker `-e` to the otp-build
  container (alongside the existing `OTP_HEAP`, `OTP_OSM_SCOPE`).
- Entrypoint (`docker/otp/entrypoint.sh`) injects
  `"transitModelTimeZone": "<value>"` into the generated
  `build-config.json` whenever `OTP_TIMEZONE` is set.

**Migration**: existing sessions that don't have `otp_timezone` set
default to `Europe/Paris` — single-FR-rail sessions keep building
unchanged, no operator action required. Sessions adding non-French
agencies (Eurostar, Trenitalia FR, ICE) need to either keep
`Europe/Paris` (recommended — most VIATOR demos are anchored to FR
station search) or pick a different canonical tz for the graph via
the dropdown.

#### How OTP handles the chosen tz — verified against OTP 2.9 docs and Entur

Cross-checked our design against [OTP 2.9 BuildConfiguration §
transitModelTimeZone](https://docs.opentripplanner.org/en/v2.9.0/BuildConfiguration/#transitModelTimeZone)
and OTP issues [#2602 (How To Handle Time Zones?)](https://github.com/opentripplanner/OpenTripPlanner/issues/2602)
and [#2290 (multi-tz routing not supported)](https://github.com/opentripplanner/OpenTripPlanner/issues/2290).
Reference deployment: [Entur's national Norwegian OTP config](https://github.com/entur/otp2int-deployment-config/blob/main/helm/otp2int/templates/configmap-graph-builder.yaml)
which uses exactly the same single-canonical-tz pattern with
`transitModelTimeZone: Europe/Oslo` for cross-border NeTEx feeds. Our
design (single canonical tz, default `Europe/Paris`, dropdown of
European tz ids) is **the only OTP-supported approach** in 2.9 — no
multi-tz routing in a single graph exists today, and the OTP team
explicitly does not expect to ship one soon.

**What the chosen tz actually does** (verbatim from OTP docs): "stores
the timetables in the transit model, and **interprets times in incoming
requests**." In practice this has five operationally-visible
consequences operators need to understand:

1. **All API times — in and out — are in `otp_timezone`.** A search for
   "London → Paris on Eurostar leaving 08:01 local" with
   `otp_timezone=Europe/Paris` is interpreted by OTP as 08:01
   Europe/Paris (= 07:01 UTC). The St Pancras local-time 08:01
   departure renders as **09:01 Europe/Paris** in the GraphQL
   response. This is by design, not a bug — but it means the
   journey UI's clock is the canonical zone, not the stop's
   geographic zone. If you ever build a "local time at this stop"
   feature you'll need a stop→tz lookup at the UI layer.

2. **`agency.txt agency_timezone` becomes informational only** once
   `transitModelTimeZone` is set. Don't write any downstream consumer
   that reads it for time math — read `transitModelTimeZone` instead.

3. **Changing this field requires a full graph rebuild.** It's a
   build-time setting baked into the generated `streetGraph.obj` +
   `graph.obj`. The v0.1.7 streetGraph cache key
   (`sha256(osm.pbf):scope`) does NOT include the tz, so changing
   `otp_timezone` invalidates only the transit graph (Phase 2,
   ~5 min) — but NOT the streetGraph (Phase 1, ~25 min). So a tz
   swap is a fast cache-hit rebuild.

4. **GTFS-RT alignment**: real-time updaters interpret incoming
   timestamps against `transitModelTimeZone`. A producer publishing
   wall-clock-local timestamps in a different tz from the canonical
   one will be silently re-anchored — usually fine inside continental
   Europe (Paris/Brussels/Amsterdam/Berlin all share the same DST
   rules), risky if the producer publishes UK-local or non-EU. When
   onboarding a new GTFS-RT feed: confirm it publishes either UTC
   epoch OR wall-clock matching the graph tz.

5. **DST: safe inside Schengen rail today, risky if Eurostar London
   segments come into scope.** Europe/London has different DST
   transition dates from Europe/Paris during the spring/autumn
   shoulder. Eurostar trips on those weekends would display times
   with a 1-hour artificial offset for ~2 weeks per year. Today this
   only matters on London-side stops, and only twice yearly. Flag
   for any operator routing actual London journeys.

**Note about Eurostar's GTFS specifically**: their published feed
declares `Europe/Brussels` for ALL stops including London and Paris,
because the operating company's admin entity is Belgian. Setting
`transitModelTimeZone: Europe/Paris` is the right call for an
SNCF-anchored deployment — OTP normalises every stop-time to Paris
at build time; downstream times are consistent and correct so long
as the UI labels them clearly.

**Choosing the tz for a session** — quick rules:
- Single-country session → use that country's tz (FR → Europe/Paris,
  DE → Europe/Berlin, IT → Europe/Rome, etc.).
- Multi-country session anchored to one dominant operator → use the
  dominant operator's tz (e.g. SNCF + Eurostar + Trenitalia FR + ICE
  routing from French stations → `Europe/Paris`).
- Multi-country session with no dominant operator (a hypothetical
  pan-European search demo) → pick the country where most operators'
  passengers physically board, or `UTC` if you really want
  zone-neutral. Operators searching from Brussels would then see
  times labelled UTC; OK if your UI renders that clearly.

If you change `otp_timezone` after a session is already serving,
remember to click **Save config** AND **Rebuild graph** — the running
otp-`<sid>` container won't pick up the new value until promoted, and
the displayed times shift across the whole journey UI for that
session.

**v0.1.20**: rebuild graph panel — current build card + history.

The "Rebuild graph" panel only showed a flat table of jobs with status,
timestamps, and a 1500-char log tail. Operators couldn't tell what
*version* a build was, what *went into* it, whether it was the one
*currently serving*, or whether it was a fast cache-hit build vs. a
slow OSM-rebuild — even though `graph_snapshots` had been silently
recording most of that information schema-wise since spec §6.6.

Two changes:

1. **Worker now writes `graph_snapshots` rows on successful build**
   (`app/worker.py`). The `record_snapshot()` helper that has lived in
   `app/graph_snapshots.py` for months wasn't actually being called —
   v0.1.20 wires it up. Each successful build records:
   - `built_at`, `graph_path`, `feed_signature`
   - `timetable_main_version` / `timetable_update_version` (e.g.
     `2026-W14_2026-W39 #3`) derived from GTFS calendar.txt
   - `service_period_start` / `_end` (the calendar window the schedule
     covers)
   - `is_current` — auto-set to True; previous `is_current=true` row
     for the same session is demoted in the same transaction
     (partial unique index in Postgres enforces "at most one current
     per session")
   - `source_uploads` — list of files that went into the build
     (filename, sha256, kind)

2. **`GET /api/sessions/<sid>/rebuilds` joins the snapshot data** and
   adds `duration_seconds` + `cache_hit` (parsed from log markers
   emitted by the OTP entrypoint at lines 227-235). The admin UI's
   "Rebuild graph" panel now renders:

   - **Current build card** at the top — version label, status pill,
     "✓ serving now" badge if `is_current`, ⚡ cache-hit / ⏰ cache-miss
     pill, duration, full inputs list with feed_signature short-hash,
     and an expandable full log.
   - **History** below — every other rebuild as a collapsible
     `<details>` row (same accordion pattern as the v0.1.19 provider
     cards). Summary line is one row of pills + version + timestamp +
     duration; expand to see inputs and full log.

   Live polling: while any job is in `running` or `pending` state the
   panel re-fetches every 5 s. Polling stops automatically when nothing
   is running, or when the operator collapses the session row.

**Limitation worth knowing**: today's `Upload` table only holds
manually-uploaded files — refresh-from-URL doesn't write rows there.
So a session built purely from refreshed providers (e.g. `nap-fr-rail`
populated via NAP) will record an empty `source_uploads` list. The
build still gets the rest of the snapshot fields (version, period,
is_current, cache_hit). v0.1.21+ may extend the refresh path to
record Upload rows so the inputs list is complete.

**Backfill**: existing sessions that have successful builds from
before v0.1.20 won't have snapshot rows, so the "Current build" card
will fall back to the most recent successful job and label it
`(no snapshot — legacy build)`. **One fresh Rebuild click after the
deploy** populates the table going forward.

**v0.1.19**: per-provider status pills on each provider card.

After bulk-importing 12 providers from the NAP, the admin UI showed
**zero state** on each card — operators couldn't tell which feeds had
actually been pulled into the inbox vs. which were still pending the
next "Refresh providers" click, nor whether previous attempts had
succeeded or failed. v0.1.19 adds a status pill to every provider card's
summary row.

- **Four states**: `ok` (green ✓ + size + age), `stale` (amber ⏰, file
  older than 24 h), `pending` (grey ⏳ "Never fetched"), `error` (red ⚠
  "Last refresh failed").
- **No new tables**: state is derived live from
  `/data/inbox/<sid>/{gtfs,netex}/<feed_id_lower>.zip` (file exists +
  mtime + size) plus the most recent
  `session.sources.refreshed` / `session.provider.refreshed` audit row
  (used to disambiguate "never attempted" from "attempted and failed").
- **New endpoint**: `GET /api/sessions/<sid>/providers/status` returns
  `{<feed_id>: {state, fetched_at, size_bytes, error_hint}}`. The UI
  hits it on session-row expand and after every Refresh click.
- **Freshness window**: 24 h, hard-coded in v0.1.19. Operator-tunable
  later if there's a real ask — most use cases are "fetch daily, alert
  if older."

Pills update live after `Refresh providers` and per-provider
`Refresh this provider` clicks — no page reload needed. Cards added
mid-session via "+ Add provider" gain their pill on save.

**Limitation worth knowing**: when a refresh attempt fails the audit
log only stores task **keys**, not the error reason. The pill shows
"Last refresh failed — click Refresh to see why"; clicking the
per-provider Refresh button returns the actual reason in the toast.
A future v0.1.20+ may move this to a dedicated `provider_fetch_status`
table written by ingestion, with sparkline-grade history; v0.1.19 is
the deliberate "ship the obvious thing first" version.

**v0.1.18**: NAP-import dropdown — bug fix + label rename, bundled.

Two operator-visible problems with the **Import from NAP** modal landed
in one release:

1. **Dropdown was stuck on "Loading…" forever.** Latent bug since v0.1.8:
   `app/templates/admin/sessions.html` called an `escHTML()` helper in
   `napLoadCatalogues` / `napRenderResult` that was **never defined** —
   only its sibling `escAttr()` existed. The browser threw
   `ReferenceError: escHTML is not defined` mid-render and the dropdown
   never populated. v0.1.13's "show errors instead of spinning" fix was a
   different code path, so it didn't catch this one.
   - Fix: define `escHTML()` next to `escAttr()`. Standard HTML-text
     escape (`&<>"'` → entities). No behaviour change for any other code
     path — the function had simply never run before.
   - Trip-wire: new `tests/unit/test_sessions_template_js.py` parses the
     template and asserts every helper referenced in `JS_HELPERS` is
     also defined. Catches the same class of bug at CI time.

2. **Dropdown label said "Catalogue" — nobody knew what it meant.**
   Renamed to **"Available NAP APIs"**. The corner link "manage
   catalogues ↗" became "manage list ↗". Empty / error placeholders
   follow the same vocabulary ("No NAP APIs available yet").
   - **Internal names unchanged on purpose**: the `nap-catalogue` element
     id, the `/api/admin/nap-catalogues` route, the `NAP_CATALOGUES` JS
     module variable, the `/admin/nap-catalogues` admin page route, and
     the `nap_catalogues` DB table all keep their existing names.
     Renaming any of those would force every bookmark, audit-log search,
     and direct-link shortcut to update for zero operator-visible benefit
     beyond what the label change already gives.

Verification after deploy: hard-refresh the **Import from NAP** modal.
The first row should read **"Available NAP APIs"**, the dropdown should
populate with your registered NAPs (e.g. *France NAP
(transport.data.gouv.fr)*), and the DevTools console should be clean.

**v0.1.15**: dynamic nginx upstream — fixes the 502-after-deploy bug.

- nginx now uses Docker's embedded DNS (`127.0.0.11`) with a 10 s cache
  to re-resolve `web` and `otp-<sid>` hostnames at request time,
  instead of caching IPs at config-load time. Recreating the web
  container (every `docker compose up -d web`) used to give it a new
  internal IP that nginx kept missing → 502 Bad Gateway until you ran
  `docker compose restart nginx`. With v0.1.15 you no longer need that
  step — nginx picks up the new container within ~10 s on its own.
- Per-session OTP upstreams (`/otp/<sid>/`) get the same treatment via
  `app/sessions_orchestrator.py::render_nginx`. Restarting an OTP
  container during a session swap stops 502'ing too.

**Behaviour change for deploy procedure**: drop the `docker compose
restart nginx` step from any post-deploy runbooks. The new pattern is
self-healing.

**v0.1.14**: split provider refresh from OSM refresh — fixes the
recurring "I added a GTFS feed and now I'm waiting 30 min for OSM" pain.

- **"Refresh providers"** (renamed from "Refresh all sources") downloads
  every provider URL — timetables, MCT, stations CSVs — but **never** the
  OSM PBF. The streetGraph.obj cache stays valid → next rebuild finishes
  in ~5 min instead of ~30.
- **"⚠ Refresh OSM"** (new, amber-coloured button next to it) re-fetches
  just the PBF. Shows a confirm dialog spelling out the cost ("≈25 min
  added to next build"). You'll click this maybe quarterly when Geofabrik
  ships an OSM with a relevant fix; otherwise leave it alone.
- **Three-generation rotation**: before each OSM refresh, the previous
  PBF is preserved as `osm.pbf.old.1` (existing `.old.1` → `.old.2`,
  oldest dropped at `.old.3`). One-command rollback if a fresh PBF
  regresses: `mv osm.pbf.old.1 osm.pbf` on the VPS, then click Rebuild.
- **Audit trail**: provider and OSM refreshes write distinct audit rows
  (`session.sources.refreshed` vs `session.osm.refreshed`); the OSM one
  flags `invalidates_street_graph_cache: true` so monitoring can alert.

**Behaviour change for CLI callers**: `POST /sources/refresh` no longer
includes the OSM PBF (use the new `POST /sources/osm/refresh` instead).
The UI button rename is the operator-visible part; the API change is
documented in the endpoint docstrings.

**v0.1.13**: NAP UX polish + inline credential creation:

- **Inline credential creation** in the NAP catalogue form — no more
  alt-tabbing between `/credentials` and `/admin/nap-catalogues`.
  The "+ Create new" button next to the credential dropdown opens a
  sub-form; on save the new credential is created (owned by you,
  reusable elsewhere) AND pre-selected in the catalogue form.
- **Stuck "Loading…" bug fixed** in the Import-from-NAP modal.
  Three terminal states now: error (with HTTP code + role hint),
  empty (with link to add one), found N (populates dropdown).
- **Help expander on /credentials** explains the connection between
  credentials and the two places they get used (NAP catalogues +
  provider URLs in sessions). Resolves the most common confusion
  ("I made a credential, where do I use it?").

**v0.1.12**: NAP catalogue picker + accordion + import-time picker:

- **NAP catalogues** (Top nav → **NAPs**, platform_admin only). A new
  table holds pre-configured NAP endpoints — name, URL, default country,
  default modes, optional credential reference. Comes seeded with the
  France NAP. Add Germany Mobilithek / Swiss data.ch / Italian
  trasportiamo.it as your demonstrator expands.
- **Import modal: catalogue dropdown** replaces the old free-text URL
  field. Selecting a catalogue auto-fills country + modes from its
  defaults. Authenticated NAPs (🔒 in the dropdown) have their
  credential applied server-side; the operator never sees the secret.
- **Picker checkboxes in the Preview**. Each row has a tick — header
  checkbox toggles all. Confirm sends only the ticked subset
  (`include_dataset_ids` filter). No more all-or-nothing imports.
- **Provider-card accordion**. Cards default to collapsed (only the
  first one open) — no more wall-of-forms when a session has 10+
  providers. New "Expand all / Collapse all" toolbar at the top of the
  Providers list. Newly-added cards via "+ Add provider" open by
  default (the operator just clicked Add — they want to see the form).

**v0.1.11**: worker timing knobs in the UI + header polish:

- **Worker timing** card in Admin → Configuration with two new editable knobs:
    - `REBUILD_DEBOUNCE_SECONDS` (default 1800 = 30 min). Was previously
      `.env`-only as `DEBOUNCE_SECONDS`; the worker now live-reads from
      `platform_config` (30 s cache TTL, no restart needed). **Set to 0
      for "rebuild starts on click"** — main fix for the "I clicked
      Rebuild and nothing happened for half an hour" complaint.
    - `WORKER_TICK_SECONDS` (default 15). Previously hardcoded in
      `app/worker.py`. Lower = rebuilds start sooner after their debounce
      window expires; higher = less DB chatter.
- **Header role badge** showing each user's current role (red =
  platform_admin, amber = content_manager, steel = end_user) on the
  right side of the nav.
- **Version badge** capitalised: `V0.1.11` instead of `v0.1.11` in the UI
  (the canonical lowercase form is preserved at /healthz/version and on
  the OCI image label, so tooling that parses the version is unaffected).
- **Config UI fix**: long parameter labels (e.g. `MASTER_STATIONS_REFRESH_DAYS`)
  no longer truncate to `MASTER_STATIONS_REFRESH_DA…`.

**v0.1.10**: per-user encrypted API credentials. See §9 below for the
operator workflow.

**Still queued**:

1. **Fix `maxAccessEgressDurationForMode` field name** for OTP 2.9 —
   currently silently ignored, so the access/egress walking bound isn't
   enforced. Hits when the destination is far from any transit stop.
2. **Credential picker dropdown in sessions.html** — completes the v0.1.10
   credential UX. Today operators must PATCH the API directly to attach a
   credential id to a provider URL.
3. **`OTP_BUILD_TIMEOUT_MINUTES`** — currently no timeout; a stuck build
   can block the next one indefinitely. Add as a third key in the Worker
   timing card.

When you tackle any of these, the release process is exactly §2.3.

---

## 9. API credentials (v0.1.10)

VIATOR consumed only fully-public feeds until v0.1.10. SNCF GTFS-RT,
Swiss SBB, most German VRRs and many private operator feeds need an HTTP
auth header or API-key query string. v0.1.10 adds a per-user credential
library that you attach to provider URLs.

### 9.1 Add a credential

Top nav → **My credentials** (visible to every logged-in user) →
**＋ New credential**.

Fill in:

| Field | What |
|---|---|
| **Friendly name** | Picker label, must be unique per user. e.g. `SNCF prod key`, `My Trenitalia token` |
| **Auth scheme** | One of: bearer, basic, query, header (see below) |
| **URL parameter / Header name** | Only for `query` (the URL key, e.g. `apikey`) and `header` (the HTTP header name, e.g. `X-API-Key`) |
| **Secret value** | The plaintext. AES-256-GCM-encrypted on save; never shown back to anyone |
| **Note** | Optional reminder, e.g. `expires 2027-01` |

Auth schemes:

- **bearer** — `Authorization: Bearer <token>` header. Most common for
  modern OAuth-y APIs. Plaintext is the token alone (no `Bearer ` prefix
  — VIATOR adds it).
- **basic** — `Authorization: Basic <b64(user:pass)>`. Plaintext format
  is `username:password` (literally, with the colon).
- **query** — appends `?<param>=<value>` to the URL. e.g. SNCF
  GTFS-RT uses `?apikey=...`.
- **header** — custom HTTP header. Plaintext is the value; the param
  field is the header name.

### 9.2 Attach to a provider URL

Admin → Sessions → click into a session → expand a provider card. Each
URL field has a paired "credential" dropdown that lists *your* saved
credentials by name. Pick one (or leave "(none)" for anonymous fetch).

The same credential can be attached to:

- `timetable.url` (the GTFS / NeTEx feed)
- All three `gtfs_rt.*` URLs (one credential covers alerts +
  trip_updates + vehicle_positions — they're virtually always on the
  same domain with the same auth)
- `mct_url`
- `stations_csv_url`

When the session refreshes a URL with a credential attached, VIATOR
decrypts at request time, applies the auth, and stamps `last_used_at`
on the credential.

### 9.3 Rotation, deletion, JWT_SECRET rotation

- **Rotate**: My credentials → click **Rotate secret** on the row →
  paste the new value. The friendly name and any session attachments
  stay intact.
- **Delete**: My credentials → **Delete**. Sessions still pointing at
  the deleted credential will start failing refresh with
  `credential not found` until you detach or re-attach a different
  credential. That's intentional — silent fallback to anonymous would
  be worse.
- **JWT_SECRET rotation**: rotating `JWT_SECRET` in `.env` (rare —
  invalidates every JWT cookie too) breaks AES-GCM authentication on
  every stored credential. Refresh will surface
  `credential X cannot be decrypted (JWT_SECRET rotated)`. The fix is
  to delete affected credentials and have users re-create them. There
  is no recovery key.

### 9.4 What's NOT credential-protected

Audit log records credential.{created,updated,deleted} events including
the credential's id, name, auth_type, and changes — but **never** the
plaintext secret. The audit row's metadata for a rotation looks like
`{"changes": {"secret": {"rotated": true}}}`, no actual value.

The decrypted secret is only ever materialised in two places:
1. Just before an `httpx.AsyncClient` call in
   `app/api/admin/sessions.py::_refresh_one_task`, used immediately, not
   logged.
2. Just before writing `router-config.json` for OTP, in
   `app/worker.py::run_build`, also used immediately.

OTP itself sees the auth (it's what makes the GTFS-RT URLs reachable);
that's unavoidable since OTP fetches them at runtime.

---

## 10. One-page reference

```
RELEASE                       DEPLOY                       VERIFY
───────                       ──────                       ──────
git tag -a vX.Y.Z -m "..."    cd /opt/viator/docker        curl -s http://host/healthz/version
git push origin main          sed -i 's/^VIATOR_VERSION    Browser: v badge in header
git push origin vX.Y.Z         =.*/VIATOR_VERSION=vX.Y.Z   docker inspect viator-web-1 \
gh run watch <id>              /' .env                      --format '{{index .Config.Labels
                              docker compose pull web        "org.opencontainers.image.version"}}'
                                worker otp-build
                              docker compose up -d
                                web worker

ROLLBACK                      BACKUP                       MONITOR
────────                      ──────                       ───────
sed -i 's/^VIATOR_VERSION     pg_dump → off-VPS daily      docker compose logs -f web worker
=.*/VIATOR_VERSION=vX.Y.(Z-1) Restore drill quarterly      df -h /var/lib/docker
/' .env                                                    free -h (during builds)
docker compose up -d                                       certbot renew --dry-run (weekly)
  --force-recreate web worker
```

---

## Index of related guides

- [`docker/INSTALL.md`](../docker/INSTALL.md) — first-time VPS install
- [`docker/README.md`](../docker/README.md) — container stack reference
- [`VIATOR-strategy.md`](../VIATOR-strategy.md) — why this exists, roadmap
- [`VIATOR-technical-spec.md`](../VIATOR-technical-spec.md) — engineering spec
- [`docs/nap-fr-rail.md`](nap-fr-rail.md) — operator walkthrough (France rail)
- [`branding/VIATOR-brand-brief.md`](../branding/VIATOR-brand-brief.md) — brand identity
