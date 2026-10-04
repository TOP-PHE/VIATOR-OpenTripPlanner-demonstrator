# Station panel — design

The five screens, the tables behind them, and how the reference table is rebuilt when an input
changes. Companion to `docs/station-identity-and-journey-planner.md`, which carries the
measurements this design answers to.

**Version 2, 2026-10-03.** The first version was reviewed adversarially against the repository and
the real data and came back with 12 blocking findings. This version incorporates them; §12 lists
what changed and why. Status: step 1 is being built on `feat/station-panel`; §13 lists every place
where building it contradicted this document, and the text above §13 has been corrected to match.

> **Two kinds of figures appear below.** Statements about this repository carry a `file:line` and
> are checkable here. Figures about the offline mapping chain — row counts, the 16 `nap_*` columns,
> build time and memory — are measured **outside** this repository and cannot be checked from it.

---

## 1. What changes, in one paragraph

Today VIATOR's only station reference is `master_stations`, seeded from Trainline and keyed on UIC.
It feeds the journey typeahead and a config-save country gate, and it keeps nothing from any NAP.
This design demotes Trainline to **one input among several** and introduces VIATOR's own reference
table, keyed on the CRD primary location code, carrying one code per provider, a MERITS code with
its provenance, a station-complex grouping and — later — connection times. The table is built inside
the server from sources the operator configures through the interface, and is rebuilt when an input
changes.

`master_stations` is **not** extended and **not** dropped. Its grain is wrong for this — one row per
UIC, and a station without a UIC cannot exist in it — and the journey typeahead depends on it today.

---

## 2. The menu

One dropdown group, five entries, replacing the single `Stations` link at
`app/templates/_base.html:201`.

