# OJP Reference Comparison — design proposal

A design for an **opt-in journey-search comparison** feature: when an
operator runs a search in the VIATOR journey UI, they can tick a box to
*also* send the same search to an external **reference Open Journey
Planner (OJP) endpoint** — the Swiss national one at
`opentransportdata.swiss` — and see the reference itineraries
side-by-side with VIATOR's own OTP results.

The point is **validation**: VIATOR routes on OpenTripPlanner graphs
built from filtered GTFS + OSM. There's currently no oracle to answer
"is that itinerary actually *right*?" The reference OJP endpoint is a
sanctioned, standards-based answer key.

**Status**: Phase 0 (verification spike) ✅ done · Phase 1 (MVP)
implemented — see the changelog. Phases 2–3 remain future work items.
This doc started as a proposal and now also serves as the
implementation reference.
**Audience**: Platform admins, demonstrator product owners, future
implementers.

> This pairs with [VIATOR_Federation_Strategy.md](VIATOR_Federation_Strategy.md):
> that doc covers VIATOR's **NAP-vs-MERITS** comparison (two data
> *sources*, both routed through VIATOR's own OTP). This doc adds an
> orthogonal third axis — **VIATOR-vs-reference-engine** — comparing
> VIATOR's routing against an independent OJP implementation. The two
> are complementary: one tests *data*, the other tests *routing*.

---

## 1. Motivation

### 1.1 The problem

VIATOR produces itineraries from OTP graphs built on heavily-processed
inputs: GTFS feeds filtered to rail-only (see
[nap-ch-rail.md §3](nap-ch-rail.md)), OSM filtered to `rail-focused`,
OTP feature flags disabled to fit commodity hardware. Every one of those
processing steps is a place results can quietly drift from reality.

