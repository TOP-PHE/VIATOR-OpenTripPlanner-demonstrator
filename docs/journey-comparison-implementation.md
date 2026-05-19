# Journey-comparison implementation summary

**As of v0.1.35.04 — 2026-05-18.**

This is the operator-facing summary of the OJP-reference-comparison
feature: what's implemented, what OTP tuning we did to bring its
output as close to OJP's as practical, and — importantly — what we
have *not* done and why. The longer design rationale lives in
[`ojp-reference-comparison-design.md`](./ojp-reference-comparison-design.md);
this doc is the "what + how + state of play" reference.

Companion docs:
- [`nap-ch-rail.md`](./nap-ch-rail.md) — the Swiss nation-wide rail
  session setup (GTFS source, rail-only filter, OTP build settings,
  why TransferConstraints is disabled, OSM extract choices).
- [`ojp-reference-comparison-design.md`](./ojp-reference-comparison-design.md)
  — the design spec, with appendices documenting the Phase-0 spike
  and the verified OJP 2.0 request/response shapes.

---

## 1. What got shipped

A "tick a checkbox, see VIATOR's results next to the canonical Swiss
OJP reference" feature, with structured comparison telling the
operator at a glance which journeys both engines agree on and which
each finds alone.

### 1.1 Versions and PRs

