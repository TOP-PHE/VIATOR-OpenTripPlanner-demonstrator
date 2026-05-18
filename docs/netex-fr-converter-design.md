# NeTEx-FR → GTFS converter — design proposal

**Status:** design proposal (no code yet). Filed under the `1.D` thread
from the multi-NAP roadmap. Sibling to
[`nap-fr-rail.md`](./nap-fr-rail.md) §2.4 / §10 (where the gap is
acknowledged but not yet filled).

> **Position up front.** VIATOR's France session already routes
> France-wide using **GTFS feeds where they exist** — SNCF Trains,
> Île-de-France Mobilités, Trenitalia France, Eurostar all publish
> GTFS to transport.data.gouv.fr. This converter is for the **gap**:
> French operators (mostly regional) who publish *only* NeTEx-FR.
> Today those operators show up as "warnings" in the FR session
> import; this doc proposes the path to making them first-class.

---

## 1. Motivation

### 1.1 What NeTEx-FR is

**NeTEx** (CEN EN 12896) is the European standard for exchanging
public-transport timetable data. **NeTEx-FR** is the French national
profile, published by AFIMB / CEREMA. Three sub-profiles cover the
data types:

| Sub-profile | Content | Comparable GTFS counterpart |
|---|---|---|
| `NeTEx-FR-Arrets` | Stop places, quays, parents, accessibility | `stops.txt` + `pathways.txt` |
| `NeTEx-FR-Horaires` | Service journeys, calendars, trips, stop times | `trips.txt` + `stop_times.txt` + `calendar*.txt` |
| `NeTEx-FR-Reseaux` | Lines, routes, networks, operators | `routes.txt` + `agency.txt` |

`transport.data.gouv.fr` (the French NAP) lists which operators publish
GTFS, which publish NeTEx-FR, and which publish both. As of late 2026,
a meaningful tail of regional operators is **NeTEx-FR-only** — meaning
VIATOR can't route through them today.

### 1.2 Why OTP can't read it directly

OpenTripPlanner reads three native formats: GTFS, NeTEx-EPIP (the
European Passenger Information Profile), and NeTEx-Nordic. **It does
not read NeTEx-FR.** The reason is the French profile makes
incompatible choices on:

- `ServiceJourney` interpolation (FR uses dwell times in seconds, EPIP
  expects ISO 8601 durations)
- Stop-hierarchy nesting (FR's `monomodalStopPlace` containing
  `quay` doesn't match EPIP's `StopPlace > Quay` shape exactly)
- Operator references (FR uses `OperatorRef` on `Line`; EPIP expects
  it on `Network`)
- Calendar encoding (`DayType` vs `OperatingDay` vs `DayTypeAssignment`
  — the FR profile is more verbose)

OTP's NeTEx-EPIP loader is strict about these and rejects NeTEx-FR
files with parse errors.

### 1.3 Today's workaround in VIATOR

Per [`nap-fr-rail.md`](./nap-fr-rail.md) §2.4:

> If you have NeTEx-FR archives for compliance reasons, use the
> **Upload a file** form … Files land in `inbox/<sid>/archive/` and
> never touch OTP.

This is correct but **doesn't route**. The data is preserved for audit
but the trains it describes are invisible to the journey planner. For
the demonstrator's "fanout across all French operators" promise to
hold for regional NeTEx-FR-only operators, we need to convert.

---

## 2. The gap concretely

A small sample of FR operators by publishing format (counts as of late
2026, may have shifted; check `transport.data.gouv.fr` for the live
inventory):

| Operator class | Example | Format | Today's VIATOR routing |
|---|---|---|---|
| National rail (long-distance) | SNCF Voyageurs (TGV INOUI, Intercités) | GTFS via OpenData | ✅ works |
| National rail (regional) | TER (24 régions) | GTFS via SNCF | ✅ works |
| Cross-border | Eurostar, Trenitalia France, RENFE-SNCF | GTFS | ✅ works |
| Urban Île-de-France | IDFM (RATP + ~40 operators) | GTFS via IDFM | ✅ works |
| Urban regional | Tisséo (Toulouse), TCL (Lyon) | Mostly GTFS | ✅ works |
| Regional bus / interurban | Some départemental networks | NeTEx-FR only | ❌ archive-only |
| Inter-régions express | A few historical operators | NeTEx-FR | ❌ archive-only |

For the rail-focused demonstrator, the gap is **small but non-zero**.
For a full multi-modal France session, the gap would be bigger.

---

## 3. Converter tooling landscape

I surveyed open-source NeTEx-FR → GTFS converters. None is a clear
"obvious choice"; the trade-off is between maturity and modernity.

### 3.1 `chouette` / `chouette-iev` (CEREMA / AFIMB)

- **Origin**: built by the French mobility agency (formerly AFIMB,
  now CEREMA) specifically to handle NeTEx-FR. The de facto standard
  tool in the French mobility ecosystem.
