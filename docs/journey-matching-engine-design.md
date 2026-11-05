# Configurable fuzzy journey matching engine — design proposal (#304)

**Status**: PROPOSAL for review, 2026-11-04. No code. #304 is blocked by #303 and "MUST NOT start
before" it is complete; this design is reviewed now so that both epics are agreed together, and
its implementation follows #303's PRs (canonical-journey-design.md §8). **Revised 2026-11-05**
with measurements from a full eu19 coverage run (§1.1): M1 and M5 confirmed, an amendment to M2
and a new fallback for M4 proposed, three new questions M8–M10.
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
| M1 | Time tolerance | **±120 s** on first boarding and on last alighting (#303's "±2 minutes"), for every profile, as a setting. Coverage's fuzzy tier uses ±300 s today on departure only. *Measured (§1.1): a ride both sides take has the same minutes on both; 11 of 1 124 ÖBB connections had a first ride 3–10 min away; those checked were different trains or another timetable version. Confirmed.* |
| M2 | Different train numbers on both sides | **Veto** (no pair), as today's coverage guard does: on Paris–Lyon (a TGV every 30 min) only the train number tells two trains apart. Setting: `veto` \| `penalty`. **Proposed amendment after measurement, for the owner** (§1.1 c): `veto_unless_same_minutes` — when a leg's departure and arrival are the same minute on both sides, a different number is a warning (`train_number_differs_same_minutes`), not a veto. Of the 31 distinct legs measured with different numbers, 30 were such legs: Eurostar's feed numbers a train 94xx where ÖBB says 93xx (EUR 9341 / 9441), TGVs carrying another number on part of their run (8619 / 8719); the 31st compared a Styria course id with a train number. Two TGVs 2 min apart stay a veto. |
| M3 | Different line labels (after normalisation) | **Penalty, never a veto** (#304 comment, expectation 4: "a label difference alone should not present a time-matched pair as two different trains"). The pair is kept as *probably the same service* and the difference is shown. |
| M4 | How stations are compared | **Decided direction 2026-11-04: by MSMM station** (`station_id`, resolved through the station module, #303 D8 and PR 5). Open: the fallback when MSMM has no link for a stop (MOTIS `<feed>_<local>` vs ÖBB `extId` with nothing to join them). First recommendation: pair only when both times are within tolerance and the train number or the line agrees. **Revised recommendation after measurement** (§1.1 c, d): that rule drops most true pairs — a train number is found on only 40 % of the legs both sides take, and MOTIS's line code (`671A`, `83`) never equals ÖBB's name (`TGV 9583`). Instead: a leg whose departure and arrival are both within tolerance, and whose two stops are each within `position_fallback_m` (1 000 m) of the other side's, counts its stations as agreeing, marked `stations_by_position` (info, shown). Only inside a time-matched leg, never as station identity (St Pancras / King's Cross stay two stations); the residual risk, two trains between the same two station areas in the same two minutes, is shown on the page. Alternatives as before: times alone with a note, or never pair (today's coverage guard). |
| M5 | Station names as evidence when codes are missing | **Off in V1.** Names drift ("Graz Hbf" / "Graz Hauptbahnhof"); proximity is already rejected as identity. Revisit once the station module resolves codes (CLAUDE.md §7 item 7). *Measured (§1.1 d): names differ on the same ride in many ways (DB's bare "Hauptbahnhof" for Stuttgart Hbf, "Brussel-Zuid" / "Bruxelles Midi", "Plzeň hlavní nádraží" / "Plzen hl.n."). Confirmed.* |
| M6 | Coverage scores after the switch | Scores change (tolerant pairing, folded modes, normalised labels), so coverage runs are run again. Runs scored before the switch can be deleted like the others (#303 D1, no backward compatibility). One new nullable column holds the scorer version and the reason counts per cell. |
| M7 | Where the knobs live | Tolerances, thresholds and the three policies (M2–M4) as `JOURNEY_MATCH_*` / `COVERAGE_MATCH_*` platform settings; weights in code per profile (documented, unit-tested); coverage freezes its knobs in the run's `execution_snapshot` like every other `COVERAGE_*` knob. |
| M8 | Journeys that share some rides, not all (§1.1 a, b) | **No pair** (the gates are unchanged), but the unpaired journey's reason says how it relates to its closest counterpart and where: `splits_after_shared_ride` "same train to München Hbf, then different trains", and `shorter_transfer` "VIATOR changes at München Hbf in 5 min, ÖBB in 19 min" when one side's change is at least `transfer_difference_s` (300 s) shorter; `rejoins_after_split`; `other_first_or_last_ride`. Coverage counts them per cell (M6's column), so "VIATOR's connections are shorter than ÖBB's" reads apart from "no overlap". Measured: 44 % of ÖBB connections share rides with a VIATOR journey, then split (32 %) or split and rejoin (12 %). |
| M9 | Which VIATOR journeys a coverage cell compares (§1.1 e) | **Only those whose first boarding lies within ÖBB's span**: from ÖBB's first to its last first boarding, plus the departure tolerance. ÖBB answers 5 connections from the run's departure time (about 4 h); VIATOR's 10 cover the day. The others are `outside_reference_window` (info): neither `left_only` nor counted. Measured: 52 % of VIATOR's journeys are outside. |
| M10 | A local ride only one side has (§1.1 a) | **Compared like an access walk**: a leading or trailing TRAM, BUS, SUBWAY, FERRY, CABLE_CAR or FUNICULAR leg that aligns with nothing on the other side, while the other side has no leg there, is left out of the gates, marked `local_access_ride` (info). MOTIS starts some journeys with a tram from the hub's point to the station; ÖBB starts at the station. Measured: 3 % of ÖBB connections. `SUBURBAN` and rail are never trimmed (Graz's S7 is the main ride). |

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

### 1.1 What a full coverage run shows (measured 2026-11-05)

**Data.** The full eu19 coverage run shared by the owner on 2026-11-04 (MOTIS vs ÖBB HAFAS,
departures from 08:00 Vienna time; 13 110 cells, 11 414 where both answered; recorded before
#303 PR 4a, so in the old shapes). 220 cells were read through the run's share page: 437
directions, 2 375 ÖBB connections. **1 251 of them were left out**: the old sweep parser read a
time past midnight as `01:<hour>` (HAFAS `01HHMMSS`, day prefix taken for the hour), so every
connection crossing midnight has wrong times (fixed by #303 PR 4a). The 1 124 kept lean to
day-time journeys. A ride was taken as the same on both sides when its departure and arrival were
both within 2 min and at least one end was the same station by code or name (names because the
codes do not match, d below).

**Today's scorer** finds almost nothing: 11 401 of the 11 414 cells are `no_overlap`, 4 `disagree`.
Its fuzzy tier compares the first leg's `route_short_name` with ÖBB's name as text, and MOTIS's
`route_short_name` is a line code: `671A` for ÖBB's `TGV 9583`, `83` for `TGV 9577`, `385` for
`IC 107`. Behind it, the endpoint-UIC guard (CLAUDE.md §7 item 2).

**a) How each ÖBB connection relates to the closest VIATOR journey** (ÖBB connections leaving
within VIATOR's span, 1 004):

| Relation | Connections | Share |
|---|---|---|
| same rides | 282 | 28 % |
| same rides, plus a tram/bus/metro only VIATOR takes at the start or end (M10) | 28 | 3 % |
| same rides, one side has an extra ride | 13 | 1 % |
| same rides until a station, then different trains to the end (M8) | 319 | 32 % |
| different trains in the middle, same rides before and after (M8) | 120 | 12 % |
| different first rides, then the same | 131 | 13 % |
| no ride in common: another VIATOR journey within 30 min / none / same end times by other rides / first ride 3–10 min away | 79 / 16 / 9 / 7 | 11 % |

**b) Connection times where the two split.** 287 splits where both board their next ride at the
same station:

| | VIATOR (MOTIS) | ÖBB |
|---|---|---|
| wait at the station, median (quartiles) | 8 min (4–17) | 17 min (10–35) |
| waits under 5 min | 76 (26 %) | 8 (3 %) |

VIATOR waits less in 181 of the 287, takes a next train a median 4 min earlier and arrives a
median 19 min earlier (earlier in 189, later in 35, at the same time in 63). In 215 of the 287,
VIATOR's next train is in none of ÖBB's 5 connections. By station, ÖBB's shortest waits point to
station minimums that MOTIS does not apply:

| Station | Splits | VIATOR median / shortest | ÖBB median / shortest |
|---|---|---|---|
| München Hbf | 25 | 5 / 2 min | 19 / 10 min |
| Basel SBB | 14 | 11.5 / 4 | 15.5 / 7 |
| Milano Centrale | 13 | 12 / 8 | 42 / 15 |
| Zürich HB | 11 | 4 / 2 | 8 / 6 |
| Lyon Part-Dieu | 11 | 9 / 2 | 12 / 10 |
| Strasbourg | 11 | 10 / 2 | 20 / 10 |
| Hamburg Hbf | 7 | 3 / 3 | 20 / 8 |
| Graz Hbf | 7 | 3 / 2 | 14 / 6 |
| Paris Bercy | 8 | 8 / 8 | 36 / 25 |

A different second train with the same first train is therefore mostly a transfer-time
difference, not a different journey; M8 reports it as such.

**c) Train numbers** on the 2 088 legs both sides take, with the number read from the MOTIS field
that carries it (§5.1):

| Result | Legs | Share |
|---|---|---|
| same number | 836 | 40 % |
| different number | 30 | 1.4 % |
| no number on VIATOR's side (DB, ÖBB, NS, Trenord, Styria, PKP, DSB feeds; MOTIS's line code only) | 1 205 | 58 % |
| no number on ÖBB's side | 17 | 1 % |

Behind them are 31 distinct legs. 30 have the same departure and arrival minute on both sides:
22 Eurostar, whose feed numbers 94xx what ÖBB calls 93xx (`9441` / `EUR 9341`, `9463` /
`EUR 9363`); 5 SNCF TGVs carrying another number on part of their run (`8719` / `TGV 8619`) or
showing another one; 3 TER numbers the measurement itself misread (zou `SN86983FERRE`). The last,
1 min apart, compared a Styria course id with ÖBB's train number. Hence M2's amendment.

**d) Stations.** The same ride often has different codes and names on the two sides. Codes come
from different series (VIATOR's SNCF code for Avignon TGV `8731896`, ÖBB's `8704918`; Spain
`7171801` / `7100064`; Ulm `8029103` / `8000170`), so code equality fails without MSMM (M4).
Names differ by language, abbreviation, transliteration and missing city: DB's bare
"Hauptbahnhof" / "Stuttgart Hbf" (482 legs), "ZUERICH HB" / "Zürich HB", "Brussel-Zuid" /
"Bruxelles Midi", "Francfort sur le Main" / "Frankfurt(Main)Hbf", "Plzeň hlavní nádraží" /
"Plzen hl.n.". Names are no fallback (M5). ÖBB's stop positions were not stored before
#303 PR 4a, so M4's position fallback is not measured here.

**e) Window.** ÖBB answers its 5 connections from the run's departure time; VIATOR gives up to 10
across the K slots of the day. 1 234 of VIATOR's 2 371 journeys leave after ÖBB's last one: they
are not "missing on ÖBB" (M9).

To repeat on the first run the engine scores: the reason counts of M6's column give the same
figures without a script.

---

## 2. Goals and non-goals

Goals: one engine, on canonical journeys only; explainable verdicts; tolerant to timing jitter,
label spelling, walk differences and provider ids; strict where identity is actually known (train
numbers, station codes); configurable; deterministic (same input → same pairs).

Non-goals (V1): machine-learned scoring (the features are ready for it); station resolution by
name or distance (M5; M4's proposed position fallback only confirms the stations of a leg whose
times already agree, it never says two stations are one); fares; real-time; matching inside one
source (dedup stays on `JourneyFingerprint.strict_key`).

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
| `train_number` | equal → AGREE (`train_number_equal`); different → CONFLICT (`train_number_differs`; under M2's proposed amendment a warning `train_number_differs_same_minutes` when the leg's departure and arrival are the same minute on both sides) |
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

### 5.1 Where MOTIS carries the train number (measured 2026-11-05)

The MOTIS adapter fills `train_number` with nothing today: its `published_name` and `line` are
`routeShortName`, which is a line code for most feeds. On eu19 the number is in other fields,
feed by feed (legs both sides take, §1.1 c; ÖBB's number from `(Train-No. N)` or
`<category> <N>`):

| Feeds | Where | Example |
|---|---|---|
| `sncf` and the SNCF regional feeds (`rgion*`, `breizhgo`, `hdf-ter`, `zou`) | `headsign` when all digits; a 6-digit TER number starting with `8` drops it (`879322` → `79322`); also `tripId` `OCESN<n>F`, zou `SN<n>FERRE` | `headsign` `9583` for `TGV 9583` |
| `eurostar_*` | `tripId` `<n>-<MMDD>` (Paris–Brussels–Amsterdam services: the feed's 94xx is ÖBB's 93xx) | `9314-1109` |
| `sbb` (SBB's own trains) | `tripId` `…sjyid:<n>:<n>` | `IC 722` |
| `renfe-avld`, `renfe-fr` | `tripId` `0<n><digit><date>` | `0973012026-11-09` for `AVE 9730` |
| `entur-no` (Vy) | `tripId` `ServiceJourney:<n>-…` | `IC 141` |
| `mav-hu` (IC), `sk-rail` | `routeShortName` `<category> <n>` | `IC 801 MECSEK`, `Os 1339` |
| `db`, `obb`, `nl-nap`, `trenord`, `styria`, `pkp-plk_*`, `dk-rail`, SBB's foreign trains (`NoGlobalID…`) | **nowhere** in the fields VIATOR reads | `routeShortName` `83`, `A3`, `S7` |

So: the adapter reads a number by a per-feed rule (a table beside `motis_adapter.py`, each rule a
unit test from a captured leg), never "any digits in the trip id"; a feed without a rule gives
`None` (UNKNOWN, §5 table). Whether eu19's MOTIS also returns `tripShortName` (canonical-journey
design §5.1) is still to see on a captured answer; when present it wins over the rules.

---

## 6. Station agreement

From `CanonicalStopPlace` (canonical-journey-design.md §3.1). The MSMM station decides first
(M4); codes and positions are only the fallback for a stop MSMM has not placed.

| Left key | Right key | Agreement |
|---|---|---|
| `station_id` x | `station_id` x | AGREE |
| `station_id` x | `station_id` y, same `station_parent_id` | AGREE, marked `same_complex` (info): Bruxelles-Midi / Midi Eurostar |
| `station_id` x | `station_id` y, no common parent | CONFLICT |
| `UIC:x` | `UIC:x` | AGREE |
| `UIC:x` | `UIC:y`, x ≠ y | CONFLICT — unless the two share `parent_ref` evidence from the station data (Bruxelles-Midi / Midi Eurostar), which V1 does not have: CONFLICT |
| `GEO:p` | `GEO:p` (same 3-dp cell) | AGREE, marked `location_only` (info). Kept so OTP-vs-MOTIS on the same feed still pairs; never used for the coverage endpoint gate |
| anything | `UNRESOLVED:…` / `OEBB:` token / no id | UNKNOWN (`station_unresolved`) |
| `UIC:x` | `GEO:p` | UNKNOWN |
| a stop of a leg MSMM has not placed, the leg's departure and arrival within tolerance on both sides | the other side's stop within `position_fallback_m` | AGREE, marked `stations_by_position` (info, shown) — M4's proposed fallback, only for that leg; it never turns a code CONFLICT into AGREE |

No distance threshold for station identity and no name matching (M5): St Pancras ↔ King's Cross
(247 m) must stay apart while Bruxelles-Midi ↔ Midi Eurostar (53 m) should merge, and only station
data can say so — which is what MSMM's stations and parents are. The position fallback above does
not decide that: it accepts the stations of one leg whose two times already agree, and says so on
the page.

---

## 7. Gates, score, verdict

**Gates** (a failed gate means no pair; the reason is recorded for the unmatched journey):

| Gate | Rule | Reason code |
|---|---|---|
| G1 | both journeys have a timed leg | `no_transit_leg` (→ uncomparable) |
| G2 | `|departure_delta| ≤ departure_tolerance` and `|arrival_delta| ≤ arrival_tolerance` | `departure_outside_tolerance`, `arrival_outside_tolerance` |
| G3 | first boarding and last alighting stations: no CONFLICT; UNKNOWN allowed only if `unresolved_endpoints = allow` (M4) | `endpoint_station_differs`, `endpoint_unresolved` |
| G4 | no train-number CONFLICT when `train_number_conflict = veto` (M2); under `veto_unless_same_minutes`, none on a leg that is not the same minute at both ends on both sides | `train_number_differs` |

The gates read the spines after M10 (a local ride only one side has is left out, like a walk).

**When G2 fails but legs align (M8).** The pair is not made; the journey's reason, instead of
`arrival_outside_tolerance` alone, describes the alignment with its closest candidate (largest
`route_overlap`, then smallest `|Δdep|`): where the two first take different rides
(`splits_after_shared_ride`, station name), the wait on each side there, `shorter_transfer` when
they differ by at least `transfer_difference_s`, and `rejoins_after_split` when a later ride is
shared again.

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
| `train_number_differs_same_minutes` | warning | "same times, the sources number the train differently: EUR 9341 / 9441" (M2 amendment) |
| `stations_by_position` | info | "stations compared by position (no shared station code)" (M4 fallback) |
| `local_access_ride` | info | "VIATOR adds a tram to the station" (M10) |
| `splits_after_shared_ride` | info | "same train to München Hbf, then different trains" (M8) |
| `shorter_transfer` | warning | "VIATOR changes at München Hbf in 5 min, ÖBB in 19 min" (M8) |
| `rejoins_after_split` | info | "different trains between Basel SBB and Zürich HB, then the same" (M8) |
| `outside_reference_window` | info | "leaves after ÖBB's last connection" (M9, coverage) |
| `endpoint_station_differs` | veto | "different first/last station" |
| `departure_outside_tolerance`, `arrival_outside_tolerance` | gate | "departure 4 min apart" |
| `no_transit_leg` | gate | "walk only" |

The sentences live in one table on the server (like `REASON_TEXTS` for station extracts); the
pages print them with `textContent`.

---

## 10. Configuration

`MatchProfile` fields: `name`, `departure_tolerance_s`, `arrival_tolerance_s`, `same_service_min`,
`probable_min`, `train_number_conflict` (`veto` | `veto_unless_same_minutes` | `penalty`, M2),
`line_conflict_penalty`, `unresolved_endpoints` (`allow` | `reject`), `position_fallback_m`
(1 000; 0 = off, M4), `transfer_difference_s` (300, M8), `local_access_modes` (M10),
`use_route_aliases`, `weights`.

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
| `JOURNEY_MATCH_TRAIN_NUMBER_CONFLICT` | str | `veto` (`veto_unless_same_minutes` if M2's amendment is taken) | `veto`, `veto_unless_same_minutes`, `penalty` |
| `JOURNEY_MATCH_UNRESOLVED_ENDPOINTS` | str | `allow` | `allow`, `reject` |
| `JOURNEY_MATCH_POSITION_FALLBACK_M` | int | 1000 | 0–5000 |
| `JOURNEY_MATCH_TRANSFER_DIFFERENCE_S` | int | 300 | 60–3600 |
| `COVERAGE_MATCH_*` (same eight) | | as above, `UNRESOLVED_ENDPOINTS` = `reject` | |

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
trips through one selection function (fixes canonical-journey-design.md §11 item 5). That function
gets an upper bound (M9): today's `journeys_from_depart_at` keeps every VIATOR journey from the
run's departure time on; it keeps those up to ÖBB's last first boarding plus the departure
tolerance, and the modal marks the rest "after ÖBB's last connection".

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

From the coverage measurement (§1.1), each built from a leg of that run:

- `EUR 9341` (ÖBB) / feed number `9441`, same minutes at both ends: paired `probably_same` with
  `train_number_differs_same_minutes` under `veto_unless_same_minutes`, not paired under `veto`;
- same first train to München Hbf, VIATOR on in 5 min, ÖBB in 19 min: not paired; the VIATOR
  journey's reason is `splits_after_shared_ride` + `shorter_transfer` naming München Hbf and both
  waits;
- VIATOR starts with a tram from the hub's point, then the same train as ÖBB: `same_service`
  with `local_access_ride`; a leading S-Bahn (`SUBURBAN`) is never trimmed;
- DB's stop "Hauptbahnhof" vs ÖBB's "Stuttgart Hbf", no station code shared, same minutes, stops
  300 m apart: stations agree with `stations_by_position`; 1 500 m apart: UNKNOWN;
- a VIATOR journey leaving after ÖBB's last connection: `outside_reference_window`, in neither
  `left_only` nor the cell's counts;
- the train-number rules of §5.1, one captured leg per feed (`headsign` `879322` → `79322`;
  Renfe `0973012026-11-09` → `9730`; a DB leg → `None`).

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
4. Should a journey that shares rides and then splits (M8) earn any coverage credit? Recommendation:
   none — the credit stays for pairs; the cell shows the `shorter_transfer` count beside its tier,
   so a cell that differs only by transfer times is told apart from one that differs by trains.
5. Not the engine's question, but raised by §1.1 b: MOTIS connects in 2–5 min where ÖBB requires
   10–25 (München Hbf, Milano Centrale, Paris Bercy). Whether eu19's MOTIS should be asked for
   longer transfers (its plan API's transfer-time parameters, to check on the deployed version) is
   a VIATOR routing decision; the engine's `shorter_transfer` counts measure its effect.

---

## Changelog

- 2026-11-04 — first version, for review together with the #303 design.
- 2026-11-04 — owner: stations compared by MSMM station (M4); the fallback for stops MSMM has not
  placed is still open; M6 follows #303 D1 (no backward compatibility); M1–M3, M5, M7 as
  recommended.
- 2026-11-05 — measured on a full eu19 coverage run (§1.1): M1 and M5 confirmed; proposed for the
  owner: M2's amendment `veto_unless_same_minutes`, a position fallback for M4, and new questions
  M8 (journeys that share some rides; `shorter_transfer`), M9 (coverage compares only within
  ÖBB's span), M10 (a local ride only one side has); §5.1 where MOTIS carries the train number,
  per feed; reasons, settings and tests to match; open questions 4 and 5.
