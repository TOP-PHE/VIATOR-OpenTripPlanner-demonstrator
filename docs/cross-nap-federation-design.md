# Cross-NAP federation — tactical implementation plan

**Status:** design proposal (no code yet). Filed under the `2.A` thread
from the multi-NAP roadmap.

> **How this doc relates to the existing strategy.**
> [`VIATOR_Federation_Strategy.md`](./VIATOR_Federation_Strategy.md)
> is the **strategic** document — it explains why we ruled out
> single-graph Pan-Europe and chose federation, defines the three
> session classes (regional + corridors + comparison-source), and
> sketches the federated-planner role at the architectural level
> (v0.1.32.2, May 2026).
>
> This doc is **tactical** — given that strategy, here is the
> *concrete sequence of small steps* needed to ship a first working
> cross-NAP journey (Paris → Zürich on TGV Lyria) and what's already
> in place vs what needs building. A developer can read this in 20
> minutes and start writing code.

---

## 1. Where we are today

VIATOR has two independent regional sessions in production:

- **`nap-fr-rail`** — French national rail via transport.data.gouv.fr,
  OTP graph at `rail-focused` OSM scope, ~8 GB serve heap.
- **`nap-ch-rail`** — Swiss national rail via the Swiss NAP,
  rail-only GTFS filter + TransferConstraints disabled,
  ~10 GB serve heap.

Each session:
- Builds its own OTP graph from its own GTFS feeds + OSM PBF.
- Serves its own `/api/journey/fanout` calls independently.
- Knows nothing about the other.

The journey UI dispatches each search to **every serving session**
in parallel and merges results — but **without geographic awareness**.
A Paris → Zürich request currently:

1. Goes to `nap-fr-rail` → returns Paris-side itineraries that stop
   at the French border, or LOCATION_NOT_FOUND because Zürich isn't
   in the FR graph.
2. Goes to `nap-ch-rail` → returns Swiss-side itineraries that start
   at the Swiss border, or LOCATION_NOT_FOUND because Paris isn't
   in the CH graph.

Neither session has TGV Lyria's full Paris→Zürich timetable. The
result is "no route" — even though TGV Lyria runs the service daily
and the data exists, just split across two NAPs.

This is the gap cross-NAP federation closes.

---

## 2. The minimum-viable first cross-NAP journey

For the smallest possible first step, target a **single, well-known
cross-border route**: **TGV Lyria Paris Gare de Lyon → Zürich HB**.

Why this one as the MVP:

- TGV Lyria is a joint SNCF/SBB operation; both NAPs publish parts
  of its data.
- The journey is end-to-end rail, no transfer in between (some Lyria
  trains are direct, others change at Basel SBB but that's still
  in CH coverage).
- It's heavily used (well-known, easy to manually verify against
  hafas.bahn.de / sbb.ch / sncf-connect.com).
- Both endpoints have stable, well-known UIC codes:
  Paris Lyon = `8768603`, Zürich HB = `8503000`.

A successful MVP serves this single OD pair, end-to-end, with a real
TGV Lyria trip in the result. Everything else (other corridors,
session lifecycle automation, geographic dispatch heuristics)
follows once the spine is proven.

---

## 3. What's already in place

Inventory of existing pieces we can lean on:

| Building block | Where it lives | What it does |
|---|---|---|
| **Per-session OTP container** | `docker-compose.yml` + `entrypoint.sh` | Independent OTP instance per session, owns its graph |
| **Session-state machine** | `app/session.py` | `configured → graph_built → serving` lifecycle |
| **Fanout over sessions** | `app/api/journey.py::fanout()` | Iterates serving sessions in parallel, merges results |
| **Trip-signature canonicaliser** | `app/journey/signature.py::trip_signature` | Deduplicates same train returned by multiple sessions (already used for within-feed) |
| **Cross-engine fingerprint** | `app/journey/signature.py::transit_fingerprint` (v0.1.35.03+) | UIC-aware, namespace-blind matching across feeds |
| **OJP adapter** | `app/journey/ojp_client.py` | Speaks the OJP wire protocol — useful if we go OJP-between-sub-planners later |
| **Master stations + UIC resolution** | `app/models/master_station.py` | UIC lookup for both Paris Lyon and Zürich HB already covered |
| **Recording layer** | `app/journey/recorder.py` | Persists every fanout query — federation queries get recorded automatically |

