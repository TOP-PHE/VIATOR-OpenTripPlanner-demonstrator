# OSM Geographic Scope — design proposal

A design to crop the OSM street network a session builds to **only the
countries its providers actually serve**, instead of carrying a whole
continental extract. The served countries are **auto-detected from the
providers' GTFS stops** and presented as a country **checkbox list** the
operator can adjust (e.g. add a neighbour for a cross-border corridor).

The goal is to make **graph build memory proportional to the scope the
session actually routes**, not to the size of the uploaded PBF. An
all-Europe rail build OOM-kills on commodity hardware even after the
rail-focused tag filter (a ~2.2 GB filtered PBF still blows past a 12 GB
container during the street-graph phase); a France + Switzerland corridor
needs a fraction of that.

**Status**: Proposal. Phase 1 (manual country checkboxes + build-time
geo-crop) is independently shippable and is the memory win. Phase 2
(auto-detect served countries from GTFS) is the user-friendly default on
top. Phase 3 (source per-country PBFs instead of cropping a continental
one) is an efficiency follow-up.
**Audience**: Platform admins, demonstrator product owners, implementers.

> Pairs with:
> - [cross-nap-federation-design.md](cross-nap-federation-design.md) — this is
>   the per-session counterpart to federation: a single corridors session
>   right-sized to its countries, rather than splitting into per-country
>   sessions.
> - **v0.1.38 build-memory work** (`app/otp_heap.py`, the max-memory rebuild) —
>   that made the build container's cgroup cap track the heap and added a
>   brute-force "free the whole box" escape hatch. This proposal is the
>   *principled* complement: shrink the input so the brute force is rarely
>   needed.

---

## 1. Motivation

### 1.1 The problem

OTP graph builds are RAM-bound by the OSM PBF (see `app/osm_filter.py`).
Today VIATOR has **one** axis of OSM reduction — a **tag** filter
(`session.config.osm_scope`, applied by `osmium tags-filter` in
`docker/otp/entrypoint.sh`):

| scope | what it keeps | size |
|---|---|---|
| `transit-focused` (default) | walking/cycling/transit ways + rail | ~60% of raw |
| `rail-focused` | rail + footways only, **no driving infra** | ~20% of raw |
| `comprehensive` | everything | 100% |

`rail-focused` is the smallest, and the doc already says it's "the only
scope that lets a 10-country European merge fit in ~24-28 GB build heap on
a 47 GB box." But a single-VPS demonstrator often can't give the build
24-28 GB, and **all-Europe rail-focused is still huge** — the failing build
that prompted this was a ~2.2 GB rail-focused EU PBF that OOM-killed during
`--buildStreet`.

The tag filter answers **"what kinds of ways?"**. It cannot answer **"where?"**
— so a session that only routes Paris↔Genève still ingests the street
network of Portugal, Poland, and Finland.

### 1.2 The key finding — geographic crop is *correct*, not just cheaper

For VIATOR's actual flow — **station-to-station rail** — OTP uses OSM only
to:
- snap a search's origin/destination coordinates onto a station, and
- compute the short walk between platforms within an interchange.

The **train travel between stations comes from the GTFS**, not from OSM. So
the street network outside the served countries is data OTP *never traverses*
for these flows. Cropping it away removes nothing the demonstrator routes —
it is the geographic equivalent of what `rail-focused` already does on the
tag axis. (The one capability it removes — free-text address routing across
the dropped countries — is already out of scope under `rail-focused`, which
drops driveable roads everywhere.)

### 1.3 Two orthogonal axes

```
                tag scope (what)          ×     geographic scope (where)
        transit-focused / rail-focused          {FR, CH} / {FR, CH, DE, IT} / …
                          └──────────────┬──────────────┘
                              osmium tags-filter ∘ osmium extract
```

`rail-focused × {FR, CH}` is a **tiny** graph — the combination this
proposal unlocks.

---

## 2. The toolchain already supports it

The otp-build image already ships **osmium-tool** and the entrypoint already
runs `osmium tags-filter`. The same binary does geographic extraction:

```bash
osmium extract --polygon served-countries.geojson -o cropped.pbf input.pbf
```

