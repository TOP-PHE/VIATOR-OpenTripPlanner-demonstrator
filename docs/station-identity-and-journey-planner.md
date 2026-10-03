# Station identity and the journey planner

Findings of 2026-10-02 and the plan that follows from them. Written in English to match the
rest of `docs/`; the working discussion was in French.

Everything below is measured — on the bytes of the archives, on the engines' own source, or on
VIATOR's own recorded experiments. Where something is inferred rather than observed it says so.

---

## 1. What was investigated, and how

Three questions, three fan-outs of agents, 35 agents in total, every claim required to carry its
evidence:

1. **Which NAP feeds actually carry a station code?** 30 archives on disk, probed file by file —
   no inference from the country, no reuse of earlier conclusions.
2. **Which countries publish a dedicated stop registry, separate from the timetable?** 35 national
   access points surveyed against their own catalogue pages and schemas.
3. **How do MOTIS and OTP decide that two stops are the same?** Read out of the engines' source and
   of VIATOR's own measurement notes, not from documentation prose.

Two cross-check agents then confronted the probes with the extraction code and with the mapping
table already built, which is where the defects in section 7 came from.

---

## 2. What the NAP timetable feeds carry

### 2.1 Feeds that reach a shareable station code

| Feed | Field | Code | Coverage |
|---|---|---|---|
| CZ GVD2026 (NeTEx) | `StopPlace/@id` | **the TAF/TAP PLC verbatim** (ISO2 + 5 digits) | 71,590 / 71,590 |
| CH SBB (NeTEx) | `keyList[DIDOK]`, and the `@id` tail | DIDOK = UIC-form 7-digit | 35,581 / 35,581 |
| CH SBB (NeTEx) | `keyList[SLOID]` | Swiss national object id, also per quay | 33,105 (93.0%) |
| DE DELFI | `StopPlace/keyList[GlobalID]` | **DHID / IFOPT** national id | 132,339 distinct |
| DE DELFI | `ScheduledStopPoint/@id` | EVA/IBNR, for the DB supplier only | 6,172 7-digit values |
| FR SNCF TGV-IC-TER | `StopArea:OCE<8>` | UIC 8-digit | 3,549 / 3,549 |
| FR Bretagne BreizhGo | `stop_id`, bare | UIC 8-digit | 427 / 427 |
| FR Hauts-de-France | `StopArea:OCE<8>` + `parent_station` | UIC 8-digit | 380 / 380 |
| FR Région Sud ZOU | `object_codes_extension`, system `UIC` | UIC — the only feed that names the register | 282 codes |
| FR Eurostar | `stop_code` | UIC 7-digit, incl. London 7015400 | 17 / 17 |
| FR Pays-de-la-Loire | `import_id` = `SNCF:ST:87xxxxxx` | UIC 8-digit | 121 rows |
| FR Normandie ATOUMOD | inside `stop_id` | UIC 8-digit | 205 of 12,831 rows (149 stations) |
| BE SNCB (EPIP) | `Quay/@id` | UIC 7-digit — **not** the StopPlace id | 632 / 632 stations |
| IT Trenitalia (NeTEx) | `StopPlace/@id` | 9 digits, `cc` + 7; **semantics undeclared in the file** | 1,943 / 1,943 |
| AT ÖBB (NeTEx) | `StopPlace/@id` | national IFOPT-style `at:<Verbund>:<n>`; **no Austrian UIC at all** | 1,644 / 1,644 |
| LU (NeTEx) | `StopPlace/@id` | CdT national register, 9 digits | 2,814 / 2,814 |
| ES Renfe AVLD + CERCA | `stop_id` | Renfe national 5-digit (UIC = `71` + code) | 1,507 distinct |
| ES Ouigo | `stop_id` | `0071` + the Renfe 5-digit | 16 / 16 |

### 2.2 Feeds with internal identifiers only

IDFM (53,988 stops), OURA (30,596), Nouvelle-Aquitaine (44,703), Grand-Est FLUO (19,225),
LIO Occitanie (12,328), Transilien (6,770), Trenitalia France (19), Euskotren (801),
FGC Catalunya (302), and the two Czech urban/coach archives.

The Czech urban and coach archives deserve a warning of their own: their `StopPlace/@id` is a
**per-file sequence counter**, so the same id denotes a different stop in every member file —
24,144 and 310,247 StopPlace elements respectively, none of them joinable.

The Netherlands file is not NeTEx despite its name; it is IFF. Its only identifier is an
alphabetic abbreviation (`atw` = Antwerpen-Centraal), on 781 stations.

