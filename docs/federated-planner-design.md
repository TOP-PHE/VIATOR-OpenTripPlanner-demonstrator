# Federated Planner — cross-session routing (hub-and-spoke)

A design for routing a journey **across sessions that each hold a disjoint
network**, by stitching **domestic legs onto the cross-border "corridors"
spine** at connection hubs that are **derived from the feeds, not hand-picked**.
Motivating case: **Paris → Fribourg (CH)** — *Paris →(corridors: TGV Lyria)→
Basel →(nap-ch-rail: SBB IC)→ Fribourg* — which no single graph can route
because Paris and Fribourg never co-exist in one session.

**Status**: Design proposal (no code). The query-time stitching layer — the
deeper end of `cross-nap-federation-design.md`, which only covered a single
cross-border *through-train* (dedup, no transfer).
**Audience**: Platform admins, demonstrator product owners, implementers.

> Pairs with:
> - [cross-nap-federation-design.md](cross-nap-federation-design.md) — the
>   *through-train* MVP (one Lyria, both NAPs describe it → dedup). This doc is
>   the next layer: journeys that **change trains across networks**.
> - [VIATOR_Federation_Strategy.md](VIATOR_Federation_Strategy.md) — why
>   single-graph pan-Europe was rejected; the session classes.
> - [osm-geographic-scope-design.md](osm-geographic-scope-design.md) — the
>   *combined-graph* alternative (§7): one geo-cropped multi-country graph
>   routes a transfer natively. This doc is the **keep-sessions-separate** path.

---

## 1. Problem

OTP routes inside **one graph**. The journey API (`app/api/journey.py`) has two
query modes, neither of which stitches:

- `plan()` / `_query_session()` — ask **one** session for a full O→D itinerary.
- `fanout()` — ask **every serving session** the **same** O→D and
  **compare/merge** results (the OJP-comparison + origin-flag feature).
  `_origin_flag()` does set-arithmetic over which sessions returned a trip; it
  does **not** combine partial legs.

So **Paris → Fribourg** returns nothing:

| session | has Paris? | has Fribourg? | result |
|---|---|---|---|
| `nap-eu-corridors` | ✅ | ❌ (Fribourg is Swiss-domestic, not on a cross-border route) | LOCATION_NOT_FOUND |
| `nap-ch-rail` | ❌ | ✅ | LOCATION_NOT_FOUND |
| `nap-fr-rail` | ✅ | ❌ | LOCATION_NOT_FOUND |

The journey exists (Paris→Basel on TGV Lyria, then Basel→Fribourg on an SBB
InterCity) but the **transfer at Basel spans two graphs**. Closing that is the
federated planner's job.

---

## 2. The model: corridors as the international spine

The key realisation that shapes this design: **`nap-eu-corridors` already *is*
the international meta-network.** It holds *every* cross-border train and routes
the whole international middle **natively, in one graph** — confirmed live:
**London→Basel and Paris→Milano are themselves multi-country, multi-leg
journeys resolved inside the corridors graph.** Corridors isn't "one border";
it's the connected cross-border layer of Europe.

So federation is **hub-and-spoke**, not arbitrary pairwise stitching:

```
[domestic origin]  →  [corridors spine: ALL international travel, in-graph]  →  [domestic destination]
   spoke (leg 1)                       spine (leg 2)                              spoke (leg 3)
        └─ stitch @ entry gateway ─┘                       └─ stitch @ exit gateway ─┘
```

- The **spine** (corridors) does all the cross-border routing — however many
  countries the middle crosses — in one graph.
- A **spoke** is a domestic leg in the origin or destination country.
- The planner adds a spoke **only if the endpoint isn't already a spine
  station**, so a journey needs **at most 2 stitches**, regardless of distance:

