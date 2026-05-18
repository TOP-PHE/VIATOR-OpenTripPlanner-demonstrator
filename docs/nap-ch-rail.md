# VIATOR — `nap-ch-rail` operator guide

A practical walkthrough for setting up the **Switzerland rail demonstrator
session** in VIATOR — the one we call `nap-ch-rail` throughout this doc —
backed by SBB's national fahrplan GTFS feed (`opentransportdata.swiss`).

The session pulls a single nation-wide GTFS bundle from the Swiss NAP,
filters it to rail-only routes (mainline + S-Bahn + cog rail), builds an
OpenTripPlanner graph against a rail-focused OSM extract of CH + neighbouring
countries, and serves journey queries.

> **Audience:** platform admins operating an installed VIATOR stack at
> v0.1.32 or newer who have already brought up [nap-fr-rail.md](nap-fr-rail.md).
> Assumes shell access on the VPS, Postgres + Docker familiarity, and
> ~50 GB host RAM with ~60 GB free disk in `/opt`.
>
> **TL;DR:** the SBB national feed is much denser than SNCF's — it contains
> all Swiss public transport (trains + buses + trams + cable cars + gondolas
> + funiculars + boats). A naive integration **will not boot at any heap
> size** because of how OTP's `TransferConstraints` feature scales with the
> number of constrained transfers in the feed. Two non-obvious steps below
> (rail-only GTFS filter + OTP feature-flag override) are mandatory.

---

## 1. Why SBB is harder than SNCF

SBB publishes one consolidated weekly GTFS that covers **all CH public
transport**, not just rail. Per the 2025-W50 export:

| Mode | route_type | Routes |
|---|---|---|
| Rail (all classes) | 100–117 | 864 |
| Bus / coach | 700–715, 202 | 3,586 |
| Tram | 900 | 50 |
| Aerial lift / gondola | 1300 | 295 |
| Ferry | 1000 | 104 |
| Funicular | 1400 | 53 |
| Metro (Lausanne) | 401 | 2 |
| Taxi | 1500 | 3 |
| **Total** | | **4,957** |

Loading the full feed into OTP 2.9 produces these graph stats:

```
|Stops|=62,199  |Patterns|=77,652  |ConstrainedTransfers|=336,197
```

The OTP serving JVM then OOMs **right after `Mapping complete.`** —
regardless of heap size (tested 16g, 24g, 40g, 56g, 72g; all OOM at the
same step). The killer is the **post-RAPTOR `TransferConstraints` index
construction**, which scales unfavourably with the constrained-transfer
count. Heap-bumping alone cannot fix it.

The two mitigations below — **rail-only GTFS filtering** and **OTP feature
flags** — bring serve heap requirement back to ~24 GB and let the session
fit comfortably on a 94 GB host alongside an existing FR session.

---

## 2. Sourcing the SBB feed

### 2.1 Origin

Swiss National Access Point: <https://opentransportdata.swiss>

The canonical feed is **"GTFS Switzerland — Schweiz, Suisse, Svizzera"**,
published weekly by SKI Geschäftsstelle. As of 2025-W50:

- File name: `gtfs_fp2026_2025-12-XX.zip` (filename changes each week)
- Size: ~145–150 MB compressed
- Coverage validity window: roughly one calendar year ahead, rolling
- Auth: none required for the bulk download (some endpoints need bearer
  tokens, but the GTFS bulk file is public)

### 2.2 Why manual upload, not URL-based fetch

VIATOR's "Import from NAP" UI works against DCAT-AP endpoints in the
`transport.data.gouv.fr` shape. `opentransportdata.swiss` runs CKAN with a
different schema, so the in-app fetcher cannot crawl it.

