# eu19-transit-motis — provider research (all 19 countries)

Complete provider reference for the eu19-transit-motis session.
Combines the **11 countries already onboarded in eu11** (URLs + format
+ verdicts pulled from the existing bootstrap script and ops docs) plus
the **8 new countries** added in the eu19 extension.

Every row below is "as best I know from the existing eu11 work +
training + public open-data portals". Rows marked **⚠ needs operator
probe** are ones where the landing-page URL is solid but the exact
bulk-feed URL has moved often enough that we should confirm it
resolves + downloads before scripting the bootstrap. Spot-checking
these is ~2 hours of operator work and should happen before PR #2 (the
OSM merge script update) so we don't script around a dead URL.

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

## All-19 verdict summary

The eu11 column distinguishes feeds we already run from new ones to
onboard. The "Action" column is what changes for eu19 specifically.

| # | Country | Status today | Action for eu19 | Verdict |
|---|---|---|---|---|
| 1 | 🇪🇸 ES | In eu11 (5 GTFS providers) | None — already onboarded | ✅ Keep |
| 2 | 🇫🇷 FR | In eu11 (15 GTFS providers) | None — already onboarded | ✅ Keep |
| 3 | 🇧🇪 BE | In eu11 (NeTEx-EPIP) | None — already onboarded | ✅ Keep |
| 4 | 🇱🇺 LU | In eu11 (NeTEx-EPIP) | None — already onboarded | ✅ Keep |
| 5 | 🇳🇱 NL | In eu11 (URL provider, OpenOV GTFS) | None — already onboarded | ✅ Keep |
| 6 | 🇩🇪 DE | In eu11 (NeTEx-EPIP Gesamtdeutschland) | None — already onboarded | ✅ Keep |
| 7 | 🇦🇹 AT | In eu11 (NeTEx-EPIP) | None — already onboarded | ✅ Keep |
| 8 | 🇮🇹 IT | In eu11 (Trenitalia NeTEx + Trenord/ATAC URL) | None — already onboarded | ✅ Keep |
| 9 | 🇱🇮 LI | In eu11 (covered transitively by CH + AT) | None | ✅ Keep |
| 10 | 🇨🇭 CH | In eu11 (NeTEx-EPIP SBB) | None — already onboarded | ✅ Keep |
| 11 | 🇬🇧 GB | In eu11 (Eurostar via FR NAP + OSM clip) | None — already onboarded | ✅ Keep |
| 12 | 🇩🇰 DK | **NEW** | Onboard Rejseplanen GTFS (URL provider) | ✅ Ready |
| 13 | 🇸🇪 SE | **NEW** | Operator generates Trafiklab key → onboard sweden.zip | ✅ Ready (free key) |
| 14 | 🇳🇴 NO | **NEW** | Onboard Entur GTFS bundle (URL provider) | ✅ Ready |
| 15 | 🇵🇱 PL | **NEW** | **OPERATOR DECISION**: ship city-only with documented PKP gap, OR skip | ⚠ Partial |
| 16 | 🇨🇿 CZ | **NEW** | Onboard CIS national bundle (URL provider) | ✅ Ready |
| 17 | 🇸🇰 SK | **NEW** | Skip — covered indirectly via CZ feed | ❌ Skip |
| 18 | 🇸🇮 SI | **NEW** | Probe nap.gov.si first, then onboard | ⚠ Needs operator probe |
| 19 | 🇭🇺 HU | **NEW** | Onboard MÁV-Start + GySEV (2 URL providers) | ✅ Ready |

# Part 1 — eu11 existing providers (reference)

These are already running in eu11. Documented here for completeness so
the eu19 reference is self-contained. Detailed onboarding history lives
in `docs/nap-fr-rail.md`, `docs/nap-ch-rail.md`, and the bootstrap
script `scripts/create_eurostar_corridor_session.ps1`.

### 🇪🇸 ES — Spain · ✅ In eu11

