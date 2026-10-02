# Extending eu19-transit-motis to the rest of Europe

Research 2026-10-02 for the countries eu19 does not yet cover, and what was
put in `app/data/europe_extension_providers.json`. Same rule as
`docs/eu19-providers.md`: a feed should be discoverable on the country's
MMTIS National Access Point. Where no official feed exists the operator
accepted community feeds, **provided they are clearly marked** — so every
catalogue entry carries a `provenance` and its label says it:

| provenance | meaning | label marker |
|---|---|---|
| `nap` | listed on the country's NAP | none |
| `official` | published by the operator or a government body; NAP listing not confirmed (or no NAP) | `[official, NAP unconfirmed]` / `[official, no NAP]` |
| `community` | built by volunteers from the operator's website; no official feed exists | `[COMMUNITY - not NAP, not official]` |

All entries are `source: url` — refreshed automatically, never uploaded by
hand. Add them with
`scripts/session_providers.py --session eu19-transit-motis --add all` (dry
run; `--apply` saves), then **Refresh providers**.

## In the catalogue

| Country | Id | Provenance | VPS probe 2026-10-02 |
|---|---|---|---|
| PT | CP | nap | GTFS, 174 rail routes, to 2026-12-12 |
| IE | IRISHRAIL | official | GTFS, 19 rail routes incl. Dublin–Belfast |
| FI | VR | official | GTFS, 744 rail routes, to 2027-12-31 |
| EE | ELRON | official | GTFS, 28 rail routes incl. Tallinn–Riga |
| LV | VIVI | official | GTFS, 48 rail routes |
| LT | LT-ALL | official | GTFS 65 MB, 3480 routes (73 rail) |
| HR | HZPP | official | GTFS, 138 rail routes |
| IT | ITALO | nap | gzip NeTEx — stops listed out of order upstream, check the build |
| CY | CY-INTERCITY | nap | GTFS, 60 bus routes (no rail in CY) |
| RS | BEOGRAD | official | GTFS city + suburban, 241 routes |
| AL | TIRANA | official | not probed (city transit; HSH rail publishes nothing) |
| RS | SRBIJAVOZ | community | not probed; incl. Belgrade–Bar, Subotica–Szeged |
| ME | ZPCG | community | not probed |
| MK | MZT | community | not probed |
| XK | TRAINKOS | community | not probed |

## Not in the catalogue yet

| Country | Why | Next step |
|---|---|---|
| SK | `zsr.sk/.../gtfs/gtfs.zip` returned an HTML page from the VPS | find the current ŽSR GTFS link (CC0, covers ZSSK, RegioJet, Leo Express) |
| SI | nap.si serves national rail+bus GTFS / NeTEx EPIP only after registration + OAuth2 | operator registers; then an OAuth2 resolver |
| RO | CFR + 6 private operators published as custom XML on data.gov.ro | XML → GTFS conversion step; confirm NAP listing (pna.cestrin.ro) |
| BG | BGNAP API is open; the BDZ entry may be metadata only | check the BDZ subsets; Sofia GTFS is available |
| CY, LV, GR, RO | NAPs / portals are CKAN | a `ckan` resolver would read the current file from the NAP catalogue itself |
| GB rail | only ATOC CIF, after registration | registration + CIF → GTFS converter |
| GR | Hellenic Train: XLSX from 2020–21 only | ask the operator / NAP |
| BA | ŽFBH community feed unverified, ŽRS none | — |
| MT, MD, IS | no NAP and no rail data (IS: Reykjavík bus only) | — |

## OSM

`scripts/merge_osm_eu19_corridor.sh` merges one Geofabrik extract per
country. For all of Europe, point the session's OSM source at Geofabrik's
`europe-latest.osm.pbf` instead (about the size of today's merged file). The
street-network step (`osr`) will need more memory than the 19-country file:
measure the peak of the current max-memory build first.