- **Language**: Java (Spring Boot)
- **License**: CeCILL 2.0 (LGPL-compatible)
- **Mode**: batch (CLI + REST API)
- **Maturity**: mature, used in production by enterproject-grade FR
  mobility platforms
- **Container**: an unofficial Docker image exists; we'd publish our
  own pinned variant
- **Pros**: faithful conversion (it's THE reference tool); handles
  all three sub-profiles; battle-tested
- **Cons**: heavyweight (full JVM + Spring stack); Java footprint
  adds ~400 MB to the converter container; slow build/run cycle
  compared to Python tooling

### 3.2 `transitfeed-converter` (community, navitia ecosystem)

- **Origin**: spin-off from the navitia2 project (the canonical FR
  open-source journey planner that natively eats NeTEx-FR; this
  converter extracts navitia's NeTEx-FR parser into a CLI)
- **Language**: Python
- **License**: AGPL-3
- **Maturity**: moderate; less proven than chouette
- **Container**: lightweight (Python + libxml2)
- **Pros**: small footprint; matches our Python stack; readable code
- **Cons**: AGPL is *contagious* — embedding it in VIATOR's
  Docker image would force VIATOR to be AGPL. Running it as a
  separate sidecar service in Docker Compose preserves licence
  isolation but adds complexity.

### 3.3 Mass converters (commercial)

- **Mecatran GREENWICH**, **Transamo**, **Trapeze** — all proprietary,
  out of scope for an open demonstrator.

### 3.4 Recommendation

**Lead with `chouette-iev` for the implementation phase.** The
licence is friendlier than transitfeed-converter's AGPL, the
conversion is the most faithful, and the JVM weight is acceptable
because the converter runs **once per refresh cycle, not per
request** — it's not on any latency path.

Fallback if chouette proves too heavyweight: revisit transitfeed-
converter with sidecar isolation, OR pay the engineering cost of
writing a minimal Python converter for the specific NeTEx-FR
elements we encounter (probably 1–2 weeks of focused work, plus
ongoing maintenance as the profile evolves).

---

## 4. Where the converter plugs into VIATOR

### 4.1 Today's bootstrap flow

```
session.config.sources.providers[]
            │
            ▼
   ingestion.dispatch(provider)
            │
            ├── timetable.format == "gtfs"
            │       ↓
            │   download to inbox/<sid>/gtfs/<provider_id>.zip
            │       ↓
            │   OTP picks up at graph build time
            │
            ├── timetable.format == "netex_nordic"
            │       ↓
            │   download to inbox/<sid>/netex/<provider_id>.zip
            │       ↓
            │   OTP picks up
            │
            ├── timetable.format == "netex_epip"
            │       ↓
            │   download to inbox/<sid>/netex/<provider_id>.zip
            │       ↓
            │   OTP picks up
            │
            └── NeTEx-FR → currently NOT a valid format
                    ↓
                archive-only (inbox/<sid>/archive/)
                    ↓
                never reaches OTP
```

### 4.2 Proposed flow with the converter

```
session.config.sources.providers[]
            │
            ▼
   ingestion.dispatch(provider)
            │
            ├── timetable.format == "netex_fr"        ← new
            │       ↓
            │   download to inbox/<sid>/netex_fr/<provider_id>.zip
            │       ↓
            │   converter sidecar: chouette-iev
            │       NeTEx-FR ZIP → GTFS ZIP
            │       ↓
            │   write to inbox/<sid>/gtfs/<provider_id>__converted.zip
            │       ↓
            │   OTP picks up at graph build time
            │
            └── … (other formats unchanged) …
```

Key properties of this design:

1. **Conversion runs at ingestion time, NOT at request time.** No
   latency hit on the journey API.
2. **Cached output is bit-stable** — same NeTEx-FR ZIP in → same GTFS
   ZIP out. Idempotent.
3. **Converted artefact has a distinctive name suffix
   (`__converted`)** so operators can tell at a glance which provider
   went through conversion vs which was natively GTFS.
4. **OTP doesn't know the file came from NeTEx-FR.** Once the converter
   has run, the OTP build is identical to a native-GTFS provider.

### 4.3 The converter as a Docker-Compose sidecar

Run `chouette-iev` as a separate container in `docker-compose.yml`,
exposed only on the internal Docker network:

```yaml
converter-netex-fr:
  image: ghcr.io/top-phe/chouette-iev:<pinned-sha>
  restart: unless-stopped
  volumes:
    - ./inbox:/inbox:rw   # same shared inbox volume the web container uses
  networks: [viator-internal]
  # Trivy-scanned in CI like every other image
```

The Python `ingestion` module on `web` invokes the converter via a
small HTTP call (chouette-iev exposes a REST API). Two minor wrinkles:

- **Failure mode**: if the converter sidecar is down at ingest time,
  the NeTEx-FR provider's refresh fails with a clean error. The
  graph rebuild does not start; existing routing is unaffected.
- **Versioning**: pin `chouette-iev`'s image SHA in CI like every
  other third-party action (per audit-2026-05 P1 #7). Bumps go
  through Dependabot.

### 4.4 Schema change (`session.config.sources.providers[]`)

Add `netex_fr` to the allowed `timetable.format` enum and document it
in [`nap-fr-rail.md`](./nap-fr-rail.md) §2.4. The UI's format
dropdown gains the new option; existing sessions are unaffected
because the enum widens (never narrows).

---

## 5. Open questions

| # | Question | Where it's resolved |
|---|---|---|
| 1 | Is chouette-iev still actively maintained? Last commit, last release | Phase-0 spike — check the upstream repo and recent activity |
| 2 | What's the actual conversion-time budget for a typical regional NeTEx-FR? | Phase-0 spike — convert one real operator and time it |
| 3 | Does the converter preserve `route_short_name` / `route_long_name` faithfully enough for our trip-signature canonicaliser (spec §6.4) to identify trains across feeds? | Phase-0 spike — compare converted output to a known-good direct-GTFS feed for the same operator |
| 4 | Are there NeTEx-FR profile versions we need to gate on (the spec evolves)? | Survey transport.data.gouv.fr's published files for profile version stamps |
| 5 | Should the converter validate the input before conversion (catch malformed feeds early)? | Out of scope for v1 — let chouette-iev surface its own errors |
| 6 | Does the converter produce any metadata we want to surface in the operator UI (e.g. "this provider was NeTEx-FR; converted at T")? | Yes — add a small `converted_from: netex_fr, converted_at: T` JSON sidecar in the inbox and have the operator UI render it |

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `chouette-iev` upstream stops being maintained | Medium | High — we'd be stuck on a frozen converter | Fork to TOP-PHE org; investment limited because we only need NeTEx-FR-Horaires + NeTEx-FR-Arrets, not all of chouette's features |
| Converter output diverges subtly from native GTFS (stop_ids, route_ids, calendar dates) | Medium | Medium — affects trip-signature comparison across federated NAPs | Run native-GTFS-feed-as-truth ↔ converted-NeTEx-feed-as-test parity checks for the operators that publish both |
| JVM container weight (~400 MB) | Low (cosmetic) | Low | Acceptable; runs once per refresh; could swap for a Python converter later if it becomes problematic |
| AGPL contagion if we end up using `transitfeed-converter` instead | Low (we picked chouette) | High if it happened | Documented in §3.4; not pursuing AGPL path |
| NeTEx-FR profile evolution breaks the converter | Low | Medium | Pin chouette version + monitor upstream; treat as Dependabot bump cycle |

---

## 7. Phasing

| Phase | Scope | Effort | Trigger |
|---|---|---|---|
| **0 — spike** | Run chouette-iev once against a real NeTEx-FR file from transport.data.gouv.fr (pick a small regional operator). Verify converted GTFS loads cleanly into OTP. Time the conversion. Decide go/no-go on the tool. | ~½ day | Operator commits to closing the gap |
| **1 — minimal integration** | Add `netex_fr` to `timetable.format` enum; wire chouette-iev sidecar; add converter step in ingestion.dispatch; UI dropdown gains the option. End-to-end: configure one NeTEx-FR-only operator in `nap-fr-rail`, refresh, journey through their network works. | ~3 days | Phase 0 successful |
| **2 — operator UX polish** | "This provider was NeTEx-FR" indicator on the session detail; per-provider conversion log; better error surfacing when conversion fails. | ~1 day | Phase 1 stable |
| **3 — parity testing** | Compare converted-NeTEx-FR output against direct-GTFS-publish for operators publishing both (SNCF Voyageurs does!). Document any divergence. | ~1 day | Phase 1 stable |

---

## 8. What this design is *not*

- **Not** a path to making NeTEx-FR a first-class OTP-native format.
  That would require upstream contributions to OpenTripPlanner's NeTEx
  parser, which is months of work and out of scope for VIATOR.
- **Not** a replacement for direct-GTFS publishing where it exists.
  Operators publishing GTFS keep doing so; the converter only kicks in
  for NeTEx-FR-only operators.
- **Not** a runtime translation. Conversion happens at ingestion;
  the journey API never invokes the converter.
- **Not** a NeTEx-FR validator. The converter consumes whatever
  transport.data.gouv.fr serves; if the file is malformed, chouette
  surfaces an error and that provider's refresh fails clean.

---

## 9. Recommendation

**Pursue Phase 0 (the spike) as the next step** when an operator
identifies a specific NeTEx-FR-only provider they need routed. Until
then, this design sits ready. The gap is small enough today that
deferring is the right call; the day a customer says "we need routing
through Région X's bus network and X only publishes NeTEx-FR" we'll be
ready to move fast.

---

## Changelog

- **2026-05-18** — Initial draft. Surveys the converter landscape,
  proposes chouette-iev as the lead candidate, sketches the
  ingestion-time conversion flow as a sidecar, and lists Phase-0
  spike questions to validate before committing to implementation.