We do **not** need to build session lifecycle automation or the OJP
wire layer for the MVP — both nap-fr-rail and nap-ch-rail are
already serving continuously today.

---

## 4. What needs building (minimum)

Four pieces, in dependency order:

### 4.1 Geographic dispatch index

A small lookup table `(country_iso → session_id)` that the federated
planner uses to decide which sessions to ask. Sourced from each
session's `session.config.sources.providers[].country_iso` (already
present in the schema; see `nap-fr-rail.md` §2.2 "Country gate").

- **Where**: `app/journey/federated_planner.py` (new module)
- **Shape**: a single dict, refreshed when sessions transition
  through their state machine
- **Effort**: ~half day

### 4.2 Origin/destination country detection

Given a journey request `(from_lat, from_lon, to_lat, to_lon)`,
determine which country each endpoint sits in. Three options
roughly in order of preference:

1. **If the search form sends a UIC code** (which it does whenever
   the operator picked a station from the dropdown), look up the
   country via `master_stations`. Fastest, most reliable.
2. **Otherwise reverse-geocode lat/lon** to a country. We don't
   currently have a reverse-geocoder; could use the Nominatim
   public endpoint with rate limiting, or a tiny bundled country-
   polygon shapefile if we want zero outbound dependencies.
3. **Fallback**: ask every regional session and trust the one(s)
   that return non-empty. Brute force but always works.

For the MVP, option 1 is sufficient — operators always pick from the
dropdown.

- **Where**: helper in `federated_planner.py`
- **Effort**: ~half day (option 1 only)

### 4.3 Cross-border merger

VIATOR's current fanout merger is built for "all sessions see the
same network" semantics — it dedupes itineraries that share a
trip-signature. For cross-NAP federation, sessions see *disjoint*
networks, so the merger needs different rules:

- If **only one session returns itineraries**, use those directly.
- If **both sessions return itineraries that share a UIC-based
  fingerprint** for the cross-border train (TGV Lyria runs the
  same physical train; both FR and CH NAPs should describe the
  same UIC + scheduled time + route name), dedupe — prefer the
  session with **more complete** data (typically whichever has
  end-to-end coverage of that operator).
- If **both sessions return *different* itineraries** (FR proposes
  TGV Lyria, CH proposes a Basel SBB transfer), return both —
  tagged with `source_session_id` so the UI can show which session
  contributed it.

The cross-engine `transit_fingerprint` we shipped for OJP comparison
in v0.1.35.03 already produces UIC-keyed tokens that bridge feed
namespaces (`SNCF:8768603` and `SBB:8768603` both yield
`UIC:8768603`). The federated merger reuses this — no new
fingerprinting work.

- **Where**: extend `_merge_trips()` in `app/api/journey.py` with
  a new code path triggered when the request is detected as
  cross-NAP
- **Effort**: ~1 day

### 4.4 Geographic dispatch in the fanout entry point

Wire the above three pieces together: when a request comes in,
determine origin/destination countries, look up which sessions to
ask, fan out to *only* those sessions, merge with the cross-border
rules.

- **Where**: top of `fanout()` in `app/api/journey.py`, before
  the existing per-session call loop
- **Effort**: ~half day

**Total MVP effort**: ~2.5–3 days for a developer who knows the
codebase. The pieces are small; the integration is what costs.

---

## 5. Open questions to settle before coding

### 5.1 Where do the cross-border feeds live?

Two acceptable answers:

- **(a) In one of the regional sessions** — e.g. TGV Lyria's GTFS
  is loaded into `nap-fr-rail`, which then knows about Zürich HB
  (in CH) as a destination stop. Simple; some stops bleed across
  the country gate.
- **(b) In a dedicated `nap-eu-corridors` session** — only
  cross-border services (Eurostar, TGV Lyria, Thalys, ICE
  International) live there. Clean separation; matches
  `VIATOR_Federation_Strategy.md` §3.2.

