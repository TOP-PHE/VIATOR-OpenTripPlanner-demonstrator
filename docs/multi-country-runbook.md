# Multi-country sessions — OSM integration runbook

A condensed operator runbook for the cross-border use case (e.g. building a
session that routes Eurostar from Paris to London, Brussels, Amsterdam,
plus ICE / Lyria / TGV connections to Germany, Switzerland, etc.) on
commodity VPS hardware.

Captures the lessons learned during the v0.1.30–v0.1.32.1 EU session
build — file-name contracts, heap budgets, OSM scope choices, build phases,
common failure modes, and the per-session config precedence rules that
caused most of the friction.

**Audience**: platform admins. Some sections require Postgres + Docker
shell access on the VPS host.

**Prerequisites**: VIATOR ≥ v0.1.32.1, `osmium-tool` installed on the VPS
(`sudo apt install osmium-tool`), at least 50 GB free disk in `/opt`,
and a session at `serving` state (the FR baseline) you can reference for
heap-sizing comparisons.

---

## 1. The four pillars

Multi-country builds fail in only four ways, in roughly this order of
likelihood:

| Pillar | Symptom | Section |
|---|---|---|
| **OSM sizing** | Build OOMs in JVM (heap exhaustion) or kernel (OOM-kill) | §3 Heap budgets, §4 OSM scope |
| **Filename contracts** | `Unable to build street graph, no OSM data available` | §5 File-name contracts |
| **Per-session config drift** | Build succeeds but serving container restart-loops | §7 Config precedence |
| **GTFS provider plumbing** | Refresh providers fails 404 / fetched none | §8 Adding cross-border feeds |

Each section is structured: **what happens → how to diagnose → how to fix**.

---

## 2. Sourcing OSM PBFs

### Geofabrik — the canonical source

Country-level extracts at https://download.geofabrik.de/europe/. Sizes as
of mid-2026:

| Country | Size | Country | Size |
|---|---|---|---|
| France | ~4.8 GB | Italy | ~1.9 GB |
| Germany | ~4.5 GB | UK (great-britain) | ~1.8 GB |
| Spain | ~1.5 GB | Netherlands | ~1.2 GB |
| Austria | ~0.7 GB | Switzerland | ~0.5 GB |
| Belgium | ~0.4 GB | Luxembourg | ~0.04 GB |

### Merge with `osmium-tool`

Always merge inside `tmux` — the operation runs ~10–30 min and SSH drops
will kill it:

```bash
tmux new -s eu-osm

mkdir -p /opt/viator/inbox-staging/eu && cd /opt/viator/inbox-staging/eu

# Download
for c in france great-britain belgium netherlands luxembourg \
         germany switzerland ; do
  wget -c "https://download.geofabrik.de/europe/${c}-latest.osm.pbf"
done

# Merge (deterministic — same inputs = same output bytes)
osmium merge \
  france-latest.osm.pbf great-britain-latest.osm.pbf belgium-latest.osm.pbf \
  netherlands-latest.osm.pbf luxembourg-latest.osm.pbf \
  germany-latest.osm.pbf switzerland-latest.osm.pbf \
  -o eu-rail-7c.osm.pbf

ls -lh eu-rail-7c.osm.pbf
```

`Ctrl-b d` to detach. `tmux attach -t eu-osm` to come back.

### Automated alternative: `scripts/merge_osm_eurostar_corridor.sh`

The repo ships a self-contained merge script that does the download + merge
+ UK-bbox-clip in one shot, with no host-installed dependencies (uses a
~150 MB Docker helper image built on the fly from Ubuntu + osmium-tool).
Useful when:

- The host doesn't have `osmium-tool` installed and the operator can't
  `sudo apt install` (e.g. the SSH user isn't in sudoers).
- You want the merge re-runnable / idempotent — downloads resume, the
  merge is rerun only when inputs change.
- You're building the canonical "Eurostar corridor" multi-country session
  (ES + FR + BE + LU + NL + DE + AT + IT + LI + CH + UK-rail-subset).

Run on the VPS, inside tmux:

```bash
# From your local machine — copy the script up:
scp OpenJourneyPlanner/scripts/merge_osm_eurostar_corridor.sh viator@<vps>:/tmp/

# Then on the VPS:
chmod +x /tmp/merge_osm_eurostar_corridor.sh
tmux new -d -s osm-merge '/tmp/merge_osm_eurostar_corridor.sh 2>&1 | tee /tmp/osm-merge.log'

# Check progress without attaching:
tmux ls
tail -f /tmp/osm-merge.log
```

What it does (in order):

1. Builds a tiny `viator-osmium-helper:latest` image (Ubuntu + osmium-tool).
   ~20 s first run, cached thereafter.
2. Downloads 10 Geofabrik regionals into `${WORK_DIR}/raw/` (default
   `/tmp/osm-merge/raw/`). `curl --continue-at -` so reruns resume
   partial downloads. Validates each downloaded file is ≥ 5 MB so an
   HTML stub from a retired Geofabrik URL can't be silently cached as
   a "PBF" (see Benelux gotcha below).
3. Clips great-britain to the Eurostar HS1 corridor bbox (see next
   subsection). All other regions stage verbatim.
4. `osmium merge` everything into one PBF at `${OUTPUT_PATH}` (default
   `/tmp/osm-merge/eurostar-corridor.osm.pbf`).
5. Runs `osmium fileinfo --extended` on the output for a sanity check.

Tunable env vars at the top of the script:

| Env var | Default | Notes |
|---|---|---|
| `WORK_DIR` | `/tmp/osm-merge` | Working directory; needs ~25 GB free for the full 11-country merge |
| `OUTPUT_PATH` | `${WORK_DIR}/eurostar-corridor.osm.pbf` | Final merged PBF location |
| `OSMIUM_IMAGE` | `viator-osmium-helper:latest` | Docker image with osmium-tool — built locally if missing |
| `REGIONS` | (10-region list, see script) | Edit the array to change country coverage |
| `MIN_PBF_BYTES` | `5 MB` | Minimum size before a cached/downloaded "PBF" is trusted (Luxembourg is the smallest legit Geofabrik regional at ~40 MB) |
| `UK_BBOX` | `-0.5,50.8,1.5,51.8` | bbox-clip applied to great-britain — only |

Plug the merged file into a session via either of:

```bash
# (a) Serve via nginx and set sources.osm_pbf=<URL> in the session config:
sudo cp /tmp/osm-merge/eurostar-corridor.osm.pbf \
        /opt/viator/docker/nginx/static/osm/
# (then add an nginx route for /static/osm/ and update the session URL)

# (b) Drop straight into the new session's inbox (skips refresh):
sudo cp /tmp/osm-merge/eurostar-corridor.osm.pbf \
        /opt/viator/data/inbox/<sid>/osm/osm.pbf
# (then trigger Rebuild graph in the UI — no Refresh sources needed)
```

The script is idempotent: rerun safely if interrupted. Raw downloads
under `raw/` are kept across runs so partial downloads resume rather than
restart. Delete `${WORK_DIR}` to force a full re-fetch.

#### Gotcha: Geofabrik discontinues composite extracts silently

Hit during the 2026-06 build: the Benelux composite extract
(`benelux-latest.osm.pbf`) was removed by Geofabrik but the URL still
returns **HTTP 200** with their homepage HTML (~10 KB) instead of a
404. `curl --fail` doesn't catch this — only the size sanity-check
does. If `osmium merge` later complains
`PBF error: invalid BlobHeader size (> max_blob_header_size)`, that's
the signature: run `scripts/check_osm_merge_files.sh` to find the
under-sized file, fix the `REGIONS=` array (likely by splitting a
composite into per-country URLs), delete the bad raw file, re-run.
The Benelux composite was replaced with three separate extracts
(belgium-latest / netherlands-latest / luxembourg-latest); if you're
forking this script for a different corridor, expect the same
treatment for other discontinued composites (e.g. "iberian-peninsula"
or "balkans" may go the same way).

### Reducing UK footprint to the Eurostar corridor

The UK in OTP is interesting: you don't actually need most of Britain —
the only routable destination for a Eurostar GTFS is **London St Pancras
International** (and historically Ashford / Ebbsfleet / Stratford
International). Including the full GB extract (~1.8 GB) costs you ~100 MB
of post-filter graph for ways you'll never route on.

The script clips great-britain to a bbox covering just the HS1 corridor
before merging:

```
UK_BBOX = -0.5,50.8,1.5,51.8        # min_lon, min_lat, max_lon, max_lat
```

That box contains:

| Eurostar UK terminus | Lat | Lon |
|---|---|---|
| London St Pancras International | 51.532 | -0.126 |
| Stratford International | 51.545 | +0.009 |
| Ebbsfleet International | 51.443 | +0.323 |
| Ashford International | 51.143 | +0.876 |

Plus the HS1 line itself and a comfortable margin around each station for
walking-network coverage. Post-clip GB contribution: ~50-80 MB raw,
~10-20 MB after the build's `osm_scope=rail-focused` filter.

The clip uses `osmium extract -s smart` so railway ways crossing the
bbox boundary aren't truncated mid-segment — important for the HS1 line
running south to the channel tunnel portal.

To extend the technique to other countries where you only need a subset
(e.g. ES only the Madrid–Barcelona AVE corridor), copy the great-britain
branch in the script's Phase 2 loop and add another bbox-clip entry.

### Expected merged sizes

Roughly the sum of the country files — `osmium merge` doesn't dedupe
much across non-adjacent countries. Each country PBF was generated
independently from the planet, so border ways / nodes appear in both
neighbouring countries' extracts.

| Countries | Raw merged size |
|---|---|
| FR alone (baseline) | ~5 GB |
| FR + UK + BE + NL + LU + DE + CH (7-country EU rail) | ~13–14 GB |
| Same + AT + IT + ES (10-country) | ~17–19 GB |
| Western-Europe Geofabrik extract | ~10–12 GB |

---

## 3. Heap budgets — the two-heap model

OTP uses **two separate JVM heap settings** that are easy to confuse:

| Setting | Where | Used by | Default |
|---|---|---|---|
| `OTP_BUILD_HEAP` | `.env` + per-session `config.otp_build_heap` | The one-shot otp-build container during graph build | `24g` (was `12g` pre-v0.1.32) |
| `OTP_HEAP` | per-session `config.otp_heap` (no UI yet) | The long-running serving container that answers journey queries | `4g` hardcoded in `app/sessions_orchestrator.py` |

**These are different containers with different lifecycles.** Each phase
peaks at different memory usage:

```
Build phase (otp-build):
  osmium tags-filter        ████ <100 MiB (I/O bound)
  --buildStreet load        ████ ~5-10 GB
  Parse OSM Nodes           ████████████ peak ~24-32 GB
  Build street graph        ██████ ~15-20 GB
  PruneIslands              ████████████ ~30-36 GB (heaviest!)
  --loadStreet --save       ██████████ ~25-30 GB
  Transit graph build       ████████ ~10-15 GB
  Save graph.obj            ██ ~5-8 GB

Serving phase (otp-<sid>):
  Graph load + indexing     ████████ ~8-12 GB
  RAPTOR mapping            ████████████ peak ~14-18 GB
  Steady-state serving      ██████ ~6-10 GB + per-query allocations
```

### Rule of thumb

```
build heap ≈ 5–6 × filtered PBF size
serve heap ≈ 1.5–2 × graph.obj size
```

### Heap matrix by scope

For a 47 GB host with one 13 GB FR session also running:

| OSM scope | Filtered PBF | otp_build_heap | otp_heap (serve) | Peak host usage during build |
|---|---|---|---|---|
| FR `transit-focused` | ~3 GB | 24g | 8g | ~38 GB |
| FR `comprehensive` | ~5 GB | 36g | 12g | ~50 GB ⚠️ |
| 7-country EU `rail-focused` | ~1.1 GB | **32g** ✓ | **20g** ✓ | ~45 GB |
| 7-country EU `transit-focused` | ~10 GB | 60-80g ❌ | 24-32g | won't fit |
| 10-country EU `rail-focused` | ~1.4 GB | 36-40g | 24g | borderline (we OOMed at 32g) |

⚠️ = needs FR session stopped during build.
❌ = won't fit on this host without scope reduction.

### Setting heap correctly

```bash
# Build heap — settable via UI: Sessions → <sid> → Edit → OTP build heap dropdown
# Or via SQL:
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "UPDATE sessions SET config = jsonb_set(config, '{otp_build_heap}', '\"32g\"') WHERE id='<sid>';"

# Serve heap — NO UI, must use SQL (until v0.1.33):
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "UPDATE sessions SET config = jsonb_set(config, '{otp_heap}', '\"20g\"') WHERE id='<sid>';"
```

After updating `otp_heap`, the **generated compose fragment also needs
patching** — the orchestrator only regenerates on session create/delete:

```bash
sudo sed -i '/^  otp-<sid>:/,/^  [a-z]/ s/OTP_HEAP: "OLD"/OTP_HEAP: "NEW"/' \
  /opt/viator/docker/generated/docker-compose.sessions.yml
docker rm -f viator-otp-<sid>-1
cd /opt/viator/docker && docker compose -p viator up -d otp-<sid>
sleep 5
docker exec viator-otp-<sid>-1 ps -ef | grep java | head -1
# Should show: java -Xmx<NEW>g ...
```

---

## 4. OSM scope selection

Four presets in `app/osm_filter.py`. Pick based on country count +
available RAM:

| Scope | Drops | Keeps | Use when |
|---|---|---|---|
| `transit-focused` (default) | driveways, agricultural tracks, construction | all major roads + walking + railway | single-country, ≥40 GB RAM |
| `multi-modal` | nothing significant | + service roads, parking lot detail | dense urban last-mile matters |
| `rail-focused` (v0.1.30) | **all driving infrastructure** | only railway + footway/path/steps + station entrances | multi-country / RAM-constrained |
| `comprehensive` | nothing | original PBF unchanged | OSM debugging |

**`rail-focused` is the only scope that fits a 7+-country EU build on a
47 GB box.** Other scopes either OOM during parse or pruning.

### Trade-off you accept with `rail-focused`

- ✅ Station-to-station rail routing works perfectly
- ✅ City-centre dropdowns + matrix coverage runs work (they submit station coords)
- ❌ Free-text address-to-station fails (no driveable streets in the graph for snap)
- ❌ ~25-30% of GTFS stops won't link to walking graph (rural / disconnected stops)

Acceptable for a station-to-station rail demonstrator. Wrong choice for
last-mile / mobility-as-a-service.

---

## 5. File-name contracts

OTP and the entrypoint expect specific filenames in the inbox. Direct
file uploads must respect them.

### OSM PBF

The entrypoint generates a `build-config.json` that hardcodes:
```json
"osm": [{"source": "osm.pbf"}]
```

So **the filtered output must be named `osm.pbf`** in `BUILD_DIR`. The
entrypoint preserves the input basename:
```bash
pbf_out="$BUILD_DIR/$(basename "$pbf_in")"
```

→ If you place a PBF named anything else (`eu-rail-10c.osm.pbf`, `france-latest.osm.pbf`)
in `inbox/<sid>/osm/`, the build fails with:
```
Unable to build street graph, no OSM data available.
```

**Fix**: rename the file in the inbox before building:
```bash
INBOX=/var/lib/docker/volumes/viator_inbox/_data
sudo mv "$INBOX/<sid>/osm/<your-name>.osm.pbf" \
        "$INBOX/<sid>/osm/osm.pbf"
```

The UI's URL-based fetcher does this rename automatically. Only manual
direct copies need the explicit rename.

### GTFS feeds

Files in `inbox/<sid>/gtfs/*.zip` are auto-discovered. Each becomes a
feed with `feedId = uppercase(basename - .zip)`. So:

- `sncf.zip` → `feedId=SNCF`
- `eurostarinternat.zip` → `feedId=EUROSTARINTERNAT`
- `gtfs.zip` → `feedId=GTFS` (the default name from the upload form — rename for cleaner labels)

The UI's "Upload a file" form normalizes the saved name to `<format>.zip`
(e.g. `gtfs.zip`). To match a specific provider entry's `id` for matrix
labelling, rename after upload:

```bash
sudo mv $INBOX/<sid>/gtfs/gtfs.zip $INBOX/<sid>/gtfs/sbb.zip
```

### `.zip.old` backup files

Whenever a provider is refreshed, the worker keeps the previous version
as `<basename>.zip.old`. OTP only globs `*.zip` (without the trailing
`.old`), so these are inert. Periodically clean if disk pressure:

```bash
sudo rm $INBOX/<sid>/gtfs/*.zip.old
```

---

## 6. Build phases & expected log lines

Phases in order, with the milestone log line for each — useful for
"is the build stuck or just silent?" diagnosis. Approximate wallclock
for a 7-country rail-focused EU build at 32g heap:

| Phase | Marker line | Wallclock | Memory |
|---|---|---|---|
| **Stage GTFS into BUILD_DIR** | `Generating build-config.json with feeds:` | ~30 s | <100 MiB |
| **osmium pass 1+2+3** | `OSM filter: rail-focused — running osmium tags-filter on osm.pbf` | ~25-35 min | <100 MiB |
| osmium done | `osm.pbf: NNNN → NNNN bytes (~5-7% of original)` | (instant) | <100 MiB |
| **OTP `--buildStreet` JVM start** | `OTP STARTING UP - Build Street Graph - Version: 2.9.0` | 5-10 s | climbs to 5-10 GB |
| **Parse OSM Nodes** | `Parse OSM Nodes progress: NN MB of X.X GB (NN%)` | 3-5 min | peaks 24-32 GB |
| Way / relation parse | `Parse OSM Ways progress` etc. | 1-2 min | drops |
| **Build street graph** | `OsmModule.java:535 Build street graph progress: N of N` | 20-30 min | ~36 GB plateau |
| **Index street vertex** | `StreetIndex.java:143 Index street vertex progress` | 2-3 min | ~36 GB |
| **PruneIslands** | `PruneIslands.java:70 Pruning islands and areas isolated by nothru edges` | 60-90 min | ~36 GB |
| BICYCLE pass | `Islands when BICYCLE noThruTraffic is considered: NN` | 5-15 min | ~36 GB |
| WALK pass | `Islands when WALK noThruTraffic is considered: NN` | 35-45 min (heaviest) | ~36 GB |
| CAR pass (rail-focused) | `Islands when CAR noThruTraffic is considered: NN` | 1-3 min | ~36 GB |
| **streetGraph.obj save** | `streetGraph.obj cache updated (key=<sha>:rail-focused)` | <1 min | drops to ~5 GB |
| **OTP `--loadStreet --save`** (phase 2) | `OTP STARTING UP - Build Street Graph - Version: 2.9.0` (again) | restarts JVM | new JVM |
| GTFS reading | `GtfsModule.java:328 Reading entity: ...Stop` | 2-5 min per feed | ~10-15 GB |
| Linking transit stops | `StreetLinkerModule.java:128 Linking transit stops to graph progress` | 30 s | flat |
| Linking entrances + parks | `Linking transit entrances to graph` / `Linking vehicle parks` | 1 min | flat |
| **PruneIslands again** | `PruneIslands.java:70 Pruning islands` | 60-90 min ⚠️ same as before | ~36 GB |
| **Save graph.obj** | (silent — no log line; CPU drops to ~50%) | 5-15 min | ~5-8 GB |
| **otp-build exits** | container disappears from `docker ps` | (instant) | – |
| **Worker promotes graph** | worker log `promoted graph for <sid>` | <1 s | – |
| **Serving container spawns** | `viator-otp-<sid>-1` appears | ~5 s | – |
| Serving load | `Reading graph from .../graph.obj` | 2-3 min | climbs to 15-18 GB |
| RAPTOR map | `RaptorTransitDataMapper.java:96 Mapping complete` | 30 s | peaks |
| **Grizzly running** 🎉 | `Grizzly server running.` | (final marker) | drops to ~10 GB |

**Total wallclock 7-country rail-focused: ~3 hours.**

PruneIslands runs **twice** (once during `--buildStreet`, once during
`--loadStreet --save` after GTFS load) — that's the biggest time sink
and easy to mistake for a hang. CPU stays high (>200%) throughout.

---

## 7. Tracking progress on a running rebuild

### Where the build container lives

```bash
docker ps | grep otp-build
# viator-otp-build-run-<random>   ghcr.io/top-phe/viator-otp:<version>   Up XX
```

The `-run-<random>` suffix means it was spawned via `docker compose run`
(one-shot semantics). It's NOT a regular service, so:

- `docker compose logs otp-build` returns nothing (only follows services)
- Use `docker logs -f viator-otp-build-run-<random>` directly

### Tail log with milestone filter

```bash
docker logs -f $(docker ps -q --filter "name=otp-build-run") 2>&1 | \
  grep --line-buffered -iE \
    "OSM filter|→.*bytes|Build street|Build transit|Intersect|streetGraph|Grizzly|OutOfMemory|GC overhead|graph saved"
```

### Memory canary in another window

```bash
watch -n 30 'docker stats --no-stream | grep -E "otp|NAME"; echo; free -h'
```

What to watch:
- `otp-build` mem **plateau** at expected heap (32-36 GB peak) — normal
- Mem **climbing past container limit** (default `OTP_BUILD_MEM_LIMIT=42g`) — about to OOM
- Host `available` < 3 GB AND swap usage > 5 GB — kernel oom-killer imminent
- `viator-otp-<sid>-1` (existing FR/EU serving) staying steady — not affected

### Inspect what's actually inside the build dir

```bash
# Worker spawns otp-build with a fresh /tmp/tmp.<random>/ as BUILD_DIR
# To find the current one:
docker top viator-otp-build-run-<random> | grep tmp.
# Or from the cmdline:
docker inspect viator-otp-build-run-<random> --format '{{.Config.Cmd}}'

# Then ls inside
docker exec viator-otp-build-run-<random> ls -lh /tmp/tmp.<random>/
```

Should show `osm.pbf` (filtered) + GTFS zips + `build-config.json` +
later `streetGraph.obj` + `graph.obj`.

### Confirm the build's actual JVM args

```bash
docker exec viator-otp-build-run-<random> ps -ef | grep java | head -1
# Look at -Xmx<value>g — confirms which heap setting was actually applied
```

### Build container exits — where do the logs go?

The container is started with `--rm`, so once it exits:

- Container disappears from `docker ps -a` after a few seconds
- Logs are GONE from the Docker daemon
- **Persisted into `rebuild_jobs.log`** (last 32 KB tail) by the worker

To retrieve the failed build's log post-mortem:

```bash
docker compose -p viator exec -T postgres psql -U viator -d viator -tA -c \
  "SELECT log FROM rebuild_jobs WHERE session_id='<sid>' AND status='failed' ORDER BY created_at DESC LIMIT 1;" \
  > /tmp/build.log

less /tmp/build.log
```

### Verify the rebuild_jobs row's status

```bash
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, status, started_at, finished_at,
          extract(epoch from (finished_at - started_at))::int AS dur_s
   FROM rebuild_jobs WHERE session_id='<sid>' ORDER BY created_at DESC LIMIT 5;"
```

States: `pending` → `running` → (`done` | `failed` | `cancelled`).

If a row is stuck `running` but no otp-build container exists, the
worker died mid-build. v0.1.32+ auto-cleans these on worker startup
(see admin-guide §6.x).

---

## 8. Problem determination — symptom → cause → fix

### A. JVM `OutOfMemoryError: Java heap space` mid-build

**Symptom**: Build container exits with `Terminating due to java.lang.OutOfMemoryError`.

**Diagnose**:
```bash
sudo dmesg -T | grep -iE "killed process.*java" | tail -3
# If recent kernel-OOM entries: it's host-side, not JVM. Different fix (§B).
# If no recent entries: it's a real JVM heap exhaustion.

# What heap was actually used?
grep -E "heap=|Xmx" /tmp/build.log | head -5
```

**Fix**: bump `otp_build_heap`. Also check the value actually propagated:
```bash
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, config->>'otp_build_heap' FROM sessions WHERE id='<sid>';"
```

If the JVM is running with a smaller value than session config, the
worker hasn't picked up the config — restart worker:
```bash
cd /opt/viator/docker && docker compose restart worker
```

### B. Kernel `Out of memory: Killed process (java)`

**Symptom**: Build container vanishes silently, no `OutOfMemoryError` in
log, but `dmesg` shows `Killed process XXXX (java) total-vm:XXG`.

**Cause**: total memory pressure on the host (build + serving + FR session
+ web + postgres etc.) exceeded RAM, kernel OOM-killer chose the largest
process (the build JVM) to terminate.

**Fix options**:
1. **Stop other containers during build**:
   ```bash
   docker stop viator-otp-nap-fr-rail-1   # frees ~13 GB
   ```
   Restart after build with `docker start viator-otp-nap-fr-rail-1`.
2. **Add swap** (slow but rescues from edge cases):
   ```bash
   sudo fallocate -l 16G /swap2 && sudo chmod 600 /swap2
   sudo mkswap /swap2 && sudo swapon /swap2
   echo '/swap2 none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
3. **Reduce OSM scope** (drop countries, use rail-focused, etc.)

### C. `Unable to build street graph, no OSM data available`

**Symptom**: OTP exits within 30 seconds of build start with this exact message.

**Cause**: `build-config.json` declares `"osm": [{"source": "osm.pbf"}]`
but the file in `BUILD_DIR` has a different name.

**Fix**: §5 file-name contracts. Rename the inbox PBF to `osm.pbf`.

### D. Serving container restart-loops post-build

**Symptom**: Build succeeds, `viator-otp-<sid>-1` spawns, log shows
`Mapping complete` then container exits and Docker's `restart: unless-stopped`
loops it.

**Cause**: serving heap (`OTP_HEAP`, not `OTP_BUILD_HEAP`) too small.
Default is **4g hardcoded** in `sessions_orchestrator.py` — way too small
for any session beyond IDF.

**Diagnose**:
```bash
docker exec viator-otp-<sid>-1 ps -ef | grep java | head -1
# Look at -Xmx — typically 4g if no per-session override
```

**Fix**: §3 heap budgets. Set `otp_heap` in session config + sed-edit the
generated fragment (until v0.1.33 ships a UI for it).

### E. `LOCATION_NOT_FOUND: Origin is unknown`

**Symptom**: Search returns 0 itineraries with this routing-error code.

**Cause**: OTP can't snap the requested lat/lon to a walkable street edge
within ~250m radius. Two sub-causes:

1. **Coordinates outside OSM extent** — e.g. searching for a Belgian
   address in a France-only PBF. Solution: extend the OSM PBF to cover
   the region (re-merge with the country added).

2. **`rail-focused` dropped too many roads near urban centres** — coords
   of a city-centre address might land on a residential street that no
   longer exists in the filtered graph. Solution: switch to
   `transit-focused` for that session, OR use stop-id routing (v0.1.33+
   work — bypasses snap entirely).

### F. `Provider 'X' not found in session`

**Symptom**: Refresh providers fails immediately with this message.

**Cause**: provider entry isn't in `session.config.sources.providers` —
even though you tried to add it via UI. Possibly the form save failed,
or you typed an `id` that doesn't exist in the configured NAP catalogue.

**Fix**: add directly via SQL (template in §8 below).

### G. Web container restart-loop after deploy

**Symptom**: Deploy a new VIATOR_VERSION, web container restart-loops,
journey UI returns 504.

**Cause**: alembic migration failure on startup. Usually one of:
- Migration revision ID > 32 chars (alembic_version VARCHAR(32) limit) — see v0.1.31 → v0.1.32.1
- Migration tries to add a constraint that conflicts with existing data
- DB connection issues

**Diagnose**:
```bash
docker compose logs web --tail=50 | grep -B2 -A20 "alembic\|psycopg\|sqlalchemy"
```

**Fix**: depends on the specific alembic error. For revision-too-long,
rename in the migration file's `revision: str = "..."` line to ≤30
chars, redeploy.

---

## 9. Per-session config precedence

When VIATOR resolves a config value, the order is:

1. `session.config.<field>` — per-session override, set via UI form or SQL
2. `OTP_<FIELD>` env var on the worker container (from `.env`)
3. Hardcoded default in `app/settings.py` or `sessions_orchestrator.py`

**Common gotcha**: bumping `OTP_BUILD_HEAP=36g` in `.env` does NOT
override a session that has `otp_build_heap=12g` saved in its config.
Per-session value wins.

To check:
```bash
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, config->>'otp_build_heap' AS build, config->>'otp_heap' AS serve,
          config->>'osm_scope' AS scope, config->>'otp_timezone' AS tz
   FROM sessions WHERE id='<sid>';"
```

If a field is `null` in the per-session view, the env-var fallback applies.

### Field name conventions

Field names are inconsistent (legacy of incremental development):

| Purpose | Field name | UI exposes |
|---|---|---|
| Build heap | `otp_build_heap` | ✅ dropdown |
| Serve heap | `otp_heap` | ❌ hidden — SQL only (until v0.1.33) |
| OSM scope | `osm_scope` | ✅ dropdown |
| Timezone | `otp_timezone` | ✅ dropdown |
| API timeout | `otp_api_timeout` | ✅ dropdown |

---

## 10. Adding cross-border GTFS providers

Two paths.

### Via UI "Import from NAP"

Works for NAPs that expose **DCAT-AP `/datasets` endpoint** in the same
JSON shape as `transport.data.gouv.fr/api/datasets`. Tested with the
French NAP. **Does not work** with opentransportdata.swiss (CKAN-based,
different schema), German NAPs (DELFI), or most non-French NAPs.

### Manual provider via SQL — fallback for everyone else

```bash
docker compose -p viator exec postgres psql -U viator -d viator <<'SQL'
UPDATE sessions
SET config = jsonb_set(
  config,
  '{sources,providers}',
  (config->'sources'->'providers') || jsonb_build_array(jsonb_build_object(
    'id', 'SBB',
    'label', 'Swiss Federal Railways — full GTFS',
    'country_iso', 'CH',
    'timetable', jsonb_build_object(
      'url', 'https://opentransportdata.swiss/dataset/.../resource/.../download/gtfs_fp2026_XXX.zip',
      'format', 'gtfs'
    ),
    'gtfs_rt', '{}'::jsonb,
    'mct_url', null,
    'stations_csv_url', null,
    'timetable_credential_id', null,
    'gtfs_rt_credential_id', null,
    'mct_credential_id', null,
    'stations_csv_credential_id', null
  ))
)
WHERE id='eu-nap-network';

SELECT jsonb_array_elements(config->'sources'->'providers')->>'id' AS provider_id
FROM sessions WHERE id='eu-nap-network';
SQL
```

Then in UI: **Refresh providers** to fetch.

### Manual upload — when the URL needs auth or the NAP is fussy

If the URL fetch fails (auth, rate limits, format quirks), download
locally via browser and ship to the VPS:

```bash
scp gtfs_fp2026_XXX.zip otpadmin@vps:/tmp/sbb.zip
ssh otpadmin@vps
sudo mv /tmp/sbb.zip /var/lib/docker/volumes/viator_inbox/_data/<sid>/gtfs/sbb.zip
```

Or use the **session UI's "Upload a file" form** with `Declared standard = GTFS`.
The file lands as `gtfs.zip` (generic name) — rename to your provider
slug for clean feedId labelling:

```bash
sudo mv $INBOX/<sid>/gtfs/gtfs.zip $INBOX/<sid>/gtfs/sbb.zip
```

**Caveat**: uploaded files don't auto-create a provider entry. They
contribute to the build but aren't visible in the providers list. If
you also added a SQL provider entry pointing at a (broken) URL, the UI
will display "SBB" in the list but `Refresh providers` will fail —
which is fine, the build still picks up the manually-staged zip.

---

## 11. Geographic gotchas

### OSM coverage limits routing

A session can only route between coords that are in its OSM PBF. The
GTFS feeds may contain stops in countries the OSM doesn't cover (e.g.
Eurostar GTFS includes London + Bruxelles + Amsterdam stops even in a
France-only OSM session) — but OTP can't snap city-centre lat/lon to a
walkable graph in those countries, so all such searches return
`LOCATION_NOT_FOUND`.

### To add a country to an existing session

1. Re-merge OSM PBF including the new country
2. Rename to `osm.pbf` and place in `inbox/<sid>/osm/`
3. **streetGraph.obj cache is invalidated** because the input PBF SHA
   changed → next build redoes the heavy 1+ hour street-build phase
4. May exceed heap budget — check §3 sizing

### Per-country GTFS feeds you'll likely want

| Country | Feed | Notes |
|---|---|---|
| FR | `transport.data.gouv.fr/datasets` (NAP) | DCAT-AP, works with Import-from-NAP |
| BE | SNCB at `gtfs.irail.be` or NAP DELFI | Direct download |
| NL | `gtfs.ovapi.nl` | Direct, unauthenticated |
| LU | included in DE/FR feeds (CFL is small) | Optional separate feed |
| DE | `data.deutschebahn.com` (huge — DB Fernverkehr) | Heavy feed, dominates transit phase |
| CH | `opentransportdata.swiss` (SKI Geschäftsstelle) | Bearer token for some endpoints; manual upload simplest |
| AT | `mobilitaetsverbuende.at` | OAuth |
| IT | `dati.trasporti.gov.it` | DCAT-AP |
| ES | RENFE feeds via transport.data.gouv.fr (Eurostar partners) | Usually ships in Eurostar feed |
| UK | ATOC / Rail Delivery Group | Auth-required, separate process |

---

## 12. Lessons learned (footguns we hit, fixes queued)

These bit us during the v0.1.30–v0.1.32.1 EU build. Fixes are partly
shipped, partly queued for future versions.

| Footgun | Fixed in | Note |
|---|---|---|
| OSM scope dropdown hardcoded — `rail-focused` invisible | v0.1.30.1 → v0.1.32 (auto-renders) | Now reads from `OSM_SCOPE_PRESETS` |
| `endpoint='network-coverage'` violated CHECK constraint | v0.1.29.4 | Was silent failure on every coverage row |
| Coverage runs orphaned on web restart | v0.1.29.3 | Startup hook marks `running`→`failed` |
| Rebuild jobs orphaned on worker restart | v0.1.32 | Same pattern as above |
| Default heap too small for multi-NAP era | v0.1.32 (12g→24g default) | UI form's selected option also bumped |
| Migration revision ID > 32 chars | v0.1.32.1 | alembic_version VARCHAR(32) — keep ≤30 |
| Manual file upload renames to generic name | not yet | rename inbox file post-upload |
| `otp_heap` (serve) has no UI control | not yet (v0.1.33) | SQL + sed-fragment workaround |
| Generated fragment regenerates only on session events | not yet | Full reload trigger doesn't refresh generated yaml |
| Provider list ≠ inbox files | by design | uploaded files don't auto-create entries |
| OSM-PBF filename must match build-config.json | not yet (v0.1.33+) | entrypoint should rename to `osm.pbf` always |
| OTP image not in `compose pull` | not yet | per-session containers spawned dynamically |
| Geofabrik composite extracts (e.g. `benelux-latest.osm.pbf`) silently 410'd as HTTP-200 HTML stubs | v2026-06 merge script | `--fail` can't see it; only the `MIN_PBF_BYTES ≥ 5 MB` check catches; split into per-country URLs |

---

## 13. NAP timetable feeds — findings, formats, and limits

A field log from the 2026-06 "Eurostar corridor" session build, where we
gathered timetable files from 11 national access points (ES, FR, BE, LU,
NL, DE, AT, IT, LI, CH, GB) and uncovered enough format weirdness to be
worth writing down. The take-away: **trust file contents, not filenames;
trust filenames, not portal labels**. Both can lie.

### 13.1 File-format gotchas

What the filename says vs what's actually inside:

| Source filename | Says | Actually is | How we detected |
|---|---|---|---|
| `NL-NAP_NeTEx lastes.zip` | NeTEx | **IFF** (`.dat` files: stations.dat, timetbls_new.dat, company.dat, …) | `zipfile.namelist()` — no XML, only `.dat` |
| `ES-NAP-*.zip` | (no format hint in name) | **GTFS** (stops.txt + routes.txt + …) | GTFS canonical files present |
| `BE-NAP-SNCB-epip.zip` | NeTEx-EPIP | NeTEx (works as EPIP) | Confirmed by namespace |
| `FR-NAP_gtfs_Region-Sud_ZOU.zip` (mid-2026) | GTFS | Was NeTEx-FR-Arrets (`version="…FR-NETEX_ARRET-…"`) | Confirmed by version-string + ID namespace |
| `AT-NAP_netex_evu_2026.zip` | NeTEx | NeTEx with `at:obb:` codespace (no profile marker) | xmlns + ID prefix inspection |
| `CH-NAP_netex_*.zip` | NeTEx | NeTEx with `ch:1:` codespace, **482 operators in one bundle** | xmlns + operator enumeration |
| `DE-NAP-fahrplaene_gesamtdeutschland.zip` | NeTEx | NeTEx with `DE::` codespace, 27,937 XMLs (whole country) | xmlns + operator enumeration |
| `LU-NAP-netex-*.zip` | NeTEx | NeTEx with `LU::` codespace, AVL Bus + CdT authority (bus-heavy) | xmlns + authority enumeration |
| `IT-TRENITALIA-NeTEx_L1.zip` | NeTEx-EPIP | NeTEx-EPIP, Trenitalia-only | EPIP profile marker in head |

Lesson: when adding a new NAP feed, **don't trust the filename**. Open
the zip and sniff the contents. The `scripts/` folder has the one-liner
we use:
```bash
python -c "
import zipfile, sys
z = zipfile.ZipFile(sys.argv[1])
names = z.namelist()
print(f'{len(names)} entries')
for n in names[:8]: print(f'  {n}')
"  '<path-to.zip>'
```

### 13.2 The `_classify_netex` substring bug (2026-06)

The original `_classify_netex` in `app/detect.py` matched the bare
substring `"ent:"` (in lower-cased head) to flag NeTEx-Nordic. That
substring appears in *every* non-trivial NeTEx file via element names
like `<Component>`, `<Document>`, `<Element>`, and inside schema URIs.
Effect: AT, BE, DE, LU national feeds all got false-flagged as
NeTEx-Nordic. The CH feed was rejected outright ("profile could not be
identified") because the `ch:1:` codespace matches none of the four
recognised markers (FR / Nordic / EPIP / fallback).

Fix shipped in detect.py 2026-06:
- **Nordic** now requires `xmlns:nsr=` or `codespace="nsr"` (real signals,
  not the loose `"ent:"` substring).
- **FR** now also matches the `FR-NETEX` literal in the head — needed for
  feeds (like ZOU pre-mid-2026) that omit the `xmlns:fr=` declaration and
  only carry the codespace in IDs + version strings.
- **Unrecognised national profiles** (CH, AT, DE, LU, …) now default to
  NeTEx-EPIP rather than raise. EPIP is the EU-wide passenger-info
  profile and OTP can read it; a true incompatibility surfaces at build
  time with a clearer error than rejecting upfront on a heuristic.

Tests pinned in [tests/unit/test_detect.py](../tests/unit/test_detect.py)
so the regression can't sneak back. Add a test there if you encounter a
new national feed that needs handling.

### 13.3 National bundles are usually multi-operator

Most "NAP-*.zip" files we tested are multi-operator regardless of what
the filename suggests. Don't pretend they're single-operator in the
provider list — give them country-scoped feed_ids:

| File | Filename hint | Reality |
|---|---|---|
| AT-NAP | OBB (legacy assumption) | OBB + Westbahn + private RUs + Montafonerbahn |
| CH-NAP | (no hint) | SBB + BLS + RhB + MOB + 482 unique operators incl. funiculars |
| DE-NAP | "gesamtdeutschland" | DB + Flixtrain + every regional concession |
| LU-NAP | (no hint) | Mostly AVL bus authority; CFL likely included in unsampled XMLs |
| BE-NAP | "SNCB" | SNCB-only (filename matches) |
| IT-TRENITALIA | "TRENITALIA" | Trenitalia-only (filename matches) |
| NL-NAP (IFF) | (no hint) | NS-only (IFF is the legacy NS-specific format) |

The convention we landed on: country-scoped `feed_id` for NAP bundles
(`AT-RAIL`, `CH-RAIL`, `DE-RAIL`, `LU-RAIL`), single-operator
`feed_id` only when filename + contents agree (NMBS, TRENITALIA, NS).

### 13.4 NL: IFF is not NeTEx — use OpenOV instead

The Dutch NAP (NDOV Loket) historically publishes NS timetables in **IFF**,
a legacy NS-specific format (the `.dat`-suffixed contents). VIATOR can't
ingest IFF — there's no conversion step in the build pipeline, and
adding one isn't worth the effort because:

- IFF is NS-only — no European Sleeper, Eurostar NL leg, Arriva, Keolis,
  ICE International, etc.
- A higher-quality multi-operator alternative already exists.

**Use OpenOV's national GTFS instead**:

```
http://gtfs.ovapi.nl/nl/gtfs-nl.zip
```

Maintained by OV-NL Open Data, converted from IFF + KV1, covers every
Dutch public-transport operator. Wire it as a `source: url` provider —
no local file needed.

### 13.5 Italo (NTV) is not in any NAP

Italo / Nuovo Trasporto Viaggiatori is a **private** open-access operator
competing with Trenitalia on Italian high-speed lines. Unlike Trenitalia
they publish **no public GTFS or NeTEx feed** anywhere — not on the
Italian NAP (`nap.mit.gov.it`), not on `dati.mit.gov.it`, not on
MobilityDatabase, not on transit.land, not on `eu.data.public-transport.earth`.

Data is exposed only via:

- Italo's own booking site (`italotreno.it`) and undocumented internal API
- Commercial reseller agreements (Trainline et al.)

The DATA4PT-project NeTEx Italian profile rollout *may* eventually force
publication, but as of mid-2026 there is no file to plug in. Document
this in any session that needs full Italian high-speed coverage as a
known gap; for journey planning UI, Italo trips are intentionally
absent.

### 13.6 Italian regional rail — Trenord only, in rail-focused scope

For the rail-focused-OSM corridor session, only Trenord is worth wiring:

| Operator | Feed URL | Useful in rail-focused scope? |
|---|---|---|
| Trenord (Lombardy regional rail, Malpensa Express) | `https://www.dati.lombardia.it/download/3z4k-mxz9/application/zip` | ✅ genuine rail |
| GTT (Turin / Piedmont) | aperTO portal (manual click) | ⚠️ license is non-commercial-only — VIATOR usage may not qualify; also mostly urban |
| TPER (Bologna / Emilia-Romagna) | `solweb.tper.it/web/tools/open-data/` | ❌ mostly urban bus |
| ATAC (Rome / Lazio) | `https://romamobilita.it/sites/default/files/rome_static_gtfs.zip` | ❌ entirely urban — stops will load but won't route on rail-focused OSM |

The rail-focused OSM scope (`osm_scope=rail-focused`) strips driving
roads. Loading urban bus/tram feeds against a rail-focused street graph
means OTP can't walk anywhere outside footways — urban operators show
up empty in journey results. Either keep the session rail-focused and
skip them, or fork a transit-focused-scope session for urban modes.

### 13.7 License watch-outs

- **GTT (Turin)**: GTFS license is **non-commercial only** — academic /
  research / civic-tech use. VIATOR's demonstrator status is ambiguous;
  contact GTT before enabling for any commercial use.
- **Trenord, ATAC, TPER**: CC-BY (3.0 or 4.0) — fine for our purposes
  with attribution.
- **OV-NL / OpenOV GTFS**: CC-BY 4.0.
- **NDOV Loket IFF**: free for any use including commercial.
- **Geofabrik OSM PBFs**: ODbL — share-alike, requires attribution.
- The NAP feeds themselves are typically CC-BY or equivalent under the
  EU NAP-data-sharing directive (Reg. 2017/1926 + 2024/490), but
  individual operators can attach stricter terms (GTT is the cautionary
  example).

### 13.8 What to do when a feed surprises you

1. **Open the zip.** `zipfile.namelist()` + read the first XML head.
   File extension and filename are not authoritative.
2. **Don't add ad-hoc workarounds to `app/detect.py`.** If a feed is
   genuinely NeTEx but classifies wrong, the fix belongs in
   `_classify_netex` with a test in [tests/unit/test_detect.py](../tests/unit/test_detect.py).
3. **If the file is in an unsupported format (IFF, KV1, …)**, look for
   an alternative converted feed (OpenOV-style) rather than building a
   converter into VIATOR.
4. **If a single-operator session-level feed_id label turns out to be a
   multi-operator bundle**, switch to a country-scoped name (`XX-RAIL`)
   to avoid confusing operators reading the session UI.

### 13.9 Per-country at-a-glance (2026-06)

| Country | Best feed source | Format | Single- or multi-operator | Special notes |
|---|---|---|---|---|
| ES | Per-operator zips from NAP (FGC, Euskotren, Ouigo-ES, Renfe AVLD, Renfe Cerca) | GTFS | Per-operator | Filename says `NeTEx`, contents are GTFS |
| FR | `transport.data.gouv.fr` per-region zips, including Eurostar, Renfe-on-FR, Trenitalia-on-FR | GTFS (most) + NeTEx-FR (ZOU mid-2026) | Per-region / per-operator | NeTEx-FR variants are archive-only (OTP can't read them) |
| BE | NMBS/SNCB NAP zip | NeTEx-EPIP | Single (SNCB) | Filename matches contents |
| LU | LU NAP NeTEx bundle | NeTEx-EPIP (via EPIP fallback) | Multi (AVL + likely CFL) | Bus-heavy; check CFL coverage if rail is the priority |
| NL | `gtfs.ovapi.nl/nl/gtfs-nl.zip` (OpenOV) | GTFS | **Multi-operator** (NS + Eurosleeper + Eurostar NL + Arriva + ICE Intl + …) | Skip the IFF on NDOV — use OpenOV |
| DE | DE NAP gesamtdeutschland NeTEx | NeTEx-EPIP (via EPIP fallback) | Multi (DB + Flixtrain + all regional) | 1.5 GB+ file, 27k+ XMLs — heavy |
| AT | AT NAP NeTEx EVU bundle | NeTEx-EPIP (via EPIP fallback) | Multi (OBB + Westbahn + private RUs) | — |
| IT | Trenitalia NAP NeTEx + Trenord regional GTFS | NeTEx-EPIP + GTFS | Per-operator | **Italo not available** (no public feed) |
| LI | (folded into CH extract) | — | — | LI ≈ 5 MB; CH covers it |
| CH | CH NAP NeTEx (single bundle of every operator) | NeTEx-EPIP (via EPIP fallback) | **Multi (482 operators!)** | Codespace is `ch:1:` — was being rejected pre-2026-06 detect fix |
| GB | UK Rail Delivery Group / ATOC, plus Eurostar GTFS for the cross-channel side | (auth-required) | — | We use just the Eurostar GTFS — Eurostar's UK service is the only thing the corridor needs |

This table goes stale fast — NAP rollouts shift quarterly under Reg.
2017/1926. Verify against the source before adding a new country.

---

## Quick reference — common commands

```bash
# Session config view
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, state, config->>'osm_scope' AS scope, config->>'otp_build_heap' AS bh, config->>'otp_heap' AS sh FROM sessions;"

# Inbox content
sudo ls -lh /var/lib/docker/volumes/viator_inbox/_data/<sid>/{osm,gtfs}/

# Active build container
docker ps | grep otp-build

# Build log tail (running)
docker logs -f $(docker ps -q --filter "name=otp-build-run") 2>&1 | \
  grep --line-buffered -iE "OSM filter|→.*bytes|Build|Intersect|Linking|streetGraph|Grizzly|OutOfMemory"

# Failed build's persisted log
docker compose -p viator exec -T postgres psql -U viator -d viator -tA -c \
  "SELECT log FROM rebuild_jobs WHERE session_id='<sid>' AND status='failed' ORDER BY created_at DESC LIMIT 1;" > /tmp/build.log

# Memory pressure
docker stats --no-stream | grep -E "otp|NAME"; free -h

# Kernel OOM check
sudo dmesg -T | grep -iE "killed process.*java" | tail -3

# Generated compose fragment
sudo less /opt/viator/docker/generated/docker-compose.sessions.yml

# Rebuild jobs history
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, status, started_at, finished_at FROM rebuild_jobs WHERE session_id='<sid>' ORDER BY created_at DESC LIMIT 5;"
```

---

See also: [admin-guide.md §6](./admin-guide.md#6-troubleshooting) for general
troubleshooting, [nap-fr-rail.md](./nap-fr-rail.md) for the single-country
walkthrough.
