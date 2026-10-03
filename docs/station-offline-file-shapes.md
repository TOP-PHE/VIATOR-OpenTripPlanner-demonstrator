# Offline station files — shapes for the step 1 importer

The five files that `docs/station-panel-design.md` §11 imports as build #1. They are produced by
the offline mapping chain and are **not in this repository** — only their shape is. Headers and
value vocabularies below were read from the real files on 2026-10-03; the example rows are
**invented** (PLC prefix `ZZ` does not exist) and carry no real data.

All five: UTF-8 **with BOM**, RFC 4180, comma-separated, header on the first line. Read every column
as text — several look numeric and are not (a leading zero is significant in a code).

---

## Cell conventions

| Separator | Meaning | Where |
|---|---|---|
| `\|` | several values of the same series | every `nap_*` column, `eva_all`, `nap_station_ids`, `review_links`, `uic_merits_conflict_values` |
| `;` | several tokens | `flags`, `warnings`, `op_type_all`, `iso2_all` |
| `+` | several source labels | `uic_merits_sources`, `eva_src` |
| `#` | feed label, then the feed-local stop key | inside `nap_*_regional` values, e.g. `ES_FGC#MC` |
| `:` | token, then payload | inside `flags`, e.g. `candidate_displaced_to:ZZ00002` |

An empty cell means "no value", never zero. Anything the importer does not recognise is kept as
opaque text, not rejected — the offline vocabulary grows between issues.

---

## 1. `station_master_crd_*.csv` — 73 columns, 63,049 rows

→ `station_ref`, `station_ref_merits`, `station_ref_flag`, `station_ref_code`, `station_ref_alias`.

```
plc,era_uopid,era_name,era_alt_name,op_type_all,is_passenger,plc_kind,iso2,iso2_all,lat,lon,position_flag,n_op_with_plc,rl100,nat_code,nat_code_series,nat_code_src,ifopt_dhid,ifopt_dhid_src,eva,eva_src,eva_all,uic_merits_candidate,uic_merits_check_digit,uic_merits_sources,uic_merits_n_sources,uic_merits_confidence,uic_merits_conflict_values,uic_merits_collision,uic_merits,uic_merits_origin,uic_merits_rule,nap_AT_OEBB,nap_BE_SNCB,nap_CH_SBB,nap_CH_SBB_non_rail_members,nap_CZ_CZPTT,nap_DE_DELFI,nap_ES_OUIGO,nap_ES_RENFE,nap_ES_regional,nap_EUROSTAR,nap_FR_SNCF,nap_FR_TRENITALIA_FR,nap_FR_regional,nap_IT_TRENITALIA,nap_LU,nap_NL_IFF,n_nap_feeds,nap_station_ids,best_match_method,best_tier,review_links,flags,warning_level,warnings,n_issues,n_warnings,spine_source,previous_plc,crd_rl100,crd_sncf_codes,crd_ns_abbrev,crd_sbb_enee,crd_dium_codes,era_name_2022,era_crd_dist_m,name_src,pos_src,op_type_src,crd_source_tag,link_pos_src,crd_key_check
```

**Key**: `(plc, era_uopid)`. `plc` alone is not unique — 314 PLCs carry more than one operational
point. `era_uopid` is never empty in this file.

**PLC shape**: always 7 characters, but not always two letters and five digits. Three other kinds
exist (`plc_kind`): `era_internal_eu` (89 rows, prefix `EU`, border points), `uic_numeric_hu`
(9 rows, seven digits) and `placeholder` (1 row). Validate on length only.

**Vocabularies**