**The group must stay inside that link's role block** — `role in ('platform_admin',
'content_manager')`, lines 200–202 — and **not** move into the platform-admin block where the
existing `Admin dashboard` group lives. Copy that group's markup (a native
`<details class="nav-group">`, `role="menu"`, section headers), not its position; otherwise the menu
disappears for the content managers four of its five entries are for. The fifth entry is wrapped in
its own `{% if role == 'platform_admin' %}`.

| Entry | Path | JSON gate | Nature |
|---|---|---|---|
| NAP station information | `/admin/stations/nap` | `require_content_manager` | what each feed says about each stop |
| Infrastructure registers | `/admin/stations/registers` | `require_content_manager` | CRD (default tab) and ERA |
| Trainline codes | `/admin/stations/trainline` | `require_content_manager` | today's panel, relabelled as an input |
| VIATOR station reference | `/admin/stations/reference` | `require_content_manager` | the built table |
| Sources and integration | `/admin/stations/sources` | `require_platform_admin` | how every input is acquired |

`/admin/master/stations` keeps working and serves the same template as `/admin/stations/trainline`:
an integration test asserts it returns 200, and it is bookmarked.

---

## 3. Data model

Names are prefixed `station_`, `crd_` or `era_` so they sort together and never collide with
`master_stations`. Every key column is `text` (never `char(n)`, which pads — a real PLC is
`ATNs G`), with `COLLATE "C"` on the natural keys so ordering is byte-wise and stable.

**Vocabularies.** A `CHECK` constraint needs a migration to extend, which is the opposite of the
requirement. So:

- the **code series** is data: a lookup table, FK'd, extended by `INSERT`;
- source `format`, `acquisition`, `resolver_type` and `kind` are plain `text`, validated in the API
  layer — a new value there needs new code anyway (a new resolver, a new parser), so the database is
  not the place to pin them;
- `CHECK` is kept only where the set is fixed by the schema's own logic (`complex_role`, the
  transfer `tier`).

### 3.1 Sources

**`station_source`** — one row per (portal, dataset).

`id` uuid pk · `key` text unique (`CRD`, `ERA_TELREF`, `TRAINLINE`, `nap_CH_SBB`, `REG_BE_INFRABEL`)
· `label` · `kind` (`spine`, `timetable`, `registry`, `merits_input`, `crosscheck`,
`offline_build`) · `format` ·
`acquisition` (`resolver`, `url`, `upload`) · `resolver_type` null · `resolver_config` jsonb, the
same shape as an `app/data/eu19_nap_sources.json` entry · `credential_id` uuid null, FK
`ON DELETE SET NULL` as `nap_catalogues` already does · `country_iso` · `operator` · `licence`,
`licence_url` · `access_expires_on` date null · `refresh_cadence` · `triggers_rebuild` bool ·
`enabled` bool · `source_key_unresolved` bool.

`source_key_unresolved` exists because three of the offline provider columns are aggregates
(`nap_FR_regional`, `nap_ES_regional`, `nap_CH_SBB_non_rail_members`) that do not map onto one
(portal, dataset). They are seeded as sources flagged unresolved so their codes import now and the
split is a later, trackable job.

**What the first migration seeds.** Configuration, never data: one source per input the step 1
importer knows. `CRD`, `ERA_TELREF` and the three offline outputs `OFFLINE_MASTER`, `OFFLINE_LINKS`,
`OFFLINE_UNMAPPED` (kind `offline_build`) each declare, in `format`, which of the five file shapes
they accept; `TRAINLINE`; and the 16 provider columns of the offline master, whose `key` is the
column name verbatim (`nap_CH_SBB`, not `NAP_CH_SBB`) so that `station_ref_code.source_key` joins
`station_source.key` without a case mapping.

**Trainline is a `station_source` too.** Otherwise a build's input manifest cannot be complete: the
existing 04:00 refresh writes `master_stations` with no fingerprint. The cron writes a
`station_source_version` row with the CSV's sha256 from step 1 on.

**`station_source_version`** — one row per acquired file.

`id` · `source_id` FK `ON DELETE RESTRICT` · `acquired_at` · `as_of` date null · `filename` ·
`bytes` · `sha256` · `status` · `error` · `stats` jsonb · `stored_path` · `uploaded_by` null.
Index `(source_id, acquired_at desc)`; unique `(source_id, sha256)` so re-uploading the same file
is a no-op, not a duplicate.

### 3.2 Registers — the backing tables of screen B

Version 1 defined none, so screen B could not have been built.

**`crd_location`** — `id` · `source_version_id` FK `ON DELETE CASCADE` · `country` · `location_code`
· `plc` · `start_validity` · `end_validity` · `active_flag` · `name` · `free_text` · `lat` · `lon`
· `passenger_flag` · `freight_flag` · `responsible_im` · `nuts`.
Unique `(source_version_id, country, location_code, start_validity)` — CRD's own key is (country,
code, validity). Index `(source_version_id, plc)`. The validity and the three flags are stored as
text, exactly as the offline extractor wrote them; `station_ref.crd_start` / `crd_end` are the
parsed dates.

**`crd_subsidiary`** — `id` · `source_version_id` · `plc` · `subsidiary_type` · `allocation_company`
· `code` · `name`. Index `(source_version_id, plc)`.

**`era_operational_point`** — `id` · `source_version_id` · `plc` · `uopid` · `name` · `op_type` ·
`iso2` · `lat` · `lon` · `rl100` null. Unique `(source_version_id, plc, uopid)`.

The delta shown on upload is a set difference between two `source_version_id`s on these tables.

### 3.3 The reference table

**`station_ref`** — current state only. Identity is stable across builds.

| Group | Columns |
|---|---|
| Identity | `id` bigserial pk · `plc` text not null, `CHECK (length(plc) = 7)` · `era_uopid` text **not null** · `previous_plc` text null |
| Constraint | `UNIQUE (plc, era_uopid)` |
| Names | `name` · `name_src` · `alt_name` text[] · `alt_name_text` |
| Position | `lat` · `lon` · `pos_src` · `link_pos_src` · `position_flag` |
| Classification | `iso2` · `iso2_all` text[] · `op_type_all` text[] · `op_type_src` · `is_passenger` · `is_passenger_src` · `plc_kind` |
| Multiplicity | `n_op_with_plc` int · `plc_op_max_sep_m` int |
| Spine | `spine_source` · `crd_start` · `crd_end` · `is_current` bool, set by the build · `crd_source_tag` |
| MERITS (chosen) | `uic_merits` · `uic_merits_origin` · `uic_merits_rule` · `uic_merits_confidence` |
| Other codes | `rl100` · `nat_code` · `nat_code_series` · `nat_code_src` · `ifopt_dhid` · `ifopt_dhid_src` · `eva` · `eva_src` · `eva_all` text[] |
| Grouping | `complex_id` FK `ON DELETE SET NULL` · `complex_role` |
| Quality | `warning_level` · `best_tier` · `n_nap_feeds` |
| Lineage | `first_seen_build_id` · `last_built_build_id` · `last_changed_build_id`, all FK `ON DELETE RESTRICT` |

Why these and not version 1's:

- **`era_uopid` is `NOT NULL`.** A unique index treats NULLs as distinct, so `('BE00220', NULL)`
  would be insertable without limit. Where a source has no uopid the PLC is the sentinel, which is
  what the offline builder already does.
- **The grain is real.** 314 PLCs carry more than one operational point; a natural-key upsert on
  `plc` alone would silently collapse 698 rows.
- **No `build_id` on the row.** Version 1 put one there, which meant either a full copy per build —
  with every child FK and every hand correction pointing at a dead build — or an in-place rewrite
  with no history. The row is current state; history is `station_ref_history`.
- **`previous_plc`** is the only safe historical join after a renumbering, on a table keyed on PLC.
- **`is_current`** = `crd_end IS NULL OR crd_end >= ` the build date, with a partial index. Without
  it nothing says a row is retired, on the table step 7 puts behind the typeahead. It is a plain
  boolean the build sets, **not** a generated column: a generation expression and an index predicate
  must be immutable, and `CURRENT_DATE` is not, so Postgres refuses both. The consequence is that it
  goes stale between builds; a nightly `UPDATE` is a small later addition.
- **`complex_role`** is `principal` or `member`, the one `CHECK` on this table besides the PLC
  length.

Indexes: GIN trigram on `name` and on `alt_name_text` (`pg_trgm` is already used by
`master_stations`); btree `(iso2, is_passenger)`; partial GIN trigram on `name`
`WHERE is_passenger AND uic_merits IS NOT NULL` for the typeahead; btree on `uic_merits`,
`previous_plc`, `complex_id`; GIN on `iso2_all`; partial btree `(plc) WHERE is_current`.

`alt_name_text` is `alt_name` joined into one string, maintained by the build. It exists only for
the trigram index: `gin_trgm_ops` cannot index a `text[]`, and `array_to_string` is not immutable,
so neither an expression index nor a generated column can do it.

**`station_ref_code`** — one row per (station, source, series, code). Where "one column per
provider" lives: stored long, displayed wide.

`station_id` FK `ON DELETE CASCADE` · `source_key` · `series` FK to `station_code_series` · `code` ·
`code_raw` null · `normalisation_rule` null · `is_primary` · `evidence_only` bool · `confidence` ·
`method`.

`UNIQUE NULLS NOT DISTINCT (station_id, source_key, series, code)`. **`(series, code)` is a lookup
index, not a unique key**: on the real data, 3,580 `(series, code)` pairs map to more than one
station, so a unique constraint would fail on the first import. A second index on `(code)` serves
bare-code search.

`series` is **nullable**. The offline master's provider columns carry codes and no series, and one
column can mix shapes (`9900001|9900001:0:1`), so a per-column series would be a guess. The importer
fills `series` only where the links file names it for the same station and the same code value, and
leaves it NULL otherwise; `NULLS NOT DISTINCT` (Postgres 15+, the stack runs 16) keeps the unique
key meaningful for those rows.

`code_raw` and `normalisation_rule` exist because `code` is not always what the provider published —
DIUM codes are normalised. `evidence_only` marks a code that must never be used as a join key.

**`station_code_series`** — `key` pk · `label` · `family` null · `is_joinable`. Seeded with the 14
keys the offline files actually use (`SNCF_8digit`, `CH_service_point_number`, `DELFI_stop_key`,
`PLC`, … — listed verbatim in `docs/station-offline-file-shapes.md`). `family` is where a coarser
grouping (`uic_intl`, `eva`, `dhid`) goes when it is decided; mapping the offline keys onto families
is a later normalisation, not an import-time guess.

**`station_ref_merits`** — `station_id` · `code` · `origin` · `rule` · `confidence` · `sources` ·
`check_digit` · `is_chosen`. `UNIQUE (station_id, code)` and a partial unique
`(station_id) WHERE is_chosen`. A scalar `uic_merits` alone would have pre-decided an open question
— whether a station may hold two MERITS values, as Chiasso does — and left the calculated value of
18 rows surviving only inside prose. `station_ref.uic_merits` mirrors the chosen row for fast
filtering.

**`station_ref_alias`** — `station_id` · `alias_plc` · `reason` · `build_id`.
`UNIQUE (alias_plc, build_id)`. An old PLC resolves in one lookup: a build replaces the whole
set, so the table holds the aliases of the latest build and nothing of an earlier one.

**`station_ref_flag`** — `station_id` · `token` · `payload` · `level` · `warning_code` ·
`related_station_id` FK `ON DELETE SET NULL`. `UNIQUE (station_id, token, payload)`. A `text[]` of
`token:payload` strings cannot be joined, and many tokens name another station — a swap partner, a
displaced candidate — which the detail page must link to. `payload` is `NOT NULL`, the empty string
for a bare token: left nullable, the unique key would not constrain bare tokens, for the same reason
`era_uopid` is `NOT NULL`.

**`station_ref_override`** — `station_id` · `field_name` · `value` · `reason` · `set_by` · `set_at`
· `computed_value_at_set` · `computed_value_latest` · `released_at`. Partial unique
`(station_id, field_name) WHERE released_at IS NULL`. One boolean per row cannot work against
per-value provenance: when a rebuild improves a *different* field of a corrected row, it must not
have to choose between its improvement and the correction.

`station_ref` holds the *effective* value, so a search or a filter sees the correction.
`computed_value_latest` is what the last build computed underneath it: without it a release after a
rebuild could only restore the value from the day the correction was made. `station_id` is
`ON DELETE RESTRICT` here — a station carrying hand corrections is not deleted by accident.

**`station_ref_link`** — the adjudication ladder, at the offline links file's grain.
`station_id` null · `source_version_id` · `offline_station_id` · `feed_key` · `stop_key` ·
`stop_name` · `iso2` · `lat` · `lon` · `label` · `code_value` · `code_series` · `match_method` ·
`tier` · `asserted` bool · `distance_m` · `name_sim` · `reason` · `nearest_plc` ·
`nearest_distance_m` · `note`.
A null `station_id` is an unmatched stop, and `nearest_plc` with its distance is the hint the
operator works from. `match_method` is free text — 6,544 distinct values offline — and `tier` has 21;
both are stored as text, never as a vocabulary. This table, not `nap_stop`, is what screen A's two
lists are rendered from.

**`station_ref_history`** — `station_id` · `build_id` · `field_name` · `old_value` · `new_value`.
Written by the build from its own diff. `field_name` is a column of `station_ref`, or `codes`,
`merits` or `flags` for a station whose rows in that child table are no longer the ones it had:
`old_value` then lists the rows that went and `new_value` the rows that came, each as
`column=value` pairs. Either kind of row makes the station "changed" in the build's diff summary
and sets its `last_changed_build_id`.

**FK actions this section leaves open**, as built: every child's `station_id` is `ON DELETE
CASCADE` (codes, MERITS candidates, aliases, flags, links, history are derived rows) except the
override's, which is `RESTRICT`; `build_id` on aliases and history is `CASCADE`, since those rows
belong to one build; `set_by` and `uploaded_by` are `SET NULL`. Indexes added beyond the ones named
above: `station_ref_link (station_id)` and a partial `(label, iso2) WHERE station_id IS NULL` for
screen A, `station_ref_flag (token)` for the flag filter, `station_ref_alias (station_id)`, and
`station_ref_history (station_id, build_id)` and `(build_id)`.

### 3.4 Complexes

**`station_complex`** — `id` · `label` · `kind` · `source` (`derived`, `manual`) · `rule` ·
`requires_physical_separation` bool · `separation_reason`.

`kind` distinguishes four structurally different groupings the measurements produced, because the
artefacts of §6 are chosen by which kind it is: `one_site`, `shared_operational_point`,
`parallel_register`, `adjacent_treated_as_one`. `requires_physical_separation` is the border-control
flag — the Midi case.

CRD carries no station-to-sub-station link (type 40 Metastation is empty in the export), so every
grouping is derived or manual, and the screen says which.

### 3.5 Connection times — defined here, **created in step 6**

**`station_ref_transfer`** — `from_station_id` · `to_station_id` · `min_seconds` · `directed` ·
`is_prohibited` · `tier` smallint `CHECK (tier BETWEEN 1 AND 5)` · `from_service_ref` ·
`to_service_ref` · `operator_ref` · `service_type` · `guaranteed` · `max_wait_seconds` ·
`valid_from` · `valid_to` · `rule_source` · `source_version_id` · `confidence` · `note`.

Version 1 held the scope in a `jsonb`, which cannot carry a unique constraint and does not say which
tier a rule is — so no resolver could pick a winner deterministically, and two contradictory rules
at the same scope were undetectable. `is_prohibited` is there because a prohibition is exactly what
the loss report must say was lost.

This is **not** a small table: at station-pair grain it is about a million rows, more with
service-pair rules. Index `(from_station_id, tier)` and `(to_station_id, tier)`. It has **no offline
producer** — the connection times are still inside the NeTEx archives — which is why it is not in
step 1.

### 3.6 `nap_stop` — defined here, **created in step 2**

`id` · `source_version_id` · `feed_key` · `stop_id` · `parent_id` · `name` · `lat` · `lon` · `modes`
· `platform_code` · `codes` jsonb (field path → value as found) · `extra` jsonb · `station_id` null.

Its `codes` and `extra` are produced inside the extractors and were never written to any offline
file, so nothing can populate it until an extractor runs in the server.

### 3.7 Builds

**`station_build`** — `id` bigserial, so builds are numbered · `started_at` · `finished_at` ·
`status` · `builder_version` · `inputs` jsonb (source version ids and sha256) · `counts` jsonb ·
`diff_summary` jsonb · `log_path`.

**Retention**: keep the last three builds' rows and their `station_ref_history`; keep every
`station_source_version` row but only the last two stored files per source.

---

## 4. The screens

### A. NAP station information — `/admin/stations/nap`

**In step 1** this screen is the two lists that do the daily work, rendered from
`station_ref_link`: **stops we could not match** to a reference row, and **stops whose code
contradicts** the reference. Filter by feed and by country.

**From step 2** it gains the per-stop view: a row is a stop in a feed, with every field the feed
carries and the raw record. Where a dedicated stop registry exists for the country (Infrabel, DiDok,
OpenStation, CHB, NSR, NaPTAN), the screen prefers it and says so in an `origin` column.

As built (`app/api/master/station_links.py`):

- **Unmatched** is a link row with no station, that is a row of the unmatched-stops file. The list
  opens on the `Rail` and `Multimodal` labels only: the `Urban` rows are tram and bus stops that
  were never expected to match a railway location. The label filter is a set of checkboxes built
  from the labels the data carries; with none ticked the list shows every label. Rows are ordered
  nearest first, and each shows the nearest PLC with its distance, linked to the reference row(s)
  of that PLC.
- **Contradicting** is a link that names a reference row, is **not asserted**, and **carries a code
  value**: the code said "this reference row", the distance or the name said otherwise, so the
  offline chain refused the match. This is the definition `station_ref_link` can answer on its own
  and it is an interpretation, listed in section 13. A link records the chain's verdict on one stop;
  it does not compare the stop's code with `station_ref_code`, so a code asserted on one station
  while another station carries the same code is not found here. That comparison belongs to the
  resolver.
- **Feed**: an unmatched stop can be listed for several feeds in one cell (`A|B`). The filter finds
  it under each of them, and the feed counts count it once per feed.
- **Country**: the stop's own for the unmatched list. A matched link has no country, so the
  contradictions are filtered by the country of the reference row they point at.
- A link naming a reference row the master does not carry is not imported, so it is in neither list.

### B. Infrastructure registers — `/admin/stations/registers`

Two tabs, CRD first, each reading the latest `source_version_id` of its source.

**CRD**: PLC, name, position, type, country, validity, subsidiary codes. Above the table, the
extraction date, the fingerprint, the row count and a **licence banner** — RNE-licensed,
VIATOR-internal.

**Upload a new version** computes the delta against the previous one before anything is rebuilt:
created, removed, renumbered, renamed, moved further than a threshold.

*Step 1 limitation, stated plainly:* the server does not yet parse the CRD XML export. Until the
spine extractor is ported (step 4a), the operator runs `crd_extract.py` offline and uploads its
CSV. The delta works identically on two CSV versions.

**ERA**: the same view, read-only — operation types, national identifiers, RL100.

As built (`app/api/master/station_registers.py`, `app/master/station_delta.py`):

- Each list reads one version, by default the latest one whose rows are **loaded**. A version that
  was uploaded and not yet imported is listed, marked as not loaded, and is not what the list
  shows: the rows are written by the importer's extract stage, not by the upload.
- The delta is computed on demand between any two loaded versions (`/{register}/delta`), the two
  most recent by default, with the "moved" threshold as a parameter (100 m by default). It
  compares locations, not validity periods: for CRD, the row with the latest start of validity
  stands for its PLC.
- A rename needs a name in both versions, as a move needs a position in both: one that appears or
  disappears is neither. A location retired in CRD has no name and no position in the CRD register
  from then on (the file carries ERA's); CRD changed its validity, not its name. The delta has no
  kind for a retirement: such a location counts as unchanged.
- **`renumbered` is inferred**, not read: `crd_location` and `era_operational_point` keep no
  "this code replaces that one" column, so a removed and a created location are paired only when
  they carry the same name at the same place. It is a reading aid; the reference's own
  `previous_plc` and `station_ref_alias` remain the record of a renumbering.
- The licence banner is the `licence` of the register's source, so it is there before any file is
  uploaded. `rl100` on the ERA tab is empty in step 1: the telref extract has no such column.

### C. Trainline codes — `/admin/stations/trainline`

Today's panel, relabelled as an input, with a header saying what it is for: the MERITS code when a
station is known to Trainline, matched by railway code only.

Three defects fixed in passing, each stated where it really is:

- **The whole drift table is materialised on every call to `GET /api/master/stations`** —
  `app/api/master/stations.py:199` builds a set of UICs out of full ORM rows, `trainline_snapshot`
  JSONB included. The panel is submit-driven, so it pays this per search and page flip; the journey
  typeahead (`journey.html:483-495`, 150 ms debounce) pays it per keystroke on the same endpoint.
  Fix: `select(MasterStationPendingDrift.uic)`.
- **`« First` is a silent no-op during a context search, and so is typing `1` in the page-jump
  box.** The cause is server-side: `page == 0` doubles as the sentinel for "caller pinned no page",
  so `stations.py:188` overrides it with the match page. Fix it at the API with
  `page: int | None = None`. The `filter` default stays untouched: the typeahead depends on it.
  **The template has to follow**, which version 2 ruled out: the panel sent `page=0` on every
  fresh search, and once the API respects a page it is given, that would pin every search to the
  first page and the jump to the first match would be gone. The panel now sends `page` only when
  the operator asked for one by number (`« First`, `‹ Prev`, `Next ›`, `Last »`, the page-jump
  box) and leaves it out otherwise.
- **`.flag` is used by the panel but defined only in `journey.html`'s own style block**, so the
  DRIFT badge renders unstyled. Promoted into `_base.html`.
- **`.hint` is used by the panel and was defined nowhere.** Version 2 said `journey.html` defined
  it; it only has `.hub-form-grid .hint`, and the other pages have scoped rules of their own
  (`.detail-section .hint`, `.compare-toggle .hint`, `.cov-trip-section summary > .hint`). Every
  one of them sets the same colour, so `_base.html` now carries `.hint { color: var(--rail-steel) }`
  and nothing else.

As built:

- `app/api/master/stations.py`: `drift_uics_of()` selects `MasterStationPendingDrift.uic` alone.
  `page` is `int | None`; `pinned_page()` reads it. Omitted, the list starts on the first page, or
  in `context` mode with `q` on the page of the first match. Given, it is the page shown.
  `X-Match-Page` is unchanged. `/drift`, the queue itself, still returns the full rows.
- **`.flag` in `_base.html` is not a verbatim move.** `.flag`, `.flag.ALL` and `.flag.NAP-ONLY`
  are the journey page's rules, the two colours written as the `--ok` and `--fail` variables that
  hold the same values. `.flag.MERITS-ONLY` and `.flag.SUBSET` are not: the journey page's blue
  (`#1C75BC` on `#e8f0fb`, 4.2:1) and amber (`#b3760e` on `#fff8e1`, 3.6:1) are below the 4.5:1
  that WCAG AA asks of text this size, and the Sonar gate refuses new CSS that is. The base rules
  take the pairs `_base.html` and the station panel already use on those two tints
  (`--brand-blue-d`, and `#6f5100` as in `.msg.warning`). `journey.html` keeps its own two rules,
  which it shares with `.leg-mode` and `.cmp-count`; they come later in the stylesheet and win, so
  **the journey page renders exactly as before**. Two palettes for two variants is a debt, listed
  in section 13.