| journey | origin on spine? | dest on spine? | stitches |
|---|---|---|---|
| Paris → Fribourg | yes (Paris is international) | no | **1** (corridors → ch-rail @ Basel) |
| Fribourg → Paris | no | yes | **1** |
| Stockholm → Naples | no | no | **2** (se-rail → corridors @ Copenhagen; corridors → it-rail @ Milano) |
| Paris → Zürich | yes | yes (Zürich is international) | **0** — corridors alone (today) |

This scales to the extreme case **without** a pan-Europe combined graph (the
RAM blocker) and **without** a separate routing-over-a-meta-graph engine —
because corridors *is* the international router; the planner only reasons about
the first/last domestic legs.

### Is / isn't
- **Is:** a query-time orchestrator that attaches domestic spokes to the
  corridors spine at data-derived gateways, time-coordinated, as a **fallback**
  when no single session serves the whole O→D.
- **Isn't:** a new router (it drives existing per-session OTP planners); the
  combined-graph approach (§7); a fares layer; or — in v1 — multi-spine
  journeys (§5).

---

## 3. What's already reusable

| Building block | Where | Use here |
|---|---|---|
| Time-anchored, UIC-keyed leg query | `app/journey/otp_client.py::fetch_plan(when=…, from_stop_id/to_stop_id=…)` | Route one leg from a given departure time, keyed on UIC |
| One-session query wrapper | `app/api/journey.py::_query_session` | Per-leg dispatch + status/timing |
| Cross-engine fingerprint | `app/journey/signature.py::transit_fingerprint` | Dedup a stitch vs a through-train; match a train across feeds |
| UIC → country | `app/models/master_station.py` | Endpoint country detection; hub country |
| Per-session served stops | each session's GTFS `stops.txt` (UIC) — or the OTP graph | **Hub derivation** (§4.2): intersect two sessions' stop sets |
| Major-hub tiers | `app/models/network_coverage.py` | Rank/bound hub candidates |
| Origin-flag set-arithmetic | `app/api/journey.py::_origin_flag` | Label which sessions + hub contributed each itinerary |
| Recording layer | `app/journey/recorder.py` | Stitched queries recorded like any fanout |

No new OTP wire protocol and no session-lifecycle automation are needed for v1.

---

## 4. Design

### 4.1 Dispatch + "does this need stitching?"

1. Resolve origin & destination **country** (UIC → `master_stations`; the form
   sends UICs when the operator picks from the dropdown).
2. Build a `country_iso → [session_id]` index from
   `session.config.sources.providers[].country_iso` (already in the schema).
   Mark which session is the **spine** (the cross-border / corridors one).
3. **Try the normal path first** (single session / fanout). If any session
   returns an end-to-end itinerary, use it — **stitching is a fallback**, so
   through-trains (and the 0-stitch cases above) always win and the common path
   stays fast.
4. Otherwise: route through the spine, adding a domestic spoke at whichever
   endpoint isn't already a spine station.

### 4.2 Hubs are derived from the feeds — not curated

A connection hub is, precisely, **a station served by trains in *both* sessions
being joined**. So the candidate set is a set intersection, computed
automatically per session-pair:

```
hubs(spine, domestic) = { UIC stop served in spine } ∩ { UIC stop served in domestic }
```

For Paris→Fribourg that's `corridors ∩ nap-ch-rail` = the gateway cities (Basel,
Genève, Bern, Zürich, Lausanne) — because the international trains *call* there
and those stations are *also* in the Swiss domestic feed. **No human picks
them; the overlap of the feeds picks them.** An international stop with no
domestic connection simply isn't in the intersection.

- Compute the per-session UIC stop set from its staged GTFS `stops.txt` (or the
  built graph) at promote time; cache the pairwise intersections.
- **Rank/bound** the candidates (degree, or `network_coverage` tier) and keep
  the top-N near the destination country to cap the query count — but the
  candidate set itself is data-derived, never a hand-maintained list.

### 4.3 The spoke → spine → spoke query (time-coordinated)

