![TrackOnPath](brand/trackonpath-logo-sm.png)

# VIATOR — User guide

**Audience:** operators and analysts using VIATOR, plus anyone evaluating what it is for.
No development experience assumed.
**Status:** as-built, 2026-08-28.

---

## 1. What VIATOR is

VIATOR is an **instrument for measuring European rail open data**, built in the shape of a
journey planner.

That distinction matters. It looks like a trip planner — you type an origin, a destination, a
time, and get itineraries — but the itineraries are not the product. The **product is the
answer to a question**:

> *When a European country publishes its rail timetables as open data on its National Access
> Point (NAP), is that data good enough to reproduce the journeys travellers actually get from
> the national planner?*

VIATOR answers that by doing both halves itself:

1. It **ingests** rail timetable data from official National Access Points across 19 countries.
2. It **routes** on that data using an open-source engine (MOTIS; historically also
   OpenTripPlanner).
3. It **compares** its own results against incumbent production planners — ÖBB's planner and
   Switzerland's official OJP service — and scores the agreement.

Where the two agree, the open data is sufficient. Where they diverge, VIATOR has found a gap in
the published data — which is the finding worth having.

### What VIATOR is not

- **Not a consumer travel app.** No tickets, no prices, no real-time disruptions.
- **Not a replacement for a national planner.** It is a measuring device pointed at one.
- **Not multimodal.** It is deliberately rail-focused: local bus, tram and metro networks are
  filtered out so they cannot mask or inflate rail coverage.

---

## 2. Who uses it, and what they can do

| Role | Can do |
|---|---|
| `end_user` | Search journeys, view results |
| `content_manager` | The above, plus manage data content |
| `platform_admin` | Everything: sessions, coverage runs, platform config, users, credentials |

Most of the analytical surface (coverage runs, session management, configuration) is
`platform_admin`-only, because it consumes significant compute and changes what everyone else
sees.

---

## 3. Orientation — the pages

| Page | Path | What it is for |
|---|---|---|
| **Journey search** | `/journey` | Plan one journey; compare engines and reference planners side by side |
| **Network coverage** | `/admin/network-coverage` | Systematically test many station pairs at once; the main analytical surface |
| Sessions | `/admin/sessions` | Onboard data, build routing graphs, control which are live |
| Master stations | `/admin/master/stations` | Station identity: UIC codes, aliases, cross-references |
| NAP catalogues | `/admin/nap-catalogues` | Saved National Access Point endpoints (and their credentials) |
| Reports | `/admin/reports` | Search history and analytics |
| Configuration | `/admin/config` | Platform behaviour, changed without redeploying |
| Users | `/admin/users` | Accounts and roles |
| Credentials | `/credentials` | API keys for authenticated NAPs |

The two that matter for the mission are **Journey search** and **Network coverage**. The rest
exist to feed them.

---

## 4. Task: compare one journey against reference planners

**Purpose:** investigate a specific route — typically one you suspect is missing or wrong.

1. Go to **`/journey`**.
2. Enter **From** and **To**. The fields autocomplete against VIATOR's master station list.
3. Set **Depart** (date and time).
4. Choose an **Engine**: *All engines*, *OTP only*, or *MOTIS only*. Selecting one engine
   isolates it; selecting all runs them in parallel and merges results.
5. Tick the reference planners you want to compare against:
   - **Compare with Swiss OJP reference** — official Swiss service. Swiss content only.
   - **Compare with ÖBB HAFAS** — covers DACH plus major cross-border services
     (Eurostar/TGV/AVE/Iberian, Nordic cross-border).
6. Tick **Side-by-side comparison** to put VIATOR and the reference in parallel columns rather
   than stacked panels.
7. **Search.**

### Reading the result

- Each itinerary is a **card**; click it to expand the individual legs (train, walk, transfer).
- The **flag** on each card (`ALL`, `SUBSET`, `<session>_ONLY`) shows which data sources found
  that journey — useful when several countries' feeds are loaded at once.
