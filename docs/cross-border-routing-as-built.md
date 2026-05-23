# Cross-border routing — as-built record & behaviour-change log

**Purpose.** A single, version-tagged record of *everything we changed to make
cross-border journeys work* and *every adjustment that brought the federated
results closer to the SBB / OJP reference*. The design intent lives in
[federated-planner-design.md](federated-planner-design.md) and
[provider-source-modes-design.md](provider-source-modes-design.md); **this doc
is the as-built truth** where the shipped behaviour deviates from or refines
those proposals.

**Key framing — we did not modify OTP.** Every serving session runs **stock
OpenTripPlanner 2.9**. The behaviour changes are in the two layers *around* OTP:

1. **What enters OTP** — the GTFS we feed each session's graph (the cross-border
   filter + the corridors session model).
2. **How we drive & post-process OTP** — the federated planner that issues
   per-leg OTP queries and stitches/ranks the results.

So "changing OTP behaviour" here means changing its **inputs** and the
**orchestration/ranking** on top, never OTP internals.

---

## Part 1 — Making cross-border work (the inputs to OTP)

### 1.1 The corridors session model
`nap-eu-corridors` is the **international spine**: a session whose GTFS contains
*only* the cross-border rail services, so OTP routes the whole international
middle (e.g. London→Basel, Paris→Milano) natively in one graph. National
sessions (`nap-fr-rail`, `nap-ch-rail`, `nap-sp-rail`, …) stay isolated for the
per-NAP comparison mission. See `VIATOR_Federation_Strategy.md`.

### 1.2 The cross-border filter
`app/gtfs_cross_border_filter.py` — `filter_to_cross_border(input, output, *,
rail_only=True, home_country=None)`. Extracts the cross-border subset of a big
national feed:
- **Cross-border test:** keep a route iff its stops span **2+ distinct
  countries**.
- **rail_only** (default): drop ferries/buses/trams so they can't masquerade as
  cross-border rail.
- **home_country** (origin-ownership): keep only trips *departing* the home
  country, so several national feeds can each contribute their own cross-border
  services without duplicating the same train (dedup across NAPs).
- Stdlib-only / DB-free (streams `stop_times.txt` in two passes), so it can run
  inside the worker at refresh without the app DB layer.

### 1.3 How a stop's country is determined — **two methods** (the #127 adjustment)
The cross-border test needs each stop's country. The filter resolves it in this
order:

1. **UIC country prefix** in the `stop_id` (primary, exact). SNCF/SBB feeds key
   stops by UIC, so `87…`→FR, `85…`→CH, etc. A 2-digit prefix that isn't a
   *recognised* UIC country (whitelist `UIC_COUNTRY_NAMES`) is rejected — this is
   the `#15` guard that stops SBB-internal codes (e.g. Evian `1400001`→`14`)
   from faking a crossing.
2. **Point-in-polygon on the stop's coordinates** *(v0.1.42.04, PR #127)* —
   **only when the `stop_id` carries no UIC-shaped code at all**. Renfe's GTFS
   uses 5-digit internal codes (`17000`, `37606`, `87303`), so *every* Renfe
   stop read as "unknown country" and the filter kept **0** cross-border routes
   (a `0.00 MB` derived feed) even though the feed reaches Marseille / Lyon /
   Porto. The fallback classifies such stops by location via
   `osm_geo.country_for_point` against the bundled country-borders GeoJSON
   (`app/data/country_borders.geojson`) — still stdlib / DB-free.
   - **Deliberately surgical:** the fallback fires only when there is *no*
     UIC-shaped code. A code that *has* a UIC-shaped prefix which merely isn't
     whitelisted (`1400001`→`14`) is still left "unknown" — the `#15` guard is
     untouched.
   - Result: Renfe's ES↔FR / ES↔PT services are now detected and the
     **Madrid→Toulouse** (RENFE AVE INT + SNCF) journey routes.

### 1.4 Loading a cross-border feed from the UI — the derived-provider flow
Instead of running the filter offline, a corridors-session provider can
**derive** its feed from a national one. Source mode `cross_border_filter`
(`app/ingestion.py`, `app/api/admin/sessions.py`, `app/templates/admin/sessions.html`):

- The provider stores a **link** `derived_from = {session_id, provider_id}` +
  filter params (`home_country`, `rail_only`) — it owns no feed of its own.
- On **refresh** the worker runs `filter_to_cross_border` on the linked national
  feed into the provider's slot, surfacing the kept-routes stats.
- **Cascade (single source of truth)** — refreshing or uploading the national
  feed auto re-derives every linked cross-border view, so the operator loads a
  national feed once and the corridors view follows. No drift, no forgotten file.
- **UI** — the provider card's Source toggle gains "Cross-border filter
  (derived)", with **dropdowns** for the national session + provider (no typos).

Shipped across PRs #122 (backend) + #123 (UI) + #124 (cascade) + #125
(per-provider refresh) + #126 (skip-reason surfaced) + #127 (coordinate
detection) + #128 (dropdowns). See the version table at the end.

---

## Part 2 — Bringing federated results closer to SBB / OJP (the orchestration)

The federated planner (`app/journey/federated_planner.py`) is a **fallback**:
when no single session routes origin→destination, it stitches a domestic leg
onto the corridors spine at a connection hub. Phase 1 shipped, then four
adjustments tuned it toward SBB-quality results. **These are the adjustments
that changed the routing behaviour you see.**