- **The base `.hint` reaches further than the panel.** A hint that had no rule now takes the muted
  colour its class always asked for: on the coverage page most of its hints, on the sessions page
  those outside a detail section. Colour only — a font size would have changed the hints that
  scoped rules already style.
- **The journey page opts out.** Section 11 puts any change to the journey page outside step 1,
  and two of its hints had no rule (the paragraph at the top of the "Promote to hub" form, the note
  beside "Cross-session journeys"). `journey.html` therefore carries `.hint { color: inherit; }`:
  those two keep the colour of their parent, and the scoped rules, more specific than either, keep
  theirs. Deleting that one line lets the base rule apply there too.

### D. VIATOR station reference — `/admin/stations/reference`

Search by name, PLC or any code in any series. Filter by country, confidence, flag, and whether a
timetable code exists.

Row: PLC · **`era_uopid`** · name · country · complex · MERITS with its origin · one column per
provider · flags. `era_uopid` is half the key and the only thing that tells two operational points
of one PLC apart: 297 of the 314 multi-point PLCs are otherwise identical in every displayed
column. The row also carries a badge — "PLC carries 7 operational points, 0 m apart" — and those
rows are collapsed by default.

**The pivot has one affordable shape**: paginate `station_ref` first (50 rows), then one query for
`station_ref_code WHERE station_id = ANY(...)`, pivoted in the application. Never join first.