**Workflow:** download in browser, scp to VPS, manual upload via UI or
direct file placement. See [§5 staging the feed](#5-staging-the-feed) below.

---

## 3. The dense-feed problem — what to filter

A station-to-station rail demonstrator needs only the rail rows. The other
~80% of the feed (buses, trams, gondolas) bloats the graph without
contributing useful itineraries.

### 3.1 What to keep

GTFS route_type values, broken down. **Keep the bold rows** for a UIC/MERITS
rail demonstrator:

| route_type | Mode | SBB count | Keep? |
|---|---|---|---|
| 100 | Railway Service (generic) | 1 | ✅ |
| 101 | High-Speed Rail (TGV Lyria, ICE) | 44 | ✅ |
| 102 | Long-Distance (IC, EC) | 52 | ✅ |
| 103 | Inter-Regional (IR) | 46 | ✅ |
| 105 | Sleeper (Nightjet) | 6 | ✅ |
| 106 | Regional Rail (RE, R) | 391 | ✅ |
| 107 | Tourist Railway (Glacier Express etc.) | 10 | ✅ |
| 109 | Suburban (S-Bahn) | 210 | ✅ |
| 116 | Rack & Pinion (Jungfraubahn, Pilatusbahn) | 12 | ✅ |
| 117 | Additional Rail | 92 | ✅ |
| **rail subtotal** | | **864** | |
| 202 | Coach Service | 1 | ❌ |
| 401 | Metro Service | 2 | ❌ |
| 700–715 | Bus / trolleybus | 3,585 | ❌ |
| 900 | Tram | 50 | ❌ |
| 1000 | Ferry | 104 | ❌ |
| 1300 | Aerial lift / gondola | 295 | ❌ |
| 1400 | Funicular | 53 | ❌ |
| 1500 | Taxi | 3 | ❌ |

### 3.2 Expected reduction after filter

| Metric | Before | After (rail-only) | Retained |
|---|---|---|---|
| Routes | 4,957 | 864 | 17% |
| Trips | 1,342,319 | 203,624 | 15% |
| Stop times | 21,378,457 | 2,225,560 | 10% |
| Stops | 97,545 | 8,893 | 9% |
| Transfers | (n/a) | 29,936 | — |
| File size | 147 MB | 30 MB | 20% |

The graph stats after build drop from `62K stops / 77K patterns` to
`~5.7K stops / ~46K patterns`. The 46K-pattern figure is still high
(SBB encodes many trip variants per route), but is no longer the OOM
driver once the feature flags below are in place.

### 3.3 Filter script

The filter runs on the VPS. Uses pandas-only (no `gtfs-kit` dependency
which has heavy native libs).

```bash
sudo apt install -y python3-pandas    # Ubuntu 24.04 system-wide is fine

mkdir -p /tmp/sbb-filter && cd /tmp/sbb-filter
sudo cp /var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/gtfs/sbb.zip sbb-original.zip
sudo chown $USER:$USER sbb-original.zip

python3 <<'PY'
import zipfile, io, pandas as pd
SRC, DST = "sbb-original.zip", "sbb-rail.zip"
RAIL_TYPES = {"2"} | {str(t) for t in range(100, 118)}   # legacy 2 + extended 100-117

with zipfile.ZipFile(SRC) as zf:
    files = {n: zf.read(n) for n in zf.namelist()}
def get(n):
    return pd.read_csv(io.BytesIO(files[n]), dtype=str, low_memory=False) if n in files else None

routes = get("routes.txt"); trips = get("trips.txt"); st = get("stop_times.txt")
stops = get("stops.txt"); xfers = get("transfers.txt")
cal = get("calendar.txt"); cal_d = get("calendar_dates.txt")
agency = get("agency.txt"); shapes = get("shapes.txt"); freq = get("frequencies.txt")

routes_k = routes[routes["route_type"].isin(RAIL_TYPES)]
trips_k  = trips[trips["route_id"].isin(routes_k["route_id"])]
st_k     = st[st["trip_id"].isin(trips_k["trip_id"])]

stop_ids = set(st_k["stop_id"])
if "parent_station" in stops.columns:
    stop_ids |= set(stops[stops["stop_id"].isin(stop_ids)]["parent_station"].dropna()) - {""}
stops_k = stops[stops["stop_id"].isin(stop_ids)]

services_k = set(trips_k["service_id"])
cal_k   = cal[cal["service_id"].isin(services_k)]   if cal   is not None else None
cal_d_k = cal_d[cal_d["service_id"].isin(services_k)] if cal_d is not None else None

xfers_k = None
if xfers is not None:
    xfers_k = xfers[xfers["from_stop_id"].isin(stop_ids) & xfers["to_stop_id"].isin(stop_ids)]

shapes_k = None
if shapes is not None and "shape_id" in trips_k.columns:
    sids = set(trips_k["shape_id"].dropna()) - {""}
    shapes_k = shapes[shapes["shape_id"].isin(sids)]

freq_k = freq[freq["trip_id"].isin(trips_k["trip_id"])] if freq is not None else None

agency_k = agency
if agency is not None and "agency_id" in agency.columns and "agency_id" in routes_k.columns:
    agency_k = agency[agency["agency_id"].isin(routes_k["agency_id"].dropna())]

print(f"BEFORE: routes={len(routes)} trips={len(trips)} stop_times={len(st)} stops={len(stops)}")
print(f"AFTER : routes={len(routes_k)} trips={len(trips_k)} stop_times={len(st_k)} stops={len(stops_k)}")
print(f"        services={len(services_k)} transfers={len(xfers_k) if xfers_k is not None else 'n/a'}")

out = {"agency.txt": agency_k, "routes.txt": routes_k, "trips.txt": trips_k,
       "stop_times.txt": st_k, "stops.txt": stops_k}
if cal_k    is not None: out["calendar.txt"]       = cal_k
if cal_d_k  is not None: out["calendar_dates.txt"] = cal_d_k
if xfers_k  is not None: out["transfers.txt"]      = xfers_k
if shapes_k is not None: out["shapes.txt"]         = shapes_k
if freq_k   is not None: out["frequencies.txt"]    = freq_k

known = set(out.keys())
for n, b in files.items():
    if n not in known and not n.endswith("/"):
        out[n] = b

with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as zf:
    for n, c in out.items():
        zf.writestr(n, c.to_csv(index=False) if isinstance(c, pd.DataFrame) else c)
print(f"\nWrote {DST}")
PY

ls -lh sbb-rail.zip
```

Expect output along the lines of:
```
BEFORE: routes=4957 trips=1342319 stop_times=21378457 stops=97545
AFTER : routes=864 trips=203624 stop_times=2225560 stops=8893
        services=42016 transfers=29936
Wrote sbb-rail.zip
30M sbb-rail.zip
```

If your AFTER routes < 800 or > 900, recheck the `route_type` value space —
SBB occasionally adds extended subtypes (e.g. a future `118`). Adjust
`RAIL_TYPES` accordingly.

---

## 4. OSM scope and heap sizing

### 4.1 OSM PBF — `rail-focused` mandatory

Use the `rail-focused` scope (defined in [app/osm_filter.py](../app/osm_filter.py)).
Drops all driving infrastructure, keeps only railway tracks + walking
paths + station entrances. Cuts the street graph from ~7.2M vertices
(transit-focused) to ~1.4M vertices.

| OSM scope | Filtered PBF | Build heap | Serve heap | Why |
|---|---|---|---|---|
| `transit-focused` | full road net | 48g | 56g+ OOMs | bloated street graph |
| `multi-modal` | + service roads | 56g | 60g+ OOMs | even more bloat |
| **`rail-focused`** | rail + paths only | **48g** | **24g** ✓ | the only viable choice |
| `comprehensive` | unchanged PBF | won't fit | won't fit | OSM debugging only |

### 4.2 Trade-offs accepted with `rail-focused`

- ✅ Station-to-station rail routing — works perfectly
- ✅ City-centre dropdown searches against `master_stations` — work
- ❌ Free-text address-to-station — fails (no driveable streets in graph)
- ❌ ~50% of non-rail GTFS stops won't link to walking graph — fine because
  we filtered those out anyway

For a station-to-station MERITS demonstrator this is the right trade.
For mobility-as-a-service / last-mile use cases it isn't.

### 4.3 Heap sizes after both mitigations

| Heap setting | Recommended value |
|---|---|
| `otp_build_heap` | **48g** (peak during second PruneIslands) |
| `otp_heap` (serve) | **24g** (steady-state ~12 GB working set) |

Both per-session in `session.config`. See [multi-country-runbook.md §3](multi-country-runbook.md#3-heap-budgets--the-two-heap-model)
for general heap budget guidance.

---

## 5. Disabling expensive OTP features

OTP supports per-graph feature flags via `otp-config.json` placed next to
`graph.obj`. The orchestrator does **not** generate this file — operator
creates it manually after the first successful build.

### 5.1 The file

Path: `/var/lib/docker/volumes/viator_graphs/_data/nap-ch-rail/current/otp-config.json`

```json
{
  "otpFeatures": {
    "OptimizeTransfers": false,
    "ConsiderPatternsForDirectTransfers": false,
    "TransferConstraints": false
  }
}
```

Must be owned by `1000:1000` (the `appuser` inside the OTP container).

### 5.2 What each flag does and what disabling costs

| Flag | What it does (ON) | Cost of disabling (OFF) |
|---|---|---|
| `OptimizeTransfers` | When RAPTOR finds multiple itineraries with the same arrival time, pick the transfer station/time optimal for non-time criteria (fewer transfers, less walking) | Negligible — 1- and 2-leg journeys (95% of demo queries) are unaffected. On 3+ leg journeys the displayed transfer station may be sub-optimal but the journey still works. |
| `ConsiderPatternsForDirectTransfers` | Only generate walking-only direct transfers between stops actually served by useful patterns | Negligible — adds a few unused walking edges to the graph; doesn't affect computed itineraries. |
| **`TransferConstraints`** | Read `transfers.txt` and enforce per-station, per-train minimum and maximum transfer times, including "transfer impossible" rows | **Real impact** — OTP now uses one generic `transferSlack` (was `2m`, bumped to `5m` in v0.1.35.05 — see §5.3) for every transfer. SBB's `transfers.txt` encoded 36K rows of station-specific min transfer times (most 4–7 min at major hubs). With the 5m global slack the worst infeasible-2-minute transfers at Zürich HB / Bern / Basel SBB are gone; some technically-feasible 2-minute platform-adjacent transfers at smaller stations are now over-conservative, which is the acceptable side of the trade-off. |

### 5.3 Mitigation for `TransferConstraints` off — `transferSlack` raised to 5m *(applied v0.1.35.05)*

To compensate for the lost per-platform precision, the global transfer
slack in [docker/otp/router-config.json](../docker/otp/router-config.json)
is bumped from `2m` (OTP's default) to `5m`:

```json
"routingDefaults": {
  "numItineraries": 5,
  "transferSlack": "5m",
  ...
}
```

The Python equivalent in `app/router_config.py::_DEFAULT_ROUTING_DEFAULTS`
mirrors this so sessions built without an explicit router-config keep the
same behaviour.

Global change — affects every session, not just CH. Acceptable trade-off
because:

- For rail-rail transfers at major hubs (Paris Gare de Lyon, Zürich HB,
  Frankfurt Hbf), 5 min is the operational reality anyway.
- For platform-adjacent transfers (e.g. cross-platform IR → S-Bahn at small
  Swiss stations), 5 min may be conservative — search will miss a few
  technically-feasible 2-minute transfers but never propose an impossible one.

If a session needs per-platform precision (e.g. a passenger-information
deployment, not a demonstrator), build it with `TransferConstraints=true`
and accept the heap cost. That requires either filtering the feed even more
aggressively to drop the transfers.txt rows, or a future OTP version with
better scaling.

### 5.4 Applying the file

After writing `otp-config.json`:

```bash
docker rm -f viator-otp-nap-ch-rail-1
cd /opt/viator/docker && docker compose -p viator up -d otp-nap-ch-rail

# Watch the feature list in the boot log
docker logs viator-otp-nap-ch-rail-1 2>&1 | grep -A30 "Features turned off"
```

You should see `OptimizeTransfers`, `ConsiderPatternsForDirectTransfers`,
and `TransferConstraints` all in the "off" list. If they're still in the
"on" list, the file wasn't picked up — recheck path and ownership.

---

## 6. Staging the feed

The session inbox lives in the `viator_inbox` Docker volume. To replace
or rotate the SBB feed:

```bash
INBOX=/var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/gtfs

# 1. See current state
sudo ls -lh $INBOX/

# 2. Move existing sbb.zip aside (keeps rollback path)
sudo mv $INBOX/sbb.zip $INBOX/sbb.zip.$(date +%Y%m%d)-backup

# 3. Drop the filtered feed in. Filename MUST end in .zip (no .zip.backup
#    or similar — the worker globs *.zip and ignores everything else).
#    The basename (without .zip) becomes the GTFS feedId in OTP, uppercased.
sudo cp /tmp/sbb-filter/sbb-rail.zip $INBOX/sbb.zip
sudo chown viator:viator $INBOX/sbb.zip

# 4. Confirm
sudo ls -lh $INBOX/
```

Then trigger **Sessions → nap-ch-rail → Rebuild graph** in the admin UI.

Note: VIATOR's rebuild API performs a filesystem glob (`gtfs/*.zip`) — if
the inbox has only `.zip.backup` files, you'll get
`No transit feed staged for session 'nap-ch-rail'`. Always leave at least
one `.zip` file present.

---

## 7. Operational checklist — fresh CH session from scratch

Step-by-step. Tested 2026-05-13. Assumes you can create sessions via UI
or SQL.

### 7.1 Create the session

Admin UI → **Sessions** → **New session** with:

| Field | Value |
|---|---|
| ID | `nap-ch-rail` |
| Label | `Switzerland — SBB rail demonstrator` |
| Category | (whatever your conventions are) |
| OSM scope | `rail-focused` |
| Timezone | `Europe/Zurich` |
| OTP build heap | `48g` |
| Include in fanout | yes |

After save: set `otp_heap` (no UI yet, must use SQL):

```bash
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "UPDATE sessions SET config = jsonb_set(config, '{otp_heap}', '\"24g\"') WHERE id='nap-ch-rail';"
```

### 7.2 Provider entry (optional — only for UI metadata)

The full SBB feed has no DCAT-AP endpoint we can crawl, so the provider
entry is purely cosmetic — it shows up in the UI but `Refresh providers`
will fail. The actual GTFS comes from a manual upload (next step).

If you want the UI to label the feed nicely, insert:

```bash
docker compose -p viator exec postgres psql -U viator -d viator <<'SQL'
UPDATE sessions
SET config = jsonb_set(
  COALESCE(config, '{}'::jsonb),
  '{sources,providers}',
  COALESCE(config->'sources'->'providers', '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
    'id', 'SBB',
    'label', 'Swiss Federal Railways — rail-only (filtered)',
    'country_iso', 'CH',
    'timetable', jsonb_build_object('format', 'gtfs'),
    'gtfs_rt', '{}'::jsonb
  ))
)
WHERE id='nap-ch-rail';
SQL
```

### 7.3 OSM PBF — source and stage

Recommended source: regional merge from [Geofabrik](https://download.geofabrik.de/europe/)
covering CH plus any neighbours you want to include for cross-border rail
(DE south, FR east, IT north, AT west). See [multi-country-runbook.md §2](multi-country-runbook.md#2-sourcing-osm-pbfs)
for the merge recipe with `osmium`.

Stage as `osm.pbf` in `inbox/<sid>/osm/` — filename matters:

```bash
INBOX=/var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/osm
sudo mkdir -p $INBOX
sudo cp /opt/viator/inbox-staging/eu-rail-something.osm.pbf $INBOX/osm.pbf
sudo chown viator:viator $INBOX/osm.pbf
```

### 7.4 SBB GTFS — download, filter, stage

```bash
# Download SBB feed on your laptop, scp to VPS:
scp gtfs_fp2026_2025-12-XX.zip viator@vps:/tmp/sbb-raw.zip

# On VPS:
ssh viator@vps
sudo mv /tmp/sbb-raw.zip /var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/gtfs/sbb.zip
sudo chown viator:viator /var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/gtfs/sbb.zip

# Run filter (§3.3 script). Output: /tmp/sbb-filter/sbb-rail.zip
# Stage the filtered version:
INBOX=/var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/gtfs
sudo mv $INBOX/sbb.zip $INBOX/sbb.zip.unfiltered-backup
sudo cp /tmp/sbb-filter/sbb-rail.zip $INBOX/sbb.zip
sudo chown viator:viator $INBOX/sbb.zip
```

### 7.5 First build

Admin UI → **Sessions** → `nap-ch-rail` → **Rebuild graph**.

Expected duration on a 94 GiB / 18-vCPU VPS:
- streetGraph build (first time): 30–45 min
- transit overlay: 5–10 min
- second PruneIslands: 30–45 min
- save graph.obj: 1–2 min
- **Total first build: ~75–100 min**

Subsequent rebuilds with unchanged OSM hit the `streetGraph.obj` cache and
finish in 15–20 min.

### 7.6 Add the otp-config.json

After the build finishes, before clicking **Start serving** (or after the
serving container starts and OOMs — works either way; the file is read at
each container start):

```bash
GRAPH=/var/lib/docker/volumes/viator_graphs/_data/nap-ch-rail/current
sudo tee $GRAPH/otp-config.json > /dev/null <<'JSON'
{
  "otpFeatures": {
    "OptimizeTransfers": false,
    "ConsiderPatternsForDirectTransfers": false,
    "TransferConstraints": false
  }
}
JSON
sudo chown 1000:1000 $GRAPH/otp-config.json
```

### 7.7 Promote to serving

In the admin UI, click **Start serving**. The orchestrator regenerates the
compose fragment and spawns the OTP container.

> ⚠️ **The green "✓ serving now" badge appears immediately** — but OTP's
> JVM still needs ~3–5 minutes to load graph.obj, build the street index,
> and complete RAPTOR mapping. The authoritative readiness signal is the
> log line `Grizzly server running.`, not the badge.

```bash
docker logs -f viator-otp-nap-ch-rail-1 2>&1 | \
  grep --line-buffered -iE "grizzly|outofmemory|mapping complete"
```

When you see `Grizzly server running.`, the session is genuinely ready.

---

## 8. Searching the CH session — UX gotchas

### 8.1 SBB station names are in local languages

The dropdown is populated from GTFS stop names. SBB names stations in
their local language, **not** English:

| English you'd type | What's in the feed | Result |
|---|---|---|
| Zurich | `Zürich HB` (umlaut `ü`) | typing "Zurich" → no match |
| Geneva | `Genève` (grave `è`) | typing "Geneva" → no match |
| Lucerne | `Luzern` | typing "Lucerne" → no match |
| Basel | `Basel SBB` or `Basel Bad Bf` | works |
| Bern | `Bern` | works (same in all languages) |
| Lausanne | `Lausanne` | works (same in FR/EN) |

**Tell users to use local-language names**, or paste `ü` / `è` / `à` /
etc. from a reference list. A future UI enhancement could surface a hint
near the search box.

### 8.2 Avoid free-text address entry

With `rail-focused` OSM scope, there are no driveable streets in the
graph — only railway tracks and walking paths near stations. Typing an
address (e.g. "Bahnhofstrasse 15, Zürich") will produce
`LOCATION_NOT_FOUND`. Always pick a station from the dropdown.

### 8.3 Cross-session fan-out

When `nap-fr-rail` and `nap-ch-rail` are both serving, a search runs
against both. Expect lines like:

```
nap-fr-rail: 0 trips in 200ms (no_route) · nap-ch-rail: 3 trips in 480ms
```

`nap-fr-rail` returning `no_route` for Swiss-only journeys is **expected**
— the FR session doesn't have CH transit data. Don't treat that as a
problem.

---

## 9. Limitations and restrictions

A consolidated list of what this session **cannot do**, by design:

| Limitation | Cause | Workaround |
|---|---|---|
| No address-to-station routing | `rail-focused` OSM strips driveable streets | Use station dropdowns; future: integrate a separate address-geocoder layer |
| No bus / tram / cable car / boat / funicular | rail-only GTFS filter (§3) | Out of scope for a UIC/MERITS rail demonstrator; build a separate `nap-ch-multimodal` session if needed |
| Some 2-minute transfers may appear infeasible | `TransferConstraints` disabled (§5.2) | Raise global `transferSlack` to `5m` (§5.3) |
| 3+ leg journeys may show non-optimal transfer station | `OptimizeTransfers` disabled (§5.2) | Negligible for demo use; user sees a working itinerary, just not always the absolute best one |
| Station names are German/French/Italian only | SBB GTFS convention | Tell users to type local-language names; consider a UI hint |
| No GTFS-RT real-time updates wired in | Default router-config.json points to SNCF GTFS-RT URLs which aren't valid for SBB | Manually edit the per-graph `router-config.json` to add SBB GTFS-RT endpoints; or remove the SNCF updaters which will fail silently anyway |
| "Serving" badge appears 3–5 min before search actually works | UI badge tied to state transition, not container health | Wait for `Grizzly server running.` log line before testing searches |
| GTFS-RT requires bearer-token endpoints on opentransportdata.swiss | Auth-gated CKAN resources | Out of scope for v0.1.32; document in §10.x if/when implemented |
| Weekly feed rotation requires manual download + filter + stage | No DCAT-AP autocrawl for `opentransportdata.swiss` | Script the filter + scp + restage as a cron job if rotation cadence becomes painful |
| ~~Small / border stations like Travers, Pontarlier, Les Verrières return `LOCATION_NOT_FOUND`~~ — **fixed in v0.1.34** | `rail-focused` PruneIslands removed the walking-graph islands those stops sat on; the lat/lon snap then fails | The journey UI sends `master_stations.uic` alongside lat/lon. The server routes via OTP's `planConnection` query using a `stopLocation` input (`SBB:<uic>`), which bypasses the walk-graph snap entirely, and transparently falls back to coordinate routing for feeds where the naive UIC-based stop id doesn't resolve. See §9.1 for the mechanism and its constraints. |

### 9.1 Stop-id routing (v0.1.34)

The CH session would otherwise fail for any station that PruneIslands
left disconnected from the walking graph — roughly 50% of SBB rail
stops after rail-focused OSM filtering. The walking graph is irrelevant
for station-to-station rail routing, so the journey API bypasses the
lat/lon → walk-graph snap entirely when the operator picked a station
from the master_stations dropdown.

> **History — why `planConnection`, not `plan`.** The first attempt
> (v0.1.33, reverted in #76) passed `{stopId: …}` to OTP's legacy
> `plan` query. OTP rejects that — `plan`'s `InputCoordinates` requires
> `lat`/`lon` and has no `stopId` field. The correct API is OTP 2.9's
> `planConnection`, whose `PlanLocationInput` accepts **either** a
> `coordinate` **or** a `stopLocation`. This was verified against the
> live OTP 2.9 schema before re-implementation — see the lesson in the
> project memory note.

**Mechanism**, end-to-end:

1. **UI** captures `master_stations.uic` on dropdown pick (hidden input),
   includes it in the fanout request body, clears it on text edit.
2. **Fanout API** receives `{from: {lat, lon, label, uic}, to: ...}`.
3. **Per session**, `_query_session` derives the primary OTP feedId from
   `session.config.sources.providers[0].id` and builds a candidate stop
   id `<feedId>:<uic>` (e.g. `SBB:8771500` for Pontarlier). It also
   passes the session's `otp_timezone` so a naive depart time is
   localised correctly (see below).
4. **OTP `planConnection`** is called with
   `origin: {location: {stopLocation: {stopLocationId: "<feedId>:<uic>"}}}`
   instead of a `coordinate`. The walk-graph snap is skipped.
5. **Fallback**: if OTP returns empty `edges` with a
   `LOCATION_NOT_FOUND` routing error, `otp_client` retries once with
   `coordinate` inputs. Other routing errors
   (`NO_TRANSIT_CONNECTION_IN_SEARCH_WINDOW`, etc.) do **not** trigger a
   retry — those mean OTP located both endpoints and routed, so a
   coordinate retry of the same endpoints wouldn't change the answer.

**Timezone note.** The UI's `datetime-local` input yields a *naive*
depart time. The legacy `plan` query sent bare date+time strings, which
OTP interpreted in the graph's own timezone. `planConnection`'s
`earliestDeparture` is an `OffsetDateTime` and needs an explicit offset
— so `otp_client._earliest_departure` localises a naive time to the
session's `otp_timezone`, preserving the "operator picks 12:51 → OTP
searches 12:51 graph-local" semantics. A tz-aware time (e.g. the
"now" default) is used as-is.

**Works when:**

- The session has at least one provider configured (single-provider
  sessions like `nap-ch-rail` and `nap-fr-rail` are the common case).
- The feed keys stops by UIC code (true for SBB and most CH/DE national
  feeds; **false** for SNCF, which uses `OCETrain-NNNNNNNN`-style ids —
  there the coordinate fallback kicks in and preserves existing
  behaviour).
- The operator picked the station from the autocomplete dropdown (so
  `uic` is populated). Searches without a `uic` route by coordinate,
  unchanged.

**Does NOT work when:**

- The session is multi-provider AND the requested station belongs to a
  **second-or-later** provider. The naive `<first-feed-id>:<uic>`
  construction returns `LOCATION_NOT_FOUND`, the coordinate fallback
  takes over — which may then still fail for small/border stations.
  Acceptable trade-off; a future per-session UIC→stop_id index (built
  by querying OTP at serving-state transition) would route exactly
  across feeds.
- The feed keys stops by an operator code rather than the UIC. We rely
  on UIC == GTFS stop_id, which holds for SBB. If a future feed uses
  operator codes, `_stop_id_for` would need to consult
  `master_stations.other_codes`.

**Verifying it works** (against the live OTP — this is the exact query
shape the server sends):

```bash
curl -sk https://localhost/otp/nap-ch-rail/gtfs/v1 \
  -H "Content-Type: application/json" \
  -d '{"query":"{ planConnection(origin:{location:{stopLocation:{stopLocationId:\"SBB:Parent8771500\"}}}, destination:{location:{stopLocation:{stopLocationId:\"SBB:Parent8504215\"}}}, dateTime:{earliestDeparture:\"2026-05-18T06:00:00+02:00\"}, searchWindow:\"PT8H\", first:3) { edges { node { start end duration legs { mode from { name } to { name } route { shortName } } } } routingErrors { code description } } }"}' \
  | python3 -m json.tool
```

A populated `edges` array (the RE9 ~11:03→11:28 Pontarlier → Travers
service) confirms stop-id routing works at the OTP level. The journey
UI test is then: type "Pontarlier" → pick from dropdown → type
"Travers" → pick → Search — should return the same itinerary instead of
the previous empty result.

Note the endpoint is `/otp/nap-ch-rail/gtfs/v1` — the modern GTFS
GraphQL endpoint VIATOR uses. The legacy
`/otp/nap-ch-rail/routers/default/index/graphql` path is only good for
ad-hoc `stops(name:…)` lookups; it does not serve `planConnection`.

---

## 10. Quick-reference commands

```bash
# Session config view
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "SELECT id, state, config->>'osm_scope' AS scope, config->>'otp_build_heap' AS bh,
          config->>'otp_heap' AS sh, config->>'otp_timezone' AS tz
   FROM sessions WHERE id='nap-ch-rail';"

# Container heap actually in use
docker exec viator-otp-nap-ch-rail-1 ps -ef | grep java | head -1

# Inbox content
sudo ls -lh /var/lib/docker/volumes/viator_inbox/_data/nap-ch-rail/{osm,gtfs}/

# otp-config.json (the feature-flag file we placed manually)
sudo cat /var/lib/docker/volumes/viator_graphs/_data/nap-ch-rail/current/otp-config.json

# Container health
docker ps --filter "name=nap-ch-rail" --format "table {{.Names}}\t{{.Status}}"

# Boot progression
docker logs viator-otp-nap-ch-rail-1 2>&1 | \
  grep -iE "grizzly|outofmemory|mapping complete|Graph loaded|Transit loaded" | tail -10

# Rerun the GTFS filter (replace feed without rebuilding from scratch)
cd /tmp/sbb-filter
python3 ./filter.py    # or paste the §3.3 script

# Trigger rebuild from CLI (alternative to UI button)
docker compose -p viator exec postgres psql -U viator -d viator -c \
  "INSERT INTO rebuild_jobs (session_id, status) VALUES ('nap-ch-rail', 'pending');"
```

---

## 11. References

- [docs/multi-country-runbook.md](multi-country-runbook.md) — heap budgets, OSM scope, file-name contracts, problem determination
- [docs/nap-fr-rail.md](nap-fr-rail.md) — the FR session (provider-bundle model, country gate, GTFS-RT plumbing)
- [docker/otp/router-config.json](../docker/otp/router-config.json) — global `transferSlack` setting
- [docker/otp/entrypoint.sh](../docker/otp/entrypoint.sh) — OSM filter integration, streetGraph cache
- [app/osm_filter.py](../app/osm_filter.py) — canonical `rail-focused` tag-filter preset
- OTP feature flags reference: <https://docs.opentripplanner.org/en/latest/Configuration/#otp-features>
- SBB GTFS source: <https://opentransportdata.swiss/dataset/?keywords_filter=gtfs>

---

## Changelog

- **2026-05-14** — §9.1 stop-id routing re-implemented against OTP's
  `planConnection` query (v0.1.34). The v0.1.33 attempt used the legacy
  `plan` query with a `{stopId}` input, which OTP rejects; it was
  reverted in #76. `planConnection`'s `stopLocation` input is the
  correct API, verified against the live OTP 2.9 schema before
  re-implementation.
- **2026-05-13** — Initial version. Documents the rail-only filter +
  feature-flag-override pattern discovered while bringing `nap-ch-rail`
  to a serving state on the `vmi3259514` VPS.
