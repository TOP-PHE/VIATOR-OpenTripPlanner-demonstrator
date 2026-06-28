# eu19-transit-motis — provider research (all 19 countries)

Complete provider reference for the eu19-transit-motis session, with
sources anchored to the **official National Access Points (NAPs)** per
EU Delegated Regulation 2017/1926 (action 'a' — Multimodal Travel
Information Services).

> **Compliance principle (added 2026-06-29 after operator review):**
> Every dataset onboarded into VIATOR MUST be discoverable through the
> destination country's official MMTIS NAP. Community mirrors (e.g.
> Rejseplanen labs, Entur's marduk bucket, Trafiklab, OpenOV) MAY be
> used as the download mechanism — they're often faster / more
> convenient than the NAP portal's catalogue indirection — but ONLY
> when the same dataset is also published through the NAP. If a
> community URL exists but the NAP doesn't reference the dataset, we
> don't onboard it.

Authoritative NAP list: extracted from the European Commission
`its-national-access-points.pdf` (updated October 2025), MMTIS column.
Stored in `European NAP TimeTables/` outside the repo.

## Compliance audit of the previous draft

The first version of this doc (commit `94ae9a5`) used community URLs
rather than the official MMTIS NAPs. Corrected below.

| Country | Previous (wrong) URL | Official MMTIS NAP (per PDF) |
|---|---|---|
| DK | `rejseplanen.info/labs/GTFS.zip` (community) | https://nap.vd.dk/ |
| SE | `trafiklab.se` (community) | www.trafficdata.se |
| NO | `storage.googleapis.com/marduk-production` (Entur internal) | https://transportportal.atlas.vegvesen.no/no/ |
| PL | Fragmented city URLs | https://dane.gov.pl/en/dataset/1739,NAP |
| CZ | `portal.cisjr.cz` (CIS — provider, not NAP) | http://registr.dopravniinfo.cz/en/ |
| SK | "no NAP exists" — wrong | https://aplikacie.zsr.sk/MapaVylukZsr/index.aspx |
| SI | `nap.gov.si` — wrong domain | "NAP - National Traffic Management Centre" (PDF gives no working URL) |
| HU | `nap.gov.hu` — wrong | https://napportal.kozut.hu/ |
| ES | `nap.transportes.gob.es` (works via redirect) | https://nap.mitma.es/ |
| BE | `belgianmobilitydataportal.be` (works via redirect) | https://www.transportdata.be/en/ |
| LU | `mobiliteit.lu` (provider) | https://data.public.lu/en/ |
| NL | `gtfs.ovapi.nl` (community OpenOV) | https://ntm.ndw.nu |
| DE | `delfi.de` (provider behind portal) | https://mobilithek.info/ |
| AT | `mobilitaetsverbuende.at` (provider portal) | http://www.mobilitydata.gv.at/ |
| IT | "no single NAP" — wrong | https://www.cciss.it/ |
| GB | "no NAP listed" — wrong | https://data.gov.uk/ |
| FR | https://transport.data.gouv.fr/ | ✅ Same — IS the NAP |
| CH | www.opentransportdata.swiss | ✅ Same — IS the NAP |

## Live NAP probe findings (2026-06-29)

I attempted to verify each official NAP URL with a non-interactive
HTTP fetch + content extraction. **Mixed results — the modern NAP
portals are JavaScript-rendered single-page apps that don't expose
their catalogues to programmatic probes.** Operator browser-based
navigation will see what's there; my static probes can't.

What I actually confirmed:

| Country | NAP URL | Live probe verdict |
|---|---|---|
| 🇨🇿 CZ | registr.dopravniinfo.cz/en/ | ✅ Confirmed: portal lists "NeTEx - Timetable information" as a published source category (`sources/cz-mdcr_NeTEx-timetables-v1.0/`). Specific operator coverage requires browser navigation into that sub-page. |
| 🇸🇪 SE | trafficdata.se | ⚠ **Concerning**: catalogue has 49 datasets but is dominated by Trafikverket **road data** (38 datasets); only ~10 transit-related (Public Transport 4, Bus 3, Train 3). The Trafiklab GTFS Sverige bundle is **NOT** referenced. Suggests the SE MMTIS NAP per the PDF is actually a road-traffic-focused portal, with transit feeds living separately under Trafiklab. |
| 🇭🇺 HU | napportal.kozut.hu | ⚠ **Concerning**: portal's own title in Hungarian is `Közúti közlekedés nemzeti adathozzáférési pontja` = "**Road traffic** National Access Point". This is operated by Magyar Közút (Hungarian Road Administration). The /datasets sub-URL returned 404. Strongly suggests rail/MMTIS data is on a different portal that the PDF doesn't list. |
| 🇳🇴 NO | transportportal.atlas.vegvesen.no | ⚠ SPA — portal exists ("Felles datakatalog" / Shared Data Catalogue) but the dataset list requires JavaScript to render. Couldn't programmatically inventory. Catalogue is at `data.transportportal.no/datasets` (per the landing page text) which redirects to the SPA shell. |
| 🇵🇱 PL | dane.gov.pl/dataset/1739 | ⚠ SPA — dataset page returns "Otwarte Dane" header only; resources behind JS render. Couldn't confirm whether PKP IC / Polregio are catalogued. |
| 🇩🇰 DK | nap.vd.dk | Not probed (operator confirmed it's the right NAP) |
| 🇸🇮 SI | (no URL in PDF) | Cannot probe — URL unknown |
| 🇸🇰 SK | aplikacie.zsr.sk/MapaVylukZsr | Not probed; portal name ("disruption map") makes it unlikely to host timetables |

**What this means**:

1. **The PDF MMTIS column may be misleading for SE and HU.** Both URLs appear to be road-traffic NAPs operated by road administrations, not multimodal/transit portals. Sweden's actual transit-data hub is widely understood to be **Trafiklab** (Samtrafiken-operated) and Hungary's is **MÁV's NAP** or one of the ministry-level portals — but neither is in the EU Commission's official list. This is a **real EU enforcement gap**: multiple member states have published "NAP" URLs that don't comply with the multimodal scope of Regulation 2017/1926. If we strictly follow the PDF for SE/HU, we get road data only and effectively no transit. If we use Trafiklab/MÁV directly, we're compliant in spirit (the data is public per the regulation's intent) but not anchored to the formally-listed NAP.

2. **NO is probably fine** — Entur is state-owned and operates as Norway's de-facto MMTIS data hub. The transportportal almost certainly references Entur's bundle URLs once the operator can navigate into the catalogue. Browser confirmation should be quick.

3. **PL is unknowable until browser probe** — the PKP IC question (the headline blocker for PL coverage) can't be answered from a static fetch. Operator needs to visit dane.gov.pl/dataset/1739 in a browser.

4. **CZ looks solid** — the NeTEx-timetables source category is referenced on the landing page; navigating into it should yield the canonical download URL.

**Practical recommendation**: when the operator does the NAP probes (action items at the bottom), screen-capture or paste the dataset listings into a follow-up so we can record the exact compliant URLs. For SE and HU specifically, document why we use Trafiklab and MÁV-direct respectively (if we do), citing the formal-NAP-is-road-only gap.

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

The "Action for eu19" column is the operator-facing decision: keep
existing eu11 feed, onboard via official NAP, or skip.

| # | Country | Official MMTIS NAP | In eu11? | Action for eu19 | Verdict |
|---|---|---|---|---|---|
| 1 | 🇪🇸 ES | https://nap.mitma.es/ | Yes (5 GTFS) | Confirm existing feeds also catalogued in nap.mitma.es | ✅ Keep, verify compliance |
| 2 | 🇫🇷 FR | https://transport.data.gouv.fr/ | Yes (15 GTFS) | None — current source IS the NAP | ✅ Compliant |
| 3 | 🇧🇪 BE | https://www.transportdata.be/en/ | Yes (NeTEx-EPIP) | Verify SNCB feed catalogued via transportdata.be | ✅ Keep, verify compliance |
| 4 | 🇱🇺 LU | https://data.public.lu/en/ | Yes (NeTEx-EPIP) | Verify CFL feed catalogued via data.public.lu | ✅ Keep, verify compliance |
| 5 | 🇳🇱 NL | https://ntm.ndw.nu | Partial (OpenOV community URL) | **Verify OpenOV referenced from ntm.ndw.nu**, otherwise switch source | ⚠ Compliance check needed |
| 6 | 🇩🇪 DE | https://mobilithek.info/ | Yes (NeTEx-EPIP from DELFI) | Verify DELFI feed catalogued via mobilithek.info | ✅ Keep, verify compliance |
| 7 | 🇦🇹 AT | http://www.mobilitydata.gv.at/ | Yes (NeTEx-EPIP) | Verify NAP feed catalogued via mobilitydata.gv.at | ✅ Keep, verify compliance |
| 8 | 🇮🇹 IT | https://www.cciss.it/ | Yes (Trenitalia NeTEx) | **Verify Trenitalia + Trenord + ATAC all catalogued via cciss.it**, switch URLs if needed | ⚠ Compliance check needed |
| 9 | 🇱🇮 LI | n/a (EEA, no MMTIS NAP) | Yes (transitive via CH/AT) | None | ✅ Keep |
| 10 | 🇨🇭 CH | www.opentransportdata.swiss | Yes (NeTEx-EPIP) | None — current source IS the NAP | ✅ Compliant |
| 11 | 🇬🇧 GB | https://data.gov.uk/ | Yes (only Eurostar via FR NAP) | None — coverage is via FR NAP | ✅ Keep |
| 12 | 🇩🇰 DK | **https://nap.vd.dk/** | No | **Operator probe** — navigate nap.vd.dk to find DSB / Rejseplan datasets, capture exact feed URLs | ⚠ NAP probe needed |
| 13 | 🇸🇪 SE | www.trafficdata.se | No | **Operator probe** — navigate trafficdata.se, confirm whether Trafiklab feeds are catalogued there OR find the official NAP-published feed | ⚠ NAP probe needed |
| 14 | 🇳🇴 NO | https://transportportal.atlas.vegvesen.no/no/ | No | **Operator probe** — confirm Entur GTFS bundle is catalogued via the Vegvesen transportportal; if so, that URL is the legitimate download | ⚠ NAP probe needed |
| 15 | 🇵🇱 PL | https://dane.gov.pl/en/dataset/1739,NAP | No | **Operator probe** — visit dataset 1739 on dane.gov.pl, see what rail / city transit feeds are listed | ⚠ NAP probe needed (+ operator scope decision) |
| 16 | 🇨🇿 CZ | http://registr.dopravniinfo.cz/en/ | No | **Operator probe** — confirm CIS JŘ GTFS is catalogued via registr.dopravniinfo.cz | ⚠ NAP probe needed |
| 17 | 🇸🇰 SK | https://aplikacie.zsr.sk/MapaVylukZsr/index.aspx | No | **Operator probe** — verify if this ZSR portal exposes timetable feeds (NAP is listed but it's a "Mapa výluk" / disruption map portal, may not include schedules) | ❌ Skip likely, probe to confirm |
| 18 | 🇸🇮 SI | "NAP - National Traffic Management Centre" (no URL in PDF) | No | **Operator action: find the working URL for SI's NAP**, then probe | ❌ Blocked until NAP URL discovered |
| 19 | 🇭🇺 HU | https://napportal.kozut.hu/ | No | **Operator probe** — confirm MÁV / GySEV / Volánbusz feeds catalogued via napportal.kozut.hu | ⚠ NAP probe needed |

# Part 1 — eu11 existing providers (compliance verification needed)

These are already running in eu11. The action for eu19 is to verify
each existing feed source is also catalogued in the official NAP, NOT
to re-onboard from scratch.

### 🇪🇸 ES — Spain · ✅ Keep, verify

- **Official NAP**: https://nap.mitma.es/ (MITMA — Ministerio de Transportes y Movilidad Sostenible)
- **Currently onboarded** (all GTFS despite NeTEx-prefixed filenames):
  - FGC Catalunya, Euskotren, Ouigo, Renfe AVLD, Renfe Cercanías
- **Compliance check**: Visit nap.mitma.es, search for each of the above operators, confirm the dataset is catalogued. If yes → current setup is compliant. If a dataset isn't listed in the NAP, raise with the operator before next refresh.
- **Auth**: None for downloads
- **Refresh**: Per-operator, typically weekly
- **Gotchas**: Filenames misleadingly carry "NeTEx" prefix but contents are GTFS — `detect.py` classifies by content, not filename.

### 🇫🇷 FR — France · ✅ Compliant (current source IS the NAP)

- **Official NAP**: https://transport.data.gouv.fr/ — same as currently used. No action needed.
- **Currently onboarded** (15 GTFS):
  - SNCF national: TGV + Intercités + TER + Transilien (2 separate feeds)
  - Regional TERs: BreizhGo, Fluo, HDF, LiO, Atoumod, Nouvelle-Aquitaine, Oura, Aleop, ZOU, IDFM
  - International on FR territory: Eurostar v2, Renfe AVE Int, Trenitalia FR
- **Refresh**: Daily/weekly per operator
- **Gotchas**: cross_border_filter applied per `docs/cross-border-routing-as-built.md`.

### 🇧🇪 BE — Belgium · ✅ Keep, verify

- **Official NAP**: https://www.transportdata.be/en/ (Vlaamse Mobiliteitscentrale + SPF Mobilité)
- **Currently onboarded**: `BE-NAP-SNCB-epip.zip` (NeTEx-EPIP) — SNCB/NMBS national rail
- **Compliance check**: Confirm the SNCB-EPIP feed is catalogued in transportdata.be. Belgian NAP federates Flemish + Walloon + federal data; the SNCB feed should appear under federal-level catalog.
- **Auth**: None
- **Refresh**: Weekly

### 🇱🇺 LU — Luxembourg · ✅ Keep, verify

- **Official NAP**: https://data.public.lu/en/ (general gov open-data portal — NAP listed under "organizations/administration-des-ponts-et-chaussees")
- **Currently onboarded**: `LU-NAP-netex-20260618-20260823.zip` (NeTEx-EPIP) — CFL national rail + bus
- **Compliance check**: Find the dataset under the Ponts-et-Chaussées organisation on data.public.lu, confirm it's the same feed currently in use.
- **Auth**: None
- **Refresh**: Quarterly (date range in filename)
- **Gotchas**: Tiny country, single feed covers everything (rail, bus, tram in Luxembourg City)

### 🇳🇱 NL — Netherlands · ⚠ Compliance check needed

- **Official NAP**: https://ntm.ndw.nu (NDW — Nationaal Dataportaal Wegverkeer; "NTM" = Nationaal Toegangspunt Mobiliteit)
- **Currently onboarded**: OpenOV community GTFS at `http://gtfs.ovapi.nl/nl/gtfs-nl.zip` (URL-mode)
- **Compliance check**: Visit ntm.ndw.nu, search for the multimodal GTFS / NeTEx feed catalogued there. Two possibilities:
  - **Best case**: OpenOV (gtfs.ovapi.nl) is officially referenced from ntm.ndw.nu as the canonical bulk download → current setup is compliant
  - **Worst case**: ntm.ndw.nu points at a different feed (perhaps the IFF-format one we previously rejected, or a separate NeTEx published by NDW itself) → we'd need to switch sources, document the OpenOV-vs-NAP gap, or argue for a derogation
- **Refresh**: Daily (current OpenOV)
- **Operator coverage**: NS + European Sleeper + Eurostar NL + ICE International + Arriva + Keolis + all NL urban transit (current OpenOV bundle)
- **Action**: Operator probes ntm.ndw.nu and confirms; PR if a source switch is needed.

### 🇩🇪 DE — Germany · ✅ Keep, verify

- **Official NAP**: https://mobilithek.info/ (the merger of Mobilitäts Daten Marktplatz + ÖPNV-Datenmarktplatz; replaces the old MDM)
- **Currently onboarded**: `DE-NAP-fahrplaene_gesamtdeutschland.zip` (NeTEx-EPIP) from DELFI — national multimodal bundle
- **Compliance check**: DELFI publishes its Gesamtdeutschland bundle THROUGH mobilithek.info — should be discoverable as a dataset there. Confirm and document the mobilithek.info dataset URL.
- **Auth**: None
- **Refresh**: Weekly
- **Operator coverage**: DB Fernverkehr + DB Regio + S-Bahnen + private operators (FlixTrain, ÖBB Nightjet German legs) + ~250 regional transit authorities

### 🇦🇹 AT — Austria · ✅ Keep, verify

- **Official NAP**: http://www.mobilitydata.gv.at/ (note: HTTP not HTTPS in PDF — operator should verify whether it redirects to HTTPS)
- **Currently onboarded**: `AT-NAP_netex_evu_2026.zip` (NeTEx-EPIP) — ÖBB + WESTbahn + regional Verkehrsverbünde
- **Compliance check**: Confirm the AT-NAP bundle on mobilitydata.gv.at. AT also has a separate `mobilitaetsdaten.gv.at` (with the 's') — those are the two faces of the same federal NAP.
- **Auth**: None
- **Refresh**: Annual major + weekly minor

### 🇮🇹 IT — Italy · ⚠ Compliance check needed

- **Official NAP**: https://www.cciss.it/ (Centro di Coordinamento Informazioni Sicurezza Stradale; Ministero delle Infrastrutture)
- **Currently onboarded**:
  - `IT-TRENITALIA-NeTEx_L1.zip` (NeTEx-EPIP) — Trenitalia national rail
  - Trenord (URL): `https://www.dati.lombardia.it/...` — Lombardy regional
  - ATAC Roma (URL): `https://romamobilita.it/...` — Rome urban
- **Compliance check**: Visit cciss.it, search for each onboarded feed:
  - Trenitalia NeTEx — likely catalogued, confirm exact URL
  - Trenord — Lombardy regional data may be on cciss.it OR may live on dati.lombardia.it only (regional NAP rather than national); if dati.lombardia.it is referenced FROM cciss.it, compliant
  - ATAC — similar; check whether romamobilita.it is referenced from cciss.it
- **If any non-NAP source is unreferenced**: document the gap, operator decides whether to retain (with rationale) or switch.
- **Gotchas**:
  - **Italo (NTV)**: private HSR competitor, no public GTFS/NeTEx — neither on cciss.it nor anywhere else. Missing from our matrix; out-of-scope until NTV publishes.
  - GTT (Turin) + TPER (Bologna) not onboarded — per existing script comments.

### 🇱🇮 LI — Liechtenstein · ✅ No action (transitive)

- **Official NAP**: Not listed in the PDF (LI is EEA but not EU; not bound by Delegated Regulation 2017/1926)
- **Coverage**: Transitive via CH (SBB PostBus services into LI) and AT (ÖBB Vorarlberg services). Schaan-Vaduz buses appear via SBB feed.
- **Gotchas**: Vaduz has no rail.

### 🇨🇭 CH — Switzerland · ✅ Compliant (current source IS the NAP)

- **Official NAP**: www.opentransportdata.swiss — same as currently used. No action needed.
- **Currently onboarded**: `CH-NAP_netex_202606200406.zip` (NeTEx-EPIP) — SBB + all CH operators
- **Note**: CH is in the PDF despite being non-EU (Switzerland is in the EFTA NAP framework via bilateral agreement).
- **Operator coverage**: SBB CFF FFS + BLS + PostBus + every regional Verkehrsverbund (ZVV, TPG, etc.) + funicular operators
- **Refresh**: Quarterly

### 🇬🇧 GB — United Kingdom · ✅ Keep

- **Official NAP**: https://data.gov.uk/ (UK retained the NAP post-Brexit via WA + EU agreements)
- **Currently onboarded**: Eurostar feed in FR NAP + GB OSM clip (HS1 corridor only). NO native UK rail data.
- **Compliance check**: data.gov.uk catalogues ATOC / National Rail timetables in CIF format under https://datafeeds.networkrail.co.uk. NOT currently onboarded into eu19 (out of scope for transit-MOTIS unless we add a CIF→GTFS converter — separate future work).
- **Gotchas**: Eurostar termini covered; domestic UK (LNER, Avanti, GWR, ScotRail) NOT covered. Matrix `London → Edinburgh` will return `no_route`.

# Part 2 — eu19 NEW providers (8 countries, NAP-anchored)

All new providers MUST be discovered through the official MMTIS NAP per
the compliance principle. Every row marked **⚠ NAP probe needed** means
the operator should visit the NAP portal and capture the exact dataset
URL before bootstrap.

### 🇩🇰 DK — Denmark · ⚠ NAP probe needed

- **Official MMTIS NAP**: **https://nap.vd.dk/** (Vejdirektoratet — Danish Road Directorate hosts the multimodal NAP)
- **Operator probe action**:
  1. Visit https://nap.vd.dk/
  2. Locate the public-transport timetable dataset (likely under "MMTIS" or "kollektiv trafik" categories)
  3. Find the dataset for Rejseplanen / DSB / national bundle — capture the canonical download URL
  4. Note the format (likely GTFS based on Danish practice; possibly NeTEx-EPIP per EU mandate from 2025)
- **Previously suggested** (WRONG, community URL): `https://www.rejseplanen.info/labs/GTFS.zip` — this is Rejseplanen's developer-labs URL and is NOT the official NAP-published canonical source. May contain the same data; may not be referenced from nap.vd.dk.
- **If nap.vd.dk references rejseplanen.info as the canonical download**: we can use it (with the rationale documented as "NAP-referenced community mirror"). Otherwise, use whatever URL nap.vd.dk catalogues.
- **Auth**: Unknown until probed; EU NAP regulation requires public access without barriers.
- **Refresh**: Unknown until probed (Rejseplanen has historically been weekly).
- **Operator coverage** (expected, from Rejseplanen's known scope):
  - DSB (long-distance + S-tog) + DSB Øresund + Lokaltog (regional) + Arriva Tog DK + Metro København + Movia bus + Midttrafik / Sydtrafik / FynBus / NT + ferries
- **Verdict**: ⚠ Onboard pending nap.vd.dk probe

### 🇸🇪 SE — Sweden · ⚠ NAP probe needed

- **Official MMTIS NAP**: **www.trafficdata.se** (Trafikverket — Swedish Transport Administration)
- **Operator probe action**:
  1. Visit www.trafficdata.se
  2. Find the multimodal timetable catalogue
  3. Determine whether the Trafiklab "GTFS Sverige" feed is referenced as the canonical source OR whether trafficdata.se hosts/proxies its own NAP feed
- **Previously suggested** (WRONG, community URL): `https://opendata.samtrafiken.se/gtfs-sweden/sweden.zip?key={API_KEY}` — Trafiklab is operated by Samtrafiken (industry consortium), not by Trafikverket. The trafficdata.se NAP may catalogue Trafiklab's feed as the canonical source (in which case the Trafiklab key path is compliant) or may have its own.
- **Key complication**: if trafficdata.se requires the operator to register for a separate key (different from Trafiklab), we'd need a new key. Probe will tell.
- **Auth**: TBD per probe
- **Refresh**: TBD per probe (Trafiklab is daily)
- **Operator coverage** (expected, from Trafiklab's known scope):
  - SJ + MTR + Snälltåget + Tågkompaniet/Vy + Pågatågen + Öresundståg + SL + Skånetrafiken + Västtrafik + 12 more regional
- **Verdict**: ⚠ Onboard pending trafficdata.se probe

### 🇳🇴 NO — Norway · ✅ Confirmed by operator probe (2026-06-29)

- **Official MMTIS NAP**: **https://transportportal.atlas.vegvesen.no/no/** (Statens Vegvesen)
- **Confirmed canonical timetable dataset URL** (operator-verified): https://transportportal.no/datasets/c7960768-96a0-3cf0-8692-8af4afe8c423
- **Format**: NeTEx Nordic profile + GTFS both published (per Entur's standard practice)
- **Auth**: None (Entur open data)
- **Refresh**: Daily
- **Operator coverage** (expected): Vy + Go-Ahead Nordic + SJ Nord + Flytoget + Ruter + AtB + Skyss + Kolumbus + Kystverket coastal ferries + Nor-way Bussekspress
- **Important — separate stop place file**: The NO NAP ALSO publishes a dedicated **Stop Place Register (NSR — Nasjonalt Stoppestedsregister)** as a distinct dataset, referenced from https://developer.entur.org/pages-intro-files. This contains the authoritative national stop register that all NO transit operators reference. See the cross-cutting "Stop-point identification" section below for why this matters for the eu19 corridor as a whole.
- **Verdict**: ✅ Ready to onboard — operator has confirmed the canonical URLs

### 🇵🇱 PL — Poland · ⚠ NAP probe needed + operator scope decision

- **Official MMTIS NAP**: **https://dane.gov.pl/en/dataset/1739,NAP** (dane.gov.pl is Poland's main open-data portal; dataset 1739 is the NAP-specific one)
- **Operator probe action**:
  1. Visit https://dane.gov.pl/en/dataset/1739,NAP
  2. Inventory what rail / urban transit feeds are catalogued
  3. **Critical question**: is PKP Intercity catalogued? If yes, the previous "no public PKP IC feed" concern goes away — that would be a game-changer.
  4. If PKP IC absent, list which operators ARE published (likely just regional + city operators)
- **Previously suggested** (WRONG approach): I listed fragmented city URLs (Warsaw, Kraków, Gdańsk SKM) without checking whether dane.gov.pl/1739 already references them.
- **Auth**: dane.gov.pl is no-key for browsing; per-dataset auth may vary.
- **Refresh**: Per-dataset
- **Operator scope decision (post-probe)**:
  - **If PKP IC is in NAP** → full PL onboarding, removes the "Warsaw → Berlin" gap
  - **If only city/regional feeds** → operator decides: ship those with documented PKP IC gap, or skip until PKP publishes
- **Verdict**: ⚠ Probe results determine scope

### 🇨🇿 CZ — Czech Republic · ⚠ NAP probe needed

- **Official MMTIS NAP**: **http://registr.dopravniinfo.cz/en/** (Ministerstvo dopravy — Czech Ministry of Transport)
- **Operator probe action**:
  1. Visit http://registr.dopravniinfo.cz/en/
  2. Confirm CIS JŘ GTFS bundle (`https://portal.cisjr.cz/static/jdf/JDF-GTFS.zip`) is the NAP-referenced canonical source
- **Previously suggested**: CIS JŘ portal directly — this is the operator-side data publisher, not the NAP. The NAP catalogue at registr.dopravniinfo.cz almost certainly references CIS JŘ as the canonical source, in which case our previous URL is compliant once attested via the NAP.
- **Auth**: None
- **Refresh**: Weekly
- **Operator coverage** (expected from CIS):
  - ČD (national rail) + Leo Express + RegioJet + ARRIVA vlaky + GW Train Regio + ~30 regional bus operators
- **Verdict**: ⚠ Onboard pending NAP probe (likely just confirms CIS as canonical)

### 🇸🇰 SK — Slovakia · ❌ Likely skip, probe to confirm

- **Official MMTIS NAP** (per PDF): **https://aplikacie.zsr.sk/MapaVylukZsr/index.aspx** (ŽSR — Železnice Slovenskej republiky, infrastructure manager)
- **Concern**: The PDF lists this URL but "MapaVylukZsr" translates to "Map of Disruption Exclusions" — it's a disruption map, NOT a timetable catalogue. SK may have listed an inappropriate URL as their MMTIS NAP. EU enforcement on NAP compliance is patchy in Slovakia.
- **Operator probe action**:
  1. Visit https://aplikacie.zsr.sk/MapaVylukZsr/index.aspx
  2. Verify whether timetable feeds (GTFS / NeTEx) are catalogued there at all
  3. If not → SK is non-compliant with the Delegated Regulation, and onboarding becomes "skip until SK publishes a proper MMTIS NAP"
- **Workaround**: CZ CIS feed includes some cross-border services into SK (Bratislava-area RegioJet/Leo Express). Partial SK rail coverage as a side effect of onboarding CZ.
- **Verdict**: ❌ Skip likely; probe to confirm

### 🇸🇮 SI — Slovenia · ❌ Blocked until NAP URL discovered

- **Official MMTIS NAP** (per PDF): "NAP - National Traffic Management Centre" — **PDF lists no working URL**
- **Operator action**:
  1. Search the Slovenian Ministry of Infrastructure (gov.si/drzavni-organi/ministrstva/ministrstvo-za-infrastrukturo/) for the NAP URL
  2. SI has historically used promet.si but that's the live traffic portal, not the MMTIS NAP
  3. May need to email the Ministry directly for the NAP entry point — EU NAPs are sometimes published only after operator inquiry
- **Previously suggested** (WRONG): `https://nap.gov.si/` — I guessed this domain; the PDF doesn't reference it. May or may not exist.
- **Verdict**: ❌ Blocked — cannot onboard SI without a working NAP URL. Defer to Phase B follow-up once URL discovered.

### 🇭🇺 HU — Hungary · ⚠ NAP probe needed

- **Official MMTIS NAP**: **https://napportal.kozut.hu/** (Magyar Közút — Hungarian Public Road Non-profit Company)
- **Operator probe action**:
  1. Visit https://napportal.kozut.hu/
  2. Inventory rail / city / bus timetable feeds
  3. Confirm MÁV-Start + GySEV are present, capture exact URLs
- **Previously suggested** (WRONG): `https://nap.gov.hu/` and `https://kif.gov.hu/` — both wrong, the official NAP is napportal.kozut.hu.
- **Auth**: TBD per probe
- **Refresh**: TBD per probe (MÁV historically weekly)
- **Operator coverage** (expected):
  - MÁV-Start (long-distance + regional rail)
  - GySEV / Raaberbahn (HU↔AT cross-border)
  - MÁV-HÉV (Budapest suburban)
  - Volánbusz (national bus)
  - BKV (Budapest metro/tram/bus/HÉV)
- **Verdict**: ⚠ Onboard pending napportal.kozut.hu probe

# Part 2.5 — Cross-cutting: stop-point identification across 19 countries

**Raised by the operator during the NO probe (2026-06-29). This may be
the single most important architectural concern of the whole eu19
effort, not just for NO.**

## The problem

Every country's transit feed identifies stop places with its OWN
identifier scheme:

| Country | Stop ID scheme | Example (Oslo S / equivalent) |
|---|---|---|
| NO | NSR (Nasjonalt Stoppestedsregister) | `NSR:StopPlace:59872` |
| SE | RKR / Samtrafiken stops | typically `9022001020000001` or similar |
| DK | Rejseplan stop IDs | `00088002` style |
| DE | DELFI Globale Haltestellen (de:08111…) | `de:09162:6:1:1` |
| CH | DiDok / SBB stop IDs | `8503000` (also valid UIC) |
| FR | StopArea:OCE… (SNCF) or numeric UIC | `StopArea:OCE87739000` |
| BE | iRail / NMBS stop_id | `008811007` (numeric UIC) |
| NL | KV15 / CHB stops | `stoparea:560000…` |
| AT | DIVA stop numbers + UIC | `at:43:300` |
| IT | RFI / Trenitalia stop IDs | `S07207` style |
| GB | NaPTAN ATCO + CRS | `9100KNGX` (CRS) |
| HU | MÁV stop IDs (TBD) | unknown |
| CZ | CIS station numbers | unknown |
| PL | PLK stop IDs / city-system specific | varies wildly |
| LU | CFL stop_id | unknown |
| SI | SŽ stop_id | unknown |
| SK | ZSSK stop_id | unknown |

**The cross-border lingua franca is the UIC 7-digit station code**
(prefix 70-99 by country: 76 NO, 74 SE, 86 DK, 80 DE, 87 FR, 88 BE,
84 NL, 81 AT, 83 IT, 70 GB, 85 CH, 55 HU, 54 CZ, 51 PL, 82 LU, 79 SI,
56 SK). But UIC codes are present **only for stations that operate
internationally** — typically major rail termini, not every regional
halt. Bus stops effectively never have UIC codes.

## Why this matters for the matrix

When the operator queries `Oslo → Stockholm`, MOTIS needs:

1. Find Oslo S in the NO feed (NSR:StopPlace:59872, lat/lon ~59.91,10.75)
2. Find Oslo S in the SE feed too (probably a different ID, same coords)
3. Decide whether these are "the same stop" so the SJ overnight train
   that departs Oslo and arrives in Stockholm appears as one
   continuous journey

**MOTIS's default behaviour**: cluster stops by coordinate proximity
(~10m default radius) and name similarity. Works well for major
stations (coords differ by <5m between feeds, names roughly match).
Fails for:

- **Border-adjacent stations** where each operator's feed places the
  stop centroid in a slightly different location (>10m apart)
- **Multilingual names** — e.g. Brussel-Zuid vs Bruxelles-Midi vs
  Brussels-South, or Genève vs Geneva vs Ginevra
- **Bus / tram interchanges** where the coordinate is approximate but
  the platform-level interchange is the real point

## What the dedicated stop-place registers give us

Each country with a national stop-place register (NSR equivalent)
publishes:

- A canonical list of stops with stable identifiers
- Hierarchy: StopPlace (the station) → Quay (the platform/track)
- Multi-modal codes: rail UIC, IATA where applicable, local bus codes
- Stable coordinates (the operator's surveyed position, not whatever
  individual operators happen to plot)

For NO: NSR (Entur)
For DK: published alongside Rejseplan
For DE: DELFI Globale Haltestellen
For FR: BANO / IRVE / stop_area registry
For CH: DiDok
For NL: CHB (Centraal Haltebestand)

**Most NAPs publish their stop register as a separate file from the
timetable feed.** Currently in eu11 we ingest the timetables but NOT
the registers, relying on coordinate clustering.

## Recommendation for eu19

**Three options, ranked by ambition:**

**A. Status quo — coordinate clustering only (default MOTIS behaviour)**
Don't ingest stop registers; rely on coordinate proximity + name
matching. Works for ~95% of major stations. Risks: occasional missed
cross-border connections at border-adjacent or multi-platform sites.
Zero extra work for eu19 onboarding.

**B. Ingest national stop registers per country, no cross-country mapping**
For each country where a register is available, ingest it as an
authoritative stop reference. Internal-to-country queries become
more reliable; cross-country still relies on coordinate clustering.
~½ day per country (varies by format complexity).

**C. Build a cross-country UIC stop-mapping table**
Author a master `stop_equivalences.json` (or similar) that explicitly
maps NSR:StopPlace:59872 ↔ SE:Stop:1234 ↔ UIC:7600103 etc. for every
international station in our hub set (~50 stops). MOTIS gets perfect
cross-border interop on those stops. Manual effort: ~1 day to populate
the table from UIC code lookups + ad-hoc verification.

**My recommendation**: **A for the initial eu19 build, then C for the
~50 hubs we actually use in the coverage matrix.** B is interesting in
theory but the per-country effort doesn't pay for itself unless we're
running per-country coverage too, which the country-filter (PR #168)
makes less pressing.

The eu19 bootstrap should include the NO NSR file as a separate
ingest (since we now know it's published as a distinct dataset on the
NAP). For other countries, defer until we see actual cross-border
matching failures in the coverage matrix.

## Operator action items added for stop-point handling

- During each NAP probe, ALSO inventory whether the country publishes
  a dedicated stop-place register file (separate from the timetable).
  Record its URL alongside the timetable URL.
- Once eu19 is running, audit the coverage matrix for cross-border
  `no_route` cells that should clearly have routes (e.g. Oslo →
  Stockholm at 22:00 — there's a known SJ Nattåg). If we get
  `no_route` there, it's likely a stop-matching failure and we should
  pursue Option C for those stops specifically.

# Part 3 — Action items & decisions before PR #2

**Operator action items (block the OSM merge script + bootstrap PRs):**

**Critical NAP probes (must be done before bootstrap)** — for each, capture BOTH the timetable URL AND any separate stop-place register file:
1. **DK**: Visit https://nap.vd.dk/ → capture (a) rail timetable URL, (b) DK stop-place register URL if separate
2. **SE**: Visit www.trafficdata.se → determine whether to use Trafiklab feed (with NAP attestation) or a separate NAP-hosted one; if Trafiklab, register for key. Inventory whether SE stop register is separate
3. **NO**: ✅ Operator confirmed (2026-06-29):
   - Timetable: https://transportportal.no/datasets/c7960768-96a0-3cf0-8692-8af4afe8c423
   - Stop Place Register (NSR) is a separate file referenced via developer.entur.org
4. **PL**: Visit https://dane.gov.pl/en/dataset/1739,NAP → critical: is PKP IC present? Determines scope. Inventory PL stop register
5. **CZ**: Visit http://registr.dopravniinfo.cz/en/ → confirm CIS JŘ GTFS catalogued + CZ stop register
6. **SK**: Visit https://aplikacie.zsr.sk/MapaVylukZsr/index.aspx → confirm or refute that timetable feeds are catalogued
7. **SI**: Discover SI's working NAP URL (Ministry of Infrastructure inquiry if needed)
8. **HU**: Visit https://napportal.kozut.hu/ → capture MÁV-Start + GySEV URLs + HU stop register

**Compliance verification for existing eu11 feeds (lower priority but should be done in same sweep)**:
1. **NL**: Visit https://ntm.ndw.nu → confirm OpenOV is NAP-referenced (highest risk of compliance gap)
2. **IT**: Visit https://www.cciss.it/ → confirm Trenitalia + Trenord + ATAC referenced
3. ES/BE/LU/DE/AT: lower-risk confirmations — current sources are likely NAP-published

**Once probes return**:
- PR #2: `scripts/merge_osm_eu19_corridor.sh` — extends the eurostar corridor PBF merger
- PR #3: `scripts/create_eu19_transit_motis_session.ps1` — bootstraps the new MOTIS session with **only NAP-attested providers**
- PR #4: Alembic migration adding new hubs

## Out of scope (Phase C, not in eu19)

For reference, the Balkans + GR NAPs per the same PDF:

- 🇭🇷 **HR** Croatia — https://www.promet-info.hr/ (live traffic-focused; rail timetable coverage TBD)
- 🇷🇸 **RS** Serbia — NOT in the PDF (Serbia is EU candidate, not member; no NAP obligation yet)
- 🇷🇴 **RO** Romania — pna.cestrin.ro/ (NAP exists but rail coverage TBD)
- 🇧🇬 **BG** Bulgaria — https://www.mtitc.government.bg/en/category/294/national-access-points-transport-related-data
- 🇬🇷 **GR** Greece — www.nap.gov.gr

If Phase C is ever revisited, each of these should follow the same
NAP-first principle — probe the official portal, capture the canonical
dataset URLs.

## Open questions / things to revisit

- **PKP Intercity** in PL NAP: critical question for the matrix's PL coverage. The probe should answer.
- **CIF→GTFS converter** for GB national rail: integrating ATOC/Network Rail timetables — half-day effort to onboard once we want it.
- **Italo Italy**: still no public feed via cciss.it or anywhere. Watch the data4pt-project rollout.
- **SI NAP**: needs proactive operator outreach to the Slovenian Ministry of Infrastructure.
- **Quarterly NAP sweep**: PL, SK, SI feeds may improve over time. Re-probe each NAP quarterly to catch new datasets.
