# Federated Planner — cross-session leg-stitching design

A design for routing a journey **across two (or more) sessions that each hold
a disjoint network**, by querying each for a leg and **stitching the legs at a
connection hub** with a time-coordinated transfer. The motivating case:
**Paris → Fribourg (CH)** — *Paris →(cross-border)→ Basel/Bern →(Swiss
domestic)→ Fribourg* — which no single graph can route because Paris and
Fribourg never co-exist in one session.

**Status**: Design proposal (no code). The query-time *leg-stitching* layer —
the deeper end of `cross-nap-federation-design.md`, which only covered a
single cross-border *through-train* (dedup, no transfer).
**Audience**: Platform admins, demonstrator product owners, implementers.

> Pairs with:
> - [cross-nap-federation-design.md](cross-nap-federation-design.md) — the
>   *through-train* MVP (Paris→Zürich on one Lyria train, both NAPs describe
>   the same train → dedup). This doc is the next layer: journeys that
>   **change trains across networks**.
> - [VIATOR_Federation_Strategy.md](VIATOR_Federation_Strategy.md) — why
>   single-graph pan-Europe was rejected; the session classes.
> - [osm-geographic-scope-design.md](osm-geographic-scope-design.md) — the
>   *combined-graph* alternative (§6): one geo-cropped multi-country graph
>   routes a transfer natively. This doc is the **keep-sessions-separate**
>   path the operator chose instead.

---

## 1. Problem

OTP routes inside **one graph**. Today the journey API (`app/api/journey.py`)
has two query modes, neither of which stitches:

- `plan()` / `_query_session()` — ask **one** session for a full O→D itinerary.
- `fanout()` — ask **every serving session** the **same** O→D and
  **compare/merge** the results (the OJP-comparison + origin-flag feature).
  `_origin_flag()` does set-arithmetic over which sessions returned a trip; it
  does **not** combine partial legs.

So **Paris → Fribourg** returns nothing:

| session | has Paris? | has Fribourg? | result |
|---|---|---|---|
| `nap-eu-corridors` | ✅ | ❌ (Fribourg is Swiss-domestic, not on a cross-border route) | LOCATION_NOT_FOUND |
| `nap-ch-rail` | ❌ | ✅ | LOCATION_NOT_FOUND |
| `nap-fr-rail` | ✅ (Paris) | ❌ | LOCATION_NOT_FOUND |

The journey exists in reality — Paris→Basel on TGV Lyria (in corridors) then
Basel→Fribourg on an SBB InterCity (in nap-ch-rail) — but the **transfer at
Basel spans two graphs**. Closing that is the federated planner's job.

This is distinct from `cross-nap-federation-design.md`'s MVP: that was a
*single through-train* both NAPs happen to describe (solved by dedup). Here the
two legs are **different trains on different networks**, joined by a real
transfer — genuinely harder.

---

## 2. What the federated planner is (and isn't)

**Is:** a query-time meta-planner that, when no single session can serve an
O→D, (a) picks candidate **connection hubs**, (b) routes **leg 1** origin→hub
in one session and **leg 2** hub→destination in another, **time-coordinated**
so leg 2 departs after leg 1 arrives + a minimum connection time, then (c)
stitches, ranks and dedupes the combined journeys.

**Isn't:**
- A new router. It orchestrates existing per-session OTP planners; it does not
  re-implement RAPTOR.
- The combined-graph approach (a single geo-cropped FR+CH graph that routes the
  transfer in-graph — see osm-geographic-scope-design.md §6). That's the
  pragmatic complement; this doc is the keep-sessions-separate path.
- A fares / ticketing layer (out of scope).
- Multi-hop across 3+ sessions in v1 (deferred — see §7).

---

## 3. What's already reusable

| Building block | Where | Use here |
|---|---|---|
| Time-anchored, UIC-keyed leg query | `app/journey/otp_client.py::fetch_plan(when=…, from_stop_id/to_stop_id=…)` | The core primitive: route a single leg from a given departure time, keyed on UIC stop ids |
| One-session query wrapper | `app/api/journey.py::_query_session` | Per-leg dispatch + status/timing |
| Cross-engine fingerprint | `app/journey/signature.py::transit_fingerprint` | Dedup a stitched journey against a through-train a session already returned; recognise the same train across feeds |
| UIC → country | `app/models/master_station.py` | Origin/destination country detection; hub country |
| Major-hub catalogue | `app/models/network_coverage.py` (hubs, tiered per country) | Connection-hub candidates |
| Origin-flag set-arithmetic | `app/api/journey.py::_origin_flag` | Extend to label which sessions contributed each leg |
| Recording layer | `app/journey/recorder.py` | Stitched queries get recorded like any fanout |