### 2.1 v0.1.41.01 (PR #117) — Phase 1 hub-and-spoke
`plan_federated`: find a session serving the origin and one serving the
destination that **share a connection hub** (UIC intersection of their served
stops, derived from the feeds), route origin→hub then hub→destination, stitch.
Surfaced under a separate `federated_trips` group in the journey UI.

### 2.2 v0.1.41.02 (PR #118) — hubs ranked by route proximity, not UIC string
**Problem:** hubs were chosen with `sorted(shared)[:MAX_HUBS]` — lexicographic
by UIC string. Swiss `85…` gateways sort behind every lower-numbered code, so
the cap dropped **Basel SBB (`8500010`)** and the corridors pair produced no
stitch. **Change:** `rank_hubs()` scores each hub by **great-circle detour**
`haversine(origin,hub) + haversine(hub,dest)` and keeps the most on-route ones.
`MAX_HUBS` 6→8.

### 2.3 v0.1.41.03 (PR #119) — legs route by exact station, not just coordinate
**Problem:** federated legs queried OTP by coordinate only, so a hub attached to
the *nearest* stop (wrong platform/station at a big interchange). **Change:**
pass `from_stop_id`/`to_stop_id` as `<feedId>:<uic>` (derived from the session's
provider id); OTP falls back to coordinate routing when a feed isn't UIC-keyed,
so it's strictly precise-where-it-can-be.

### 2.4 v0.1.41.04 (PR #120) — prefer destination-country hubs
**Problem:** even proximity-ranked, Paris→Fribourg cut at French waypoints
(Besançon, Avoudrey) — they sit on the straight Paris→Fribourg line so they
scored the lowest detour, filling `MAX_HUBS` before any Swiss gateway was tried.
Cutting in France forced the *Swiss* network to claw back across the border on
regional lines (12 legs, ~9 h). **Change:** `rank_hubs` now tiers
**destination-country hubs first**, then detour within the tier — the spoke
(destination network) is dense only inside its own country. Swiss gateways now
win and the ~5 h Genève/Lausanne stitch surfaces. (Diagnostic confirmed the
individual legs were already fine; only the hub *choice* was wrong.)

### 2.5 v0.1.41.05 (PR #121) — generalized-time ranking + clean hub transfer
Two presentation/ranking fixes after the routing came good:
- **Ranking by generalized time** — `dedup_and_rank` was sorting by *earliest
  arrival*, which put a 6 h 29 / 3-change journey above a clean 5 h 19 / 1-change
  one that merely arrived a few minutes later. Now it sorts by
  `duration + TRANSFER_PENALTY_SECONDS × changes` (a change is worth ~20 min of
  riding), tie-broken by arrival — so the clean journey tops the list.
- **Phantom hub-walk cleanup** — each per-leg OTP search wraps its ride in
  access/egress walks, so a stitch showed two phantom walks at the hub
  (`Lausanne → Destination` then `Origin → Lausanne`). `assemble_stitch` now
  drops each non-final trip's trailing walk and each non-first trip's leading
  walk; the genuine origin-access / destination-egress walks are kept and the
  hub still counts as one transfer.

**Net result:** Paris→Fribourg now returns the **TGV Lyria + SBB IC via Lausanne
(~5 h 19, 1 transfer)** as the top option — comparable to SBB's own ~5 h /
1-change result.

---

## Part 3 — Tuning knobs & known limits

**Knobs** (`app/journey/federated_planner.py`):
| Constant | Value | Effect |
|---|---|---|
| `TRANSFER_PENALTY_SECONDS` | 1200 (20 min) | How much a change "costs" in ranking; raise to push multi-change options further down |
| `MAX_HUBS` | 8 | Hubs tried per session pair (after proximity + dest-country ranking) |
| `MAX_LEG1_OPTIONS` | 3 | Origin→hub departures tried per hub |
| `DEFAULT_MCT_SECONDS` | 600 (10 min) | Flat minimum connection time at a hub |
| `MAX_RESULTS` | 5 | Stitched itineraries returned |

**Limits (by design):**
- The federated planner stitches **two independent OTP searches** with a fixed
  MCT, so it won't perfectly match a single-graph through-search (occasional
  looser connection / a couple extra legs vs SBB).
- It can't express **international → domestic → international** (a domestic leg
  *between* two spine rides) — see federated-planner-design.md §5. The fix when
  it bites is "that service belongs in corridors" (the filter keeps any 2+
  country route).
- Coordinate-based country detection uses **coarse 50 m borders**; a stop within
  a few km of a border could resolve to the neighbour. Fine in practice (major
  stations are well inside their country); the UIC prefix stays primary where
  present.

---

## Version map

| Version | Theme | PRs |
|---|---|---|
| v0.1.41.01 | Federated Phase 1 (hub-and-spoke fallback) | #117 |
| v0.1.41.02 | Hubs ranked by route proximity | #118 |
| v0.1.41.03 | Legs route by exact station (stop-id) | #119 |
| v0.1.41.04 | Destination-country hub preference | #120 |
| v0.1.41.05 | Generalized-time ranking + hub-walk cleanup | #121 |
| v0.1.42.01 | Cross-border derived-provider source + cascade + UI | #122, #123, #124 |
| v0.1.42.02 | Per-provider refresh runs the filter for derived providers | #125 |
| v0.1.42.03 | Refresh result shows the skip reason | #126 |
| v0.1.42.04 | **Coordinate-based country detection** (Renfe non-UIC feeds) | #127 |
| v0.1.42.05 | Session/provider dropdowns on the derived card | #128 |