For the MVP, **(a) is faster** — TGV Lyria's GTFS gets added to
`nap-fr-rail`, Zürich HB becomes a known stop, the FR session can
plan Paris→Zürich end-to-end and the CH session continues to handle
Swiss-domestic queries. We later refactor to (b) when the corridors
catalogue grows past 1-2 operators.

### 5.2 What if TGV Lyria is in *both* NAPs' GTFS?

It might be. SBB publishes TGV Lyria as a service in their GTFS too
(for Swiss-side ticketing/timetables). If both `nap-fr-rail` and
`nap-ch-rail` independently describe the same TGV Lyria train, the
fingerprint catches it and dedupes. **This is the cross-engine
matcher we already shipped for OJP comparison doing its day job.**

### 5.3 What about session lifecycle (promote/demote)?

[`VIATOR_Federation_Strategy.md`](./VIATOR_Federation_Strategy.md)
§3.4 says we eventually need promote/demote to fit 16-24 sessions on
47 GB hardware. **For the MVP we don't need it** — both
`nap-fr-rail` and `nap-ch-rail` are already serving continuously.
Lifecycle automation becomes urgent when we add a 3rd, 4th, 5th
regional session.

### 5.4 What about UIC code normalisation across NAPs?

Different NAPs may use different prefix conventions for the same
station's stop_id:

| NAP | Paris Lyon stop_id |
|---|---|
| SNCF GTFS | `SNCF:OCETrain-8768603` (with internal prefix) |
| SBB GTFS (for cross-border services) | `SBB:8768603` |
| OJP (CH OJP exposing TGV Lyria) | `ch:1:sloid:8603:0:N` (Swiss DSN: drop `87` country, last 4 digits)? Or full UIC because non-Swiss? |

The cross-engine fingerprint's UIC-from-stop_id parser
(v0.1.35.03) already handles `SNCF:NNNNNNN`, `SBB:NNNNNNN`,
and `ch:1:sloid:NNNN:...` (with the 850-prefix reconstruction).
Need to **verify with live data** that French stations appear in
OJP with the full UIC (no DSN truncation, since 87xxxxx isn't a
Swiss DSN). Test fixture in
`tests/unit/test_transit_fingerprint.py::test_pontarlier_french_station_in_sbb_feed`
already pins this case for the Frasne French station.

### 5.5 How does the UI label cross-NAP results?

Today each card shows the source session as a flag (`NAP-FR-RAIL_ONLY`,
`NAP-CH-RAIL_ONLY`, `ALL`, `SUBSET`). Cross-NAP itineraries might
show:

- `NAP-FR-RAIL + NAP-CH-RAIL` (both sessions agreed on the same
  trip — UIC match)
- `NAP-FR-RAIL_ONLY` (only the FR session knew about it)
- `NAP-CH-RAIL_ONLY` (only the CH session)

The existing flag mechanism (`_origin_flag` in `app/api/journey.py`)
already does set-arithmetic on session ids, so this comes for free
once federation is wired.

### 5.6 What about the OJP comparison strip on cross-NAP itineraries?