### 2.3 Three traps found in the data

- **The Austrian feed carries its neighbours' codes, but only in the per-line files.** 284 foreign
  stations exist nowhere in the dedicated stop file — including 13 Czech UICs. A consumer reading
  only `EU_PI_STOP_OFFER` silently loses all of central Europe.
- **Renfe reserves an `87xxx` block for French stations.** `87089` is Marseille Saint-Charles
  inside the Renfe 5-digit space, not an SNCF code. Any "87 means France" rule is wrong on ten
  stations.
- **11 SNCF station codes carry `0` instead of the TAF/TAP check digit** — 6 Belgian, 3
  Luxembourgish, 2 Spanish.

---

## 3. Dedicated stop registries, per country

Published separately from the timetable, and therefore richer and more stable.

| Country | Registry | Code carried | Access |
|---|---|---|---|
| BE | Infrabel operational points | **the TAF/TAP PLC**, PTCAR id, telegraph code | open, CC0 |
| CH | Service Points / DiDok | DiDok = UIC 85 + check digit (59,896 valid) | open |
| DE | OpenStation NeTEx (DB InfraGO) | DHID + EVA + RL100, 5,393 stations | open, CC0 |
| DE | DB InfraGO infrastructure | RL100 (`Kürzel`) | open |
| DE | DELFI zHV | national DHID | free registration |
| AT | ÖBB Geo Netz | `IFOPT_ID`, joins straight to the AT NAP key | open (validity ended 2025-12-13) |
| AT | MVO Haltestellen | `hst_globid` IFOPT | registration + licence acceptance |
| NL | Centraal Haltebestand | `NL:Q:<8 digits>` | open |
| NO | National Stop Register | NSR StopPlace / Quay | open |
| DK | DSB stationer | **UIC 86xxxxx** | open |
| SE | Samtrafiken / Trafiklab | national SE:050 id | free API key |
| GB | **NaPTAN** | **ATCOCode = 9100 + TIPLOC** | open |
| IE | NaPTAN (Ireland) | AtcoCode | open |
| BG | NRIC map layer | national station number + check digit | open, read-only query |
| SI | Stop places (NeTEx) | structured StopPlace id | OAuth2 + per-dataset grant |

**No usable dedicated registry**: Luxembourg, Italy, Spain, Poland, Hungary, Romania, Serbia,
Latvia, Lithuania, Estonia, Croatia, Portugal. Three misleading cases: Czechia publishes stop
lists with **no code at all**, only names; France publishes a 417 MB national aggregate whose own
description states its identifiers "are not unique and cannot serve to deduplicate"; Luxembourg's
station file carries **no identifier of any kind**.

**Two openings worth acting on.** NaPTAN makes Great Britain reachable — today its 7,314 rows in
our table carry no code whatsoever, because no British timetable is read. And Infrabel gives the
PLC directly under CC0, for a country where PLC↔UIC alignment is measured at **0 of 643**.

**Six registries need an account**: Slovenia (OAuth2, the grant runs to 2027-02-10), DELFI,
MVO Austria, Trafiklab, Croatia, Hungary. Only Slovenia's has a stated expiry — which is itself a
requirement for the sources screen.

---

## 4. What VIATOR keeps from a NAP today: nothing

- The **catalogue importer** reads a DCAT-AP catalogue and writes provider entries into the
  session config. It persists no station-level anything. The only NAP identifier it ever holds is a
  *dataset* id, and that is stripped before saving.
- The **feed resolvers** (27 entries, 8 hosts, 5 resolver types) resolve a download URL. They do
  not open the file.
- `stations_xref` — the per-session table designed precisely as the feed stop id → UIC bridge — is
  read by the signature code and cleared on session delete, and **has no writer anywhere in the
  repository**.
- A provider may declare a `stations_csv_url`. It is fetched, format-checked, staged to
  `inbox/<sid>/runtime/SNCF-Stations/latest.csv` — and **nothing reads that directory**.
- `master_stations` is Trainline-only and has no NAP, EVA or ÖBB column.

The only NAP-derived station identifier that reaches the database arrives at query time, in
`stop_code` on a MOTIS leg — and **1 of 30 eu19 feeds puts a real UIC there**: Eurostar.

---

## 5. How the engines treat stop identity — measured

### 5.1 MOTIS

- The lookup key is the pair **(source, feed-local id)**. The same id in two feeds is two nodes.
  Rewriting ids therefore changes nothing on its own: as long as two stops come from two feeds,
  they stay two nodes.