Detail page per station: every code with its series, source and rule · every MERITS candidate, not
only the chosen one · the matched NAP stops with method and distance · the complex and who else is
in it · its flags, each linking to the related station · its overrides.

Actions: correct a field by hand (writes `station_ref_override`); group or ungroup a complex.
Connection times arrive in step 6 — until then the panel says so rather than showing an empty table.

Header: the build this state comes from, its input versions, and the diff against the previous one.

As built (`app/api/master/station_ref.py`):

- **Collapsed means one row per PLC, decided by the server.** `collapse=true` is the default: the
  list keeps, per PLC, the operational point whose id is the PLC itself, else the first in byte
  order, among the rows the filters match — so a search that hits only the second operational
  point shows that one. `X-Total-Count` then counts PLCs. The badge's button fetches
  `?plc=…&collapse=false` and inserts the others under the row.
- **Search** is name, alternative names and PLC by substring; operational-point id, previous PLC,
  MERITS code, EVA, RL100 and any provider code by equality. The provider codes are reached through
  a subquery on `station_ref_code (code)`, the flag filter through one on
  `station_ref_flag (token)`: neither joins.
- **A correction** is `POST /{id}/overrides` with a field, a value and a reason. Correctable
  fields: `name`, `lat`, `lon`, `iso2`, `is_passenger`, `uic_merits`, `rl100`, `nat_code`,
  `ifopt_dhid`, `eva`. Identity (`plc`, `era_uopid`) is not. Correcting a field already corrected
  releases the earlier correction and keeps it as history.
- **A complex** is created from two or more stations selected in the list
  (`POST /complexes`), with one of them optionally its principal, and removed whole
  (`DELETE /complexes/{id}`). Two or more different stations: an id given twice counts once, and
  one station alone is refused with `400`. One made here is `manual`; a station is in one
  complex at most; a rebuild leaves `complex_id` and `complex_role` alone.
- **While a station build is writing the reference**, the four routes that write (set or release
  a correction, group or ungroup a complex) answer `409` at once and save nothing; the panel
  shows the reason. They do not wait: a build's write stage lasts far longer than a request
  should. Reading is never refused.

### E. Sources and integration — `/admin/stations/sources`

One row per source: label · kind · format · acquisition · credential · cadence · triggers rebuild ·
last acquisition · fingerprint · as-of · access expiry · state.

An access expiry inside 90 days is amber, past is red. Slovenia's grant ends 2027-02-10.

Actions: add, edit, **upload a version**, enable/disable. From step 4: **test**, **acquire now**,
**rebuild now**. The build history shows each build's inputs and diff summary.

---

## 5. The pipeline inside VIATOR

1. **Acquire** — resolver, URL or upload → `station_source_version` with its sha256. An unchanged
   sha stops here.
2. **Extract** — per format, into `nap_stop` or the register tables.
3. **Build** — spine + registers + stops + Trainline → the `station_ref*` tables, with a
   `station_build` row carrying inputs and diff.
4. **Publish** — the artefacts of §6.

