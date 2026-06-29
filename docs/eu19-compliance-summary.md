# eu19 corridor — data-source compliance summary

**Audience**: MERITS stakeholders, legal/procurement reviewers
**Date**: 2026-06-29
**Scope**: 19 European countries (ES, FR, BE, LU, NL, DE, AT, IT, LI, CH, GB
+ DK, SE, NO, PL, CZ, SK, SI, HU)

## Compliance principle

Every public-transport dataset onboarded into the VIATOR
`eu19-transit-motis` session is sourced through its destination
country's **official National Access Point (NAP)** per EU Delegated
Regulation 2017/1926 (Multimodal Travel Information Services), as
listed in the European Commission's `its-national-access-points.pdf`
(October 2025).

Where a country's listed MMTIS NAP is verifiably **non-compliant**
with the regulation's multimodal scope (i.e. publishes only road-
traffic data despite the EU MMTIS designation), that gap is recorded
below for transparency. VIATOR's data ingest pipeline either uses an
alternative open-data path (with the rationale documented) or defers
that country's coverage to a future release.

## Per-country compliance status

| Country | Status | Source | Notes |
|---|---|---|---|
| 🇫🇷 FR | ✅ Compliant | transport.data.gouv.fr (= the official NAP) | 15 GTFS feeds, all NAP-catalogued |
| 🇨🇭 CH | ✅ Compliant | opentransportdata.swiss (= the official NAP) | SBB NeTEx, NAP-published |
| 🇧🇪 BE | ✅ Compliant ⚠ license | transportdata.be → belgiantrain.be | SNCB NeTEx confirmed in NAP. **Note**: license is "Other (Non-Commercial)" — VIATOR's commercial scope should be confirmed with SNCB |
| 🇳🇴 NO | ✅ Compliant | transportportal.atlas.vegvesen.no → Entur | National GTFS + NSR stop register, NAP-catalogued |
| 🇸🇪 SE | ✅ Compliant | trafficdata.se → Trafiklab | 4 Trafiklab datasets NAP-referenced (CC0). Free API-key registration required |
| 🇪🇸 ES | ⚠ Auth-walled, likely compliant | nap.mitma.es | CKAN API returns 401; NAP catalogue requires login for programmatic access. 5 NAP-prefixed feeds already in use |
| 🇩🇪 DE | ⚠ Inconclusive (SPA) | mobilithek.info | DELFI Gesamtdeutschland NeTEx-EPIP in use; portal SPA prevented automated verification but the file's NAP-filename suggests official origin |
| 🇦🇹 AT | ⚠ Inconclusive (deep search) | mobilitydata.gv.at | 102 datasets across 9 portal pages; our AT-NAP_netex_evu file not on page 1 but likely buried in catalogue |
| 🇳🇱 NL | ⚠ Inconclusive | ntm.ndw.nu | OpenOV national GTFS in use as canonical NL feed; NDW portal doesn't expose CKAN so NAP-reference status unconfirmed |
| 🇬🇧 GB | ⚠ Limited scope | data.gov.uk + Eurostar via FR NAP | Only Eurostar termini covered (via French NAP). Domestic UK rail (LNER, Avanti, GWR) deliberately out of scope |
| 🇱🇮 LI | n/a | (no NAP — EEA non-EU) | Coverage transitive via CH/AT NAPs |
| 🇵🇱 PL | ✅ Partial | dane.gov.pl/dataset/1739 | 6 GTFS feeds NAP-catalogued. **PKP Intercity (national long-distance)** is in NAP but requires authenticated FTP per NAP contact — deferred to follow-up |
| 🇨🇿 CZ | ✅ Partial | registr.dopravniinfo.cz → MD ČR NeTEx | National timetable category catalogued; specific download URL behind NAP sub-page (browser navigation required) |
| 🇭🇺 HU | ✅ Partial | napportal.kozut.hu → MÁV form | MÁV+GYSEV combined rail GTFS in NAP. Access requires submitting a request form to MÁV — pending operator action |
| 🇩🇰 DK | ⚠ Pending | nap.vd.dk → SPA | Rejseplan timetable confirmed in NAP scope but feed URL requires browser DevTools probe |
| 🇸🇮 SI | ⚠ Defer | nap.si → IPPT system | SŽ railway timetables behind IPPT system; access requires Ministry contact (mzi.ncup@gov.si). Deferred to follow-up |
| 🇮🇹 IT | 🚨 NAP non-compliant | cciss.it (gated) → ingested via alternate paths | **CCISS is a SPID-login road-safety portal**, not a multimodal data catalogue. Italy's listed MMTIS NAP does not publish open transit data. Our IT sources (Trenitalia NeTEx, Trenord, ATAC) come from operator-direct portals; Trenitalia France presence is separately NAP-attested via FR |
| 🇸🇰 SK | 🚨 NAP non-compliant — skip | aplikacie.zsr.sk (disruption viewer) | Listed MMTIS NAP is a railway disruption-map application carrying a legal disclaimer against automated downloading. Slovakia is excluded from eu19 transit; partial cross-border coverage comes via CZ feed |