- **No merging of any kind.** No name comparison, no id comparison, no code comparison.
  `merge_dupes_intra_src` and `merge_dupes_inter_src` are false and, in any case, merge *trips*,
  not stops.
- The only cross-feed link is `link_nearby_stations`: for pairs whose source differs, a
  straight-line footpath for every pair within **300 m, hardcoded and not configurable**, with
  duration `max(transfer_time_a, transfer_time_b, distance / 1.5 m/s)`. The default transfer time
  is 2 minutes.
- Footpaths are straight-line, not street-routed: `osr_footpath` defaults to false and VIATOR never
  sets it. `connect_components` then takes the transitive closure up to `max_footpath_length`
  (15 min).
- GTFS `parent_station` and NeTEx StopPlace→Quay are honoured **within one dataset only**.
- `transfers.txt` ids are resolved against that feed's own `stops.txt`; an unknown id is logged and
  dropped. **A cross-feed minimum connection time cannot be declared.**
- VIATOR writes no timetable knob at all — every value above is a MOTIS 2.11.2 default.
- **MOTIS receives `from_stop_id` / `to_stop_id` and deliberately ignores them.** On eu19, routing
  is purely geographic.

### 5.2 OTP

- Ids are feed-scoped `<feedId>:<localId>`, the feed id being the uppercased zip stem. Same
  consequence.
- OTP **does** route by stop id when the UI supplies a UIC, and that path exists because the
  coordinate→walk-graph snap fails at border stations (Travers, Pontarlier, Les Verrières).
- Transfers between feeds are generated at build time by routing on the street graph, up to
  `maxTransferDuration` (default 30 min). None of the transfer knobs is set in this repo.
- OTP has a sandbox **stop-consolidation** feature — a CSV of
  `(stop_group_id, feed_id, stop_id, is_primary)` that rewrites patterns onto a primary stop. It is
  not configured anywhere. **This is exactly the shape of our mapping table.**

### 5.3 What MOTIS does and does not honour for connection times