**Where the build runs.** In the worker process — but "the worker" as it stands cannot take it.
`app/worker.py:342-377 tick()` polls `rebuild_jobs` only and dispatches every job to a graph
builder; `RebuildJob` has no kind column; `_enqueue_rebuild` coalesces on `(status, session_id)`,
so a station job with a null session would collide with the `_phase1` graph job. The change is one
migration and three edits: add `kind` to `rebuild_jobs` (`'graph'` by default, or
`'station_build'`), branch on it in `tick()` **before** the engine lookup, and coalesce on
`(status, session_id, kind)`.

What the branch keeps a station build away from is what a graph job itself does: the cancel
watcher, which would `docker kill` a build container that never existed; the session state
advance; the graph snapshot; the max-memory stop of the serving stack. Compose up, nginx reload
and orphan-container removal are a different thing: they live in `handle_reload_trigger()`, run
after every tick whatever the job was, and act only when the `.reload-trigger` file exists, so no
job provokes them and there was nothing to suppress there.

**The three stages of a build**, as built (`app/master/station_import.py`):

1. *Parse*, with no database. All five files are read and checked first; a refusal here leaves
   every table untouched.
2. *Extract*. The CRD locations and the ERA operational points go into the register tables, once
   per source version, and are committed — so the delta between two versions is available
   whether or not the build that follows succeeds.
3. *Build*, in one transaction. `station_ref` is upserted on `(plc, era_uopid)`, hand corrections
   are re-applied, and the derived children are replaced. The transaction opens by taking a
   Postgres advisory lock (`app/master/station_lock.py`) and reads the reference and the active
   corrections only once it holds it: the build plans from one reading, so no correction may be
   set or released between that reading and the commit. The routes that write a correction or a
   complex take the same lock, shared, before they read anything, and do not wait for it.

A row present in an earlier build and absent from the current master is **kept**, untouched, and
is recognisable by its `last_built_build_id`: deleting it would delete its hand corrections.

**Where the files live.** There are exactly two data volumes, `inbox` and `graphs`, and
`app/storage.py:297-303` offers any top-level inbox folder that is not a session id to the operator
for deletion on the Storage page. So the station store, `inbox/_stations/`, must be added to the
reserved set in `app/storage.py` — and `_staging`, which already falls into that trap, with it.

**What triggers a rebuild.** A new source version on a source with `triggers_rebuild`, or the
button. Never a schedule alone: a rebuild is an event. As built: the upload queues a build only
once all five inputs are present, and otherwise says what is still missing — with four files of
five, a build could only be refused. The button (`POST /api/admin/stations/builds`) is in step 1,
not step 4: re-uploading an identical file is a no-op, so without it a build that failed for a
reason outside the files could never be run again. A queued build waits out the worker's rebuild
debounce like any other job; uploads in a row coalesce into one build.

**What the build asserts before it replaces anything**: every CRD-derived row carrying its
licence tag (rows whose `spine_source` is `CRD_and_ERA` or `CRD_only`); every active override
re-applied. A build that fails is recorded and discarded. "Every flag token classified" is **not**
asserted in step 1: no classification of tokens exists in the repository or in the files, and the
shapes document says an unknown token must be stored, not refused. `station_ref_flag.level` and
`warning_code` are NULL until one exists; the build counts the distinct tokens.

---

## 6. The artefacts the engines need

| Artefact | Consumer | Step |
|---|---|---|
| Stop consolidation CSV | **OTP only** — `stopConsolidationFile` | 5 |
| Merged-source manifest + id map | **MOTIS only** | 5 |
| `transfers.txt` compiled from NeTEx interchanges | both | 6 |

**They cannot be delivered today.** `app/ingestion.py:87` allows four timetable sources — `url`,
`upload`, `cross_border_filter`, `nap` — and the comment above it says a `server_file` mode "is
still planned and is not yet a valid value". Writing a file straight into `inbox/<sid>/gtfs/` works
exactly once: `inbox_sweep` renames anything not in the expected provider list to `*.orphaned`, and
the next refresh overwrites the slot. **`server_file` must land first**, as its own change: one
value in `_TIMETABLE_SOURCES`, one validator branch, one refresh branch that copies instead of
fetching. The publish must also call `_forget_fetch_state_for_slot` as the upload path does, or the
provider panel reports a format check against a hash that no longer describes the file.

**The merged feed is MOTIS-only.** On OTP it would silently disable every GTFS-RT updater —
`router-config.json` binds them by `feedId` — and destroy the per-feed namespace. OTP stays on
unmerged feeds plus the consolidation file.

**OTP consolidation is not "a configuration file".** It needs four things, none of which exists:
the worker writing the CSV next to `router-config.json` (the inbox is mounted read-only in the
build container, so the entrypoint cannot fetch it), the entrypoint copying it into the build
directory, a `stopConsolidationFile` key in the build-config heredoc at
`docker/otp/entrypoint.sh:231-246`, and an `otp-config.json` enabling the sandbox feature — there is
no `featuresEnabled` anywhere in the tree. The CSV column order must be checked against the OTP 2.9
documentation; nothing here pins it.

**The cross-border filter comes first.** `app/gtfs_cross_border_filter.py:524-540` keeps a
`transfers.txt` row only if both ends are on a kept cross-border trip, and has no counter for what
it drops. It also reads the country out of the **UIC prefix of the stop id** (`_UIC_RE`, line 86),
so any rewrite that changes digit adjacency can make it emit an empty feed with a green refresh.
Therefore: run the filter first and publish into its output; make stop ids **additive** — keep the
UIC-shaped local id intact and carry the group in a separate column, which is what OTP's
consolidation CSV wants anyway; add a dropped-transfers counter to `CrossBorderStats`.

Each publish writes a **loss report**: direction lost on a station minimum, prohibition not
expressible, TEL TSI tiers collapsed to one value per pair.

---

## 7. Rollout

| Step | What | Size and dependency |
|---|---|---|
| **1** | Schema; the upload route; the storage reservation; the `rebuild_jobs.kind` column; import of the five offline files as build #1; the five screens | this document's §11 |
| 2 | `nap_stop` from **GTFS** feeds only — the one format the repo parses. NeTEx and IFF stops arrive as uploads of the offline `per_feed_output/*.csv` | the repo has no NeTEx or IFF reader |
| 3 | A streaming NeTEx stop-place reader and an IFF reader. Reconcile the seven patched copies of the extractor | **needs `lxml` approved as a runtime dependency**; ~10,600 lines of extractors offline, with a known defect in the `nl-cz` IFF patch to fix on port |
| 4 | Port the chain: (a) spine extract 1,509 lines, (b) DE bridge 1,779, (c) NL bridge 1,810, (d) master 4,012 | ~19,000 lines across 17 scripts in total, not one builder. `openpyxl` needed for the DE bridge |
| 5 | The `server_file` source; OTP consolidation (four touch points); the MOTIS merged-source manifest | §6 |
| 6 | NeTEx interchange → `transfers.txt`, with the loss report. Creates `station_ref_transfer` | nothing in the repo reads a NeTEx interchange |
| 7 | Move the typeahead and the country gate onto `station_ref`; retire `stations_xref` | touches the journey page |

**What step 1 delivers, honestly:** screens B, C and E complete; screen D without connection times;
screen A as the two match lists, with no per-stop record panel. That is still worth building even
if everything after it slips.

**How step 4 is verified.** The offline chain's only correctness proof is byte-identical replay,
which a port into SQL cannot reproduce. So the comparison is defined before any code: per stage, set
equality on a named key and column list, with an explicit tolerated-difference list (row order,
float representation, list order), against a pinned `--winner-key`.

---

## 8. What this touches that already exists