| Column | Values |
|---|---|
| `is_passenger` | `yes` · `no` |
| `plc_kind` | `national` · `era_internal_eu` · `uic_numeric_hu` · `placeholder` |
| `position_flag` | `ok` · `location_resource_empty` · `implausible` · `implausible_plausible_if_swapped` |
| `spine_source` | `CRD_and_ERA` · `CRD_only` · `ERA_only` · `ERA_retired_in_CRD` |
| `uic_merits_origin` | empty · `Trainline` · `Trainline = calculated` · `Calculated` · `Trainline (calculated differs)` |
| `uic_merits_confidence` | empty · `high` · `medium` · `low` · `conflict` |
| `warning_level` | `OK` · `INFO` · `WARNING` · `ISSUE` |
| `name_src` | `CRD` · `ERA` |
| `pos_src` | `CRD` · `ERA` · `none` |
| `op_type_src` | `ERA` · `CRD_derived` |
| `nat_code_series` | empty · `CZ_SR70_6digit` · `CH_service_point_number` · `OeBB_DB640_abbreviation` · `NS_station_abbreviation` |
| `best_tier` | 20 values, free-form prefix `T0_` … `T5_`, plus `none` — store as text |
| `crd_source_tag` | a constant licence tag on CRD-derived rows; empty on `ERA_only` rows |

**Mapping**

- `era_name` → `station_ref.name`; `era_alt_name` → `alt_name` (split on `|`).
- `lat`, `lon` are empty where `pos_src` is `none` (9,014 rows) — nullable.
- `iso2_all`, `op_type_all` → arrays, split on `;`.
- `eva_all` → array, split on `|`.
- `previous_plc` → `station_ref.previous_plc` and one `station_ref_alias` row.
- The MERITS group → one `station_ref_merits` row per distinct code: `uic_merits` is the chosen
  one; `uic_merits_candidate` is the calculated one and may differ (it does on the 18 rows whose
  origin is `Trainline (calculated differs)`); each value in `uic_merits_conflict_values` is a
  further non-chosen row.
- Each of the 15 `nap_*` provider columns → `station_ref_code` rows, one per `|`-separated value.
  `source_key` is the column name. `nap_station_ids` is **not** a provider column — it lists the
  offline station ids the row is linked to.
- `flags` → one `station_ref_flag` row per `;`-separated token; split each on the first `:` into
  token and payload. When the payload is a PLC present in this file, set `related_station_id`.
- `n_nap_feeds`, `best_tier`, `warning_level` → the columns of the same name.

**Example** (invented):

```
plc,era_uopid,era_name,…,uic_merits,uic_merits_origin,…,nap_CH_SBB,…,flags,warning_level,…,spine_source
ZZ00001,ZZ00001,Exampleville Central,…,9900001,Trainline = calculated,…,9900001|9900001:0:1,…,candidate_displaced_to:ZZ00002;plc_kind_national,INFO,…,CRD_and_ERA
```

---

## 2. `station_links_crd_*.csv` — 24 columns, 44,708 rows

→ `station_ref_link`, asserted and non-asserted.

```
plc,era_uopid,station_id,feed,railway_label,stop_key,stop_name,code_value,code_series,match_method,tier,asserted,distance_m,name_sim,rail_served,modes,station_distance_m,station_name_sim,station_label,note,spine_source,link_pos_src,station_distance_spine_m,crd_source_tag
```

`(plc, era_uopid)` resolves the reference row. `station_id` here is the **offline** NAP station id
(`NAPST…`), not a database id — store it as `offline_station_id`.

| Column | Values |
|---|---|
| `asserted` | `yes` (26,360) · `no` (18,348) |
| `tier` | 21 values, prefix `T0_` … `T5_` — text |
| `code_series` | `DELFI_stop_key` · `CH_service_point_number` · `SNCF_8digit` · `feed_local` · `PLC` · `Renfe_5digit` · `Trenitalia_9digit` · `SNCF_8digit_in_feed_key` · `OeBB_NAP_stop_key` · `SNCB_quay_root_7digit` · `NS_station_abbreviation` · `LU_CdT_stop_number` · `Eurostar_stop_code_intl7` · `Ouigo_ES_9digit` |
| `match_method` | free text, 6,544 distinct values — text, never a vocabulary |
| `rail_served` | `1` · `0` |

**Seed `station_code_series` with these 14 keys verbatim.** They are the offline vocabulary; mapping
them onto coarser families (`uic_intl`, `eva`, …) is a later normalisation, not an import-time
decision.