- With side-by-side on, VIATOR and ÖBB itineraries that depart at the same instant are **aligned
  on the same row**. Where the two sources label the same train differently (VIATOR reads its
  GTFS feed, ÖBB reports its own product name), an amber note says so rather than letting you
  misread it as two different trains.
- A **"possible duplicate"** badge marks a second itinerary with identical departure *and*
  arrival — usually one physical train split into two portions (e.g. SNCF `601` and `601A`),
  which the data reports as two services.
- The **`{}`** button on a card shows the raw engine response, for debugging.

---

## 5. Task: run a network coverage assessment

**Purpose:** the core measurement. Instead of one journey, test *every pair* of major stations
across a set of countries, across a whole day, and see where coverage exists.

1. Go to **`/admin/network-coverage`**.
2. **Mode** — *Single session* (one data set) or *All fanout-enabled sessions* (cross-session
   matrix).
3. **Session** — which data set to test.
4. **Departure** — the date and time to test. *This is the date the run will search.*
5. **Direction** — *Both* tests A→B and B→A; *Single* halves the work.
6. **Countries** — tick the countries whose hub stations to include. Leaving all unticked means
   the full matrix, which is large; pick two neighbours for a fast cross-border probe.
7. **Verify externally on completion** — when ticked, VIATOR asks ÖBB about the cells after the
   run and scores agreement. This adds time (roughly 1 request per second).
8. **Advanced** *(optional)* — day window, timezone, reference date. Defaults to a full day
   (00:00–24:00) on the departure date.
9. **Run coverage.**

The run executes in the background; the page shows progress. External verification happens
**at the end**, after every pair has been routed — so a verify-enabled run stays in `running`
state for a while with the matrix already visible.

### Reading the matrix

Rows are origins, columns are destinations. Each cell is one station pair.

| Cell | Meaning |
|---|---|
| green | an itinerary was found |
| amber | no route found |
| red | timeout or error |
| grey | not tested |

Click any cell for the detail modal: the itineraries VIATOR found, the reference planner's
itineraries beside them, and the agreement score.

In the modal, **"Show walk legs"** toggles the access/egress walking segments. With it off, the
card headers recompute to show the *train's* own departure, arrival and duration — which is
usually what you want when comparing against another planner, because the two will resolve
stations differently and produce different walks.

**Download HTML report** produces a self-contained file with the full detail, openable offline
and shareable by email. **Copy share link** gives a URL that others can open without an account.

---

## 6. Task: onboard a new country

**Purpose:** extend coverage to another national data set.

At a high level: register the NAP endpoint → import the feed → build a routing graph → promote
it to serving.

1. **`/admin/nap-catalogues`** — register the country's National Access Point endpoint. If it
   requires an API key, store that under **`/credentials`** first and attach it.
2. **`/admin/sessions`** — create a session (one isolated data set + routing graph), choose its
   engine, and import the feed. VIATOR detects the format automatically (GTFS, or NeTEx in the
   French, Nordic or EPIP profiles).
3. The background worker builds the routing graph. This is the slow step — minutes to hours
   depending on size.
4. Once built, **promote** the graph to serving. Only then does the session answer searches.

See `docs/multi-country-runbook.md` and `docs/nap-fr-rail.md` for worked examples.

---

## 7. Using VIATOR programmatically

**Current state, stated plainly: VIATOR has no public machine API.**

The application is driven through its web UI. There is an internal HTTP endpoint
(`/api/journey/fanout`) that the journey page itself calls, but it is authenticated with a
browser session, has no published contract, and is not versioned — it is an implementation
detail, not an interface. Nothing external can consume VIATOR today.

This is a known and deliberate gap that is being closed in two stages. The full plan — business
requirements, target architecture and sequencing — is **chapter 12 of `docs/architecture.md`**:

| Stage | Surface | Purpose |
|---|---|---|
| 1 | `POST /api/v1/plan` — versioned REST/JSON, API-key auth, published OpenAPI | Make VIATOR consumable at all |
| 2 | `POST /ojp` — **OJP 2.0** (CEN/TS 17118) request and response | Make VIATOR interoperable with the European journey-planning ecosystem |

The intended split is deliberate: the OJP endpoint will return **VIATOR's own itineraries only**,
strictly conformant, so that any standard OJP client can consume it. A **separate comparison
endpoint** will return VIATOR *plus* selected reference planners with the agreement scoring —
because that comparison data is richer than OJP can express, and mixing it into an OJP response
would mislead standard clients into attributing other planners' trips to VIATOR.

Until Stage 1 ships, the practical integration points are the **HTML report download** and the
**share link** from a coverage run.

---

## 8. Interpreting results — read this before drawing conclusions

VIATOR measures data quality, so its own caveats matter.

**Coverage and agreement are different claims.**
*Coverage* ("a route exists at this hour") needs only VIATOR. *Agreement* ("and it matches the
incumbent") needs a reference planner. Where no reference planner is reachable for a region,
only coverage can be claimed.

**Reference planners have limited geography.** Swiss OJP covers Switzerland only. ÖBB covers
DACH plus major cross-border services. Neither covers everywhere — so an empty reference column
may mean "no reference available here", not "the reference found nothing".

**The ÖBB reference is an unofficial interface.** It is the backend of ÖBB's own app, widely used
and politely accessed, but it carries no availability guarantee. Deutsche Bahn's equivalent
endpoint was silently retired in mid-2026.

**Known issue — agreement scoring is currently unreliable.** The station-identity matching used
in alignment scoring parses the wrong field, so cells can report *no overlap* even when both
planners clearly found the same train. Coverage results are sound; **treat the alignment
tier and score with caution** until fixed. Tracked in the project notes.

**Check the run's date.** A coverage run searches the day given by its *reference date*. If a
run is badged as having searched a different day than its stated departure, its numbers are real
but describe that other day.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **NAP** | National Access Point — the portal each EU country must run to publish mobility data, under Delegated Regulation (EU) 2017/1926 |
| **GTFS** | A widely used open timetable format, originally from Google |
| **NeTEx** | The European standard timetable format. The new rail Telematics TSI mandates it |
| **SIRI** | The European standard for *real-time* transport data |
| **OJP** | Open Journey Planner (CEN/TS 17118) — the European standard for asking a planner for a journey and getting an answer back |
| **Session** | One isolated data set plus its routing graph. Countries are usually separate sessions so they can be compared |
| **Engine** | The routing software computing itineraries: MOTIS, or historically OpenTripPlanner |
| **Reference planner / oracle** | An external, incumbent planner VIATOR compares itself against (ÖBB, Swiss OJP) |
| **Fanout** | Asking several sessions the same question at once and merging the answers |
| **Hub** | A major station used as a row/column in the coverage matrix |
| **Leg** | One segment of a journey — a single train, or a walk between platforms |

---

## 10. Where to go next

| Document | Content |
|---|---|
| `docs/architecture.md` | How the system is built, module by module, with diagrams (for developers). Chapter 12 is the plan for the OJP API |
| `docs/admin-guide.md` | Deployment, releases, day-2 operations |
| `docs/multi-country-runbook.md` | Field notes for onboarding many countries |
| `docs/eu19-compliance-summary.md` | Per-country NAP compliance findings |
| `VIATOR-strategy.md` | Why the project exists; data sources; roadmap |

---

**© 2026 TrackOnPath SAS. All rights reserved.**

VIATOR is designed, developed and owned by TrackOnPath SAS. The software is distributed under the
**Apache License, Version 2.0**. Open-source licensing grants rights of use; it does **not** transfer
ownership of the intellectual property, which remains vested in TrackOnPath SAS. The Licence confers
no right to use the TrackOnPath or VIATOR names, trademarks or logos beyond the reasonable and
customary use required to describe the origin of the work.