- **`stations_xref`** has never had a writer (`docs/architecture.md:1311`), but it is not inert:
  `app/journey/signature.py` reads it and `app/api/admin/sessions.py:431` deletes its rows. It
  cannot simply be written either — its PK is per-session `(session_id, stop_id)` and its `uic` is
  FK'd to `master_stations.uic` (`app/models/runtime.py:27-36`), so no row could be written for a
  station without a MERITS code, the very case this design exists for. **Recommendation: drop it in
  step 7, together with `signature._stop_token`'s lookup.** Decide before step 5, not during it.
- **The journey typeahead** calls `/api/master/stations?q=…&size=20` and relies on the `filter`
  default. In step 7 its filter is **routability, not passenger status**: only rows that are
  passenger, current and carry a `uic_merits`. Over half the passenger rows have no UIC; serving
  those would silently send the user back to the coordinate path that exists precisely because it
  fails at border stations.
- **The country gate** is a save-time gate on session config, returning 409
  `missing_master_stations_for_countries`, with **two** call sites. After step 7 its predicate is:
  passenger, current, `uic_merits` present, and country not flagged for review. Counting all
  reference rows would admit a country on the strength of its freight yards.
- **`stations_csv_url`** is fetched, checked, staged and read by nothing. It becomes a source or it
  goes.
- **`docs/eu19-providers.md:583-584`** says MOTIS clusters stops by coordinate proximity and name
  similarity. The name-similarity half is wrong; MOTIS does no name matching. Correct it, and
  reconcile the proximity figure with the 300 m / 100 m this design relies on.

---

## 9. House rules

- **Alembic**: revision ids are hand-edited to `YYYYMMDD_HHMM_<desc>` and must stay **≤32
  characters** — the prefix burns 14, leaving 18. Longer fails at `upgrade`, not in CI, and locks
  the web container in a restart loop. `downgrade()` must really drop everything; an integration
  test asserts it, and its `REQUIRED_TABLES` list (`tests/integration/test_migrations.py:23-43`)
  must be extended.
- **Linting and coverage of migrations**: the `**/migrations/versions/*` exclude in
  `pyproject.toml:14` has never matched `alembic/versions/`, so ruff lints migrations. Sonar does
  **not** see them: `sonar-project.properties` sets `sonar.sources=app`, and `alembic/` is outside
  it, so the stale globs there are moot and migrations count in neither Sonar's issues nor its
  new-code coverage. What checks a migration is ruff, the offline-SQL unit test
  (`tests/unit/test_station_schema.py`) and the integration upgrade/downgrade test.
- **Routers**: `app/api/__init__.py` is empty; registration is two hand-written lines in
  `app/main.py`.
- **Auth**: there is no router-level dependency anywhere, so an endpoint has no auth unless its own
  signature declares one. Three gates exist at `app/security.py:156-158`: `require_logged_in`,
  `require_content_manager` (platform_admin **or** content_manager) and `require_platform_admin`.
  Screens A–D take the second — it is what `app/api/master/stations.py` already uses on every route
  — and screen E the third. Pages use the separate redirect-on-failure guard in `app/api/pages.py`.
- **Sonar**: ≥80% coverage on new code, cognitive complexity ≤15. Extract nested template JS into
  named helpers; `globalThis`, not `window`; no empty `catch`.
- **Tests**: `tests/unit` needs no database. The current panel has almost no coverage; the new
  screens must assert columns, query parameters, pagination headers and **both** role gates.
- **Data**: no CRD row, no Trainline row, no extract of the mapping table in the repository — the
  repository is public. Fixtures are synthetic.

---

## 10. Open decisions

1. The border-control complex: artificial separation beyond MOTIS's 300 m, an upstream fix, or an
   accepted and reported false positive.
2. The eight content decisions already listed in `Station_Master_CRD_2026-09.md` §10.
3. Whether a station may hold two MERITS values. The schema now leaves this open.
4. The `--winner-key` tie-break, never pinned. Step 4's verification needs it pinned.
5. `lxml` and `openpyxl` as runtime dependencies — needed before steps 3 and 4.
6. Whether `master_stations` is retired after step 7 or kept as the Trainline input of record.

---

## 11. Step 1 — the work package

Everything below is implementable from this repository alone, with synthetic fixtures.

**Migrations** (ids ≤32 characters):

| Id | Creates |
|---|---|
| `20261003_1200_station_sources` | `station_code_series` (seeded), `station_source`, `station_source_version`, `station_build` |
| `20261003_1210_station_reg` | `crd_location`, `crd_subsidiary`, `era_operational_point` |
| `20261003_1220_station_ref` | `station_complex`, `station_ref`, `station_ref_code`, `station_ref_merits`, `station_ref_alias`, `station_ref_flag`, `station_ref_override`, `station_ref_link`, `station_ref_history` |
| `20261003_1230_rebuild_kind` | `rebuild_jobs.kind`, server default `'graph'` |

**The upload route** — `POST /api/admin/stations/sources/{key}/versions`, `require_platform_admin`.
Streams to `inbox/_stations/<key>/` while computing sha256, writes `station_source_version`, and
**bypasses `detect.detect` and `ingestion.dispatch` entirely**. Both existing upload routes refuse a
station file: they require a `declared_standard` in `detect.KNOWN_KINDS`, and `_detect_csv` accepts
a CSV only if its header looks like SNCF stations or MCT. While there, wrap the bare `detect` call
in `upload_to_session` (`app/api/admin/sessions.py`) so it returns 400 rather than 500.

As built: the file lands in `<key>/_incoming/` while it streams, and moves to
`<key>/<sha256 prefix>-<name>` once kept. Where the source's `format` is one of the five offline
shapes, the header is checked before anything is recorded; a mismatch is a 400 that names the
missing and unexpected columns, and nothing is stored. An identical re-upload answers 200 with
`created: false`. An optional `as_of` form field states the date the file describes; without it
the date is read off the file name (`…_2026-09.csv`, `…_2026-09-14.csv`) when it carries one.

**The storage reservation** — add `_stations` and `_staging` to the reserved top-level names in
`app/storage.py`'s inbox scan (`INBOX_ROOT_RESERVED`). Their size is still reported; they are never
a clean-up candidate, and `delete` refuses them because it only accepts ids from a fresh scan.

**The importer** — reads five offline files, each uploaded as a source version, and writes build #1:

| File | Feeds |
|---|---|
| `station_master_crd_*.csv` (63,049 × 73) | `station_ref`, `station_ref_merits`, `station_ref_flag`, and `station_ref_code` from its 16 `nap_*` provider columns — 22,134 non-empty cells, 1,540 of them `\|`-separated multi-value. (This row used to say 39,515 and 2,069: that count included `nap_station_ids`, which is not a provider column) |
| `station_links_crd_*.csv` (44,708 × 24) | `station_ref_link`, asserted and non-asserted |
| `nap_rail_stations_unmapped_crd_*.csv` (29,670 × 23) | `station_ref_link` with a null `station_id` |
| `crd_locations_*.csv` (63,049 × 65) | `crd_location` |
| the ERA telref extract | `era_operational_point` |

The importer runs as a `station_build` job in the worker, through the new `kind` branch.

**Routers** — registered in `app/main.py`:

- `app/api/admin/station_sources.py` — sources CRUD, versions, builds. `require_platform_admin`.
- `app/api/master/station_ref.py` — list, detail, overrides, complexes. `require_content_manager`.
- `app/api/master/station_registers.py` — CRD and ERA lists, version delta. `require_content_manager`.
- `app/api/master/station_links.py` — the two lists of screen A. `require_content_manager`.