No new OTP wire protocol and no session-lifecycle automation are needed for v1.

---

## 4. Design

### 4.1 Dispatch + "does this need stitching?"

1. Resolve origin & destination **country** (UIC → `master_stations`; the
   search form sends UICs whenever the operator picks from the dropdown — same
   path `cross-nap-federation-design.md` §4.2 option 1 uses).
2. Build a `country_iso → [session_id]` index from
   `session.config.sources.providers[].country_iso` (already in the schema).
3. **First, try the normal path** (single session / fanout). If any session
   returns an end-to-end itinerary, use it — **stitching is a fallback**, not
   the default (keeps the common case fast and avoids stitched journeys
   competing with real through-trains).
4. Only when no single session covers origin **and** destination, invoke the
   stitcher with the candidate session pair(s): the origin-country session(s)
   (incl. corridors) for leg 1, the destination-country session(s) for leg 2.

### 4.2 Connection-hub selection (the crux)

A hub `H` is viable iff leg 1 can reach `H` **and** leg 2 can depart `H`. We
don't want to probe every station, so candidates come from the **intersection**:

```
candidates = (stations the origin-side network reaches in the destination country)
           ∩ (destination-side session's stations)
           ∩ (tier-1 hubs in network_coverage)          # bound the set
```

Concretely for Paris→Fribourg: the **corridors** graph reaches into CH at the
gateway cities (Basel SBB, Genève, Bern, Lausanne, Zürich) — those are exactly
the handoff points, and they're tier-1 hubs present in `nap-ch-rail`. v1 may
seed a **curated per-country-pair gateway list** (FR↔CH = {Basel, Genève, Bern,
Lausanne}) and graduate to dynamic discovery later (§7). Bound to top-N (≈3-5).

### 4.3 Two-phase, time-coordinated query

```
T = requested earliest departure
for H in candidate_hubs (in parallel):
    leg1 = fetch_plan(origin_session, origin → H, when=T)           # arrivals at H
    for arr in leg1.arrivals[:k]:                                   # a few earliest
        leg2 = fetch_plan(dest_session, H → destination,
                          when = arr.time + MCT(H))                 # depart after transfer
        emit stitch(leg1[arr], H, leg2.first)
```

`fetch_plan`'s `when` (earliestDeparture) + `from_stop_id`/`to_stop_id` (UIC)
are exactly what this needs. Phase 2 depends on phase-1 arrival times, so it's
sequential per hub but **parallel across hubs** — latency ≈ 2 × single-session,
not N×. Bound `k` (arrivals per hub) and N (hubs) to cap the query count.

### 4.4 Minimum connection time (MCT)

Leg 2 must depart ≥ `arrival + MCT(H)`. v1: a flat default (rail↔rail same
station, e.g. 10 min). Later: per-hub MCT (big interchanges need more), sourced
from the MCT enrichment layer the strategy doc describes.

### 4.5 Stitch, rank, dedup

- A stitched journey = `leg1 ⊕ {transfer at H} ⊕ leg2`; total duration =
  leg2.arrival − leg1.departure; transfers = leg1.transfers + 1 + leg2.transfers.
- **Rank** by arrival, then duration, then transfers.
- **Dedup** with `transit_fingerprint`: if a session already returned a
  through-itinerary equivalent to a stitch (e.g. a direct Lyria that continues
  into CH), keep the through one. Also dedup stitches that differ only by hub
  but ride the same trains.

### 4.6 Entry point + UI

- New module `app/journey/federated_planner.py`; called from `fanout()` after
  the per-session loop when §4.1 step 3 found no end-to-end result.
- Each stitched itinerary is tagged with its **contributing sessions + hub**
  (`{nap-eu-corridors → nap-ch-rail @ Basel}`); the existing `_origin_flag`
  set-arithmetic generalises so the card can show "via Basel · corridors +
  nap-ch-rail".

---

## 5. Hard parts / open questions

1. **Hub discovery accuracy.** A curated gateway list is reliable but manual;
   dynamic discovery (leg-1 reachable ∩ dest-country ∩ hubs) is automatic but
   needs a cheap "what can leg 1 reach in country X" query. Start curated.