The OJP comparison feature (v0.1.35.x) targets the **Swiss** OJP
endpoint. For a Paris → Zürich query, Swiss OJP should know about
TGV Lyria (it's a Swiss-operator-involved service), so the
comparison still works on the cross-border segment. The French
end of the journey is comparable too as long as both engines
report it.

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| TGV Lyria's GTFS isn't faithful in one of the NAPs (missing trains, wrong times) | Medium | High — federation MVP shows wrong results | Pick the operator with the cleaner feed; test against a real-world journey timetable manually before relying on results |
| Cross-NAP latency adds up | Medium | Medium — UI feels slow | Sessions already query in parallel; latency = max of all sessions queried, not sum. Should be OK for 2 sessions. |
| Fingerprint mismatches due to NAP-specific stop_id prefixes we haven't seen | Low | Medium — false `*_only` buckets | The cross-engine matcher handles SNCF/SBB/OJP-DSN today; add more if encountered |
| Cross-border feeds in *both* NAPs cause UI to show the same train twice | Low | Low (cosmetic) | Fingerprint deduplicates — but verify with a live test, fix any edge cases |
| Adding TGV Lyria GTFS to `nap-fr-rail` bloats its graph noticeably | Low | Low | TGV Lyria's feed is small (~50 daily trips, dozens of stops); graph growth negligible |

---

## 7. Phasing

| Phase | Scope | Effort | Trigger |
|---|---|---|---|
| **0 — feed sourcing** | Identify the TGV Lyria GTFS source (SNCF? SBB? Lyria themselves?) + verify it covers both directions and current dates. Document the source URL and update cadence. | ~1 day | Operator commits to cross-NAP MVP |
| **1 — MVP** | Add TGV Lyria GTFS to `nap-fr-rail`. Test Paris → Zürich end-to-end without federation code (does the FR session alone now return a valid result?). If yes → no code needed for MVP. If no → §4 work above. | ~3 days | Phase 0 complete |
| **2 — bidirectional** | Same but reverse (Zürich → Paris). Verify both sessions agree on the same TGV Lyria trip via the fingerprint. | ~½ day | Phase 1 complete |
| **3 — extend corridors** | Add Eurostar (Paris ↔ London — but watch Brexit ID requirements / data publishing), Thalys (Paris ↔ Brussels ↔ Amsterdam), ICE International (Paris ↔ Frankfurt / Brussels ↔ Frankfurt). Each is similar work to Phase 1. | ~3 days each | Phase 2 successful + operator ask |
| **4 — promote/demote lifecycle** | Implement the lifecycle automation from `VIATOR_Federation_Strategy.md` §3.4. Becomes urgent at ~3rd+ regional session because RAM pressure starts mattering. | ~3-5 days | Active sessions exceed 2 × ~10 GB |
| **5 — geographic dispatch hardening** | Replace "ask both NAPs" with "look up countries first, ask only the relevant NAPs". Saves latency + load. | ~2 days | After several cross-border corridors live |
| **6 — OJP wire format for sub-planner calls** | The §3.6 vision in the strategy doc — speak OJP between federated planner and sub-planners, so non-VIATOR sub-planners can join the federation. | Multi-day, research-y | Strategic decision to open up federation |

---

## 8. What this design is *not*

- **Not** a rewrite of the existing per-session OTP setup. Each
  regional session stays as it is. Federation is a layer above,
  not a replacement.
- **Not** the comparison-matrix runner. That's a separate workflow
  (`app/network_coverage/runner.py`) that already iterates OD pairs;
  it'll consume the federation API once it exists.
- **Not** an MaaS / ticketing layer. Federation handles routing only;
  fares are out of scope.
- **Not** the OJP-as-comparator feature. That's the v0.1.35.x work
  using Swiss OJP as a fixed external reference. Cross-NAP federation
  is the inverse — VIATOR's own sessions stitched into one logical
  planner.

---

## 9. Recommendation

**Pursue Phase 0 (feed sourcing) as the next concrete step** when
cross-NAP routing becomes a customer ask. Until then, this design
sits ready. The investment scales linearly — each new cross-border
corridor is ~3 days of work, not a major architecture project — so
deferring costs us little.

The biggest unknown is **Phase 0** itself: which TGV Lyria GTFS
source is the most complete and stable? That's a half-day of looking
at transport.data.gouv.fr + opentransportdata.swiss + Lyria's own
site, plus checking a real Paris → Zürich timetable for a date one
week out and matching it against the feed contents. Worth doing
proactively even if Phase 1 stays parked.

---

## Changelog

- **2026-05-18** — Initial tactical implementation plan. Builds on
  `VIATOR_Federation_Strategy.md` (the existing strategy doc, May
  2026) by laying out concrete next-step work: pick TGV Lyria Paris
  → Zürich as the MVP, identify what's already in place vs what
  needs building (~2.5-3 days of small pieces), surface six
  open-question pre-conditions, phase the work from MVP through to
  the §3.4 lifecycle work the strategy doc described at
  architectural level.