---

## 3. `nap_rail_stations_unmapped_crd_*.csv` — 23 columns, 29,670 rows

→ `station_ref_link` with a null `station_id`: stops no reference row could be matched to.

```
station_id,iso2,name,lat,lon,rail_served,rail_repl,label,modes,feeds,uic_intl_all,uic_intl_nl_chb,eva_delfi,reason,review_links,nearest_era_plc,nearest_era_name,nearest_era_op_type_all,nearest_era_distance_m,nearest_spine_source,nearest_name_src,nearest_pos_src,crd_source_tag
```

`label` is `Urban` (25,235) · `Rail` (3,141) · `Multimodal` (916) · `unknown` (378). Screen A's
"unmatched" list should default to `Rail` and `Multimodal` — the urban rows are tram and bus stops
that were never expected to match a railway location.

`nearest_era_plc` and its distance are the hint an operator works from; keep them on the link row.

---

## 4. `crd_locations_*.csv` — 65 columns, 63,049 rows

→ `crd_location`. This is the spine as the offline extractor writes it, already joined with ERA.

```
plc,iso2,uopid,era_uri,name,op_type,is_passenger,lat,lon,n_plc_on_op,n_op_with_plc,plc_prefix,plc_kind,iso3,era_iso2_table,iso2_all,n_countries,op_type_all,n_op_types,is_passenger_src,alt_name,n_names,position_flag,lat_dp,lon_dp,location_uri,n_locations,plc_op_max_sep_m,plc_op_names_agree,net_element,n_line_refs,line_ids,line_ids_src,n_tracks,n_sidings,local_rules_or_restrictions,spine_source,crd_country,crd_location_code,crd_start,crd_end,crd_passenger_flag,crd_freight_flag,crd_responsible_im,crd_nuts,crd_rl100,crd_sncf_codes,crd_sncf_site_codes,crd_ns_abbrev,crd_sncb_telegraph,crd_sbb_enee,crd_dium_codes,previous_plc,era_name,era_lat,era_lon,era_crd_dist_m,name_src,pos_src,op_type_src,era_crd_passenger_disagree,crd_source_tag,crd_position_issue,era_position_alternative,crd_key_check
```

The first 36 columns are identical to file 5, by design. `plc_op_max_sep_m`, `n_op_with_plc`,
`is_passenger_src` and `crd_start` / `crd_end` are needed by `station_ref` and are **not** in file 1
— read them from here, joined on `(plc, uopid)`.

The subsidiary codes are flattened into `crd_rl100`, `crd_sncf_codes`, `crd_sncf_site_codes`,
`crd_ns_abbrev`, `crd_sncb_telegraph`, `crd_sbb_enee`, `crd_dium_codes`. In step 1 the importer
writes one `crd_subsidiary` row per non-empty value, with the column name as `subsidiary_type`; the
real subsidiary table arrives with the XML parser in step 4a.

---

## 5. `telref_locations_v3.csv` — 36 columns, 37,112 rows

→ `era_operational_point`.

```
plc,iso2,uopid,era_uri,name,op_type,is_passenger,lat,lon,n_plc_on_op,n_op_with_plc,plc_prefix,plc_kind,iso3,era_iso2_table,iso2_all,n_countries,op_type_all,n_op_types,is_passenger_src,alt_name,n_names,position_flag,lat_dp,lon_dp,location_uri,n_locations,plc_op_max_sep_m,plc_op_names_agree,net_element,n_line_refs,line_ids,line_ids_src,n_tracks,n_sidings,local_rules_or_restrictions
```

---

## What the importer must refuse

- a file whose header does not match one of the five above — report which columns are missing or
  unexpected, do not guess;
- a master file in which a `(plc, era_uopid)` pair repeats;
- a build whose inputs are not all five files — partial imports leave screens silently empty.

And what it must **not** refuse: an unknown `tier`, `flag` token, `match_method` or
`nat_code_series`. Those are stored as text.