2. **Latency & fan-out.** Worst case = N hubs × k arrivals leg-2 queries.
   Bound both; parallelise across hubs; cache hub reachability. Set a hard
   budget (e.g. ≤ ~8 OTP calls per federated query).
3. **MCT data.** Flat default vs per-hub table — affects realism at big
   interchanges (Basel SBB ≠ a country halt).
4. **Multi-hop (>1 transfer / >2 sessions).** Paris→…→a third country. Explodes
   combinatorially; **out of scope for v1** (exactly 2 sessions, 1 stitch).
5. **Over-counting vs combined graph.** If the operator *also* runs a combined
   FR+CH graph (osm-geo §6), the same journey could come from both — dedup via
   fingerprint, or treat them as alternative "sources".
6. **Recording shape.** A stitched journey references trips from two sessions —
   `recorder.py` / `journey_trips` may need a `via_hub` + multi-session origin
   representation.
7. **Correctness signal.** Validate stitched results against a real timetable
   (sbb.ch / sncf-connect) for Paris→Fribourg before trusting them.

---

## 6. Alternatives considered

- **Combined regional graph** (osm-geo §6): one geo-cropped FR+CH graph routes
  the transfer natively, no stitching code. **Faster to ship, more robust for
  transfers**, but mixes networks (loses the per-NAP comparison separation) and
  the graph grows with each added country. Good for "I just want the journey";
  the federated planner is better for "keep each NAP isolated and stitch on
  demand". They can coexist.
- **OJP between sub-planners** (`cross-nap-federation-design.md` §6): speak OJP
  between the federated planner and each session so non-VIATOR planners can
  join. Strategic, multi-day; not needed for FR↔CH stitching.

---

## 7. Phasing

| Phase | Scope | Trigger |
|---|---|---|
| **1 — MVP** | 2 sessions, **1** stitch, **curated** FR↔CH gateway hubs ({Basel, Genève, Bern, Lausanne}), flat MCT. Target: **Paris → Fribourg** end-to-end. `federated_planner.py` + fanout fallback hook + UI label. | This design approved |
| **2 — dynamic hubs** | Replace the curated list with leg-1-reachable ∩ dest-country ∩ tier-1 hubs. | Phase 1 validated |
| **3 — per-hub MCT** | Real connection times per interchange from the MCT layer. | Realism gaps observed |
| **4 — multi-hop / 3+ sessions** | Journeys crossing two borders (FR→CH→IT). Combinatorial control. | Demand |
| **5 — OJP sub-planner wire** | Open the federation to external planners. | Strategic |

---

## 8. Worked example — Paris → Fribourg

1. Dispatch: origin FR, destination CH. No single session has both → stitch.
2. Hubs (curated FR↔CH): Basel, Bern (Genève/Lausanne also tried).
3. Leg 1 (`nap-eu-corridors`): Paris Gare de Lyon → Basel SBB, depart 08:00 →
   TGV Lyria, arrive Basel 11:03.
4. Leg 2 (`nap-ch-rail`): Basel SBB → Fribourg, depart ≥ 11:03 + 10 min MCT →
   SBB IC, 11:34 → Fribourg 12:30.
5. Stitch → "Paris 08:00 → Fribourg 12:30, 1 change at Basel SBB (10 min)",
   tagged `corridors + nap-ch-rail @ Basel SBB`. Bern path offered as an
   alternative if competitive.

---

## 9. Testing

- **Unit** — hub-candidate intersection logic (pure, against fixture station
  sets); stitch assembly + total-time/transfer math; MCT enforcement (reject a
  leg-2 departing before arrival+MCT); fingerprint dedup of a stitch vs an
  equivalent through-train.
- **Integration** — federated query end-to-end against two fixture sessions
  (mock `fetch_plan`) returning a leg each; assert one stitched itinerary with
  the right hub and times; assert the fallback only fires when single-session
  returns nothing.
- **Manual / live** — Paris→Fribourg vs a real sbb.ch timetable a week out.

---

## 10. Recommendation

Build **Phase 1** (2-session, single-stitch, curated FR↔CH hubs, flat MCT)
against **Paris → Fribourg** as the proving case — it reuses `fetch_plan`'s
time + UIC anchoring, `transit_fingerprint`, and `network_coverage` hubs, so
the new surface is the stitcher + the fanout fallback hook, not new routing.
Keep the per-country sessions untouched (the comparison mission is preserved);
the planner is a layer above. Defer dynamic hub discovery, per-hub MCT, and
multi-hop until the single FR↔CH stitch is proven end-to-end.