```
T = requested earliest departure
# entry: if origin not on the spine, hop the origin-domestic leg to a hub
# spine: route the international middle in corridors (origin-or-entry-hub → exit-hub-or-dest)
# exit:  if dest not on the spine, hop the destination-domestic leg from a hub
for each viable (entry_hub?, exit_hub?) combination (parallel across hubs, bounded):
    legs = []
    if origin off spine:  legs += fetch_plan(origin_session, origin → entry_hub, when=T)
    legs += fetch_plan(spine, (entry_hub or origin) → (exit_hub or dest), when=arrive(prev)+MCT)
    if dest off spine:    legs += fetch_plan(dest_session, exit_hub → dest, when=arrive(prev)+MCT)
    emit stitch(legs)
```

`fetch_plan`'s `when` (earliestDeparture) + `from_stop_id`/`to_stop_id` (UIC)
are exactly the primitive this needs. Each downstream leg is anchored on the
previous leg's arrival, so it's sequential along a chain but **parallel across
hub combinations**; bound the hubs (and arrivals per hub) to cap OTP calls.

### 4.4 Minimum connection time (MCT)

Each onward leg departs ≥ `arrival + MCT(hub)`. v1: a flat default (rail↔rail
same station, ~10 min). Later: per-hub MCT from the MCT enrichment layer (a big
interchange ≠ a country halt).

### 4.5 Stitch, rank, dedup

- A stitched journey = the legs joined by transfers at the hub(s); total
  duration = last arrival − first departure; transfers summed + one per stitch.
- **Rank** by arrival, then duration, then transfers.
- **Dedup** with `transit_fingerprint`: drop a stitch that's equivalent to a
  through-itinerary a session already returned, and collapse stitches that
  differ only by hub but ride the same trains.

### 4.6 Entry point + UI

- New module `app/journey/federated_planner.py`, called from `fanout()` after
  the per-session loop when §4.1 step 3 found no end-to-end result.
- Each stitched itinerary is tagged with its contributing sessions + hub(s)
  (`{nap-eu-corridors → nap-ch-rail @ Basel}`); the existing `_origin_flag`
  set-arithmetic generalises so the card shows "via Basel · corridors +
  nap-ch-rail".

---

## 5. What it can't do (the limit)

The model allows **one connected corridors ride** between the (optional)
domestic spokes. So the pattern it **cannot** express is a **domestic leg in
the middle of the international portion** — i.e. *international → domestic →
international*: leave the cross-border network, ride a purely-domestic train,
then re-enter the cross-border network. There's no place for a domestic leg
*between* two spine rides.

Why this is acceptable in practice:
- **Rare for rail.** A mid-journey change is almost always onto *another*
  cross-border train — which is already in corridors and handled in-graph.
- **Usually avoidable.** Corridors typically offers an all-international
  alternative that doesn't need the domestic detour.
- **The right fix when it bites** is "that service belongs in corridors": the
  cross-border filter keeps **any** route spanning 2+ countries, including
  regional cross-border trains — so genuine corridor services should be on the
  spine, not stranded in a domestic feed.

Other (correct) non-results: an O→D with **no cross-border service at all**
between the countries simply has no train — the planner returns nothing, which
is right.

The true general case (route over a graph-of-networks with arbitrary
domestic↔international interleaving) needs a meta-router — deferred to §6/§8 and
only justified if real demand shows the spine model leaving journeys unfound.

---

## 6. Open questions

1. **Computing per-session stop sets.** From staged GTFS `stops.txt` (cheap,
   pre-build) vs the built graph (authoritative, post-build). Probably the
   feed, refreshed at promote.
2. **MCT data.** Flat default vs per-hub table.
3. **Latency & fan-out.** Cap total OTP calls per federated query (e.g. ≤ ~8);
   parallelise across hubs; cache hub reachability.
4. **Recording shape.** A stitch references trips from 2-3 sessions —
   `recorder.py` / `journey_trips` may need a `via_hub` + multi-session origin
   representation.
5. **Multi-spine / >2 stitches.** Two separate corridors rides with a domestic
   leg between (the §5 limit) — out of scope for v1.