| Version | PR | Date | What it added |
|---|---|---|---|
| v0.1.34.03 | [#79](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/79) | 2026-05-14 | **Phase 1** — live side-by-side. OJP adapter (`app/journey/ojp_client.py`), `compare_ojp` branch in `/api/journey/fanout`, search-form checkbox, config-UI section, side-by-side render. No persistence, no scoring — just "show both, let the operator judge". |
| v0.1.35.01 | [#85](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/85) | 2026-05-18 | **Phase 2** — structured diff. `transit_fingerprint` (DB-free, walks stripped, 4-dp lat/lon), `_build_comparison` bucketing, `comparison_summary` on the fanout response, per-card pills + summary strip in the UI. |
| v0.1.35.02 | [#86](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/86) | 2026-05-18 | **Fingerprint** — UIC-aware token (was lat/lon-only). Parses 7-digit UIC out of `SBB:NNNNNNN` ids. Added a `{}` debug button to OJP cards. (Fixed: Pontarlier → Geneva via Lausanne fingerprinted as ojp_only because OTP returns platform-precise coords. UIC sidesteps coord precision.) |
| v0.1.35.03 | [#87](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/87) | 2026-05-18 | **Fingerprint** — Swiss SLOID DSN reconstruction. OJP emits `ch:1:sloid:7000:4:7` not `ch:1:sloid:8507000:0:7` — the `850` country prefix is dropped, only the 4-digit DSN survives. Parser now prepends `850` for `ch:1:` namespace ids. (Fixed: Bern → Geneva IR15 fingerprinted as ojp_only.) |
| v0.1.35.04 | [#88](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/88) | 2026-05-18 | **OTP defaults** — `num_itineraries` 8 → 12, `searchWindow` 4 h → 6 h. Wider slate for 1–2-transfer alternatives. |
| v0.1.35.05 | [#91](https://github.com/TOP-PHE/VIATOR-a-MERITS-OpenTrip-Planner-demonstrator/pull/91) | 2026-05-18 | **transferSlack** — 2 m → 5 m. Applies the long-prescribed `nap-ch-rail.md §5.3` mitigation for `TransferConstraints` being disabled; eliminates physically-infeasible 2-min transfers at major hubs. |
| v0.1.35.06 | (this release) | 2026-05-18 | **OJP pagination** — `fetch_reference_paginated` issues up to 4 sequential `TripRequest`s with successively-later anchor times until OJP's coverage catches up to OTP's 6 h window. Closes the structural alignment gap that left legitimate trains in OTP's tail bucketed as spurious `otp_only`. See §2.6 below. |

### 1.2 What the operator sees

When **"Compare against Swiss OJP"** is ticked on the search form
and the OJP call succeeds, the result panel shows:

- A **summary strip** above the cards:
  `Comparison vs Swiss OJP: N common · M OTP-only · K OJP-only`
- A **coloured pill** on each card:
  - green `✓ common` — both engines returned this journey
  - blue `OTP only` — only VIATOR/OTP found it
  - amber `OJP only` — only the OJP reference found it
  - grey `no transit` — walk-only itinerary (excluded from buckets)
- A **`{}` button** on every card (both OTP and OJP) opening a modal
  with the raw normalised JSON. Critical for diagnosing the rare cases
  where the comparison surprises the operator.

The OJP reference is rendered as a separate **"Reference — Swiss OJP"**
panel beneath VIATOR's own results. Counts in the summary strip are
distinct itinerary fingerprints, not raw card counts — within-engine
duplicates collapse.

---

## 2. OTP-side tuning — what we did to approximate OJP

OTP and OJP are different routing engines. OJP is the canonical Swiss
reference (operated by the SBB on behalf of the federal transport
office); OTP is what VIATOR runs internally on the same GTFS data the
NAP publishes. We've tuned OTP across several axes to bring its
output reasonably close to what OJP produces for the same query.

### 2.1 GTFS-source filtering: rail-only

**Why**: The Swiss NAP publishes a single nation-wide GTFS bundle
covering **all** public transport — mainline rail, S-Bahn, regional
trains, the entire Postauto bus network, urban trams, funiculars,
boats, gondolas. A naive integration of the full bundle OOMs at
*any* heap size because OTP's `TransferConstraints` index
construction scales unfavourably with the number of constrained
transfers in such a dense feed. The Swiss OJP reference, by design,
serves multimodal trips — but our comparison target is rail.

**What**: A pre-graph-build GTFS filter strips routes whose
`route_type` is not in {`2` (rail), `109` (S-Bahn), `116` (cog
rail)}. The filter is in the session bootstrap; details and the
heap-size investigation in [`nap-ch-rail.md`](./nap-ch-rail.md).

**Effect on comparison**: When the operator asks for Bern → Geneva
rail, both OTP and OJP plan rail itineraries. They sometimes disagree
about which rail combinations to surface (see §3), but neither
returns a Postauto bus or a tram, which is the correct intersection
for rail-corridor planning.

### 2.2 `TransferConstraints` disabled

**Why**: As above — the post-RAPTOR `TransferConstraints` index does
not survive the full Swiss feed at *any* heap size; we tested 16 GB
through 72 GB, all OOM at the same construction step. The constrained
transfers in `transfers.txt` are needed to enforce GTFS-RT real-time
transfer guarantees (passenger A's late train holds passenger B's
connecting train); we don't run real-time and don't need this.

**What**: `otp-config.json` flips `TransferConstraints` to `false` for
the nap-ch-rail session.

**Effect on comparison**: OTP's transfer search reverts to the
default RAPTOR transfer model. Compared to OJP, OTP may pick a
slightly different transfer at a junction (e.g. Lausanne) when
multiple connections are close in scheduled time — but the
*itinerary* (set of trains used) is preserved. The fingerprint
matches on UIC + scheduled minute + route name, so this rarely
causes a cross-engine mismatch.

### 2.3 `planConnection` with stop-id routing

**Why**: After the rail-only filter, OTP's walk graph is sparse around
many smaller stations. Routing by lat/lon caused `LOCATION_NOT_FOUND`
errors for journeys like Pontarlier → Travers because the walk-graph
snap radius didn't reach the rail-only stop nodes.

**What**: When a station's UIC is known to VIATOR (via
`station_xref`), the fanout sends a `stopLocation` reference in OTP's
`planConnection` query — bypassing the walk-graph snap entirely.
Falls back to coordinates if the stop-id approach returns empty.

**Effect on comparison**: OTP's results carry platform-precise
coordinates (e.g. Lausanne CFF platform 5 vs platform 4, lat/lon
~130 m apart, different at 4-dp). The fingerprint deals with this by
matching on UIC, not lat/lon — see §4.

### 2.4 `planConnection` parameters: `first: 12, searchWindow: PT6H`

**Why**: The OJP reference typically returns 6 alternatives spanning
~3–6 hours around the requested time. OTP's defaults (8 itineraries
in a 4-hour window) were narrower than OJP's, with one specific
consequence: OTP's Pareto-optimal top-8 was filling with the direct
trains every 30 min, clipping legitimate 1–2-transfer alternatives
(Bern → Geneva via Neuchâtel + Renens VD on IR66 → IC5 → IR90 —
shorter total time than IR15 direct, more transfers).

**What**: Bumped `fetch_plan` defaults to `num_itineraries=12`,
`search_window_seconds=21600` (6 h). The network-coverage runner
keeps its own overrides (50 / 24 h for full-day matrix runs).

**Effect on comparison**: More variants reach the comparison stage.
Cost on SBB rail-only is ~50 ms per query (RAPTOR is near-quadratic
on `searchWindow`); the 5 s `timeout_ms` ceiling is untouched.

### 2.5 Stop-id formats: OTP vs OJP

Both engines reference the same physical Swiss stations, but the
strings they emit are very different. The fingerprint reconciles
them with a deliberately small canonicaliser:

| Source | Example | Component meaning |
|---|---|---|
| OTP — GTFS `gtfsId` | `SBB:8501120:0:5` | `SBB` feed, full 7-digit UIC `8501120`, sub-stop `0`, platform `5` |
| OTP — non-Swiss in SBB feed | `SBB:8771513` | French border station Frasne, full UIC, no platform suffix |
| OJP — Swiss SLOID | `ch:1:sloid:1120:0:5` | `ch:1` Swiss authority, type `sloid`, **4-digit DSN `1120`** (DiDok-Nummer = UIC less the `850` prefix), sub-stop `0`, platform `5` |
| OJP — non-Swiss in OJP feed | `ch:1:sloid:8771513:0:1` | Same SLOID shape but with full 7-digit UIC because the station isn't Swiss |

The canonicaliser (`_uic_from_stop_id` in `app/journey/signature.py`):

1. Search for a 7-digit chunk → use it directly. Catches all OTP ids
   and any non-Swiss OJP id.
2. If the id starts with `ch:1:`, search for a 4-digit chunk →
   prepend `850` to reconstruct the full UIC. Catches the Swiss
   OJP-emitted SLOIDs.
3. Otherwise → return None → fall back to `lat,lon` rounded to
   3 decimals (~110 m).

The 3-dp fallback is intentional. The 4-dp precision the within-feed
`trip_signature` uses is too tight for cross-engine matching because
OTP and OJP report the same station's centroid with up to ~100 m
disagreement (entrance vs platform vs ticket hall, depending on
which source datafile each engine ingested).

### 2.6 OJP anchor-time pagination *(v0.1.35.06)*

**Why**: OTP's `planConnection` covers a *time window*
(`searchWindow=21600s` = 6 h at the v0.1.35.04 default). OJP's
`TripRequest` covers a *trip count* (~6 alternatives clustered around
the requested time) with no `searchWindow` parameter. On a busy
corridor like Bern ↔ Geneva, a single OJP request spans only ~2 h,
so the comparison strip shows spurious `otp_only` itineraries for
trains in the 2–6 h tail of OTP's range — trains OJP would happily
return if asked, just not in *this* request.

**What**: `fetch_reference_paginated` in `app/journey/ojp_client.py`
issues **up to 4 sequential `TripRequest`s** with successively-later
anchor times. Each follow-up request is anchored at `latest departure
from the previous batch + 1 min`. Loops until any of:

1. OJP returns an empty batch (exhausted at this anchor).
2. All trips in a batch are duplicates of earlier pages
   (`transit_fingerprint` match) → no forward progress.
3. Latest trip's `departure_at` >= `when + 6 h` → OJP coverage caught up.
4. `max_pages=4` reached (rate-limit safety cap).
5. A page raises mid-flight → if we have partial data, return it with
   a warning logged; otherwise propagate so the caller maps to
   `error` / `rate_limited`.

Boundary deduplication uses the same `transit_fingerprint` that
powers `_build_comparison`. The fanout response gains a `pages` field
on `ojp_reference` when pagination fired (>1), so the UI can show
"OJP took N requests to cover the window" if desired.

**Cost**: pages are sequential (next anchor unknown until previous
batch returns). 4 pages × ~600 ms = ~2.4 s for OJP-side wall-time in
the worst case. The fanout runs OJP and OTP in parallel
(`asyncio.gather`), so user-visible wall-time is `max(otp, ojp)`, not
the sum. Rate-limit math: a heavy operator running one search per
second peaks at ~4 OJP calls/sec ≈ 240/min, well past the 50/min
free-tier ceiling — the per-search opt-in toggle is the design safety
net.

---

## 3. What the comparison surfaces — and how to read it

This section is the most useful one for operators using the feature
day-to-day.

### 3.1 The three buckets

| Tag | What it means | How to read it |
|---|---|---|
| **common** | Both engines returned a journey using the same train(s) on the same schedule. | The strongest signal: both engines agree this is a way to make the trip. |
| **otp_only** | VIATOR/OTP returned this; the OJP reference did not. | OTP found a journey OJP doesn't show. Sometimes OTP is exploring graph corners (see §3.2); sometimes OTP genuinely surfaces an option OJP omits. Worth investigating the `{}` JSON. |
| **ojp_only** | The OJP reference returned this; VIATOR/OTP did not. | OJP found a journey OTP doesn't show. **Often this means OTP scored it below its top-12 cutoff**, not that OTP doesn't know the route. See §3.2. |
| **uncomparable** | Walk-only itinerary (no transit spine). | Excluded from the bucket counts in the summary strip. Rendered with a grey "no transit" pill. |

### 3.2 Why divergence is expected, not a bug

The two engines are **legitimately** different in what they consider
a "good" itinerary:

**OTP's `planConnection` returns Pareto-optimal top-N by composite
score.** RAPTOR weighs `duration` AND `transfers` AND `walking time`
into a single ranking, and the connection limits the result to the
best N. A 2-transfer alternative needs to dominate the direct train
on some axis — typically arrival time — to make it into the top N.
Otherwise it's ranked outside and clipped.

**OJP's `TripRequest` returns a fixed set of alternatives that look
*different* in some way.** OJP's transfer penalty is much lighter,
and it deliberately surfaces options the user might want even if
they're not Pareto-optimal — earlier-arriving routes, less-crowded
routes, alternative operators, etc.

So when the comparison strip reads `1 common · 0 OTP-only ·
1 OJP-only` for Bern → Geneva, the "OJP-only" is almost always **a
journey that OTP knows but ranked below the cutoff**, not a journey
OTP can't construct. Click the `{}` on the OJP card and you'll
typically see a 2-transfer route with a slightly earlier arrival
than the direct IR15.

We tried two settings (v0.1.35.04: 12 itineraries / 6 h) to widen
OTP's slate. The Bern → Geneva via Neuchâtel/Renens VD case still
shows OJP-only because OTP fills its 12 slots with direct-train
variants instead of the 2-transfer route. Further widening
(`num_itineraries=20`, `searchWindow=8h`, or lowering OTP's
`transferCost` in `router-config.json`) would surface more
alternatives, but at the cost of:
- response payload size in the UI
- query latency
- the risk of surfacing degenerate routes (the 17-hour-via-France
  result on Bern → Geneva that v0.1.35.04 occasionally produces when
  RAPTOR explores graph corners with the wider window)

**The position we've taken (v0.1.35.04 onward):** the comparison
strip is most useful as a **divergence indicator**, not a target.
Trying to force OTP and OJP into bit-identical alignment isn't
worthwhile and would degrade VIATOR's own results in the process.

### 3.3 Edge cases worth knowing about

**Within-engine duplicates.** Both OTP and OJP can return multiple
itineraries that share the exact same transit spine (e.g. two cards
both using the 09:32 IR15 with different walk/wait variations
around the train). The fingerprint hashes only the transit legs, so
those collapse to one fingerprint. Bucket counts reflect distinct
fingerprints, not card counts. The per-card pill on each duplicate
still renders correctly.

**Walk-only itineraries.** A short search where the destination is
within walking distance returns a walk-only result. Its fingerprint
is the empty string. The bucketer treats `""` as **uncomparable**
(grey pill, excluded from counts) rather than naively matching it
against another walk-only result on the other engine — that would
create false matches between two unrelated walk-only journeys.

**Cross-border journeys.** Pontarlier (French) appears in both the
SBB GTFS feed (UIC `8771500`) and the OJP TripResult. The
canonicaliser handles this correctly: OTP's `SBB:8771500` and OJP's
`ch:1:sloid:8771500:0:1` (full UIC, not DSN, because Pontarlier
isn't Swiss) both yield `UIC:8771500`.

**Non-Swiss feeds.** If we ever add a German DELFI or French OJP
endpoint, the canonicaliser's `ch:1:` namespace check ensures we
don't accidentally prepend `850` to a German ID's DSN. Non-Swiss
namespaces fall through to the 7-digit UIC search, then to the
3-dp lat/lon fallback.

---

## 4. What we deliberately did not do

These are conscious omissions, not oversights. Each has a reason.

### 4.1 Lower OTP's `transferCost`

Lowering OTP's per-transfer penalty in `router-config.json` would
bring OJP-style "even though it has more transfers, it arrives
earlier" routes into the top N. We didn't because:

- It changes RAPTOR scoring globally — VIATOR's own primary use
  (single-engine itinerary planning) would shift toward more
  transfer-heavy routes even when the operator isn't running the OJP
  comparison.
- The change is hard to revert in a small way (each session has its
  own OTP container with its own router config).
- The current divergence is more *informative* (the strip tells the
  operator "OJP keeps a 2-transfer alternative here") than a tuned-
  to-match OTP would be.

### 4.2 Persistence of comparison verdicts

Phase 1 and Phase 2 both display the comparison live and do **not**
persist the verdict in the database. The Phase-3 work item to
persist verdicts for trend analysis is blocked on the §5.4 question
in the design doc (the synthetic OJP session has no row in `sessions`,
so the `session_id` FK can't accept it). A small follow-up that
introduces a `comparison_verdicts` table keyed on
`(otp_session_id, query_hash, fingerprint)` would unblock it — not
in scope for v0.1.35.x.

### 4.3 Per-search "search window" dropdown in the UI

Considered when the operator asked about extending the OTP search
window for afternoon journeys. Decided not to add UI clutter: the
session-level config knob (effectively the `fetch_plan` default)
covers 95% of operator needs. Per-query control is straightforward
to add when an operator asks for it.

### 4.4 OJP rate-limit handling beyond log-and-display

The OJP reference is rate-limited (50 req/min, 20 K req/day per
token). The adapter logs and displays a `rate_limited` panel header
when the limit is hit; it does not queue requests or back off
automatically. Given the comparison is opt-in (per-search checkbox),
the operator naturally throttles their own usage; explicit back-off
would add complexity without clear value at the current scale.

---

## 5. Diagnostic checklist when comparison looks wrong

A quick reference for when the comparison strip surprises you.

1. **Check the OJP panel header.** `· rate-limited`, `· timed out`,
   `· unavailable` all mean the OJP call didn't succeed — the
   comparison can't run. `· no itinerary found · NNNms` means OJP
   reached but returned an empty result; check your date and your
   from/to coordinates.
2. **Click `{}` on an OTP card and an OJP card** for the same
   journey. Compare the rail legs side by side. Look for:
   - **Different stop_ids**: OTP `SBB:8501120:0:5` vs OJP
     `ch:1:sloid:1120:0:5`. Both should fingerprint to `UIC:8501120`
     — if they don't, file a bug.
   - **Different route names**: `IR15` vs `IR-15` or `InterRegio 15`.
     The fingerprint strips whitespace and uppercases, but doesn't
     normalise different short-name conventions. If you see one,
     it's a real signal.
   - **Different times in UTC**: both engines should report scheduled
     times that round to the same minute in UTC. A 1-minute
     difference is unusual and worth a closer look.
3. **Check the version**. `https://<host>/healthz/version` should
   report at least `v0.1.35.04` for the current fingerprint logic.
   Older versions have the pre-fix lat/lon-only or DSN-blind
   matchers.
4. **The "is the route worth surfacing?" question.** If OTP's `{}`
   shows 12 itineraries and they're all variants of the direct
   train, the OJP-only alternative is almost certainly *real* — OTP
   simply ranked it below the cutoff. That's expected; it's not a
   bug. See §3.2.

---

## 6. Future work (parked, not committed)

| Item | Trigger | Effort |
|---|---|---|
| Phase 3 persistence | Operator asks for "comparison drift over time" reporting | ~1 day — new table + retention policy + admin view |
| Per-query `num_itineraries` / `searchWindow` knob in the UI | Operator asks for tighter / wider search per query | ~half day — form control + plumb through fanout |
| Lower OTP `transferCost` for the OJP-comparison code path only | Operator wants closer alignment on transfer-heavy routes | ~1 day — would need a separate OTP query variant when `compare_ojp=true` |
| OJP rate-limit back-off | Operator hits the daily quota in normal usage | ~half day — background queue + retry-after handling |
| Cross-NAP federation (German DELFI, French OJP) | Operator extends comparison to other countries | Multi-day — each NAP needs its own adapter + DSN prefix logic |

---

## Changelog

- **2026-05-18 (v0.1.35.06)** — New §2.6 describing OJP anchor-time
  pagination. Closes the structural alignment gap between OTP's
  time-window coverage (`searchWindow=6h`) and OJP's trip-count
  coverage (~6 alternatives per request). Up to 4 sequential
  `TripRequest`s per search, fingerprint-deduped at the boundary,
  sequential not parallel (each anchor requires the previous batch's
  latest departure). v0.1.35.05 also rolled in (transferSlack 2 m →
  5 m); added to the version table.
- **2026-05-18 (v0.1.35.04)** — Initial version of this doc.
  Consolidates the implementation history, OTP tuning decisions,
  divergence-is-expected position, and diagnostic checklist into one
  operator-facing reference.