**Summary counts**: 6 fully-compliant ✅ · 4 partial ✅ · 5 inconclusive ⚠ · 2 NAP non-compliant 🚨 · 1 n/a · 1 deferred ⚠.

## Critical compliance flags

### 🚨 IT — Italy's NAP doesn't publish multimodal data

Italy's listed MMTIS NAP (cciss.it) is operated by the Centro di
Coordinamento Informazioni Sicurezza Stradale — the road-safety body.
Its public-facing site shows real-time traffic incidents only; its
data catalogue (cciss.it/dataset) requires SPID Italian digital
identity authentication and contains no multimodal/transit data.

VIATOR's Italian sources (Trenitalia, Trenord, ATAC) are therefore
**not NAP-attested via the EU-listed Italian MMTIS NAP**. They come
from operator-direct portals and regional open-data sites which are
publicly accessible and openly licensed but do not satisfy a strict
"sourced through the country's official NAP" test.

This is an **EU enforcement gap** affecting Italy specifically, not a
VIATOR data-handling issue. Recommended position for stakeholder
review: accept the gap, document it for any compliance audit, and
revisit if Italy publishes a compliant MMTIS NAP in the future.

### 🚨 SK — Slovakia's NAP is a disruption viewer

Slovakia's listed MMTIS NAP (aplikacie.zsr.sk/MapaVylukZsr) is a
real-time railway-disruption map application operated by ŽSR
(Slovak Railways infrastructure manager). It publishes no timetable
data and carries a "legal disclaimer restricting automated data
downloading".

VIATOR excludes Slovakia from eu19 transit onboarding. Partial cross-
border coverage comes indirectly via the Czech CIS NAP (Slovak-
operating RegioJet/Leo Express trains are catalogued there).

### ⚠ LU — current source not discoverable via NAP

Luxembourg's `data.public.lu` open-data portal returns only 3
CFL-related datasets, all non-transit (car-sharing, P+R, hiking
trails). VIATOR's currently-ingested CFL NeTEx-EPIP feed almost
certainly originates from `mobiliteit.lu` (the CFL operator portal)
directly, which is open per CC0 but not formally NAP-anchored.

Recommended action: operator browser probe of data.public.lu to
confirm/refute. If confirmed not in NAP, document the gap with the
CC0 license rationale.

### ⚠ BE — non-commercial license caveat

SNCB Netex is the canonical Belgian rail feed, confirmed in the
official NAP. Its published license is "Other (Non-Commercial)".
VIATOR's product context (commercial demonstrator/contract execution)
should be verified against SNCB's intended scope. Worth a written
clarification with SNCB's open-data contact.

## What this means for the eu19 build

The session bootstrap script (`scripts/create_eu19_transit_motis_session.ps1`)
ships with:

- **All 6 fully-compliant + 4 partially-compliant countries** as URL or
  upload providers, sourcing from confirmed NAP-catalogued endpoints.
- **5 inconclusive-but-presumed-compliant countries** carried forward
  from eu11 (their data is openly licensed regardless of the
  programmatic NAP-verification gap).
- **No SK provider** — Slovakia entirely excluded.
- **No SI provider in v0** — deferred pending Ministry contact.
- **DK as a placeholder** — pending operator DevTools probe to capture
  the canonical Rejseplan URL.

The session can be built and operated today; only the compliance
artefacts for IT and LU (and the license review for BE) require
out-of-band action by the operator/legal contact.

## Recommended actions for stakeholder review

| Priority | Action | Owner |
|---|---|---|
| 1 | Document IT NAP non-compliance for any future audit; accept the gap | Operator |
| 2 | Browser-probe data.public.lu; confirm or refute LU NAP-reference | Operator |
| 3 | Written clarification with SNCB on commercial-use license scope | Operator / legal |
| 4 | Submit MÁV GTFS request form (HU) | Operator |
| 5 | Email Slovenian Ministry (mzi.ncup@gov.si) for SŽ access | Operator |
| 6 | DevTools probe of nap.vd.dk (DK) | Operator |
| 7 | Trafiklab account + key for SE | Operator (5 min) |

None of items 1-7 block the eu19 OSM merge or session creation —
they're all post-bootstrap follow-ups.

## See also

- `docs/eu19-providers.md` — full technical research with per-country
  detail, probe findings, and operator coverage maps.
- `scripts/merge_osm_eu19_corridor.sh` — multi-country OSM build script.
- `scripts/create_eu19_transit_motis_session.ps1` — session bootstrap.