6. **Correctness signal.** Validate stitched results against a real timetable
   before trusting them.

---

## 7. Alternatives considered

- **Combined regional graph** (osm-geo §7): one geo-cropped FR+CH graph routes
  the transfer natively, no stitching code. Faster, more robust for transfers,
  but mixes networks (loses per-NAP comparison) and grows with each country.
  Good for "just give me the journey"; the federated planner is for "keep each
  NAP isolated, stitch on demand". They can coexist.
- **OJP between sub-planners** (`cross-nap-federation-design.md` §6): speak OJP
  between the planner and each session so external planners can join. Strategic,
  multi-day; not needed for the spine model.

---

## 8. Phasing

| Phase | Scope | Trigger |
|---|---|---|
| **1 — MVP** | Spine + **one** domestic spoke, **data-derived** hubs (corridors ∩ domestic intersection), flat MCT. Target: **Paris → Fribourg** (1 stitch). `federated_planner.py` + fanout fallback hook + UI label. | This design approved |
| **2 — two spokes** | Spoke at *both* ends (origin + destination domestic). Target: **Stockholm → Naples** (2 stitches) once those domestic sessions exist. | Phase 1 validated + 2nd/3rd domestic session |
| **3 — per-hub MCT + ranking polish** | Real connection times; smarter hub ranking. | Realism gaps |
| **4 — meta-router** | Arbitrary domestic↔international interleaving (the §5 limit), route over a graph-of-networks. | Demand the spine model can't meet |
| **5 — OJP sub-planner wire** | Open federation to external planners. | Strategic |

---

## 9. Worked examples

**Paris → Fribourg (1 stitch).** Origin Paris is a spine station → no origin
spoke. Spine leg: `nap-eu-corridors` Paris Gare de Lyon → Basel SBB, 08:00 →
TGV Lyria → 11:03. Hub Basel ∈ `corridors ∩ nap-ch-rail`. Exit spoke:
`nap-ch-rail` Basel → Fribourg, depart ≥ 11:03 + 10 min → SBB IC 11:34 →
Fribourg 12:30. Result: "Paris 08:00 → Fribourg 12:30, change at Basel SBB",
tagged `corridors + nap-ch-rail @ Basel`.

**Stockholm → Naples (2 stitches, illustrative).** Origin spoke `nap-se-rail`
Stockholm → Copenhagen (entry hub ∈ `se-rail ∩ corridors`). Spine
`nap-eu-corridors` Copenhagen → Milano (Öresund + DE ICE + Brenner, **all
in-graph**). Exit spoke `nap-it-rail` Milano → Naples (exit hub ∈
`corridors ∩ it-rail`). Two stitches, one connected international middle — no
pan-Europe graph.

---

## 10. Testing

- **Unit** — the hub intersection (pure, fixture stop sets); stitch assembly +
  total-time/transfer math; MCT enforcement (reject an onward leg departing
  before arrival+MCT); fingerprint dedup of a stitch vs an equivalent
  through-train; the "needs stitching?" decision (spine-station endpoints ⇒ 0/1
  spoke).
- **Integration** — federated query end-to-end against fixture sessions (mock
  `fetch_plan`) returning a leg each; assert one stitched itinerary with the
  right hub/times; assert the fallback only fires when single-session returns
  nothing.
- **Manual / live** — Paris→Fribourg vs a real sbb.ch timetable a week out.

---

## 11. Recommendation

Build **Phase 1** — spine + one data-derived domestic spoke, flat MCT — against
**Paris → Fribourg**. The hub set comes from the feed intersection (no manual
list), the spine reuses the corridors graph that already routes the
international middle, and the leg queries reuse `fetch_plan`'s time + UIC
anchoring — so the new surface is the stitcher + the fanout hook, not new
routing. Keep the per-country sessions untouched (the comparison mission is
preserved). Defer the second spoke (Phase 2) until a second domestic session
exists, and the meta-router (Phase 4) until the spine model demonstrably leaves
real journeys unfound.