| Behaviour | Result |
|---|---|
| Explicit `min_transfer_time` in `transfers.txt` | honoured (20-minute rule moved the arrival from 10:35 to 11:00) |
| A row pointing a stop at itself | honoured (`S6_HUB,S6_HUB,2,1200` → 11:00) |
| Global default transfer time | honoured, **in minutes**; large values are silently dropped |
| `transfer_type=3` (forbidden) | **not honoured** — proven on a single stop with a self-referencing rule, where no footpath is involved |
| A declared `min_transfer_time` vs a generated footpath | durations resolve by **maximum**, so the declared value is not weakened. (nigiri#339 reports a case where it is erased by `connect_components()`; that is upstream's finding, not ours) |
| A declared minimum longer than `max_footpath_length_` (default 15 min) | **dropped, not clamped** — it is the Dijkstra bound in `connect_components()` |
| Direction | footpaths are directed (`footpaths_out_`/`footpaths_in_`), but the station minimum `transfer_time_` is **one scalar per location**, max-ed onto both directions |
| NeTEx interchange data | **read and discarded entirely** |

What is discarded is not marginal. Complete member-by-member counts for Switzerland and Austria,
and a floor from a partial scan for Germany (4,000 of 27,937 members, 12.6 GB decompressed):
Switzerland 52,060 `InterchangeRule` of which 52,059 carry a minimum time, plus 17,447
`SiteConnection`; Austria 3,127 `InterchangeRule` (all with a minimum time), 15,542
`TransferDuration` and 247,423 `SiteConnection`; Germany at least 344,831 `SiteConnection` and
337,322 `ServiceJourneyInterchange`.

And the data is missing exactly where cross-border routing needs it: **no international operator
publishes any connection time** — not Eurostar, not SNCF TGV, not Trenitalia France, not Renfe AVE.

---

## 6. What this costs today

- **Duplicate nodes.** 421,032 feed stops consolidate to 407,043 stations: close to 14,000 stops
  are the same physical place published by more than one feed. In the graph they are separate nodes
  joined by invented footpaths. Amsterdam Centraal arrives as three Dutch platform nodes plus a
  separate Eurostar node.
- **Invented connections that should not exist.** At Bruxelles-Midi / Brussel-Zuid, the six
  archives that publish the site put every rail node **between 0.2 m and 79 m** of every other,
  across different feeds — so MOTIS fabricates a footpath at the default transfer time, across a
  border control. Nothing can prevent it: no archive declares a connection time between the two
  sides, a cross-feed rule cannot be expressed at all, and a published prohibition is dropped at
  load.
- **Published connection times never reach the router**, because they are in NeTEx.
- **Border stations fail to resolve geographically**, which is why OTP's stop-id path exists.
- **Cross-engine comparison degrades to coordinate matching** whenever the identifier is lost,
  which reads as "the engines disagree" rather than as a data fault.

---

## 7. Defects found in the mapping table already built

The cross-check agents compared the probes with `station_master_crd_2026-09.csv`:

1. **London St Pancras is lost.** Eurostar publishes UIC `7015400`; it was captured in the
   intermediate file and appears nowhere in the final table. It is the only British code we have
   ever had.
2. **The German DHID was never harvested.** `ifopt_dhid` is populated from two other sources and
   from zero DELFI rows — although the DELFI archive carries it on 132,339 stops.
3. **287 foreign codes sit in the German `eva` column**: 142 Czech, 116 Polish, 11 Hungarian,
   10 Slovenian. Two registers mixed in one column.
4. **22 values are not station codes**: one Austrian timetable-point id stored as a station code at
   Tarvisio, and 21 degenerate ids (`nl:84058` on Amsterdam Centraal, `pl:51240` on Kraków Główny).

---

## 8. The plan

### 8.1 The table becomes a build input, not only a reference

The mapping table must emit three artefacts consumed when the graph is built:

| Artefact | What it fixes | Engine lever |
|---|---|---|
| **Id rewrite map + feed merge into one source** | ~14,000 duplicated stations become one node; train/tram/bus interchange at one place becomes exact | MOTIS: merge at build time. OTP: `stopConsolidationFile`, which takes exactly this shape |
| **Complex grouping** (parent station, boarding points) | Midi-Eurostar and Midi national stay two nodes, recognised as one place | both: parent/child within one feed |
| **Compiled `transfers.txt`** | published minimum times finally reach the router | MOTIS: the only channel it reads. OTP: same file |

Note the asymmetry that decides the MOTIS design: **rewriting ids is not enough**, because the key
is (source, id). Stops that must become one node have to end up in **one source**.

### 8.2 NeTEx → GTFS connection times

MOTIS discards NeTEx interchange data and honours GTFS `transfers.txt`. The compilation step is
therefore: read `InterchangeRule` / `MinimumTransferTime` / `SiteConnection` from the NeTEx feeds,
resolve both ends through the mapping table, and emit `transfers.txt` rows in the merged feed.

What it recovers: Switzerland's 52,059 minimum times, Germany's site connections, Austria's rules.

What it cannot carry, and must be reported rather than hidden:

- **direction** — MOTIS transfer times are symmetric, so an asymmetric rule is flattened to its
  maximum;
- **prohibition** — `transfer_type=3` is not enforced at all;
- **anything finer than a station pair** — the TEL TSI precedence (service pair → operator+type →
  type → operator → station default) collapses to one number per pair;
- **minutes only**, and large values are silently dropped.

This is the Box 2 / Box 3 split already decided: the rule stays fully expressive on our side, the
loss happens in the adapter and is reported.

### 8.3 The customs case, honestly

For a complex with a border control, the only lever the engine leaves is ugly: push the two nodes
further apart than the hardcoded 300 m so MOTIS cannot invent its shortcut, then declare the real
connection time. That is a workaround against an engine limit, not a data fix. It stays an open
point for the experts — alternatives are to patch nigiri, or to accept a known false positive and
flag those itineraries in the comparison report.

### 8.4 Order of work

1. Schema and importer in VIATOR; load the table built offline; the five screens.
2. Harvest the station registries that are open and carry a code — Infrabel, DiDok, OpenStation,
   CHB, NSR, NaPTAN — starting with the two that unlock a country: Infrabel (BE) and NaPTAN (GB).
3. Port the builder into the server, with input fingerprints, a feed registry held as data, and a
   rebuild report that diffs against the previous issue.
4. Emit the three build artefacts; wire OTP's stop consolidation first, since it is a configuration
   file rather than a pipeline.
5. Compile NeTEx interchanges into `transfers.txt` with a loss report.

---

## 9. Open points

- The customs complex (§8.3) — workaround, engine patch, or accepted and reported.
- Slovenia's grant expires 2027-02-10; five other registries need an account.
- The ÖBB Geo Netz register expired 2025-12-13 and has no fetched successor.
- The Czech SR70 edition held on disk is the one valid *from* 2026-10-15, not the one in force.
- The eight content decisions already listed in `Station_Master_CRD_2026-09.md` section 10 remain
  open; several change rows on every rebuild.
- No MERITS ground truth: `uic_merits` is inference throughout, confirmed against the real MERITS
  database on exactly one pair (Palermo).