There is no oracle. When the CH session returned an empty result for
Pontarlier→Travers (the bug that drove v0.1.34's stop-id routing), the
only way to know VIATOR was *wrong* — rather than there genuinely being
no train — was to manually cross-check `sbb.ch`. That manual
cross-check is exactly what this feature automates.

### 1.2 What "good" looks like

An operator runs a search, ticks **"Compare with Swiss OJP reference"**,
and sees:

- VIATOR's OTP itineraries (as today), **and**
- the reference OJP endpoint's itineraries for the same origin /
  destination / time,

rendered side-by-side, with obvious divergences flagged (different
duration, different route, itinerary present in one but not the other).
The operator forms a judgement in seconds instead of alt-tabbing to
`sbb.ch`.

### 1.3 Goals / non-goals

**Goals**

- Opt-in, per-search comparison against a reference OJP endpoint.
- Reuse VIATOR's existing multi-source result-merge UI as much as
  possible.
- Graceful degradation: if the reference endpoint is slow / down /
  rate-limited, VIATOR's own results still render; the reference panel
  shows "unavailable".
- Recorded for audit/replay, like every other VIATOR search.

**Non-goals**

- Not a routing *fallback* — VIATOR never *serves* OJP results as its
  own; they are clearly labelled "reference".
- Not always-on — see §6 rate limits; this is an operator validation
  tool, not a per-user production feature.
- Not (initially) a structured automated diff / scoring — Phase 1 is
  eyeball side-by-side. Structured diff is Phase 2 (§9).

---

## 2. Why OJP, why opentransportdata.swiss

| Option | Verdict |
|---|---|
| **opentransportdata.swiss OJP 2.0** | ✅ **Chosen.** Sanctioned public API, free tier, documented, open-source reference client to model from. It *is* the OJP standard VIATOR exists to demonstrate. |
| `bahn.de` "vendo" endpoint | ❌ No official API — reverse-engineered endpoint behind the bahn.de frontend. Brittle, no SLA, ToS-grey. Acceptable only as a future best-effort add-on if DE-network coverage is specifically needed. |
| Google Directions / Rome2Rio | ❌ Paid, restrictive ToS, not transit-standards-based. |
| Other NAPs' OJP endpoints (DELFI / France / …) | ⏳ Phase 3 — same adapter, different endpoint + token. Out of scope for the first cut. |

Choosing the OJP endpoint isn't a compromise — it's **on-mission**.
"VIATOR (OTP) vs the reference OJP implementation" is a meaningful thing
for an OJP demonstrator to show.

---

## 3. The reference API

Verified 2026-05-14 from the opentransportdata.swiss documentation:

| Property | Value |
|---|---|
| Endpoint | `https://api.opentransportdata.swiss/ojp20` — both the cookbook and the official regression-test collection use `/ojp20` for OJP 2.0. (An older `/ojp2020` path exists for the legacy "OJP 2020 beta". Phase 0 confirms.) |
| Standard | OJP 2.0 — `CEN/TS 17118`, SIRI-family |
| Protocol | **XML** request / XML response (`POST`) |
| Headers | `Authorization: Bearer <token>`, `Content-Type: text/xml` (the regression suite uses `text/xml`; the cookbook says `application/xml` — `text/xml` is the safer first choice) |
| Auth | Bearer token, free, from the [dev-dashboard](https://dev-dashboard.opentransportdata.swiss/) |
| Rate limits (free tier) | **50 requests/min, 20 000 requests/day** per key |
| Capabilities | Trip planning coordinate-to-coordinate, address-to-address, stop-to-stop; departures/arrivals boards |
| Reference material | [`openTdataCH/ojp-demo-app`](https://opentdatach.github.io/ojp-demo-app/) (open-source demo) · [`openTdataCH/ojp-tests-public`](https://github.com/openTdataCH/ojp-tests-public) (Bruno regression collection — **the source of the verified request/response shapes in Appendix A**) · [`openTdataCH/ojp-adapter`](https://github.com/openTdataCH/ojp-adapter) (**Java** — not usable from VIATOR's Python) |

**Key consequence — it's XML, and there's no Python helper library.**
VIATOR's OTP integration is JSON / GraphQL; the reference comparator
needs its own adapter that speaks OJP XML, and `ojp-adapter` is Java so
we **hand-roll the small `TripRequest` subset** (and parse `TripResult`)
with `lxml` / `xml.etree`. The concrete request and response shapes are
verified and captured in **Appendix A** — pulled from the official
regression-test collection, not guessed.

---

## 4. User experience

### 4.1 The toggle

A single checkbox in the journey search form
([app/templates/journey.html](../app/templates/journey.html)), below
the Depart field, next to the Search button:

```
[ ] Compare with Swiss OJP reference
```

- **Hidden entirely** unless the feature is enabled platform-wide
  (§7 config) — most operators never see it.
- **Disabled with a tooltip** if enabled platform-wide but no OJP
  credential is configured ("Add a Swiss OJP token in Credentials").
- Unchecked by default — opt-in every time, deliberately (rate limits,
  §6).

### 4.2 Results layout

Reuse the existing results model. VIATOR's `/api/journey/fanout`
already merges multiple sources and tags each itinerary with an
`origin_flag` (`ALL` / `NAP_ONLY` / `MERITS_ONLY` / `SUBSET` — see
[app/api/journey.py](../app/api/journey.py) `_origin_flag`). The
reference comparison extends this:

- A new **"Reference (Swiss OJP)"** panel/column alongside the existing
  results, OR a new `origin_flag` value `OJP_REFERENCE` if results are
  merged into one list. (Phase-1 decision — see §10.)
- The summary strip gains a third entry, e.g.
  `nap-ch-rail: 1 trip · ojp-reference: 2 trips (Δ duration: −12m on best)`.
- Divergence cues: duration delta vs VIATOR's best, "route differs",
  "VIATOR found no equivalent" / "reference found no equivalent".

### 4.3 What the operator does with it

Eyeballs it. "VIATOR says 2h57m Pontarlier→Genève via Neuchâtel+
Lausanne; reference says 2h45m via the same — close enough, VIATOR's
data is sane." Or: "reference found a direct option VIATOR didn't —
investigate the GTFS filter." Structured scoring comes later (Phase 2).

---

## 5. Architecture

### 5.1 Component shape

A new module `app/journey/ojp_client.py`, sibling to the existing
`otp_client.py`:

```
journey UI  ──fanout body { ..., compare_ojp: true }──▶  /api/journey/fanout
                                                              │
                          ┌───────────────────────────────────┤
                          ▼                                   ▼
                  otp_client.fetch_plan                ojp_client.fetch_reference
                  (per serving session,                (single call to the
                   GraphQL/JSON — today)                opentransportdata.swiss
                          │                             OJP 2.0 endpoint, XML)
                          ▼                                   ▼
                  normalise → trips[]                  normalise → trips[]
                          └───────────────┬───────────────────┘
                                          ▼
                            merge + origin_flag + record + return
```

`ojp_client.fetch_reference` mirrors `otp_client.fetch_plan`'s contract
— takes the same `from`/`to`/`when` inputs, returns
`(raw_response, trips)` in **the same normalised `trips` shape** the
recorder and UI already consume. That shared shape is what lets the
existing compare UI render reference itineraries with zero new
rendering code.

### 5.2 The OJP adapter

`ojp_client.fetch_reference` does:

1. Build an OJP `TripRequest` XML document from the search inputs
   (§8 mapping).
2. `POST` it to the configured endpoint with the bearer token.
3. Parse the OJP `TripResult` XML response.
4. Normalise into VIATOR's `trips[]` shape.

Error handling mirrors `otp_client`: transport / HTTP / parse failures
return an `error` status for the reference source only — VIATOR's own
results are unaffected.

### 5.3 Where it plugs into the request flow

Two viable shapes; the doc recommends **(a)**:

- **(a) Optional branch inside `/api/journey/fanout`** — the request
  body carries `compare_ojp: bool`. When true, the fanout `asyncio
  .gather` set gains one more coroutine (`ojp_client.fetch_reference`)
  alongside the per-session OTP calls. One round-trip from the UI, one
  merged response, recorder sees it as one search. Lowest UI churn.
- **(b) Separate `/api/journey/compare-ojp` endpoint** — the UI fires
  it in parallel with the normal fanout. Cleaner separation, but two
  round-trips and two records to stitch in the UI.

(a) reuses the existing concurrency/recording machinery and matches how
VIATOR already thinks about multi-source search. (b) is only better if
the reference call's latency profile is wildly different and we don't
want it blocking the fanout response — mitigated by a tight per-source
timeout (§6).

### 5.4 Recording

**Phase 1 does NOT persist the reference result** — it's returned live
in the fanout response (`ojp_reference`) for display only, and dropped
when the response is sent.

Why: `journey_search_executions.session_id` **is** an FK to
`sessions.id` (confirmed — `app/models/search.py`). Recording an
`ojp-reference` execution row would need either an FK relaxation +
migration or a seeded non-serving `sessions` row — both bigger than the
MVP warrants. Live-only display is genuinely useful on its own (the
operator gets the side-by-side), and persistence pairs naturally with
the Phase 2 structured-diff work anyway (§9), where the schema question
gets answered properly. So: Phase 1 = live compare; Phase 2 = persist +
diff.

---

## 6. Rate limits, latency, failure modes

| Concern | Handling |
|---|---|
| **50 req/min, 20K/day** | Opt-in per search keeps this manageable. Note: v0.1.35.06's anchor-time pagination (§9.2) issues **up to 4 OJP calls per search**, so an operator doing one search per second peaks at ~4 calls/sec ≈ 240/min — well past the limit. The 4-page cap + per-search opt-in is the design safety net. |
| **Latency** | Reference call gets its own timeout — `OJP_TIMEOUT_MS` (§7), default 10 s, **per page**. With pagination, total wall-time can reach `4 × OJP_TIMEOUT_MS` worst-case; in practice each page is ~600 ms so a 4-page fetch lands in ~2.5 s. The fanout runs OJP and OTP in parallel so this is the max of the two, not the sum. |
| **Endpoint down / 5xx** | Page 1 failure → propagate to caller, surface as `error`. Later-page failure → **swallow + return partial**, keep what we already have. |
| **Rate-limit hit (429)** | Same model as endpoint-down. Page 1 hits → `rate_limited` status. Later page hits → return earlier pages' trips with a partial-data warning logged. |
| **Burst protection** | The existing `concurrency` semaphores gate journey calls; each OJP page counts as one call against the same budget. |

---

## 7. Configuration & credentials

All of it goes through VIATOR's existing schema-driven platform config
([app/config_schema.py](../app/config_schema.py) → `config_service` →
the `/config` admin page) — no new infrastructure, no new storage, no
new API. **Shipped ahead of the rest of the feature** (it's independent
of the adapter and lets the operator stage the token safely instead of
pasting it around):

| Config key | Type | Default | Purpose |
|---|---|---|---|
| `OJP_COMPARISON_ENABLED` | bool | `false` | Feature toggle. While off, the journey-UI checkbox and the fanout branch don't exist. |
| `OJP_API_ENDPOINT` | str | `https://api.opentransportdata.swiss/ojp20` | The reference OJP 2.0 endpoint. |
| `OJP_API_TOKEN` | **secret** | `""` | The bearer token. A `secret` field — masked in GET responses with the `********` sentinel, never in audit metadata. Exactly the `SMTP_PASS` precedent: a *platform-level* secret, so it lives in `CONFIG_SCHEMA` — **not** the per-provider credential vault, which is for provider *feed* credentials referenced by `credential_id`. |
| `OJP_TIMEOUT_MS` | int | `10000` | Timeout for the reference call (bounded 1000–60000). |

These render under a **"Swiss OJP comparison"** section on `/config`
automatically — the page is schema-driven (the section is registered
in `config.html`'s `SECTIONS`/`SECRETS`/`BOOLS` arrays). The feature
stays dormant until `OJP_COMPARISON_ENABLED` is `true` **and**
`OJP_API_TOKEN` is non-empty.

Optional later: a per-session "default this session's searches to
include the OJP comparison" flag — but per-search opt-in is the
Phase-1 model. A "Test connection" button on the config section (like
the existing SMTP test) is a natural Phase-1 add once the adapter
exists.

---

## 8. Data mapping

### 8.1 VIATOR search → OJP `TripRequest`

| VIATOR input | OJP `TripRequest` |
|---|---|
| `from.lat` / `from.lon` | `<Origin>` `<GeoPosition>` (`<Longitude>`/`<Latitude>`) |
| `to.lat` / `to.lon` | `<Destination>` `<GeoPosition>` |
| `depart_at` (naive, session-tz-localised) | `<DepArrTime>` (ISO-8601) — see note |
| `arrive_by` | `<DepArrTime>` with arrival semantics |

**Phase 1 uses coordinates, not stop refs.** OJP also accepts
`StopPlaceRef`, but Swiss OJP uses its own DIDOK/SBB place references —
mapping VIATOR's `master_stations.uic` to those is a separate problem
(§10). Coordinates sidestep it entirely and are good enough for a
side-by-side sanity check. (Note the irony: VIATOR's *own* routing now
prefers stop-id — but the *reference* call can stay coordinate-based;
they're independent.)

Timezone: reuse the same naive→session-tz localisation logic
v0.1.34 added for `planConnection`
(`otp_client._earliest_departure`) — OJP `DepArrTime` also wants an
explicit offset.

### 8.2 OJP `TripResult` → VIATOR `trips[]`

OJP `<TripResult>` / `<Trip>` / `<TripLeg>` maps onto the existing
normalised trip dict (the shape `otp_client._normalise` produces and
the recorder + UI consume):

| OJP | VIATOR `trips[]` field |
|---|---|
| `<Trip>` `<Duration>` | `duration_seconds` |
| `<Trip>` start / end times | `departure_at` / `arrival_at` (UTC ISO) |
| `<TripLeg>` count (timed legs) | `num_transfers` |
| `<TimedLeg>` `<Service>` mode | `legs[].mode` |
| `<TimedLeg>` board / alight + times | `legs[].from_*` / `to_*` / `departure` / `arrival` |
| `<Service>` `<PublishedServiceName>` / line | `legs[].route_short_name` / `route_long_name` |
| `<Service>` operator | `legs[].agency_name` |
| — | `legs[].feed_id = "OJP"` (synthetic, for the operator badge) |

Anything OJP provides that VIATOR's shape has no slot for goes into
`_raw_itinerary` for the JSON inspector — same escape hatch the OTP
path already uses.

---

## 9. Comparison semantics

Phase 1 was **presentational** — show both, let the operator judge. The
UI computes a few cheap cues:

- **Δ duration** of each side's *best* (shortest) itinerary.
- **Coverage**: "reference returned N, VIATOR returned M".
- **Route-shape hint**: do the best itineraries share their transit
  legs' route short-names in order? If not, flag "different route".

### 9.1 Phase 2 — structured diff *(shipped)*

Phase 2 adds **per-itinerary matching** as a server-side step before the
fanout response is rendered. Each trip — OTP-side and OJP-reference-side
— gets a stable 16-hex **transit fingerprint** (`app.journey.signature.
transit_fingerprint`), and the union of fingerprints is bucketed into
`common` / `otp_only` / `ojp_only`.

The fingerprint deliberately:

- **Strips walk and transfer legs** before hashing. OJP renders an
  explicit `Origin → access stop` walk in front of every transit leg
  and an `egress stop → Destination` walk after the last; OTP with
  stop-id routing emits the bare transit leg. Stripping walks is what
  makes those two engines' views of "the 08:31 IC1 Bern → Zürich"
  hash to the same value.
- **Uses UIC (parsed from stop_id) as the primary stop token** (v0.1.35.02
  on; v0.1.35.01 used lat/lon as primary — see §9.1.1 + §9.1.2). OTP
  embeds the full 7-digit UIC; OJP/opentransportdata.swiss uses the
  4-digit Swiss DSN (DiDok-Nummer, the trailing 4 digits of the UIC)
  which the parser reconstructs by prepending `850`:
  - OTP `SBB:8507000:0:7` → UIC `8507000` (direct)
  - OJP `ch:1:sloid:7000:4:7` → DSN 7000 → UIC `8507000` (reconstructed)
  - OTP `SBB:8501120:0:5` → UIC `8501120` (direct)
  - OJP `ch:1:sloid:1120:0:5` → DSN 1120 → UIC `8501120` (reconstructed)
  Same UIC ⇒ same token, regardless of platform suffix, namespace
  prefix, or the DSN/full-UIC distinction. This is the strongest
  cross-engine identifier we have for Swiss rail, and it sidesteps
  every coordinate-precision pitfall in one go. (The within-feed `trip_signature` helper still uses
  `stations_xref` + UIC via DB lookup; the cross-engine variant parses
  UIC from the stop_id string directly because `stations_xref` has no
  rows for the synthetic `OJP` reference feed.)
- **Falls back to coordinates rounded to 3 decimals (~110 m)** when
  the stop_id doesn't contain a 7-digit chunk (non-Swiss feeds,
  synthetic ids). Coarser than the within-feed signature's 4-dp
  precision because cross-feed centroid disagreement can reach
  ~100 m; 3-dp absorbs it while still distinguishing genuinely-
  different rail stations (Pontarlier vs Frasne are 15 km apart, even
  Zürich HB vs Zürich Stadelhofen are 700 m).
- **Returns `""` when the itinerary has no transit spine** (all-walk
  result). The bucketer treats `""` as *uncomparable* — never matches
  — so two walk-only trips from different engines don't accidentally
  collide.

#### 9.1.1 The v0.1.35.01 → v0.1.35.02 regression

v0.1.35.01 shipped Phase 2 using 4-decimal coordinate rounding (~11 m)
as the primary stop token, with no special handling for stop ids. Live
testing on a Pontarlier → Geneva search (via Frasne and Lausanne)
revealed a structural false-mismatch: OTP and OJP both returned the
same three trains (P38, TGV, IC1) at the same times, but the
fingerprint classified them as `otp_only` + `ojp_only` instead of
`common`.

Root cause: OTP returns *platform-precise* coordinates. The TGV
arrival at Lausanne CFF is bound to stop `SBB:8501120:0:5` (platform
5, lat/lon `46.5165829, 6.6290278`) and the IC1 departure is bound to
stop `SBB:8501120:0:4` (platform 4, lat/lon `46.5166695, 6.6290548`).
Those two platforms are 130 m apart inside the same station — and at
4-dp they round to **different** tokens (`46.5166, 6.6290` vs
`46.5167, 6.6291`). OJP returns a single station-centroid coordinate
for both legs that almost certainly doesn't match either of OTP's
platform-precise readings at 4-dp. So one of the two transit-leg
endpoints always differed, and the overall fingerprint diverged.

v0.1.35.02 fixes this by switching to a UIC-primary token: both
`SBB:8501120:0:5` and `SBB:8501120:0:4` and `ch:1:sloid:8501120:0:5`
all parse to UIC `8501120` and produce the identical token. The lat/lon
3-dp fallback only fires for endpoints without a parseable UIC chunk
(non-Swiss feeds, the no-stop-id endpoints of access/egress walks —
both of which are stripped before fingerprinting anyway).

#### 9.1.2 The v0.1.35.02 → v0.1.35.03 SLOID-DSN regression

v0.1.35.02 made an incorrect assumption about the OJP SLOID format. It
was based on opentransportdata.swiss documentation that suggested the
full UIC appears inside the SLOID (e.g. `ch:1:sloid:8501120:0:5`).
Live OJP data captured via the new `{}` button on OJP cards (shipped
in v0.1.35.02 specifically to debug these mismatches) showed the real
format **drops the `850` Swiss country/DiDok prefix**:

- Bern (UIC 8507000) → OJP SLOID `ch:1:sloid:7000:4:7`
- Geneva (UIC 8501008) → OJP SLOID `ch:1:sloid:1008:2:3`
- Lausanne (UIC 8501120) → OJP SLOID `ch:1:sloid:1120:0:5`

The trailing 4 digits are the Swiss DSN (DiDok-Nummer). v0.1.35.02's
regex required exactly 7 digits and silently fell through to the 3-dp
lat/lon fallback for every Swiss OJP id. The OTP side still parsed
cleanly to `UIC:8507000`, and the OJP side produced `46.949,7.437` —
different tokens, no match.

v0.1.35.03 extends `_uic_from_stop_id` to also recognise the 4-digit
DSN when the id is in the Swiss `ch:1:…` namespace, prepending `850`
to reconstruct the canonical UIC. Verified against Patrick's actual
captured Bern → Geneva IR15 JSON: both sides fingerprint to the same
16-hex token after the fix.

The parser strategy (7-or-8-digit first, then 4-digit-DSN-only-for-
ch:1 namespace) handles all the formats seen in live data:

| Source | Example stop_id | Parser path | Result |
|---|---|---|---|
| SBB / OTP (GTFS) | `SBB:8507000:0:7` | 7-digit regex | `UIC:8507000` |
| SBB cross-border | `SBB:8771500` | 7-digit regex | `UIC:8771500` |
| **SNCF (8-digit)** | `StopPoint:OCELyria-87686006` | 8-digit regex, drop check digit | `UIC:8768600` |
| OJP Swiss | `ch:1:sloid:7000:4:7` | DSN regex + `850` prefix | `UIC:8507000` |
| OJP cross-border | `ch:1:sloid:8771500:0:1` | 7-digit regex | `UIC:8771500` |
| Non-Swiss feed | `STIB:1234` | (no path matches) | None → lat/lon fallback |

#### 9.1.3 The v0.1.36 SNCF 8-digit / check-digit normalisation

The cross-NAP federation spike (comparing how SNCF and SBB each
describe TGV Lyria 9263, Paris → Genève) surfaced a UIC-encoding
mismatch that would break any SNCF↔SBB matching:

- **SBB** publishes 7-digit UICs: Genève = `8501008`.
- **SNCF** publishes the 8-digit form: Genève = `85010082` (the 7-digit
  UIC `8501008` plus a trailing **check digit** `2`).

So the same physical station appears as `85010082` (SNCF) and `8501008`
(SBB). The v0.1.35.x parser matched exactly 7 digits, so SNCF's 8-digit
codes fell through to the lat/lon fallback while SBB's matched on UIC —
two encodings of one station producing different fingerprint tokens.

v0.1.36 widens `_UIC_RE` to `\d{7,8}` and keeps the **first 7 digits**
(the 8-digit form is always UIC + check digit). Verified against the
live SNCF and SBB GTFS feeds: TGV Lyria 9263 — identical route name
(`622E`), identical times, identical stop sequence, differing only in
the UIC encoding — fingerprints to the same 16-hex token on both sides
after the fix. This is the prerequisite for cross-NAP federation result
dedup (the safety net beneath origin-country ownership; see
`docs/cross-nap-federation-design.md`).

The `_build_comparison` helper in `app/api/journey.py`:

1. Computes the OTP-side and OJP-side fingerprint lists.
2. Builds the set intersection (`common_set`) and the two
   complements.
3. Tags each merged trip dict in-place with a `"comparison"` field —
   `"common"`, `"otp_only"`, `"ojp_only"`, or `"uncomparable"`.
4. Returns `{"common": N, "otp_only": N, "ojp_only": N}` summary.

The fanout response gains an optional `comparison_summary` field; the
UI renders it as a strip above the cards (`{N} common · {N} OTP-only ·
{N} OJP-only`) and a small pill on each trip card colour-matched to its
bucket. The kebab-case mapping `tag.replace("_", "-")` between server
tag and CSS class is asserted in
`tests/unit/test_transit_fingerprint.py::test_comparison_tag_kebab_case_mapping`
so the contract is hard to drift.

**Out of scope for Phase 2:** persistence of the verdict for trend
analysis (still blocked on §5.4's `session_id` FK question — the synthetic
`OJP` session has no row in `sessions`). That's deferred to a follow-up
that introduces a `comparison_verdicts` table keyed by
`(otp_session_id, query_hash, fingerprint)` so the OJP side doesn't need
its own session row.

### 9.2 Anchor-time pagination *(v0.1.35.06)*

v0.1.35.01–05 left a structural alignment gap. OTP's `planConnection`
covers `searchWindow=21600s` (6 h at the v0.1.35.04 default), but OJP's
`TripRequest` has no `searchWindow` parameter — it returns a fixed
~6 alternative trips clustered around the requested time. On busy
corridors a single OJP request covers only ~2 hours, so the
comparison strip showed spurious `otp_only` itineraries for trains
in the 2–6 h tail of OTP's range.

v0.1.35.06 adds **anchor-time pagination** in
`app/journey/ojp_client.py::fetch_reference_paginated`. After the
first OJP response, if the latest `departure_at` is earlier than
`when + target_window_seconds`, the helper issues another
`TripRequest` anchored at `latest_dep + 1 min`. Repeats until any
stop condition fires (in order):

1. **Empty batch** — OJP exhausted at this anchor.
2. **All trips are duplicates** of earlier pages (matched by
   `transit_fingerprint`) — no forward progress.
3. **Latest departure caught up to the target window end** — OJP
   coverage matches OTP.
4. **`max_pages` reached** (hard cap, default 4 — rate-limit safety).
5. **`fetch_reference` raises** mid-flight — propagate if no partial
   data yet, otherwise return the partial set with a warning logged.

Dedup uses the same `transit_fingerprint` that powers
`_build_comparison`: boundary trips appearing in consecutive
batches (the last trip of batch N may equal the first trip of
batch N+1, slightly earlier than the +1 min nudge can avoid)
collapse to one fingerprint.

The fanout response gains a `pages` field on `ojp_reference` when
pagination fired (`pages > 1`), so the operator UI can show "OJP
took N requests to cover the search window" if desired. Pages are
**sequential** — the next anchor isn't known until the current
batch returns — so per-search wall-time can grow up to
`max_pages × per_page_latency`. The fanout still parallelises OJP
against the OTP fanout (`asyncio.gather`), so the user-visible
wall-time is `max(otp_total, ojp_paginated_total)`, not the sum.

Rate-limit math: with `max_pages=4`, a heavy operator running one
search per second hits ~4 OJP calls/sec ≈ 240/min, well past the
50/min free-tier ceiling. The 4-page cap + per-search opt-in
toggle is the design safety net. See §6.

---

## 10. Open questions

| # | Question | Status |
|---|---|---|
| 1 | Exact endpoint path — `ojp2020` vs `ojp20`? | **Resolved** — `/ojp20`. Confirmed live in Phase 0 (HTTP 200, real `TripResult`). It's the `OJP_API_ENDPOINT` default. |
| 2 | `openTdataCH/ojp-adapter` — Python-usable? | **Resolved** — it's **Java**. `ojp_client.py` hand-rolls the `TripRequest` (string template) and parses `TripResult` with stdlib `xml.etree.ElementTree` — no new dependency. |
| 3 | OJP 2.0 `TripRequest` / `TripResult` exact shape. | **Resolved** — verified live in Phase 0 against our own token; `ojp_client._normalise` and `tests/unit/test_ojp_client.py` are pinned against the captured response. |
| 4 | `journey_search_executions.session_id` — FK to `sessions` or plain string? | **Resolved** — it *is* an FK to `sessions.id`. Phase 1 therefore does **not** persist the reference result (§5.4); persistence is Phase 2. |
| 5 | Results UI — separate "Reference" panel, or merged list with an `OJP_REFERENCE` origin flag? | **Resolved** — separate **"Reference — Swiss OJP"** panel below VIATOR's own results. Cleaner than overloading `_origin_flag`, and the OJP trips reuse the normalised trip shape so the existing `legsHTML` / card rendering just works. |
| 6 | Send VIATOR's session-filtered view, or raw coords? | **Resolved** — coords (§8.1) for Phase 1; stop-ref mapping is Phase 3. |
| 7 | Privacy — journey searches (coordinates + times) leave VIATOR for a third-party API. | Low sensitivity (public-transport queries, no PII). Still worth a line in the admin guide + the toggle's help text — a Phase-1.x doc tidy. |

---

## 11. Phasing

| Phase | Scope | Status |
|---|---|---|
| **0 — spike** | One manual `curl` against the live OJP endpoint with a hand-built `TripRequest`; confirm endpoint, auth, response shape. (The mandatory "verify before build" gate.) | ✅ **done** — HTTP 200, real `TripResult` captured |
| **1 — MVP** | `ojp_client.py` adapter (coords-based), `compare_ojp` branch in `/api/journey/fanout`, the search-form toggle, config + secret wiring, side-by-side render. Live display only — no persistence (§5.4). CH OJP only. | ✅ **implemented** — this PR |
| **2 — structured diff** | Per-itinerary matching via cross-engine transit fingerprint, `common`/`otp_only`/`ojp_only` bucketing, summary strip + per-card pills in the UI. Persistence deferred (still needs the §5.4 FK resolution). | ✅ **shipped** — `transit_fingerprint` + `_build_comparison` + `comparison_summary` in fanout response; tests in `tests/unit/test_transit_fingerprint.py` |
| **3 — multi-NAP** | Same adapter pointed at other NAPs' OJP endpoints (DELFI, France, …) — per-endpoint config + token. | future work item |

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| OJP XML adapter is fiddly (namespaces, SIRI envelope) | Phase 0 spike de-risks it; `ojp-demo-app` is an open-source reference; keep the request to the minimal `TripRequest` subset. |
| Reference endpoint changes / deprecates | It's a sanctioned, versioned standard (`ojp2020`) — far more stable than a reverse-engineered alternative. Pin the version in config. |
| Operators read a divergence as "VIATOR is broken" when the reference is the odd one out | UI copy: "reference", not "correct". Both are *implementations*; divergence is a prompt to investigate, not a verdict. |
| Scope creep into automated scoring | Phase 1 is explicitly eyeball-only; structured diff is gated behind Phase 2. |

---

## 13. Recommendation

Build **Phase 0 + Phase 1**. It's about a week of work, reuses VIATOR's
existing fanout / recording / credential / config machinery, and
delivers exactly what the operator hit a wall on during the v0.1.34
work: *"is this itinerary actually right?"* — answered in the UI, in
seconds, against the standard VIATOR exists to demonstrate.

Phase 0's manual `curl` gate is non-negotiable — it's the
"verify the external API before writing the adapter" discipline that
the `plan`-vs-`planConnection` detour (#75 → #76 → #77) taught us.

---

## Appendix A — Phase 0 spike (ready to run)

The shapes below are **verified** — pulled from the official
`openTdataCH/ojp-tests-public` Bruno regression collection (the
`LP-1b Coord (Thun) nach Bern` case), not guessed. They still need the
**live-call gate**: run the spike against the real endpoint with a real
token, confirm a `TripResult` comes back, *then* write the adapter.

### A.1 Verified OJP 2.0 `TripRequest` — coordinate-to-coordinate

OJP 2.0 envelope: default namespace `http://www.vdv.de/ojp`, SIRI
elements under the `siri:` prefix, `version="2.0"`. `DepArrTime` sits
*inside* `<Origin>` for a depart-at search (move it into `<Destination>`
for arrive-by). This is the exact subset VIATOR's adapter will emit:

```xml
<?xml version="1.0" encoding="utf-8"?>
<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xmlns:xsd="http://www.w3.org/2001/XMLSchema"
     xsi:schemaLocation="http://www.vdv.de/ojp" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext>
        <siri:Language>en</siri:Language>
      </siri:ServiceRequestContext>
      <siri:RequestTimestamp>__NOW__</siri:RequestTimestamp>
      <siri:RequestorRef>VIATOR-spike</siri:RequestorRef>
      <OJPTripRequest>
        <siri:RequestTimestamp>__NOW__</siri:RequestTimestamp>
        <siri:MessageIdentifier>viator-phase0</siri:MessageIdentifier>
        <Origin>
          <PlaceRef>
            <GeoPosition>
              <siri:Longitude>7.439122</siri:Longitude>
              <siri:Latitude>46.948832</siri:Latitude>
            </GeoPosition>
            <Name><Text>Bern</Text></Name>
          </PlaceRef>
          <DepArrTime>__DEPART__</DepArrTime>
        </Origin>
        <Destination>
          <PlaceRef>
            <GeoPosition>
              <siri:Longitude>8.540192</siri:Longitude>
              <siri:Latitude>47.378177</siri:Latitude>
            </GeoPosition>
            <Name><Text>Zürich HB</Text></Name>
          </PlaceRef>
        </Destination>
        <Params>
          <NumberOfResults>3</NumberOfResults>
          <IncludeIntermediateStops>true</IncludeIntermediateStops>
          <UseRealtimeData>explanatory</UseRealtimeData>
        </Params>
      </OJPTripRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>
```

### A.2 The spike — run on the VPS once you have a token

Register a key at <https://dev-dashboard.opentransportdata.swiss/>, then:

```bash
TOKEN="<paste-your-ojp-bearer-token>"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEPART="2026-05-18T08:00:00Z"     # any near-future weekday morning

sed -e "s/__NOW__/$NOW/g" -e "s/__DEPART__/$DEPART/g" > /tmp/ojp-trip.xml <<'XML'
<?xml version="1.0" encoding="utf-8"?>
<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xmlns:xsd="http://www.w3.org/2001/XMLSchema"
     xsi:schemaLocation="http://www.vdv.de/ojp" version="2.0">
  <OJPRequest>
    <siri:ServiceRequest>
      <siri:ServiceRequestContext><siri:Language>en</siri:Language></siri:ServiceRequestContext>
      <siri:RequestTimestamp>__NOW__</siri:RequestTimestamp>
      <siri:RequestorRef>VIATOR-spike</siri:RequestorRef>
      <OJPTripRequest>
        <siri:RequestTimestamp>__NOW__</siri:RequestTimestamp>
        <siri:MessageIdentifier>viator-phase0</siri:MessageIdentifier>
        <Origin>
          <PlaceRef>
            <GeoPosition><siri:Longitude>7.439122</siri:Longitude><siri:Latitude>46.948832</siri:Latitude></GeoPosition>
            <Name><Text>Bern</Text></Name>
          </PlaceRef>
          <DepArrTime>__DEPART__</DepArrTime>
        </Origin>
        <Destination>
          <PlaceRef>
            <GeoPosition><siri:Longitude>8.540192</siri:Longitude><siri:Latitude>47.378177</siri:Latitude></GeoPosition>
            <Name><Text>Zürich HB</Text></Name>
          </PlaceRef>
        </Destination>
        <Params>
          <NumberOfResults>3</NumberOfResults>
          <IncludeIntermediateStops>true</IncludeIntermediateStops>
          <UseRealtimeData>explanatory</UseRealtimeData>
        </Params>
      </OJPTripRequest>
    </siri:ServiceRequest>
  </OJPRequest>
</OJP>
XML

curl -s -w "\n--- HTTP %{http_code} ---\n" \
  -X POST "https://api.opentransportdata.swiss/ojp20" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: text/xml" \
  --data @/tmp/ojp-trip.xml \
  | tee /tmp/ojp-trip-result.xml | head -80
```

**Pass criteria:** HTTP 200 and the body contains `<OJPTripDelivery>`
with at least one `<TripResult>`. If it 404s, retry with `/ojp2020`
(open question #1). If it 401s, the token / `Authorization` header is
wrong. Save `/tmp/ojp-trip-result.xml` — it's the real fixture the
adapter's `_normalise` gets written against.

### A.3 Verified `TripResult` response skeleton

From the captured regression fixture — what `_normalise` will map from:

```
OJP > OJPResponse > siri:ServiceDelivery > OJPTripDelivery
  TripResponseContext > Places > Place*        (place dictionary: StopPlace / StopPoint / TopographicPlace, each with GeoPosition)
  TripResult > Id, Trip
    Trip: Id, Duration (ISO-8601 e.g. PT50M), StartTime (ISO-8601),
          EndTime, Transfers (int), Distance (metres), Leg+
    Leg: Id, Duration, then exactly one of:
      ContinuousLeg  — walk / personal mode: LegStart, LegEnd,
                       Service>PersonalMode (e.g. "foot"), Duration, Length
      TimedLeg       — a transit leg: LegBoard, LegAlight,
                       Service>PublishedServiceName + operator, intermediate stops
      TransferLeg    — a transfer between two legs
```

Mapping into VIATOR's `trips[]` shape is §8.2. `Trip.Duration` is an
ISO-8601 duration (parse → seconds); `StartTime`/`EndTime` are ISO-8601
(→ UTC ISO, same as the OTP path); `Leg` discrimination is by which
child element is present.

---

## Changelog

- **2026-05-18 (v0.1.35.06)** — Anchor-time pagination for OJP.
  `fetch_reference_paginated` issues up to 4 sequential `TripRequest`s
  with successively-later anchor times until OJP's coverage catches
  up to OTP's `searchWindow` (6 h). Dedupes boundary trips via
  `transit_fingerprint`. Stops on empty batch, all-dups batch,
  window-caught-up, max-pages, or mid-flight HTTP error (with partial
  preservation when at least page 1 succeeded). New
  `TestFetchReferencePaginated` covering all stop conditions. §9.2
  documents the design; §6 rate-limit table updated to reflect the
  worst-case 4× call multiplier and the per-search opt-in safety net.
  Closes the structural alignment gap that left legitimate trains in
  OTP's tail (2–6 h from the anchor) bucketed as spurious `otp_only`.
- **2026-05-18 (v0.1.35.03)** — Swiss SLOID DSN reconstruction:
  v0.1.35.02 assumed the OJP SLOID carried the full 7-digit UIC
  (`ch:1:sloid:8507000:…`), but live data shows it uses the 4-digit
  Swiss DSN (`ch:1:sloid:7000:…`, the trailing 4 digits of the UIC).
  Without prefix reconstruction, the UIC regex couldn't match, so OJP
  fell through to lat/lon while OTP cleanly produced `UIC:8507000` —
  guaranteed mismatch. Fix: when the id is in the `ch:1:` Swiss
  authority namespace and contains a 4-digit chunk, prepend `850` to
  reconstruct the canonical UIC. Verified against Patrick's captured
  Bern→Geneva IR15 JSON (the `{}` button shipped in v0.1.35.02 made
  this debuggable in 60 seconds). New regression test pins the exact
  Bern→Geneva scenario; `TestUicTokenisation` gains `test_ojp_sloid_
  with_swiss_dsn_reconstructs_full_uic` and `test_dsn_prefix_only_
  applied_for_swiss_namespace`. Cross-engine fixtures throughout the
  test file updated to the real OJP SLOID format. §9.1.2 documents
  the second regression and resolution.
- **2026-05-18 (v0.1.35.02)** — Fingerprint regression fix: UIC parsed
  from stop_id is now the primary cross-engine stop token (was 4-dp
  lat/lon in v0.1.35.01, which false-mismatched on real Pontarlier →
  Geneva itineraries because OTP returns platform-precise coordinates
  and OJP returns station centroids). Fallback to 3-dp lat/lon
  (~110 m) absorbs cross-feed centroid variance for non-Swiss feeds.
  Also adds a `{}` JSON-inspector button on OJP cards (was OTP-only)
  so cross-engine mismatches can be debugged from the UI. New tests:
  `TestUicTokenisation` class covering OTP simple/platform-suffixed
  ids + OJP sloid + non-Swiss fallback + Pontarlier French-station
  cross-engine match; plus a Lausanne-platform regression that pins
  the v0.1.35.01 bug. §9.1.1 documents the regression and fix.
- **2026-05-18** — Phase 2 (structured diff) shipped: cross-engine
  `transit_fingerprint(legs)` in `app/journey/signature.py` (walk/
  transfer-stripped, lat/lon-rounded to ~11 m to bridge the `SBB:` ↔
  `ch:1:sloid:` namespace gap, returns `""` for walk-only itineraries
  so they're treated as uncomparable rather than collide). New
  `_build_comparison(merged_trips, ojp_reference)` helper in
  `app/api/journey.py` tags each trip with `comparison ∈ {common,
  otp_only, ojp_only, uncomparable}` and emits a `comparison_summary`
  object on the fanout response. `journey.html` renders the summary
  strip + per-card pills (green = common, blue = OTP-only, amber =
  OJP-only, grey = uncomparable). Tests in
  `tests/unit/test_transit_fingerprint.py` — fingerprint basics,
  discrimination, the cross-engine matching centrepiece, the
  `_build_comparison` bucketing including the walk-only OTP edge case,
  and a parametrized kebab-case mapping check that pins the server-tag
  ↔ CSS-class contract. §9.1 documents the semantics; persistence still
  Phase 3 (blocked on the §5.4 `OJP` session-FK question).
- **2026-05-14** — Phase 1 implemented: `app/journey/ojp_client.py`
  (TripRequest builder + TripResult parser), `compare_ojp` branch in
  `/api/journey/fanout`, the gated search-form checkbox + reference
  panel in `journey.html`, `tests/unit/test_ojp_client.py`. Phase 0
  spike run against the live endpoint — HTTP 200, real `TripResult`
  captured; the parser is pinned against it. Open questions 1–6
  resolved (see §10); recording deferred to Phase 2 (§5.4 — the
  `session_id` FK).
- **2026-05-14** — Initial draft. Appendix A added the same day:
  verified OJP 2.0 request/response shapes from the official
  regression-test collection + a ready-to-run Phase 0 spike.
