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

**v0.1.20 (latest)**: rebuild graph panel — current build card + history.

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