So the build pipeline gains one stage, run **before** the tag filter (crop
first — it removes the most data; then tag-filter the remainder):

```
raw PBF
  → osmium extract  (served-countries polygon)     ← NEW
  → osmium tags-filter (rail-focused / …)            ← today
  → OTP --buildStreet
```

One new **data dependency**: a simplified country-boundary GeoJSON (Natural
Earth admin-0, ODbL/public-domain, a few hundred KB simplified) baked into
the otp image. It serves **both** the crop (polygon for `osmium extract`)
**and** the auto-detect (point-in-polygon for stops → country).

---

## 3. Design

### 3.1 New session config field: `osm_countries`

A list of ISO-3166-1 alpha-2 country codes, e.g. `["FR", "CH"]`. Empty /
absent ⇒ **no geographic crop** (today's behaviour — legacy sessions build
unchanged). Validated like the existing scope fields:

- new `app/osm_geo.py` (mirrors `app/osm_filter.py`): `COUNTRIES` (the
  supported list + display names), `validate_countries(value) -> list[str]`
  (rejects unknown codes, normalises case/order, dedupes).
- validated in `patch_session` (`app/api/admin/sessions.py`) alongside
  `osm_scope` / `otp_timezone` / `otp_build_heap`, raising `400` on a bad
  code.

### 3.2 Entrypoint geo-crop stage

`docker/otp/entrypoint.sh` gains a stage gated on a new
`OTP_OSM_COUNTRIES` env (CSV of ISO codes), passed by the worker exactly
like `OTP_OSM_SCOPE` is today:

- if `OTP_OSM_COUNTRIES` is empty → skip (no crop).
- else build a polygon from the union of those countries (a small Python
  helper, or a pre-split per-country `.poly`/GeoJSON set shipped in the
  image) and run `osmium extract --polygon … -o cropped.pbf` **before** the
  existing `tags-filter` case.