- **Provider**: NAP España (https://nap.transportes.gob.es/)
- **Onboarded feeds** (all GTFS despite NeTEx-prefixed filenames):
  - `ES-NAP-FGC_Catalunya.zip` — FGC Catalunya regional rail
  - `ES-NAP-_Euskadi_Euskotren.zip` — Euskotren Basque Country
  - `ES-NAP-_Ouigo.zip` — Ouigo Spain low-cost HSR
  - `ES-NAP-_RENFE_AVLD.zip` — Renfe AV / Long-distance
  - `ES-NAP-_RENFE_CERCA.zip` — Renfe Cercanías commuter
- **Auth**: None for downloads (registration needed for portal)
- **Refresh**: Per-operator, typically weekly
- **Gotchas**: Filenames misleadingly carry "NeTEx" prefix but contents are GTFS — `detect.py` classifies by content, not filename.

### 🇫🇷 FR — France · ✅ In eu11

- **Provider**: transport.data.gouv.fr (national OAP)
- **Onboarded feeds** (15 GTFS):
  - **SNCF national**: TGV + Intercités + TER + Transilien (2 separate feeds)
  - **Regional TERs**: BreizhGo (Bretagne), Fluo (Grand Est), Hauts-de-France, LiO (Occitanie), Atoumod (Normandie), Nouvelle-Aquitaine, Oura (Auvergne-Rhône-Alpes), Aleop (Pays de la Loire), ZOU (Région Sud), IDFM (Île-de-France)
  - **International on FR territory**: Eurostar v2, Renfe AVE Int (Madrid-Marseille HSR), Trenitalia FR (Paris-Lyon-Milan)
- **Auth**: None
- **Refresh**: Daily/weekly per operator
- **Gotchas**: cross_border_filter applied (rail-only pre-filter, UIC country whitelist) per `docs/cross-border-routing-as-built.md`.

### 🇧🇪 BE — Belgium · ✅ In eu11

- **Provider**: NAP Belgium (https://www.belgianmobilitydataportal.be/)
- **Onboarded feed**: `BE-NAP-SNCB-epip.zip` (NeTEx-EPIP) — SNCB/NMBS national rail
- **Auth**: None
- **Refresh**: Weekly
- **Gotchas**: NeTEx-EPIP profile; cross-border services to NL/FR/DE/LU all wire up via UIC codes

### 🇱🇺 LU — Luxembourg · ✅ In eu11

- **Provider**: NAP Luxembourg (mobiliteit.lu)
- **Onboarded feed**: `LU-NAP-netex-20260618-20260823.zip` (NeTEx-EPIP) — CFL national rail + bus
- **Auth**: None
- **Refresh**: Quarterly (date range in filename — operator must re-download every ~2 months)
- **Gotchas**: Tiny country, single feed covers everything (rail, bus, tram in Luxembourg City)

### 🇳🇱 NL — Netherlands · ✅ In eu11 (URL provider)

- **Provider**: OpenOV (community aggregator, gov-tolerated)
- **Onboarded feed URL**: http://gtfs.ovapi.nl/nl/gtfs-nl.zip (URL-mode, not file upload)
- **Format**: GTFS (multi-operator national bundle)
- **Auth**: None
- **Refresh**: Daily, sometimes intra-day
- **Operator coverage**: NS (rail) + European Sleeper + Eurostar NL leg + ICE International + Arriva + Keolis + all NL urban transit
- **Gotchas**: The official NL-NAP `_NeTEx` file is actually IFF format (.dat files, NS-only) — VIATOR can't ingest IFF, so we use OpenOV as the primary instead. Documented in `scripts/create_eurostar_corridor_session.ps1` lines 103-109.

### 🇩🇪 DE — Germany · ✅ In eu11

- **Provider**: DELFI Gesamtdeutschland (https://www.delfi.de/)
- **Onboarded feed**: `DE-NAP-fahrplaene_gesamtdeutschland.zip` (NeTEx-EPIP) — national multimodal bundle
- **Auth**: None
- **Refresh**: Weekly
- **Operator coverage**: DB Fernverkehr + DB Regio + S-Bahnen + private operators (FlixTrain, ÖBB Nightjet German legs, etc.) + ~250 regional transit authorities
- **Gotchas**: Large NeTEx feed (~1 GB). Cross-border filter applied.

### 🇦🇹 AT — Austria · ✅ In eu11

- **Provider**: NAP Austria (https://www.mobilitaetsverbuende.at/)
- **Onboarded feed**: `AT-NAP_netex_evu_2026.zip` (NeTEx-EPIP) — ÖBB + WESTbahn + regional Verkehrsverbünde
- **Auth**: None
- **Refresh**: Annual major + weekly minor
- **Operator coverage**: ÖBB long-distance + ÖBB Postbus + 9 Verkehrsverbünde (Wien VOR, Salzburg SVV, Tirol VVT, etc.)
- **Gotchas**: Nightjet runs are in the ÖBB feed, including international legs

### 🇮🇹 IT — Italy · ✅ In eu11

- **Provider**: Multiple — no single NAP for IT national
- **Onboarded feeds**:
  - `IT-TRENITALIA-NeTEx_L1.zip` (NeTEx-EPIP) — Trenitalia national rail
  - Trenord (URL): https://www.dati.lombardia.it/download/3z4k-mxz9/application/zip — Lombardy regional + Malpensa Express
  - ATAC Roma (URL): https://romamobilita.it/sites/default/files/rome_static_gtfs.zip — Rome urban
- **Auth**: None
- **Refresh**: Trenitalia weekly, regionals varies
- **Gotchas**:
  - **Italo (NTV)**: private HSR competitor, **no public GTFS/NeTEx** — only via their booking API or Trainline. Missing from our matrix; cells like `Roma → Milano via Italo` won't appear.
  - GTT (Turin) + TPER (Bologna) not onboarded — see script comments at lines 147-160.

### 🇱🇮 LI — Liechtenstein · ✅ In eu11 (transitive)

- **Provider**: None — covered transitively by CH (SBB serves CH↔LI buses through PostBus) and AT (ÖBB Vorarlberg services include Schaan-Vaduz bus stops)
- **Onboarded feed**: N/A — no LI-specific provider exists
- **Gotchas**: Vaduz has no rail; coverage is exclusively bus. Matrix queries through LI will route via CH/AT bus services.

### 🇨🇭 CH — Switzerland · ✅ In eu11

- **Provider**: opentransportdata.swiss (federal open data portal)
- **Onboarded feed**: `CH-NAP_netex_202606200406.zip` (NeTEx-EPIP) — SBB + all CH operators
- **Auth**: None
- **Refresh**: Quarterly (date in filename)
- **Operator coverage**: SBB CFF FFS + BLS + PostBus + every regional Verkehrsverbund (ZVV, TPG, etc.) + funicular operators
- **Gotchas**: This is the multimodal SBB feed — cross-border filter applied per `docs/cross-border-routing-as-built.md`. SBB's HAFAS instance (different from this static NeTEx) is also available as a runtime verification source via transport.opendata.ch (PR #173 context).

### 🇬🇧 GB — United Kingdom · ✅ In eu11

- **Provider**: Eurostar feed in FR NAP + GB OSM clip
- **Onboarded feed**: `FR-NAP_gtfs_Eurostar_v2.zip` (already counted under FR — provides the GB termini at St Pancras + Ashford + Ebbsfleet)
- **OSM scope**: GB clipped to HS1/Eurostar corridor (bbox `-0.5,50.8,1.5,51.8`)
- **Gotchas**:
  - **National Rail (ATOC)** GTFS is not onboarded — would require parsing the ATOC CIF format or using a community converter
  - Domestic GB rail (LNER, Avanti, GWR, etc.) is **NOT in eu11** — matrix queries like `London → Edinburgh` will return `no_route`
  - Operator can extend later via Network Rail open data (https://datafeeds.networkrail.co.uk/) which publishes CIF — needs a CIF→GTFS converter step

# Part 2 — eu19 NEW providers (8 countries to onboard)

These are the additions for the eu19 extension. Detailed format per
country: provider URL, feed URL, auth, refresh, operator coverage,
gotchas, verdict.

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
- **Verdict**: ✅ Onboard as URL-mode provider (use the direct .zip URL above)

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
- **Verdict**: ✅ Onboard as URL-mode provider once operator generates a key. Key registration: https://www.trafiklab.se/login/ (5 min, free, no commercial use restriction)

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

### 🇵🇱 PL — Poland · ⚠ Partial coverage (operator decision needed)

- **Provider**: No single national NAP. Polish open-data landscape for rail is fragmented:
  - **PKP Intercity** (state long-distance — the trains you'd actually care about for cross-border Berlin↔Warsaw, Vienna↔Warsaw): **NO free public GTFS**. Only available via partner contracts with PKP Group.
  - **PKP SKM Trójmiasto** (Gdańsk regional rail): GTFS via Transportoid mirror, sometimes
  - **Koleje Mazowieckie** (Warsaw region regional rail): GTFS via Warsaw NAP
  - **Warsaw ZTM** (city transit): GTFS at https://api.um.warszawa.pl/
  - **Kraków MPK**: GTFS at https://gtfs.ztp.krakow.pl/
  - **OpenMobilityData** (community aggregator): hosts mirrors of the city feeds but no PKP IC
- **Feed URLs** to consider (if shipping city-only):
  - Warsaw ZTM: https://www.wtp.waw.pl/feed/ (registration needed for key)
  - Kraków MPK: https://gtfs.ztp.krakow.pl/GTFS_KRK_A.zip + `_M.zip` + `_T.zip` (autobus, metro, tram — separate)
  - SKM Trójmiasto: https://mkuran.pl/gtfs/skm.zip (community mirror)
- **Format**: GTFS where available
- **Auth**: Most city feeds are no-key; Warsaw needs registration
- **Refresh**: Varies per city (daily to weekly)
- **Operator coverage**:
  - With city feeds only: Warsaw metro/bus/tram, Kraków city, Gdańsk SKM
  - **Missing**: PKP Intercity (the ONLY long-distance rail in Poland), all of Silesia's regional rail (Koleje Śląskie), Pomerania (Polregio), Małopolska regional (Koleje Małopolskie)
- **Gotchas**:
  - **The headline gap**: without PKP IC, a coverage matrix cell `Warsaw → Berlin` will return `no_route` — the train exists but our data doesn't have it. This is fundamentally a data-availability problem, not a VIATOR/MOTIS bug.
  - Community Transportoid (transportoid.eu) used to mirror PKP IC by scraping; legally grey and the mirror has been intermittent.
  - Polregio briefly published GTFS in 2022 then withdrew it; worth re-checking quarterly.
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
  - **MHD Košice**: city transit GTFS available via https://www.kosice.sk/clanok/otvorene-data
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

# Part 3 — Action items & decisions before PR #2

**Operator action items (block the OSM merge script + bootstrap PRs):**
1. **Generate a Trafiklab API key** for SE (https://www.trafiklab.se/login/) — ~5 min
2. **Probe https://nap.gov.si/** and capture the SŽ feed URL — ~30 min
3. **Decide PL scope**:
   - Option A: ship city-only (Warsaw + Kraków + Gdańsk SKM) with documented "no PKP IC long-distance" gap
   - Option B: skip PL entirely until PKP publishes a public feed
4. (Optional) Verify the **HU MÁV-Start + GySEV** specific feed URLs at https://nap.gov.hu/ — should be straightforward but the per-operator URLs aren't stable

**Once those four are confirmed**:
- PR #2: `scripts/merge_osm_eu19_corridor.sh` — extends the eurostar corridor PBF merger with denmark + sweden + norway + (poland) + czech-republic + (slovakia) + slovenia + hungary
- PR #3: `scripts/create_eu19_transit_motis_session.ps1` — bootstraps the new MOTIS session with all confirmed providers
- PR #4: Alembic migration adding ~25 new hubs (Copenhagen, Stockholm, Oslo, Bergen, Trondheim, Warsaw, Kraków, Prague, Brno, Bratislava-via-CZ, Ljubljana, Budapest, etc.) so the coverage matrix can verify the new corridor

## Out of scope (Phase C, not in eu19)

- 🇭🇷 **HR** Croatia — HŽ has no national NAP, no public GTFS. Zagreb ZET city transit only available.
- 🇷🇸 **RS** Serbia — ŽS no open data. Belgrade GSP has community GTFS but no national rail.
- 🇷🇴 **RO** Romania — CFR Călători no open feed. No EU NAP compliance yet.
- 🇧🇬 **BG** Bulgaria — BDŽ no open feed.
- 🇬🇷 **GR** Greece — Hellenic Train (FS-owned) no open feed. OASA Athens partial.

These could be added as **OSM-only** in a future Phase C session (operator-facing matrix would show `no_route` on every cell — misleading without clear UX hints). Not recommended without operator-side filtering ("hide cells for unsupported countries").

## Open questions / things to revisit

- **PKP Intercity partnership**: any chance of negotiating a partner agreement with PKP? Would unlock the entire Polish long-distance rail network for the matrix.
- **ATOC GB national rail**: integrating CIF → GTFS converter for full UK rail (LNER, Avanti, GWR, ScotRail) — currently only Eurostar termini covered. Half-day effort to onboard once we want it.
- **Italo Italy**: still no public feed. Italy's NeTEx Italian profile rollout (data4pt-project) may add them — watch for 2026 updates.
- **Quarterly refresh sweep**: PL (Polregio, PKP), SK (ZSSK), GR (OASA national) all could become available over time. Worth a quarterly check.
