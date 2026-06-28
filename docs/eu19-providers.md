# eu19-transit-motis — provider research

Research deliverable preceding the eu19 session bootstrap. Extends the
existing eu11 corridor (ES/FR/BE/LU/NL/DE/AT/IT/LI/CH/GB) with 8
additional countries: **DK, SE, NO, PL, CZ, SK, SI, HU**.

Every row below is "as best I know from training + public open-data
portals". Rows marked **⚠ needs operator probe** are ones where the
landing-page URL is solid but the exact bulk-feed URL has moved often
enough that we should confirm it resolves + downloads before scripting
the bootstrap. Spot-checking these is ~2 hours of operator work and
should happen before PR #2 (the OSM merge script update) so we don't
script around a dead URL.

## Capacity verification (VPS probe, 2026-06-28)

Probed against the running prod VPS:

| Resource | Current | Headroom available | eu19 estimate | Verdict |
|---|---|---|---|---|
| RAM | 94 GiB total, 21 GiB free | +38 GiB by pausing top 2 sessions | ~36 GiB serve, ~65 GiB peak build | ✅ Feasible |
| Disk | 678 GB, 255 GB free | — | +30-35 GB OSM, +5 GB feeds, +50 GB build buffer | ✅ Feasible |
| MOTIS heap | No hard limit | Uses host RAM | Auto-sized per `rebuild-max-memory` (PR #19) | ✅ Existing mechanism |

**Build path**: pause the 21 GiB and 17 GiB session containers, run
`rebuild-max-memory` mode for eu19, restart paused sessions after the
serve container handoff. Total wallclock estimate: 4-6 hours.

## Per-country summary

| Country | Format | Auth | Coverage | Verdict |
|---|---|---|---|---|
| DK | GTFS | None | DSB + Metro + regional rail + bus + ferry | ✅ Ready |
| SE | GTFS | Free key (Trafiklab) | SJ + 21 regional operators (SL, Skånetrafiken, …) | ✅ Ready |
| NO | NeTEx-Nordic + GTFS | None | Vy + Go-Ahead Nordic + SJ Nord + Flytoget + bus + ferry | ✅ Ready |
| PL | GTFS (fragmented) | None per source | City transit (Warsaw, Kraków, …) — **no PKP Intercity** | ⚠ Partial — see notes |
| CZ | GTFS | None | ČD + Leo Express + RegioJet + ARRIVA + PID Prague | ✅ Ready |
| SK | GTFS (Bratislava only) | None | DPB Bratislava city transit only — **no ZSSK national rail** | ❌ Skip until ZSSK publishes |
| SI | NeTEx (EPIP) | None | SŽ + regional bus | ⚠ Needs operator probe |
| HU | GTFS | None | MÁV + GySEV + Volánbusz + BKV | ✅ Ready |

## Detailed per-country findings

### 🇩🇰 DK — Denmark · ✅ Ready

- **Provider**: Rejseplanen A/S (national journey-planner consortium)
- **Open data portal**: https://help.rejseplanen.dk/hc/en-us/categories/200023636
- **Feed URL** (GTFS): https://www.rejseplanen.info/labs/GTFS.zip
- **Format**: GTFS (zip), single national bundle
- **Auth**: None
- **Refresh**: Weekly (typically Wednesday)
- **Operator coverage**:
  - **DSB** (Danish State Railways, long-distance + S-tog)
  - **DSB Øresund** (cross-border to SE/Malmö)
  - **Lokaltog** (regional rail — A/S Vestsjælland, Lollandsbanen, …)
  - **Arriva Tog Danmark** (some Jutland routes)
  - **Metro København**
  - **Movia** (Zealand region bus)
  - **Midttrafik / Sydtrafik / FynBus / NT** (Jutland bus operators)
  - **Færger** (ferries — Mols-Linien, etc.)
- **Gotchas**: Multimodal bundle. Cross-border filter (`gtfs_cross_border_filter.py`) needs rail-only pre-filter as we already do for SBB. The DSB Øresund cross-border trains (København → Malmö) will route through if SE feed is also present.
- **Verdict**: ✅ Onboard as upload-mode provider (one zip per refresh)

### 🇸🇪 SE — Sweden · ✅ Ready (needs free API key)

- **Provider**: Trafiklab (Samtrafiken-operated, gov-funded data hub)
- **Open data portal**: https://www.trafiklab.se/api/our-apis/
- **Feed URL** (GTFS national bundle): https://opendata.samtrafiken.se/gtfs-sweden/sweden.zip?key={API_KEY}
- **Format**: GTFS (zip)
- **Auth**: Free Trafiklab account + per-feed API key (issued instantly on signup). Key passed as query param `?key=...`
- **Refresh**: Daily
- **Operator coverage**:
  - **SJ** (intercity rail)
  - **MTR** (MTRX intercity, MTR Pendeltåg Stockholm)
  - **Snälltåget** (private overnight operator)
  - **Tågkompaniet / Vy Tåg** (regional rail)
  - **Pågatågen, Öresundståg** (Skåne regional, includes cross-border DK)
  - **SL** (Stockholm metro, commuter rail, bus, tram)
  - **Skånetrafiken** (Skåne all-modes)
  - **Västtrafik** (Göteborg region all-modes)
  - **UL, Östgötatrafiken, Värmlandstrafik**, … (12 more regional)
- **Gotchas**: API key in URL → store in platform_config not in version control. Multimodal. Schema version drift: Trafiklab occasionally adds GTFS-Plus fields (block_id, etc.) — should pass through `gtfs_cross_border_filter` cleanly but worth a sanity import.
- **Verdict**: ✅ Onboard as URL-mode provider once operator generates a key

### 🇳🇴 NO — Norway · ✅ Ready (gold standard)

- **Provider**: Entur (state-owned, sole national journey planner)
- **Open data portal**: https://developer.entur.org/pages-nsr-nsr
- **Feed URLs**:
  - NeTEx (Nordic profile): https://storage.googleapis.com/marduk-production/outbound/netex/rb_norway-aggregated-netex.zip
  - GTFS: https://storage.googleapis.com/marduk-production/outbound/gtfs/rb_norway-aggregated-gtfs.zip
- **Format**: NeTEx (Nordic profile) + GTFS both published; **pick one**, MOTIS handles both
- **Auth**: None
- **Refresh**: Daily, sometimes intra-day
- **Operator coverage**:
  - **Vy** (formerly NSB — passenger rail, all national long-distance + regional)
  - **Go-Ahead Nordic** (Sørlandsbanen)
  - **SJ Nord** (Nordlandsbanen, Trønderbanen)
  - **Flytoget** (Oslo airport express)
  - **Bane NOR Eiendom** (the infrastructure side, not service but stops are here)
  - **Ruter** (Oslo all-modes: T-bane, tram, bus, ferry)
  - **AtB** (Trondheim region)
  - **Skyss** (Bergen region)
  - **Kolumbus** (Stavanger region)
  - **Kystverket** (national coastal ferries — Hurtigruten partially)
  - **Nor-way Bussekspress** (long-distance bus)
- **Gotchas**: NeTEx-Nordic profile detection — `detect.py` already classifies it (verified during the eu11 work). NeTEx feed is ~2x larger than GTFS bundle; if memory is tight, prefer GTFS. The "rb_" prefix means "regional bundle" — there's no national bundle separately; rb_norway-aggregated IS the national.
- **Verdict**: ✅ Onboard as URL-mode provider. Pick GTFS to keep memory tight.

### 🇵🇱 PL — Poland · ⚠ Partial coverage

- **Provider**: No single national NAP. Polish open-data landscape for rail is fragmented:
  - **PKP Intercity** (state long-distance — the trains you'd actually care about for cross-border Berlin↔Warsaw, Vienna↔Warsaw): **NO free public GTFS**. Only available via partner contracts with PKP Group.
  - **PKP SKM Trójmiasto** (Gdańsk regional rail): GTFS via Transportoid mirror, sometimes
  - **Koleje Mazowieckie** (Warsaw region regional rail): GTFS via Warsaw NAP
  - **Warsaw ZTM** (city transit): GTFS at https://api.um.warszawa.pl/
  - **Kraków MPK**: GTFS at https://gtfs.ztp.krakow.pl/
  - **OpenMobilityData** (community aggregator): hosts mirrors of the city feeds but no PKP IC
- **Format**: GTFS where available
- **Auth**: Most city feeds are no-key; some require registration
- **Refresh**: Varies per city (daily to weekly)
- **Operator coverage**:
  - With city feeds only: Warsaw metro/bus/tram, Kraków city, Gdańsk SKM, Wrocław MPK
  - **Missing**: PKP Intercity (the ONLY long-distance rail in Poland), all of Silesia's regional rail (Koleje Śląskie), Pomerania (Polregio), Małopolska regional (Koleje Małopolskie)
- **Gotchas**:
  - **The headline gap**: without PKP IC, a coverage matrix cell `Warsaw → Berlin` will return `no_route` — the train exists but our data doesn't have it. This is fundamentally a data-availability problem, not a VIATOR/MOTIS bug.
  - Community Transportoid (transportoid.eu) used to mirror PKP IC by scraping; legally grey and the mirror has been intermittent.
  - Polregio has briefly published GTFS in 2022 then withdrew it; worth re-checking quarterly.
- **Verdict**: ⚠ Partial — onboard the 3-4 best city feeds (Warsaw, Kraków, Gdańsk SKM, Koleje Mazowieckie if accessible). Document the PKP IC gap clearly in the operator-facing session notes. **Operator decision needed**: ship with city-only coverage, or skip PL until PKP publishes.

### 🇨🇿 CZ — Czech Republic · ✅ Ready

- **Provider**: CIS JŘ (Centrální informační systém jízdních řádů — national timetable system) + opendata.gov.cz aggregator
- **Open data portal**: https://data.gov.cz/datov%C3%A9-sady?dotaz=GTFS
- **Feed URLs**:
  - National GTFS bundle (CIS JŘ): https://portal.cisjr.cz/static/jdf/JDF-GTFS.zip
  - PID (Prague Integrated Transport): https://data.pid.cz/PID_GTFS.zip
- **Format**: GTFS — national bundle covers all CIS-registered carriers
- **Auth**: None
- **Refresh**: Weekly
- **Operator coverage** (CIS national bundle):
  - **ČD** (České dráhy — state rail, long-distance + regional)
  - **Leo Express** (private intercity)
  - **RegioJet** (private intercity + regional)
  - **ARRIVA vlaky** (some regional)
  - **GW Train Regio** (Karlovarský kraj)
  - **AŽD Praha** (test line)
  - + ~30 regional bus operators registered with CIS
  - **PID** (Prague metro/tram/bus/Esko commuter rail) — in the separate PID bundle
- **Gotchas**: Some regional bus operators don't publish to CIS, only locally. Acceptable for rail-focused use. CIS GTFS uses Czech-localised stop names — federation across borders may need name normalisation for `Praha hl.n.` vs `Prague` matching.
- **Verdict**: ✅ Onboard as URL-mode provider (CIS national bundle). PID optional — only needed if Prague intra-city journeys matter for coverage.

### 🇸🇰 SK — Slovakia · ❌ Skip until ZSSK publishes

- **Provider**: No national NAP for rail data
- **Status**: ZSSK (state rail) publishes timetables via cp.zsr.sk but **not as bulk GTFS**. The cp.zsr.sk interface is an HTML journey planner only.
- **Available open data**:
  - **DPB Bratislava** (city transit — metro, tram, bus): https://opendata.bratislava.sk/ — has GTFS
  - **MHD Košice**: city transit GTFS available
- **What's missing**: National rail (ZSSK, RegioJet SK, Leo Express SK Bratislava-Žilina-Košice corridor), national bus (SAD Bratislava, SAD Žilina, …)
- **Workaround**:
  - The CZ CIS feed includes some cross-border services into SK (mainly Bratislava-area RegioJet/Leo Express) since they're Czech-registered carriers crossing the border. This gives partial SK rail coverage as a side effect of onboarding CZ.
  - HU MÁV feed includes the GySEV/Raaberbahn services that cross HU↔SK↔AT (Sopron-Wiener Neustadt corridor) — minor SK presence.
- **Verdict**: ❌ Don't onboard a dedicated SK provider — it would only bring DPB city transit. Document that SK national-rail coverage is partial-via-CZ-feed only. Re-evaluate quarterly.

### 🇸🇮 SI — Slovenia · ⚠ Needs operator probe

- **Provider**: NAP Slovenia (per EU Directive 2017/1926)
- **Open data portal**: https://nap.gov.si/ (national access point — multimodal data sharing per EU mandate)
- **Feed URLs**: ⚠ I have the portal URL but not the exact feed-download URL — they tend to live behind a search-and-download page rather than a stable static URL. **Operator probe needed**:
  - Visit https://nap.gov.si/, find the SŽ (Slovenian Railways) feed listing
  - Capture the exact download URL + format (likely NeTEx EPIP profile per EU mandate)
- **Format**: Likely NeTEx EPIP. `detect.py` should classify as `NeTEx-EPIP` after the fix in PR #161.
- **Auth**: Public NAP per EU directive — should be no-key
- **Refresh**: Varies per operator (SŽ probably weekly)
- **Operator coverage**:
  - **SŽ** (Slovenske železnice — national rail)
  - **Arriva Slovenija** (long-distance bus)
  - **LPP** (Ljubljana city transit)
  - + regional bus operators
- **Gotchas**: NeTEx EPIP profile — Italian and Slovenian NAPs both use EPIP and have had cross-border consistency issues in 2024 (different coordinate reference frames). Worth a smoke import.
- **Verdict**: ⚠ Onboard pending operator probe. Drop a chip in the next sprint for "visit nap.gov.si, capture SŽ feed URL".

### 🇭🇺 HU — Hungary · ✅ Ready

- **Provider**: NAP Hungary (https://nap.gov.hu/) — per EU Directive
- **Open data portal**: https://nap.gov.hu/ (also mirrors at https://kif.gov.hu/)
- **Feed URLs**:
  - MÁV-Start (national rail) — published on NAP as GTFS, refresh approximately weekly
  - GySEV — Hungarian-Austrian cross-border rail, separate feed on NAP
  - BKV (Budapest transit) — GTFS, daily refresh
  - Volánbusz — national long-distance bus, GTFS
- **Format**: GTFS for all (NAP standardised on GTFS for Hungary)
- **Auth**: None for bulk download (registration optional, gives notification on schedule changes)
- **Refresh**: MÁV weekly, BKV daily, Volánbusz weekly
- **Operator coverage**:
  - **MÁV-Start** (long-distance + regional rail — Budapest hubs to Debrecen, Szeged, Pécs, Sopron, Miskolc)
  - **GySEV / Raaberbahn** (cross-border Sopron-Wiener Neustadt + Hungarian regional)
  - **MÁV-HÉV** (Budapest suburban — different feed from MÁV-Start)
  - **Volánbusz** (national bus, includes ÖBB-coordinated international coaches)
  - **BKV** (Budapest metro M1-M4, tram, bus, HÉV, trolley)
- **Gotchas**: HU NAP publishes each operator as a separate feed, not a national bundle. So onboarding is 3-4 URL providers, not one. MÁV-HÉV vs MÁV-Start naming confusion — both go through Budapest but different services.
- **Verdict**: ✅ Onboard MÁV-Start + GySEV as primary rail providers. BKV + Volánbusz optional depending on whether intra-Hungary city/bus coverage matters.

## Summary recommendation

**Onboard in PR #2 (OSM merge) + PR #3 (session bootstrap):**

| Country | Action |
|---|---|
| DK | Onboard Rejseplanen GTFS (URL provider) |
| SE | Operator generates Trafiklab key, then onboard sweden.zip (URL provider) |
| NO | Onboard Entur GTFS bundle (URL provider) |
| PL | **OPERATOR DECISION**: ship 3-4 city feeds with documented PKP IC gap, OR skip until PKP improves |
| CZ | Onboard CIS national bundle (URL provider) |
| SK | **Skip** — covered indirectly via CZ feed |
| SI | **Probe first** — capture SŽ feed URL from nap.gov.si, then onboard |
| HU | Onboard MÁV-Start + GySEV (2 URL providers) |

**Operator action items before PR #2:**
1. Generate a Trafiklab API key (https://www.trafiklab.se/login/) for SE — takes 5 min
2. Probe https://nap.gov.si/ and capture the SŽ feed URL — 30 min
3. Decide PL scope (city-only ⚠ or skip ❌) — informed by your willingness to live with the Warsaw↔Berlin coverage gap

Once those are confirmed, PR #2 (OSM merge script `scripts/merge_osm_eu19_corridor.sh`) and PR #3 (session bootstrap PowerShell) can land with a complete provider list.

## Open questions / things to revisit

- **PKP Intercity**: any chance of a partner agreement with PKP? Would unlock the entire Polish long-distance rail network for the matrix.
- **Eurostar**: already in eu11 via the GB clip. No change for eu19.
- **Nightjet / Eurocity overnight trains**: covered indirectly via ÖBB (in eu11) and DB (in eu11). HU MÁV → ÖBB Sopron handover should work once HU is onboarded.
- **Future Phase C** (Balkans, Greece): explicitly out of scope for eu19. The Croatia / Serbia / Romania / Bulgaria / Greece rail data landscape is so sparse that even OSM-only coverage would be misleading (operator clicks coverage cells expecting trains, sees `no_route` everywhere).