- log before/after sizes (same pattern as the tag filter's `X → Y bytes`).

`app/worker.py::run_build` resolves `osm_countries` from `session.config`
(next to `osm_scope`) and adds `-e OTP_OSM_COUNTRIES=FR,CH` to the
`docker compose run`.

### 3.3 streetGraph cache key

The entrypoint caches `streetGraph.obj` keyed on `sha256(osm.pbf):<scope>`
(§ "streetGraph.obj cache" in `entrypoint.sh`). The crop changes the
effective street input, so the **key must include the country set** —
e.g. `sha256(osm.pbf):<scope>:<sorted-countries>`. Otherwise toggling
countries would silently reuse a stale graph.

### 3.4 Auto-detect served countries (Phase 2)

When the operator opens the Configure form (or on refresh), VIATOR proposes
the served set by reading each provider's staged GTFS:

1. open `inbox/<sid>/gtfs/*.zip`, read `stops.txt` (stdlib `zipfile` + `csv`).
2. for each stop with `stop_lat`/`stop_lon`, resolve the country by
   **point-in-polygon** against the shipped boundary GeoJSON (a vendored
   ray-cast — no heavy geo dependency).
3. **cross-check** with the UIC stop-code prefix where the `stop_id` carries
   one (`87`=FR, `85`=CH, `80`=DE, `83`=IT, `88`=BE, `82`=LU, …). VIATOR
   already has this prefix→country map in
   `app/gtfs_cross_border_filter.py` (`UIC_COUNTRY_NAMES`) and parses UIC out
   of stop ids in `app/journey/signature.py`.
4. return the set of countries that have ≥ N stops (a small threshold avoids
   a single mis-geocoded stop dragging in a whole country).

Surfaced via `GET /api/sessions/{sid}/osm-countries/suggest` →
`{"detected": ["FR","CH"], "stops_by_country": {...}}`. Used only to
**pre-check** the UI; the saved value is always the operator's explicit
choice.

### 3.5 UI — country checklist

The Configure form (`app/templates/admin/sessions.html`), next to the
existing **OSM scope** dropdown, gains a **Countries** checklist:

- grouped, ordered list: EU-27 + EFTA (CH, NO, IS, LI) + UK as v1 (see
  open question 6.1).
- **auto-detected countries pre-checked** (from §3.4), with a small badge
  ("detected from N stops"); operator can tick neighbours for cross-border
  corridors or untick to shrink further.
- a hint showing the trade-off ("fewer countries → faster build, less RAM;
  add neighbours only if a route crosses them with a stop there").
- posts `osm_countries` in the same `PATCH /api/sessions/{sid}` config save.

---

## 4. Worked example

`corridors-fr-ch` with providers `SNCF-XB` + `SBB-XB`:

| step | result |
|---|---|
| auto-detect | stops resolve to **FR, CH** (+ a handful of DE/IT border stops below threshold) |
| operator | accepts FR + CH; ticks **DE** too because a Lyria branch calls Freiburg |
| build | `osmium extract {FR,CH,DE}` → `tags-filter rail-focused` → OTP |
| effect | street graph ≈ 3 countries' rail+footways instead of all-Europe — fits a modest heap; max-memory rebuild no longer required |

---

## 5. Alternatives considered

- **Source per-country PBFs (Phase 3).** Instead of cropping a continental
  PBF, download only the checked countries' extracts from Geofabrik and
  `osmium merge`. Best for bandwidth/disk (never fetch the 28 GB EU file),
  but changes the refresh/source logic. Deferred — build-time crop works
  with whatever PBF is already staged and is the smaller first change.
- **Buffer-around-stops crop.** Crop to a few-km buffer around the actual
  stop coordinates — the absolute minimum graph. Smallest of all, but less
  legible to an operator than countries, and brittle if a station is added
  without re-cropping. Possible future "ultra-compact" scope; countries are
  the intuitive default.
- **Just raise RAM / always use max-memory rebuild.** The v0.1.38 escape
  hatch. Brute force; doesn't scale to genuinely continental graphs and
  monopolises the box. Keep it as a fallback, not the everyday path.

---

## 6. Open questions

1. **Country list scope.** v1 = EU-27 + EFTA + UK. Include Balkans / Turkey /
   Ukraine / Morocco now, or add on demand? (Affects the shipped GeoJSON
   size.)
2. **Boundary polygon fidelity.** Simplified Natural Earth is fine for
   cropping (a few km of slop at borders is harmless — we keep slightly more
   than needed). Confirm the simplification doesn't drop small but served
   territories (e.g. Monaco, Liechtenstein, Luxembourg).
3. **Detection threshold.** How many stops in a country before it's
   pre-checked? (Proposal: ≥ 1% of stops or ≥ 5 stops, whichever is larger.)
4. **Interaction with `comprehensive` tag scope.** Geo-crop is orthogonal and
   should still apply (crop a comprehensive PBF to the countries). Confirm
   the entrypoint runs the crop even when `tags-filter` is skipped.
5. **Per-stop coordinate trust.** Some feeds put `(0,0)` or imprecise coords
   on a few stops; the threshold (Q3) and the UIC cross-check (§3.4) both
   guard against a stray coordinate dragging in a country.

---

## 7. Phasing

| phase | scope | independently useful? |
|---|---|---|
| **1** | `osm_countries` config + validation, entrypoint geo-crop stage, cache-key fix, country checklist UI (manual) | yes — the memory win |
| **2** | auto-detect from GTFS stops + UIC cross-check, pre-checked UI | yes — the UX win |
| **3** | source per-country PBFs + merge (skip the continental download) | yes — bandwidth/disk |

---

## 8. Validation / testing

- **Unit** — `validate_countries` (good/bad/dup/case codes); the
  point-in-polygon detector against fixture stops with known countries
  (incl. a border stop and a `(0,0)` stop); the cache-key composition.
- **Pure** — country-set → polygon selection (no osmium needed); the
  CSV→countries plumbing for the entrypoint env.
- **Integration** — `POST/PATCH` round-trip of `osm_countries`; the
  `/suggest` endpoint against a small fixture GTFS.
- **Manual / out-of-CI** — the `osmium extract` step itself (docker-only,
  same as today's tag filter): build a corridors session with `{FR,CH}` and
  confirm the street-graph phase fits a small heap and Paris↔Genève still
  routes.