**Pages** — five routes in `app/api/pages.py`, following the existing guard; four new templates
under `app/templates/admin/`, plus the existing `master_stations.html` for screen C.

**The three Trainline-panel fixes** of §4C, and the nav group of §2.

**Tests**

- unit: the importer's parsing of each file shape, multi-value cells, the `(plc, era_uopid)` grain,
  the override re-application rule, the version delta;
- integration: the migrations up and down with `REQUIRED_TABLES` extended; each page rendering for
  both roles; each JSON route refusing the wrong role; pagination headers.

**Not in step 1**: `nap_stop`, `station_ref_transfer`, any resolver, any parser of a NeTEx or CRD
XML file, any artefact, any change to the journey page.

---

## 12. What version 2 changed

| Version 1 | Why it was wrong | Version 2 |
|---|---|---|
| "import the offline CSV" | no route can accept it; both upload endpoints refuse it | a dedicated upload route |
| files stored on the inbox volume | the Storage page would offer them for deletion | a reserved folder |
| "the formats we already parse" | the repo parses GTFS `stops.txt` only | step 2 is GTFS; NeTEx/IFF readers are step 3 |
| "the worker" | it dispatches every job to a graph builder | a `kind` column on `rebuild_jobs` |
| artefacts delivered to a session | no source mode can hold a server-generated file | `server_file`, landed first |
| "OTP consolidation is a configuration file" | four touch points, none exists | listed |
| `build_id` on every reference row | a full copy per build, or no history | current-state rows plus a history table |
| screen B | had no backing table | three register tables |
| `era_uopid` nullable and "unique with plc" | a unique index does not constrain NULLs | `NOT NULL` |
| `(series, code)` is the key | 3,580 pairs map to more than one station | a lookup index, not a unique key |
| seven `CHECK` vocabularies | each extension is a migration | one lookup table; app-level validation elsewhere |
| `service_scope jsonb` | no tier, no uniqueness, no prohibition | typed columns |
| one `manual_override` boolean | cannot coexist with per-value provenance | an override table per field |
| a scalar `uic_merits` | pre-decided an open question | a candidates table |
| `flags text[]` | cannot link to the related station | a flag table |
| "every admin route uses `require_platform_admin`" | false; a dual-role gate exists and is what stations use | corrected |
| nav group copied from `Admin dashboard` | wrong role block; hides the menu from content managers | stays in the Stations block |
| typeahead exposes passenger rows | over half have no UIC | exposes routable rows |
| the port is "the builder", 4,015 lines | ~19,000 lines across 17 scripts | sized per stage |

---

## 13. What building step 1 changed

Version 2 was reviewed against the repository but never built. Each row below is a statement this
document made that the code, Postgres or the file shapes contradicted. The sections above have been
corrected; this table is the record.

### Schema

| Version 2 said | What is true | As built |
|---|---|---|
| `is_current` is a generated column with a partial index | a generation expression and an index predicate must be immutable; `CURRENT_DATE` is not | a plain boolean the build sets, partial index `WHERE is_current`. It goes stale between builds |
| GIN trigram on `alt_name` | `gin_trgm_ops` cannot index a `text[]`, and `array_to_string` is not immutable | a text column `alt_name_text`, maintained by the build, carries the index |
| `iso2_all` char(2)[] | §3's own rule is "never `char(n)`": it pads short values and rejects long ones, and the importer must keep what it does not recognise | `text[]` |
| `station_ref_code.series` is part of the key and FK'd | the master's provider columns carry no series, and one column mixes code shapes | `series` nullable, `UNIQUE NULLS NOT DISTINCT`; filled from the links file where it names the same station and code |
| `station_ref_flag` `UNIQUE (station_id, token, payload)` | with a nullable `payload` the key does not constrain bare tokens | `payload` is `NOT NULL`, `''` for a bare token |
| `station_ref_override` keeps `computed_value_at_set` only | a release after a rebuild would restore a stale value | `computed_value_latest` added |
| `complex_role` has a `CHECK` | the value set was never given | `principal`, `member` |
| "the 15 `nap_*` columns" | the header in `station-offline-file-shapes.md` lists 16 | 16 provider sources seeded; the importer takes the list from the header |
| source keys such as `NAP_CH_SBB` | the shapes document says `source_key` is the column name | `nap_CH_SBB`, verbatim |
| only `station_code_series` is seeded | the upload route needs a source to exist, and its `format` is what says which file shape it accepts | the first migration also seeds 22 `station_source` rows; kind `offline_build` added |
| Sonar counts new migrations in new-code coverage | `sonar.sources=app`; `alembic/` is outside it | corrected in §9 |
| `downgrade()` "must really drop everything" | dropping `rebuild_jobs.kind` left the station jobs in the queue. They have no session, so the worker of the previous release takes one for the legacy session-less graph job and starts an OTP build | the downgrade deletes the `station_build` jobs, finished ones included, before it drops the column |

### Storage and upload

| Version 2 said | What is true | As built |
|---|---|---|
| wrap the `detect` call "so it returns 400 rather than 500" | `detect` raises `ValueError` for what it cannot classify, but a corrupt `.zip` raises `zipfile.BadZipFile`, which is not a `ValueError` | both are caught, and the staged copy is removed |
| the upload route "writes `station_source_version`" | nothing said the file is looked at before the build refuses it | the header is checked at upload against the shape the source's `format` declares |
| `_stations` and `_staging` are the folders in the trap | `inbox/_phase1/`, the legacy session-less inbox, is offered for deletion the same way | left as it is — not asked for, and it holds no station data; noted for a decision |
| unique `(source_id, sha256)`, "so re-uploading the same file is a no-op" | a kept file is named `<sha256 prefix>-<name>`: two uploads of the same bytes under the same name, each past the check before the other committed, are moved to one path. The one refused on the unique key then removed "its" file, the only copy and the one the winner's row points to; re-uploading it is a no-op, so the panel could not put it back. Not reachable with the single web process of today; it is with several workers or replicas | the upload that loses removes its stored copy only when the winner's row points elsewhere, which is the same bytes under another name |

### Worker and importer

