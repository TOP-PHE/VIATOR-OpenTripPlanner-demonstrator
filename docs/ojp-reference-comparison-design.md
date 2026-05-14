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
| **opentransportdata.swiss OJP 2.0** | ✅ **Chosen.** Sanctioned public API, free tier, documented, open-source reference client to model from. It *is* the MERITS/OJP standard VIATOR exists to demonstrate. |
| `bahn.de` "vendo" endpoint | ❌ No official API — reverse-engineered endpoint behind the bahn.de frontend. Brittle, no SLA, ToS-grey. Acceptable only as a future best-effort add-on if DE-network coverage is specifically needed. |
| Google Directions / Rome2Rio | ❌ Paid, restrictive ToS, not transit-standards-based. |
| Other NAPs' OJP endpoints (DELFI / France / …) | ⏳ Phase 3 — same adapter, different endpoint + token. Out of scope for the first cut. |

Choosing the OJP endpoint isn't a compromise — it's **on-mission**.
"VIATOR (OTP) vs the reference OJP implementation" is a meaningful thing
for a MERITS OJP demonstrator to show.

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
| **50 req/min, 20K/day** | Opt-in per search makes this a non-issue for human operators (nobody ticks-and-searches 50×/min). The toggle being **unchecked by default** is the rate-limit safety design — do **not** auto-fire it on every fanout. |
| **Latency** | Reference call gets its own timeout — `OJP_TIMEOUT_MS` (§7), default 10 s. On timeout, the reference panel shows "timed out"; VIATOR results render normally. |
| **Endpoint down / 5xx** | Same — reference source reports `error`, VIATOR unaffected. Never blocks or fails the operator's actual search. |
| **Rate-limit hit (429)** | Surface a specific "reference rate-limited — try again shortly" message rather than a generic error. Consider a short server-side cooldown. |
| **Burst protection** | The existing `concurrency` semaphores already gate journey calls; the reference call participates in the same budget. |

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

Phase 1 is **presentational** — show both, let the operator judge. The
UI computes a few cheap cues:

- **Δ duration** of each side's *best* (shortest) itinerary.
- **Coverage**: "reference returned N, VIATOR returned M".
- **Route-shape hint**: do the best itineraries share their transit
  legs' route short-names in order? If not, flag "different route".

Phase 2 (separate work item) would add a structured diff: per-itinerary
matching, a similarity score, and persistence of the comparison verdict
for trend analysis across many searches — analogous to the
`network_coverage` matrix runs VIATOR already has.

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
| **2 — structured diff** | Per-itinerary matching, similarity score, **persisted** comparison verdicts (resolves the §5.4 FK question properly), trend view. | future work item |
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
