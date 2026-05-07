# VIATOR Federation Strategy

Architectural strategy for routing across the full European rail network in
a NAP-vs-MERITS comparison demonstrator, when single-graph routing has been
shown empirically not to fit on commodity hardware.

**Status**: Decision draft, v0.1.32.2 (2026-05-06).
**Audience**: Platform admin, demonstrator product owners, future
implementers of v0.1.33+ federation work.

---

## 1. Problem statement

### 1.1 The demonstrator's goal

Compare two pan-European rail timetable data sources cell-by-cell across
thousands of route combinations:

- **NAP source** — national access points (one per country: SNCF / DB / SBB
  / SNCB / NS / RENFE / Trenitalia / etc.). Heterogeneous publishing
  cadence and data quality, but operationally what European rail data
  consumers actually use today.
- **MERITS source** — the unified European rail timetable feed, single
  pan-European publication, single quality bar.

For each rail OD pair (Paris→Berlin, Stockholm→Madrid, Munich→Rome, ...),
the demonstrator should:

1. Route the journey using NAP-fed sessions
2. Route the same journey using MERITS-fed sessions
3. Compare results: same itinerary? Different durations? One source
   missing a viable route the other has? Different operators routing
   the same trip?
4. Aggregate into a discrepancy report: "NAP has 234 pair-routes that
   MERITS doesn't, MERITS has 87 that NAP doesn't, both agree within
   5 min on 1,329, disagree by >10 min on 56."

**The artifact** is a comparison matrix + diff report at continental
scale. That's the value the demonstrator delivers to operators / regulators
making decisions about rail data harmonization.

### 1.2 The constraint we hit

Empirical results from the v0.1.30→v0.1.32 build cycle:

| Scope | Filtered PBF | Build heap | Serve heap | Outcome |
|---|---|---|---|---|
| France only (single country, transit-focused) | ~3 GB | 24g | 12-16g | proven working |
| 7-country EU (rail-focused, FR + UK + BE + NL + LU + DE + CH) | 1.1 GB | 36g | **20-32g insufficient** | OOMed during RAPTOR mapping |
| 7-country EU + Swiss SBB GTFS | 1.4 GB | 36g | **32g insufficient** | OOMed even with FR session stopped |

Documented largest production OTP deployment is the Netherlands at 13M
stop times (single small dense country). No public reference exists for
OTP serving 28+ countries in one graph at any heap size — beyond a
certain scale, RAPTOR's data structures genuinely don't fit anywhere
reasonable.

Linear-scaling estimate for full European rail (FR + DE + IT + ES + UK +
the rest of Western Europe + Scandinavia + Central Europe + Balkans, ~30
countries):

- Filtered PBF: 4-5 GB
- Stops: 40-50k
- Patterns: 50-70k
- **Realistic serve heap: 180-250 GB**, build heap higher

**96 GB hardware doesn't fit it. 256 GB is borderline. 384 GB starts
being plausible.** The OTP project itself acknowledges continental scale
is unsolved and has R&D funding for "next-generation" routing.

---

## 2. What we ruled out

Decisions taken during the architectural review preceding this strategy.
Each is grounded in empirical or sourced reasoning.

### 2.1 Single-graph EU on bigger hardware — RULED OUT

Going from 47 GB → 96 GB Contabo (recurring ~€20-30/month) buys Western
Europe (~10-12 countries) but not Pan-European. Going to 384 GB-class
hardware crosses a cost threshold (€500+/month) that doesn't fit a
research demonstrator's budget. And no public production proof exists
that even 384 GB serves a 28-country graph reliably.

Decision: **hardware upgrade alone does not solve the problem at the
demonstrator's stated scope**. A Western-Europe-only single-graph would
be a regression from the comparison goal.

### 2.2 Rust rewrite of OTP — RULED OUT

OTP's transit routing has 12+ years of community engineering: GTFS-Flex,
fares-v2, RAPTOR, GTFS-RT updaters, NeTEx ingestion, etc. The
demonstrator's bottleneck is **graph size in JVM heap**, not language
inefficiency. Rewriting the routing engine in Rust is multi-engineer-year
work that wouldn't change the architectural memory ceiling — Rust holds
the same data structures in the same RAM.

