# Configurable fuzzy journey matching engine — design proposal (#304)

**Status**: PROPOSAL for review, 2026-11-04. No code. #304 is blocked by #303 and "MUST NOT start
before" it is complete; this design is reviewed now so that both epics are agreed together, and
its implementation follows #303's PRs (canonical-journey-design.md §8).
**Audience**: the owner (review), implementers.
**Builds on**: [canonical-journey-design.md](canonical-journey-design.md) — the engine reads only
`CanonicalJourney` (#304: "No matching logic may be implemented on provider-specific DTOs").

---

## 0. Summary

VIATOR decides "is this the same journey?" in four places today, with four rule sets, two of them
in browser JavaScript (§1). The engine replaces them with **one** server-side function:

```python
compare(left: list[CanonicalJourney], right: list[CanonicalJourney], profile: MatchProfile) -> JourneyComparison
```

It computes `JourneyMatchFeatures` for every candidate pair, applies a profile's hard gates, scores
what is left, pairs journeys one to one, and returns for every pair **a verdict and the reasons**
("same service; labels differ only in spacing", "probably the same service; line labels differ:
17 vs 1"), and for every unpaired journey the reason its best candidate was rejected. Profiles hold
the tolerances, weights and policies; their main knobs are platform settings.

### 0.1 Decisions requested from the review

| # | Question | Recommendation |
|---|---|---|
| M1 | Time tolerance | **±120 s** on first boarding and on last alighting (#303's "±2 minutes"), for every profile, as a setting. Coverage's fuzzy tier uses ±300 s today on departure only. |
| M2 | Different train numbers on both sides | **Veto** (no pair), as today's coverage guard does: on Paris–Lyon (a TGV every 30 min) only the train number tells two trains apart. Setting: `veto` \| `penalty`. |
| M3 | Different line labels (after normalisation) | **Penalty, never a veto** (#304 comment, expectation 4: "a label difference alone should not present a time-matched pair as two different trains"). The pair is kept as *probably the same service* and the difference is shown. |
| M4 | First/last station not comparable (no shared code: MOTIS `<feed>_<local>` vs ÖBB `extId`) | **Coverage: reject, and record why** (keeps the endpoint guard, CLAUDE.md §7 item 2: never trade visible false negatives for invisible false positives). **`/journey`: allow, with the note "stations could not be compared"**, because the page pairs on times alone today and would otherwise pair nothing on eu19. |
| M5 | Station names as evidence when codes are missing | **Off in V1.** Names drift ("Graz Hbf" / "Graz Hauptbahnhof"); proximity is already rejected as identity. Revisit once the station module resolves codes (CLAUDE.md §7 item 7). |
| M6 | Coverage scores after the switch | Scores change (tolerant pairing, folded modes, normalised labels), so coverage runs are run again. Runs scored before the switch can be deleted like the others (#303 D1, no backward compatibility). One new nullable column holds the scorer version and the reason counts per cell. |
| M7 | Where the knobs live | Tolerances, thresholds and the three policies (M2–M4) as `JOURNEY_MATCH_*` / `COVERAGE_MATCH_*` platform settings; weights in code per profile (documented, unit-tested); coverage freezes its knobs in the run's `execution_snapshot` like every other `COVERAGE_*` knob. |

---

## 1. What is compared today, and how

| Place | Pairs | Rule | Known faults *(read from code)* |
|---|---|---|---|
| `api/journey._build_comparison` | VIATOR vs Swiss OJP | equal `transit_fingerprint` (stations, minutes, mode, **route name**) | exact only: a minute or a label spelling breaks it; MOTIS sub-modes never equal OJP's `RAIL` |
| `journey.html::_pairViatorAndOebbTrips` | VIATOR vs ÖBB HAFAS (when HAFAS is the only reference) | first-boarding string **equal**, zero tolerance; arrival as tie-break on raw strings | different string formats per engine; no station check at all |
| `journey.html::_trainIdMismatchNote` | the pairs above | `route_short_name` (else long name), trimmed, case-insensitive | says "same departure and arrival" though arrival is not checked; "S7" ≠ "S 7" |
| `journey.html` OTP-vs-MOTIS grid | OTP vs MOTIS | `trip_signature`, or `_trainOnlySignature` (3-dp lat/lon, minute) when "excluding walk legs" | keeps `TRANSFER` legs; signature includes walks |
| `alignment.compute_alignment` | coverage VIATOR vs ÖBB | exact fingerprint (1.0), else same first/last UIC + same first-leg label + ±300 s (0.7) | endpoint guard rejects nearly everything (CLAUDE.md §7 item 2); exact and fuzzy passes parse stops differently; no reason recorded |

The test cases posted on #304 (eu19, 2026-11-03, Styria NeTEx, data set 60), both "same service"
in reality, both flagged today:

| Search | VIATOR (MOTIS, `styria`) | ÖBB HAFAS | Expected |
|---|---|---|---|
| Graz Hbf → Köflach, 09:14 → 10:11 | `S7` | `S 7` | same service, no warning |
| Graz Hbf → Graz Jakominiplatz, 09:04 → 09:12 | `17` | `Tram 1` | paired; the line difference shown, to be classified once the leg detail says whether `17` is Styria's line or a course number (expectation 3) |

---

## 2. Goals and non-goals

Goals: one engine, on canonical journeys only; explainable verdicts; tolerant to timing jitter,
label spelling, walk differences and provider ids; strict where identity is actually known (train
numbers, station codes); configurable; deterministic (same input → same pairs).

Non-goals (V1): machine-learned scoring (the features are ready for it); station resolution by
name or distance (M5); fares; real-time; matching inside one source (dedup stays on
`JourneyFingerprint.strict_key`).

---

## 3. Engine API

Package `app/journey/matching/`:

| Module | Content |
|---|---|
| `profiles.py` | `MatchProfile` (Pydantic, frozen) + the built-in profiles + `profile_from_config(cfg, name)` |
| `identifiers.py` | label normalisation and the three-valued service-identity comparison (§5) |
| `stations.py` | station-key agreement (§6) |
| `features.py` | `compute_features(a, b) -> JourneyMatchFeatures` (#303's structure) |
| `scoring.py` | gates, score, verdict, reasons (§7) |
| `pairing.py` | one-to-one assignment (§8) |
| `engine.py` | `compare(left, right, profile) -> JourneyComparison` |

```python
class JourneyComparison(BaseModel, frozen=True):
    engine_version: Literal[1] = 1
    profile: str
    pairs: list[JourneySimilarityResult]          # one per paired (left, right)
    left_only: list[UnmatchedJourney]             # with the reason of the best rejected candidate
    right_only: list[UnmatchedJourney]
    uncomparable_left: list[str]                  # journey ids without a timed leg
    uncomparable_right: list[str]
    summary: ComparisonSummary                    # counts by verdict

class JourneySimilarityResult(BaseModel, frozen=True):   # journey_similarity_result.py (#303)
    left_id: str
    right_id: str
    verdict: Literal["same_service", "probably_same", "different"]
    score: float                                  # 0..1, rounded to 2 dp
    exact: bool                                   # strict fingerprints equal
    features: JourneyMatchFeatures
    reasons: list[MatchReason]                    # ordered, most important first

class MatchReason(BaseModel, frozen=True):
    code: str                                     # stable vocabulary, §9
    severity: Literal["info", "warning", "veto"]
    leg: int | None                               # timed-leg index when it concerns one leg
    left_value: str | None
    right_value: str | None
```

`ComparisonMetadata` (#303) is what a page needs per journey card: `{bucket: common | left_only |
right_only | uncomparable, paired_with, verdict, score, reasons}`, derived from the comparison.

---

## 4. Features

Computed on the **spines** (timed legs only), so access and egress walks never count (OJP adds
them, stop-id routing has none).

| Feature (`JourneyMatchFeatures`) | Computation |
|---|---|
| `departure_delta`, `arrival_delta` | `first_boarding` / `last_alighting` difference, seconds, signed |
| `duration_delta` | `transit_duration_seconds` difference |
| `transfer_count_delta` | timed legs − 1, difference |
| `station_overlap` | over the ordered board/alight keys of both spines: agreeing keys ÷ keys comparable on both sides (§6); `None` when nothing is comparable |
| `route_overlap` | longest common subsequence of `(mode family, board key, alight key)` ÷ the longer spine |
| `service_overlap` | over legs aligned by the LCS: AGREE = 1, CONFLICT = 0, UNKNOWN excluded (§5); `None` when all unknown |
| `operator_overlap` | normalised operator names (case-folded, legal forms `AG`, `GmbH`, `SA`, `S.p.A.` and DELFI's `80___` prefix removed): Jaccard; `None` when a side names none |
| `fingerprint_similarity` | `JourneyFingerprint.similarity(other, tolerance)` |

The engine also keeps, per aligned leg, the identifier comparison and the endpoint agreement
(first boarding station, last alighting station): `AGREE`, `CONFLICT` or `UNKNOWN`. These drive
the gates.

---

## 5. Service identity: three answers, not two

A label comparison answers **AGREE**, **CONFLICT** or **UNKNOWN**. Only a CONFLICT between two
identifiers *of the same kind* counts against a pair.

Normalisation of a label (`identifiers.normalise_label`):
1. Unicode NFKC, case-fold, trim.
2. If the leg's mode is known (it always is on a timed leg), drop a leading **mode word** that only
   repeats it: TRAM `tram`, `str`, `straßenbahn`, `strassenbahn`, `tramway`; BUS `bus`; COACH `coach`;
   FERRY `ferry`, `schiff`; SUBWAY `metro`. A product letter that is part of the line (`S` of S7,
   `U` of U1, `RE` of RE5) is never dropped.
3. Remove spaces, `-`, `.`, `_`.

Comparison of two legs (first match wins):

| Both sides have… | Result |
|---|---|
| `train_number` | equal → AGREE (`train_number_equal`); different → CONFLICT (`train_number_differs`) |
| `line` (normalised) | equal → AGREE, with `label_spacing_only` / `mode_word_only` as *info* when the raw strings differed; different → CONFLICT (`line_label_differs`) |
| a train number on one side, a line on the other | UNKNOWN (`identifier_kinds_differ`) |
| a `published_name` only | parsed as `<category> <digits>` → treat as a train number; else as a line; then the rows above |
| nothing on one side (NMBS: number 0) | UNKNOWN (`identifier_missing`) |

Examples (all become unit tests):

| Left | Right | Result | Reason |
|---|---|---|---|
| line `S7` | published `S 7` (category S) | AGREE | `label_spacing_only` (info) |
| line `17` (TRAM) | published `Tram 1` (TRAM) | CONFLICT on line | `line_label_differs` 17 / 1 (warning) |
| published `RJ 1141` | published `RJ 1141` | AGREE | `train_number_equal` |
| published `RJ 1141` | published `RJ 1143` | CONFLICT on train number | `train_number_differs` (veto under M2) |
| line `IC5` | line `IC 5` | AGREE | `label_spacing_only` |
| published `TGV 6107` | published `TGV INOUI 6107` | AGREE (numbers equal) | `train_number_equal`, `category_differs` (info) |
| published `EUR 9322` | published `Eurostar 9322` | AGREE | idem |
| train number `601` | train number `601A` | CONFLICT | `train_number_differs` — coupled/portioned trains stay apart, as today's duplicate badge assumes |
| line `RE5` | nothing | UNKNOWN | `identifier_missing` |

Optional (profile flag `use_route_aliases`, off by default): before comparing lines, map both
through `route_aliases`, honouring `applies_from` / `applies_until` / `scope_*` (today's
`_route_canonical` ignores them).

---

## 6. Station agreement

From `CanonicalStopPlace` (canonical-journey-design.md §3.1):

| Left key | Right key | Agreement |
|---|---|---|
| `UIC:x` | `UIC:x` | AGREE |
| `UIC:x` | `UIC:y`, x ≠ y | CONFLICT — unless the two share `parent_ref` evidence from the station data (Bruxelles-Midi / Midi Eurostar), which V1 does not have: CONFLICT |
| `GEO:p` | `GEO:p` (same 3-dp cell) | AGREE, marked `location_only` (info). Kept so OTP-vs-MOTIS on the same feed still pairs; never used for the coverage endpoint gate |
| anything | `UNRESOLVED:…` / `OEBB:` token / no id | UNKNOWN (`station_unresolved`) |
| `UIC:x` | `GEO:p` | UNKNOWN |

No distance threshold and no name matching (M5): St Pancras ↔ King's Cross (247 m) must stay apart
while Bruxelles-Midi ↔ Midi Eurostar (53 m) should merge, and only station data can say so.

---

## 7. Gates, score, verdict

**Gates** (a failed gate means no pair; the reason is recorded for the unmatched journey):

| Gate | Rule | Reason code |
|---|---|---|
| G1 | both journeys have a timed leg | `no_transit_leg` (→ uncomparable) |
| G2 | `|departure_delta| ≤ departure_tolerance` and `|arrival_delta| ≤ arrival_tolerance` | `departure_outside_tolerance`, `arrival_outside_tolerance` |
| G3 | first boarding and last alighting stations: no CONFLICT; UNKNOWN allowed only if `unresolved_endpoints = allow` (M4) | `endpoint_station_differs`, `endpoint_unresolved` |
| G4 | no train-number CONFLICT when `train_number_conflict = veto` (M2) | `train_number_differs` |

**Score** of a pair that passed the gates: weighted mean of the components that have evidence.

```
time       = 1 − (|Δdep| + |Δarr|) / (dep_tol + arr_tol)           (in [0, 1] after G2)
station    = station_overlap
route      = route_overlap        (an unresolved station key matches any key here)
service    = service_overlap      (a line-label CONFLICT counts as no evidence, see below)
transfers  = 1 if Δtransfers == 0, 0.5 if |Δ| == 1, else 0
operator   = operator_overlap

score = Σ wᵢ·cᵢ / Σ wᵢ                  over the components that are not None
        − line_conflict_penalty × (number of line-label CONFLICTs)      (M3)
```

A line-label CONFLICT is left out of `service` and only costs the penalty. That is how
expectation 4 holds: when the times and the route agree, a label difference lowers the verdict to
*probably the same*, it does not split the pair.

Default weights (`journey_reference` and `coverage_alignment` profiles): time 0.30, station 0.25,
route 0.15, service 0.15, transfers 0.10, operator 0.05. Line-conflict penalty 0.10.

**Verdict**: `exact` (strict fingerprints equal) → `same_service`, score 1.0. Else
`score ≥ same_service_min` (0.90) and no warning-level reason → `same_service`;
`score ≥ probable_min` (0.75) → `probably_same`; else `different` (not paired, kept as the best
rejected candidate's reason).

Worked examples with the defaults (one-leg journeys, stations unresolved as on eu19, so `station`
is `None`; Σw = 0.75 without it):

| Case | Components | Score | Verdict | Reasons shown |
|---|---|---|---|---|
| Graz → Köflach, `S7` / `S 7`, same minutes, operators agree | time 1, route 1, service 1, transfers 1, operator 1 | 1.00 | `same_service` | none (`label_spacing_only` and `endpoint_unresolved` are info) |
| idem, operators named differently (`GKB` / `Graz-Köflacher Bahn`) | operator 0 | 0.93 | `same_service` | none |
| Graz → Jakominiplatz, `17` / `Tram 1`, same minutes | time 1, route 1, service none, transfers 1, operator 1; Σw = 0.60; penalty 0.10 | 0.90 | `probably_same` | "line labels differ: 17 / 1" |
| two trams of different lines on a shared section, 1 min apart | time 0.5, route 1, service none, transfers 1, operator 1; penalty 0.10 | 0.65 | `different` | "departure 1 min apart, line labels differ" |
| the same two trams in the same minute | as row 3 | 0.90 | `probably_same` | "line labels differ: 1 / 7" — the limit of the method, stated on the page |
| Paris → Lyon, two TGVs 2 min apart, numbers 6601 / 6603 | G4 veto | — | not paired | "different trains: TGV 6601 / TGV 6603" (same as today's guard) |

The unit tests pin this arithmetic, so a weight change cannot silently undo expectation 4.

---

## 8. Pairing

1. Build every (left, right) candidate that passes the gates, with its score.
2. Sort by verdict rank (same_service, probably_same), score descending, `|Δdep|` ascending, left
   index, right index.
3. Accept greedily when neither side is taken.

Deterministic and explainable ("paired with the best remaining candidate"). Sizes are small (≤ 50
journeys a side), so the O(n·m) candidate step is negligible. An optimal assignment (Hungarian) is
not needed: the gates leave almost every journey with at most two candidates.

For an unpaired journey, `UnmatchedJourney.reason` is the gate or verdict that stopped its
closest candidate (smallest `|Δdep|`), so "why is this ÖBB train VIATOR-only?" has an answer — the
"unmatched-trips diagnostics" the spec promised (§6.8) and coverage never had.

---

## 9. Reason vocabulary

| Code | Severity | Shown as |
|---|---|---|
| `exact_fingerprint` | info | — |
| `label_spacing_only` | info | (hidden by default) |
| `mode_word_only` | info | (hidden) |
| `category_differs` | info | "product names differ: TGV / TGV INOUI" |
| `location_only` | info | (hidden) |
| `endpoint_unresolved` | info / gate | "stations could not be compared (no shared station code)" |
| `line_label_differs` | warning | "line labels differ: 17 / 1" |
| `identifier_kinds_differ`, `identifier_missing` | info | "one source gives no train number" |
| `operator_differs` | info | "operators differ" |
| `transfer_count_differs` | info | "1 change / 2 changes" |
| `train_number_differs` | veto | "different trains: RJ 1141 / RJ 1143" |
| `endpoint_station_differs` | veto | "different first/last station" |
| `departure_outside_tolerance`, `arrival_outside_tolerance` | gate | "departure 4 min apart" |
| `no_transit_leg` | gate | "walk only" |

The sentences live in one table on the server (like `REASON_TEXTS` for station extracts); the
pages print them with `textContent`.

---

## 10. Configuration

`MatchProfile` fields: `name`, `departure_tolerance_s`, `arrival_tolerance_s`, `same_service_min`,
`probable_min`, `train_number_conflict` (`veto` | `penalty`), `line_conflict_penalty`,
`unresolved_endpoints` (`allow` | `reject`), `use_route_aliases`, `weights`.

Built-in profiles:

| Profile | Used by | Differences from the defaults |
|---|---|---|
| `journey_reference` | `/journey`: VIATOR vs ÖBB HAFAS, vs Swiss OJP | `unresolved_endpoints = allow` |
| `journey_engines` | `/journey`: OTP vs MOTIS (same feeds) | tolerance 60 s; `unresolved_endpoints = allow` |
| `coverage_alignment` | coverage cells | `unresolved_endpoints = reject` |

Platform settings (`app/config_schema.py`, editable on `/admin/config`):

| Key | Type | Default | Bounds |
|---|---|---|---|
| `JOURNEY_MATCH_DEPARTURE_TOLERANCE_S`, `JOURNEY_MATCH_ARRIVAL_TOLERANCE_S` | int | 120 | 0–1800 |
| `JOURNEY_MATCH_SAME_SERVICE_MIN_PCT`, `JOURNEY_MATCH_PROBABLE_MIN_PCT` | int | 90, 75 | 0–100 |
| `JOURNEY_MATCH_TRAIN_NUMBER_CONFLICT` | str | `veto` | `veto`, `penalty` |
| `JOURNEY_MATCH_UNRESOLVED_ENDPOINTS` | str | `allow` | `allow`, `reject` |
| `COVERAGE_MATCH_*` (same six) | | as above, `UNRESOLVED_ENDPOINTS` = `reject` | |

`COVERAGE_MATCH_*` are read at a run's first execution and frozen in `execution_snapshot` with the
other knobs, so a resumed run scores like it started (#392).

---

## 11. Consumers: what replaces what

| Today | After #304 |
|---|---|
| `_build_comparison` (OJP sets of fingerprints) | `compare(viator, ojp, journey_reference)` |
| JS `_pairViatorAndOebbTrips` + `_trainIdMismatchNote` | `compare(viator, hafas, journey_reference)`; the page renders `pairs`, `left_only`, `right_only` and prints the reasons. Pairing code leaves the template. The "HAFAS is the only reference" restriction goes: each reference gets its own comparison |
| OTP-vs-MOTIS grid (`trip_signature` / `_trainOnlySignature`) | `compare(otp, motis, journey_engines)` |
| `comparison_summary {common, otp_only, ojp_only}` | `comparisons: {"<reference>": JourneyComparison}` in `/fanout`; labels from the provider names, no more "OTP-only" for MOTIS results |
| `alignment.compute_alignment` exact + fuzzy tiers | `compare(viator, oebb, coverage_alignment)`; credit 1.0 per `same_service`, 0.7 per `probably_same`; same normalisation (`credits ÷ max(min(v,3), min(o,3))`) and the same nine tiers |
| nothing records why a cell scored low | `external_alignment_detail` JSONB (M6): `{engine_version, profile, counts: {verdict…}, reasons: {code: n}}` — the "legible rejections" CLAUDE.md §7 item 2 asks for, before anyone loosens a guard |

Coverage keeps comparing the same VIATOR set it shows: the scorer and the modal both take the
trips through one selection function (fixes canonical-journey-design.md §11 item 5).

---

## 12. Tests

`tests/unit/test_journey_matching_identifiers.py` (every row of §5), `test_journey_matching_stations.py`
(§6, the St Pancras / King's Cross and Midi cases stay apart), `test_journey_matching_engine.py`:

- the two #304 cases (built from a captured eu19 answer, not hand-stripped);
- ±2 min jitter pairs, 3 min does not; the tolerance knob moves the boundary;
- high-frequency corridor: two trains 2 min apart with different numbers never pair (M2);
- access/egress walks of different lengths do not change the verdict;
- a MOTIS `REGIONAL_RAIL` leg pairs with an ÖBB `RAIL` leg;
- unresolved endpoints: paired on `journey_reference`, rejected with `endpoint_unresolved` on
  `coverage_alignment`;
- one-to-one: three VIATOR and two ÖBB candidates in one minute give two pairs and one `left_only`
  with its reason; result independent of input order;
- the `OEBB:?` property: two journeys without any station id never pair as `same_service` on
  coverage;
- `test_oebb_alignment.py` rerun on the engine: every case that was `agree` stays at least
  `mostly_agree`; the cases that change are listed in the PR with the reason.

Template tests: the pairing-helper pins in `test_journey_template_compare.py` are replaced by pins
on "renders `comparisons[…].pairs`" and "prints reasons with `textContent`".

---

## 13. Open questions

1. `17` vs `Tram 1`: which field of Styria's NeTEx does MOTIS show as `17`? If it is a course
   number, the MOTIS adapter should put it in `train_number`, and the case becomes AGREE/UNKNOWN
   rather than a line conflict. Needs one captured eu19 answer (leg detail).
2. Should `probably_same` count 0.7 or 1.0 in coverage? 0.7 keeps today's fuzzy credit; the tier
   boundaries were tuned on it.
3. `one_sided_oebb` → `one_sided_reference` (ch. 12's multi-oracle prerequisite): rename in the same
   PR as M6's column, or separately?

---

## Changelog

- 2026-11-04 — first version, for review together with the #303 design.