| Version 2 said | What is true | As built |
|---|---|---|
| a station build "must not provoke the worker's per-tick side effects (compose up, nginx reload, orphan-container removal)" | those live in `handle_reload_trigger()`, run after every tick whatever the job was, and act only when `.reload-trigger` exists: no job provokes them | the `kind` branch skips what a graph job itself does — the cancel watcher, the session state advance, the graph snapshot, the max-memory stop |
| a new version on a source with `triggers_rebuild` triggers a rebuild | with four files of five uploaded, that build can only be refused | a build is queued once all five inputs are present; the upload says what is missing otherwise |
| "rebuild now" arrives in step 4 | an identical re-upload is a no-op, so a build that failed for a reason outside the files could not be re-run | `POST /api/admin/stations/builds` and its button are in step 1 |
| the delta is computed "on upload … before anything is rebuilt" | the register rows are written by the importer in the worker, not by the upload request | the extract stage commits the registers before the build stage; the delta is computed on demand between two loaded versions. There is no human gate between the two stages |
| the build asserts "every flag token classified" | no classification exists, and the shapes document forbids refusing an unknown token | not asserted; `level` and `warning_code` stay NULL, the build counts distinct tokens |
| `crd_locations_*.csv` → `crd_location` | the file's grain is (PLC, operational point): a CRD location repeats on every operational point of its PLC, which the table's own unique key forbids | one `crd_location` per (country, code, start of validity); ERA-only rows yield none |
| the master's 16 provider columns → `station_ref_code` | the columns carry no series | `series` from the links file where it names the same station and the same code value, exactly one series; NULL otherwise. A series the links introduce is added to `station_code_series` |
| a station absent from a newer master | not addressed | kept, untouched, recognisable by `last_built_build_id` |
| `previous_plc` → one `station_ref_alias` row | two operational points of one PLC carry the same `previous_plc`, and `UNIQUE (alias_plc, build_id)` allows one | one alias per old PLC, the first station in key order; collisions are counted |
| aliases carry a `build_id`, one set per build | nothing removed the set of an earlier build: an old PLC withdrawn from a station, or given to another, stayed listed under the first, and a rebuild from the same files added a full copy | the build deletes every alias and writes its own set, in its transaction. A station absent from the master keeps its `previous_plc` column and has no alias |
| history is "written by the build from its own diff" | the diff covered the columns of `station_ref` only, so a build that replaced a station's codes, MERITS candidates or flags reported it unchanged and left no record | the build reads the three child tables once, compares them in memory with the rows it writes, and writes one history row per station and child table that differs (`codes`, `merits`, `flags`). A flag that only gains or loses the station it links to counts |
| the children are bulk-inserted | an ORM bulk INSERT leaves a None-valued key out and batches only consecutive rows with the same keys left: on rows whose empty cells differ, close to one statement per row | every bulk INSERT sets `render_nulls`, and every row of a batch carries the same keys, None included. A column with a server default is given a value on every row or left out of every row |
| a flag payload that is a PLC sets `related_station_id` | a PLC can carry several operational points | the one whose operational-point id is the PLC itself, else the first in byte order |
| a flag names one other station | 51 of the 694 `candidate_displaced_to` flags list 2 to 5 PLCs joined by `\|`, and a flag row has one `related_station_id` | one flag row per PLC when every part of the payload is a PLC of the file; any other payload stays whole. The list shows a token once, and the flag filter counts stations, not rows |
| each value of `uic_merits_conflict_values` is a further candidate | every item is `<code>=<labels>`, never a bare code, and on 14 of the 35 cells the code is the chosen one again | split on the first `=`: the code is the candidate, the labels are its `sources`; a code the row already carries gains the labels and is not a second candidate |
| `era_alt_name` is split on `\|` | `;` joins several names; a `\|` only occurs inside one bilingual name | split on `;`, the pipe kept; `alt_name_text` joins the names with `; ` |
| `crd_location` takes the file's `name`, `lat`, `lon` | they are the spine's joined values: on 404 rows retired in CRD they are ERA's (`name_src`, `pos_src`), 394 register rows | `name` only where `name_src` is `CRD`, `lat` / `lon` only where `pos_src` is `CRD`; NULL otherwise |
| hand corrections are re-applied by the build, "in one transaction" | the build read the reference and the corrections once and took no lock, and the correction routes wrote `station_ref` directly: a correction set or released while a build ran was overwritten or left half applied until the next build, and the two could deadlock | one transaction-level advisory lock. The build takes it exclusively, first, and reads only then; a writing route takes it shared before it reads, without waiting, and answers `409` while a build is writing |
| `station_ref`'s four MERITS columns mirror the chosen candidate | 19,018 master rows carry no MERITS code and a `uic_merits_rule` sentence saying why, with no candidate: the sentence is on the row and nowhere else. A correction replaced it with its reason, and a release, with nothing chosen, blanked it | three columns mirror: code, origin, confidence. `uic_merits_rule` is the build's own text, which a correction never writes; its reason is on the override and on the `Manual` candidate |

### Screens

| Version 2 said | What is true | As built |
|---|---|---|
| the delta lists what was "renumbered" | neither register table keeps a column saying one code replaces another | inferred: a removed and a created location with the same name at the same place, each paired once |
| the delta lists what was "renamed" | `crd_location` holds CRD's own name only, so a location CRD retires between two versions goes from its CRD name to none: every such retirement read as a rename, although CRD changed the validity only | a rename needs a name in both versions, as a move needs a position in both; a name that appears or disappears is not a rename |
| `era_operational_point.rl100` | the telref extract's 36 columns have no RL100 | the column stays NULL in step 1 |
| screen B is two lists | a CRD PLC has one row per validity period | the lists show every row; the delta takes the latest validity as the PLC's row |
| screen A lists "stops whose code contradicts the reference" | the links file records a verdict per stop (tier, asserted or not); no column says "contradiction", and nothing in step 1 compares a stop's code with the reference's codes | a matched link that is not asserted although it carries a code value. **To be confirmed by the owner** |
| screen A filters by feed | an unmatched stop lists several feeds in one cell (`A\|B`) | the filter matches any feed of the cell; the counts count the stop once per feed |
| screen A filters by country | the links file has no country column; only the unmatched stops carry one | the reference row's country for the contradictions, the stop's own for the unmatched |
| screen C: fix `page` "at the API — **not** in the template" | the panel sends `page=0` on every fresh search; with the API fix alone, no search would ever jump to its first match again | the API fix, and the panel sends `page` only when the operator asked for a page by number |
| screen C: `.hint` and `.flag` are "defined only in `journey.html`'s own style block" | true of `.flag`. `.hint` had no unscoped rule in any template or stylesheet | `.flag` moved; `.hint` defined in `_base.html`, colour only, which also mutes the hints that had no rule on the coverage and sessions pages. `journey.html` opts out with `.hint { color: inherit; }` and renders as before. **Owner to decide** whether it should |
| screen C: "promote" `.flag` | two of its four variants are below WCAG AA (4.2:1 and 3.6:1), and the Sonar gate refuses that on new CSS | the base carries AA pairs for those two; `journey.html` keeps its own two and renders as before. **Owner to decide** whether the journey page adopts the base pairs |
| screen D: a complex is "created from two or more stations" | the body's length check counts the items of the list, and the route removed repeated ids after it: one station given twice made a complex of one, and that station was then refused from any real grouping until it was ungrouped. The panel cannot send it; a direct call can | the route counts again once the repeats are gone and answers `400` below two different stations |
| screen E: "edit" a source | the dialog lists the caller's own credentials and sent every field. A source can carry a credential saved by another administrator: the select could not show it, read back empty, and saving any other change sent `credential_id: null`, which detached it | the dialog shows such a credential under its name, marked as not in the caller's list, and sends `credential_id` only when the selection changed. The `PATCH` route leaves an absent field as it is; only an explicit `null` detaches |

**Not imported from the master in step 1**, because the shapes document maps them nowhere:
`warnings`, `n_issues`, `n_warnings`, `review_links`, `nap_station_ids`, `best_match_method`,
`uic_merits_n_sources`, `uic_merits_collision`, `era_name_2022`, `era_crd_dist_m`, `crd_key_check`
and the five `crd_*` code columns (those reach `crd_subsidiary` through the CRD locations file).
From the links file: `railway_label`, `rail_served`, `modes`, `station_distance_m`,
`station_name_sim`, `spine_source`, `link_pos_src`, `station_distance_spine_m`. From the unmatched
stops: `rail_served`, `rail_repl`, `modes`, the three code columns, `review_links` and the
`nearest_*` columns other than the PLC and its distance.

**A MERITS correction** changes which candidate is chosen, never the candidates: the build's own
candidates all stay, a `Manual` one is added when the corrected code is none of them, and
`station_ref`'s `uic_merits`, `uic_merits_origin` and `uic_merits_confidence` mirror whichever is
chosen. `uic_merits_rule` does not: it stays what the build computed, under a correction and
after its release.