Where Rust *could* help (e.g. memory-efficient streaming/tiled graphs in
the Valhalla style) is research work, not engineering, and reinvents an
existing Rust-ecosystem effort.

Decision: **continue on the OTP+Python stack**. Optionally write scoped
Rust components later (e.g. a federation/merger service, a GTFS
pre-processor) when concrete bottlenecks justify it.

### 2.3 Federation as the architectural answer — CHOSEN

What the OTP project itself recommends for multi-region scenarios
(*"deploy multiple OTP instances and orchestrate at a higher layer"*),
what Digitransit / Finland implements in production (*"each member
organisation runs its own instance of a shared codebase"*), and what
Entur publicly states as their direction for cross-Europe work
(*"exploring Open Journey Planner as a standardized interface for
distributed journey planning"*).

VIATOR's existing session model is well-suited to federation: each
session is already an independent OTP instance with its own graph and
GTFS feeds. The work to add is a federated planner above the sessions
plus geographic dispatch logic.

---

## 3. The chosen architecture

### 3.1 Concept

```
                   ┌──────────────────────────┐
                   │   Federated Planner      │
                   │   (VIATOR web layer)     │
                   │                          │
   Browser ──────► │ • Geographic dispatch    │
                   │ • Session lifecycle      │
                   │ • Cross-border merger    │
                   │ • OJP wire format        │
                   └──────┬───────────────────┘
                          │
            ┌─────────────┼─────────────────────┐
            ▼             ▼                     ▼
    ┌──────────────┐ ┌──────────────┐    ┌─────────────────┐
    │ Regional     │ │ Corridors    │    │ Comparison      │
    │ sessions     │ │ session      │    │ source sessions │
    │ (per country)│ │ (cross-border│    │ (NAP vs MERITS) │
    │              │ │  feeds only) │    │                 │
    │ nap-fr-rail  │ │ nap-eu-corr  │    │ merits-eu-corr  │
    │ nap-de-rail  │ │              │    │                 │
    │ nap-ch-rail  │ │ Eurostar,    │    │ Same shape as   │
    │ nap-it-rail  │ │ Thalys, ICE  │    │ NAP side, but   │
    │ nap-es-rail  │ │ Lyria, ...   │    │ MERITS-fed      │
    │ ...          │ │              │    │                 │
    └──────────────┘ └──────────────┘    └─────────────────┘
```

### 3.2 Three session classes

**Regional sessions** — one per country (or geographically coherent
multi-country group). Each contains:

- That country's OSM PBF at `transit-focused` or `rail-focused` scope
- That country's national + regional GTFS feeds (SNCF + regional French
  feeds for `nap-fr-rail`; DB + regional German feeds for `nap-de-rail`)
- **No cross-border feeds** — Eurostar isn't loaded in `nap-fr-rail`
- Sized to fit comfortably (~8-16g serve heap each)

Handles all **domestic queries** within its country.

**Corridors session** — single session covering cross-border services.
Contains:

- Multi-country OSM PBF at `rail-focused` scope (FR + UK + BE + NL + LU +
  DE + CH for the Western Europe corridors — already proven viable at
  v0.1.32 minus the GTFS bloat)
- **Only the international/cross-border GTFS feeds**: Eurostar, Thalys,
  ICE International, TGV Lyria, Trenitalia France cross-border services,
  RENFE AVE→Paris, etc.
- **No regional feeds** (no SNCF TER, no DB regional, no SBB regional —
  those are in the regional sessions)
- Smaller than a full multi-country session because regional feeds (the
  bulk of GTFS bytes) are excluded
- Sized at ~16-20g serve heap

Handles **all cross-border queries** where both endpoints are within its
OSM extent.

**Comparison source sessions** — a parallel set of sessions on the
MERITS side. Same regional/corridors structure, but each session is fed
from MERITS rather than the local NAP. Naming convention: prefix with
`merits-` instead of `nap-`.

Total session count for full demonstrator coverage:

| Tier | NAP side | MERITS side | Total |
|---|---|---|---|
| Regional | 7-10 sessions | 7-10 sessions | 14-20 |
| Corridors | 1-2 sessions (Western + maybe Scandinavia) | 1-2 sessions | 2-4 |
| **Total** | | | **16-24 sessions** |

Not all serving simultaneously — see §3.4 lifecycle.

### 3.3 Federated planner — what it does

A new layer in `app/journey/federated_planner.py`. Role:

1. **Receive** a journey request (origin lat/lon, destination lat/lon,
   depart_at, modes, preferences)
2. **Determine geography** — which countries do origin and destination
   belong to (reverse-geocode lat/lon to country, or look up via station
   ID if available)
3. **Select candidate sessions** from a session-geography index:
   - Same-country query → ask the regional session for that country
   - Cross-border within corridors-session coverage → ask the corridors
     session (and optionally the relevant regional sessions for
     comparison)
   - Cross-border outside corridors-session coverage → fall back to
     decomposition (see §6 — research-grade)
4. **Promote** any candidate sessions that aren't currently `serving` —
   await graph load (~3 min for an EU-scale graph) — see §3.4
5. **Fan out** to the candidate sessions in parallel (already exists in
   VIATOR, just gets scoped to candidates rather than "every serving
   session")
6. **Merge** the responses with cross-border-aware logic (see §3.5)
7. **Return** a unified itinerary set, each tagged with its source
   session

### 3.4 Session lifecycle — promote / demote

Today VIATOR's session state machine is `configured → graph_built →
serving`. A serving container holds graph + RAPTOR data structures in
memory continuously. With ~16-24 sessions in the federation, keeping
all serving simultaneously requires ~150+ GB host RAM — over budget.

**v0.1.33 lifecycle additions**:

- Worker tracks last-query-time per session
- Sessions not queried in **N minutes** (config-tunable, ~30 min
  reasonable default) auto-demote: container stopped, graph evicted from
  RAM
- Federated planner triggers auto-promote on first query: starts the
  container, awaits Grizzly (typically 2-3 min for a regional session,
  3-5 min for corridors)
- During load, journey UI shows a "warming up" state with the relevant
  session badged

For the comparison matrix runner (a batch operator process):

- Iterates over OD pairs in groups by geography
- Promotes the session(s) needed for that group
- Runs the group's queries
- Demotes when moving to the next geography

At any one moment: **2-4 sessions serving × ~10g heap each = 20-40 GB
heap usage**. Comfortable on 47 GB host. Trade-off: matrix runs are
slower (sessions must promote/demote) but bounded.

### 3.5 Cross-border-aware merger

VIATOR's current fanout merger is built around "all sessions are
alternative views of the same network — pick the best." For federation,
sessions cover **disjoint** regions. The merger needs new rules:

- If only one session returns itineraries (others return
  `LOCATION_NOT_FOUND` or empty) → the one with answers wins, no merge
- If multiple sessions return itineraries that share a trip-signature
  (e.g. corridors session and FR session both routed Paris-Lille via TGV
  because Eurostar serves Lille) → deduplicate, prefer the
  trip-signature with most data (typically corridors session for
  cross-border legs, regional session for domestic)
- If multiple sessions return *different* itineraries (e.g. corridors
  proposes Eurostar, FR proposes a TGV+Thalys combo) → return both,
  ranked by primary criterion (duration, transfers)
- Tag each itinerary with `source_session_id` so the UI can show which
  session contributed it

### 3.6 OJP as the wire format

[Open Journey Planner (OJP)](https://www.transmodel-cen.eu/standards/open-journey-planner/)
is a CEN-standardized protocol for inter-system journey-plan exchange.
Adoption pattern:

- Each sub-planner (= each VIATOR session's OTP container) exposes an
  OJP-compatible endpoint, OR the federated planner translates between
  OTP's GraphQL and OJP semantics
- The federated planner speaks OJP **outbound** to sub-planners
- Future external comparators (different MERITS provider implementations
  / national journey planners adding OJP support) can interoperate

For v0.1.33: **build the federated planner with the OJP request shape as
its internal data model**, even if the wire format to OTP sub-planners
remains GraphQL initially. Costs little (a few extra hours of design)
and aligns with where Entur and the European rail data community are
heading. Full OJP wire format adoption can be a v0.1.35+ deliverable.

---

## 4. Reference architectures we studied

Documented production deployments and their published architectures.

### 4.1 Entur (Norway) — single national graph

> "Coverage: Entire nation via Entur national journey planner —
> Architecture: Single OTP2 instance handling peak loads in excess of 20
> requests per second."

Norway has ~5M population, ~30 transport agencies, single coherent OTP
graph. Reasonable single-graph because the country is geographically
moderate and data is centrally curated. Entur is **exploring OJP** for
their cross-Europe routing work but hasn't shipped federation in
production.

### 4.2 Digitransit / Finland — multi-instance federation

> "Each member organisation runs its own instance of a shared codebase."

Finland's national journey planning platform federates regional
instances, each per metro area/region, via a common codebase + UI layer.
**This is the closest production analogue to what VIATOR's federation
will look like** — independent sessions with a coordinating layer above.
Worth studying their orchestration code.

### 4.3 Netherlands (single-graph deployment)

> 13M stop times, 1969 routes, 66000 stops in one graph — the largest
> documented production OTP.

Single small dense country with comprehensive transit (rail + bus + tram
+ metro + ferry). Graph builds at ~36-48g heap per the OTP `LargeGraphs`
wiki. Demonstrates **single-graph at country scale is feasible**, just
not at continental scale.

### 4.4 OTP project's official direction

> "OTP was largely designed, tested, and deployed in medium-sized
> metropolitan areas (approximately 2 million inhabitants). However,
> there is interest in scaling OTP up to provide international trip
> planning, with research and development funding to experiment with a
> next-generation rework of the routing code with an eye on reducing
> resource consumption."

The OTP project itself does not currently solve continental-scale
single-graph routing and treats it as research. Their guidance for
multi-region scenarios is unambiguously **deploy multiple instances and
orchestrate above**.

### 4.5 What we did NOT find

No public production reference for "OTP serving 28+ countries in one
graph." No published architecture for "fully federated OTP-based
European journey planning." Both are research frontier as of 2026.

VIATOR's federated implementation is therefore **early-implementer
territory** — a research contribution as well as operational glue.

---

## 5. Implementation roadmap

### 5.1 v0.1.33 — federated planner foundation (~1-2 weeks)

**Engineering deliverables** (well-understood, low risk):

- `app/journey/federated_planner.py` — new module
- `Session.osm_countries` field (list of ISO codes the OSM extent covers)
- `Session.session_role` field — `regional` / `corridor` /
  `comparison-source`
- Geographic dispatch logic — given a journey request, return candidate
  session IDs
- Session lifecycle — auto-promote on demand, auto-demote on idle
- Cross-border-aware fanout merger — handle disjoint-region session
  responses
- Migration to add the new session fields with sensible defaults from
  existing data
- Updated session UI form to expose `session_role` and `osm_countries`
- Tests: unit (dispatch logic, merger), integration (mock multi-session
  fanout)
- Docs update — extend `multi-country-runbook.md` with federation
  section

**Operational deliverables**:

- 5-7 regional NAP sessions built (FR, DE, CH, BE-NL-LU, IT, ES, UK)
- 1 NAP corridors session built (Western Europe + cross-border feeds
  only)
- Live UI confirmed working for: domestic queries (each region) +
  Western European cross-border queries (corridors session)

### 5.2 v0.1.34 — far-journey decomposition (~3-4 weeks)

**Skeleton-graph approach** for OD pairs no single session covers
(Stockholm→Madrid, Strömsund→Bari, etc.):

- Curate a 40-60-node "European rail skeleton" — major hubs +
  cross-border interchange stations — encoded as a CSV
- Implement Dijkstra-on-skeleton for path-finding among hubs
- Implement hub-snapping (origin/destination → nearest skeleton hub +
  feeder leg)
- Sequential per-leg session queries with time-consistency
- Stitch into unified itinerary, return to UI

**Curated comparison corpus** for the matrix runner:

- ~50 comparison hubs across Europe, ~800-1,200 meaningful OD pairs
  manually annotated with canonical decomposition
- Coverage matrix runner takes a "comparison set" input rather than a
  free-form pair list
- Output: side-by-side matrix view comparing NAP and MERITS results

**OJP wire format adoption** (optional in v0.1.34, mandatory by v0.1.36):

- Federated planner speaks OJP outbound to sub-planners
- Sub-planners (or a translation layer) accept OJP requests, translate
  to OTP-GraphQL internally, return OJP-shaped responses

### 5.3 v0.1.35+ — operational maturity

- MERITS data pipeline (assumes MERITS feed is available by then)
- Build the MERITS-side mirror of the regional + corridors sessions
- Run automated daily/weekly comparison sweeps
- Generate the discrepancy report artifact: NAP vs MERITS by route, by
  region, by service type

### 5.4 What's intentionally out of scope (deferred / never)

- **Real-time graph adaptation** when a corridors-session edge has a
  service disruption — the federated planner uses static skeleton edges;
  GTFS-RT alerts are surfaced as itinerary annotations but don't
  re-route. Operational journey planners need this; comparison
  demonstrators don't.
- **Optimal routing for arbitrary rural-rural OD pairs** that aren't in
  any pre-defined corridor — outside the demonstrator's scope.
- **Single-graph continental routing** — even with hardware upgrades,
  this remains research. Federation is the answer.
- **Backtracking on leg failure** — if leg 2 of a decomposed journey
  has no service that day, the whole route fails rather than
  re-decomposing through alternative hubs. Acceptable for a comparison
  demonstrator; would not be acceptable for a consumer-facing planner.
- **Fares-v2 across federated legs** — fare composition across operators
  is its own research problem; demonstrator reports per-leg operator
  but doesn't compute through-fares.

---

## 6. The hardest open problem — far-journey decomposition

Documented separately because it's the genuinely research-grade part of
v0.1.34.

### 6.1 Why arbitrary OD decomposition is hard

For a journey like Strömsund (rural northern Sweden) → Bari (southern
Italy):

- Multiple viable cross-Europe corridors (8-12 distinct path topologies)
- Each topology has multiple interchange options (3-6 candidate
  stations per crossing)
- Combinatorially: 50-200 distinct decompositions to evaluate
- Each decomposition costs 4-7 sequential session queries
  (~100-500ms each)
- Brute-force evaluation = many seconds per OD pair → hours for a
  matrix run

### 6.2 Three approaches, ranked by feasibility

**A — Skeleton graph** (CHOSEN for v0.1.34): pre-compute a coarse
"European rail skeleton" of major hubs + corridor edges, run Dijkstra
on the skeleton, decompose the path into per-session legs. Bounded
complexity; manageable maintenance via CSV-edited skeleton.

**B — Run-time A\* over sessions**: each session reports its
reachability envelope, federation does A\* in real time treating
sessions as edges. Adapts to live timetables but slow and complex.
Production engines (Rome2Rio, Google) work this way with massive
infrastructure. Out of scope for VIATOR.

**C — Curated route corpus**: for the demonstrator's actual goal
(NAP vs MERITS comparison), the OD pairs are bounded — major-hub to
major-hub, ~800-1,200 meaningful pairs. Manually annotate the canonical
decomposition for each pair. Used in COMBINATION with Approach A: the
matrix runs against the curated corpus; the live UI uses skeleton
decomposition for arbitrary queries.

### 6.3 The skeleton's known limitations

- Can't discover novel routes that the operator hasn't encoded
- If a new service opens (e.g. SBB-Trenitalia overnight via Brennero),
  the operator must add it to the CSV manually
- For a research demonstrator that compares two data sources rather than
  offering operational journey planning, this limitation is acceptable
- For something becoming an operational journey planner, you'd outgrow
  the skeleton and need Approach B or pay for proprietary infrastructure

---

## 7. Open decisions before v0.1.33 commences

### 7.1 Hardware budget

| Option | Monthly cost | Demonstrator scope |
|---|---|---|
| Stay on 47 GB Contabo | €0 incremental | Federation required for any cross-border depth |
| Upgrade to 96 GB Contabo | ~€20-30 incremental | Buys headroom for 2-3 sessions concurrently; helps but doesn't replace federation |
| Upgrade to 256 GB-class hardware | ~€100-200 incremental | Marginal — federation still needed for full Europe |

**Recommendation**: 96 GB upgrade alongside federation work, not as a
replacement. Frees 2-3 sessions to remain concurrently `serving`,
reducing matrix-run wallclock.

### 7.2 Scope of OJP adoption

| Level | Effort | When |
|---|---|---|
| OJP request shape as internal data model only | +2-4 hours during v0.1.33 design | v0.1.33 |
| OJP wire format outbound to sub-planners | +1-2 weeks | v0.1.34 |
| Full OJP server-side support (sub-planners speak OJP natively) | +2-3 weeks | v0.1.35+ |

**Recommendation**: do level 1 in v0.1.33 (cheap, future-proofs the
federated planner). Defer levels 2 and 3 until external interoperability
is needed.

### 7.3 Demonstrator vs operational scope

The strategy explicitly assumes **demonstrator-grade**, not operational
journey planning. This means:

- ~80% accuracy on common European rail journeys (acceptable)
- 100% accuracy on the curated comparison corpus (must)
- "No service" responses on pathological cases acceptable
- No SLA on response latency for federated queries
- No real-time adaptation requirements

If the project pivots to operational planning, the strategy needs
revisiting (skeleton becomes insufficient, decomposition needs
backtracking, GTFS-RT needs cross-leg propagation, etc.).

**Confirm scope before v0.1.33 work commences.**

### 7.4 MERITS feed availability and timing

Federation work on the NAP side can proceed independently. The MERITS
mirror is gated on MERITS data being available. Need timeline alignment
to avoid building infrastructure that sits idle.

---

## 8. What we built up to know this

This strategy is grounded in the operational learning from
v0.1.27→v0.1.32.2:

- [`multi-country-runbook.md`](./multi-country-runbook.md) — operational
  reference for multi-country builds, captures the heap-sizing matrix,
  filename contracts, build-phase tracking, and failure-mode decision
  trees. Direct source of the empirical numbers in §1.2 and §2.1.
- [`admin-guide.md`](./admin-guide.md) §6 — general troubleshooting
- [`nap-fr-rail.md`](./nap-fr-rail.md) — single-country session
  walkthrough (the FR baseline that grounds heap-budget rules of thumb)

The footguns documented in those guides are not theoretical — every
heap-OOM, file-name mismatch, config-drift, and orphan-job cleanup we
captured was hit in the v0.1.30→v0.1.32 EU build attempts. The
federation architecture chosen here is **the architecture that survives
those failure modes**, not an a-priori design.

---

## 9. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-06 | Single-graph EU on 47 GB box NOT viable | OOMed at every heap level tested with 7 countries + Swiss |
| 2026-05-06 | Hardware-only solution NOT viable for full Europe | Linear scaling estimates exceed practical hardware budgets |
| 2026-05-06 | Rust rewrite of OTP NOT pursued | Multi-engineer-year effort; doesn't fix architectural memory ceiling |
| 2026-05-06 | Federation architecture CHOSEN | Recommended by OTP project, demonstrated by Digitransit, aligned with Entur's stated direction |
| 2026-05-06 | Corridors session pattern CHOSEN for cross-border | Empirically proven viable in our v0.1.32 build minus regional GTFS bloat |
| 2026-05-06 | Skeleton-graph approach CHOSEN for far-journey decomposition | Bounded complexity; runtime A\* deferred to research |
| 2026-05-06 | OJP as future wire format ENDORSED | Aligns with European standardization direction (Entur's work) |

---

## 10. Acknowledgements & honest scope

Two limitations of this strategy worth surfacing:

1. **The federated planner being built is early-implementer territory**
   in the European context. Digitransit federates regional instances
   within Finland; Entur is exploring cross-Europe federation but hasn't
   shipped it. VIATOR's federation will be operationally useful for the
   comparison demonstrator and a research contribution to the OSS
   community, but it is not a battle-tested production pattern at
   continental scale.

2. **The 96 GB hardware sweet spot for "Western Europe" was not
   carefully validated** — early estimates suggested it'd fit the EU
   session, then v0.1.32 testing showed 32 GB serving heap insufficient
   for 7-country + Swiss. The federation strategy doesn't depend on the
   hardware estimate, but the comparative cost analysis in §7.1 should
   be re-validated empirically once a regional session deployment
   pattern is confirmed.

---

*Living document. Updates expected as v0.1.33 implementation surfaces
new constraints.*
