# NAP feed resolvers — automated download of portal-published feeds

**Status:** shipped (this PR). **Audience:** operators and implementers.
**Code:** `app/feed_resolvers.py` (which file is current), `app/feed_fetch.py` (download it safely),
`_refresh_one_task` in `app/api/admin/sessions.py` (glue).

## Why

26 of the eu19 session's timetables were downloaded by hand from national access points and
uploaded as files (`source: "upload"`), because their download URL is not a fixed address. Each
portal names its current file differently, and the files go stale within days to weeks. This
adds a fourth timetable source, `"nap"`, whose provider stores a **resolver** instead of a URL.
Every refresh then:

1. **resolves** the current file URL from the portal (`feed_resolvers.resolve`). The SSRF guard
   checks whatever the portal's JSON returns, and **every redirect hop** of the catalogue lookup
   and the download (`feed_resolvers.redirect_guard`, `nap` providers only);
2. **downloads it conditionally**, replaying the previous ETag / Last-Modified. `304` means
   *unchanged*;
3. runs a **format check before touching the slot**: zip / gzip / PBF magic bytes, then
   `detect.detect` must agree with the declared format (either NeTEx profile satisfies the
   other: both go to `netex/`). An `.xml.gz` NeTEx is re-wrapped as a zip. CSV kinds (MCT,
   stations) refuse an empty body, JSON, or HTML (even behind a BOM). A corrupt archive is a
   rejected file, not a server error. **This is a format check only.** It proves the file is the
   right *kind* of archive, not that its content is correct. UI text must say "format OK", never
   "validated";
4. treats a **sha256 match** with the previous download as *unchanged* too. This covers servers
   with no validators (OURA) and servers whose ETag flaps between backends (Renfe);
5. **retries** transport errors and 429/5xx twice (2 s, 10 s).

An *unchanged* file is not rotated and queues no rebuild. The refresh response and audit carry it
in a separate `unchanged` list. A failed or rejected download leaves the previous file in the
slot. The fetch pipeline (steps 2–5) applies to **every** URL download, not only `nap` providers:
a URL provider serving an HTML landing page is now refused instead of being staged as `<feed>.zip`.
Failure reasons never include the fetch URL, because a credential may be embedded in it.

Per-task state lives in `inbox/<sid>/_fetch_state/<task>.json`, outside the directories the builds
glob. A manual upload deletes the state of **every** task that writes the slot it replaced
(whatever the provider's current source, and both NeTEx formats), so no later refresh can call
the uploaded file "unchanged". MCT and stations CSVs share one DB-load slot per kind across all
providers, so they are always downloaded in full; a 304 or hash match is never trusted for them.
Freshness pills measure age from the later of the file's mtime and the last *unchanged* check.
Staging files carry a random suffix, so two overlapping refreshes of one session cannot collide.

## Feed status panel

Each session's detail panel opens with **Feed status**, a read-only table with one row per provider
timetable. Rows are grouped by country, and the **Source** menu filters by source mode (URL, NAP
resolver, Upload, Derived). Each row shows:

- the file in the slot (size and age, same pill as the provider card);
- the last check (when a refresh last confirmed the file current);
- the last result (`fetched`, `unchanged` or `skipped`). A skip shows its **full reason**, so
  "click Refresh to see why" is no longer needed;
- **format OK** when the file passed the format check on its way in (a checked download, or an
  upload). "not checked" means a derived feed, a file placed by hand, or a file from before the
  check existed.

Wording rule: the panel says "format OK", never "validated". Only the format is checked, not the
timetable content. A trip-wire test (`tests/unit/test_feed_status_panel.py`) enforces it.

The data comes from `GET /api/sessions/<sid>/providers/status`, which now also returns `label`,
`country_iso`, `format`, `resolver_type`, `checked_at`, `last_attempt` and `format_ok`. Every
refresh task writes its outcome to `last_attempt` in its fetch-state file. Derived
(cross-border filter) feeds have no fetch state, so their last result stays empty.

## Resolver types

| `type` | Fields | Resolution | Used for |
|---|---|---|---|
| `tdg` | `dataset_id` (24-hex), `resource_id` (int) | Checks the resource is still in `GET /api/datasets/<id>` and `is_available`, then uses `https://transport.data.gouv.fr/resources/<id>/download`. A vanished id is an error that lists the candidates. **It never picks a replacement silently** | 15 FR feeds |
| `udata` | `api`, `dataset_id`, `title_regex` | Newest resource (by `created_at`) whose title matches | LU (data.public.lu) |
| `permalink` | `url`, optionally containing `{timetable_year}` | Placeholder substituted; the server redirects to the newest file | CH (opentransportdata.swiss) |
| `dated` | `url` with `{date}` (YYYYMMDD), `max_days_back` (1–60, default 21) | Walks back day by day with a 4-byte ranged GET until a zip answers. Only a 404/410, or a 200/206 that is not a zip, steps back a day. Any other status (401, 403, 5xx) or a network error stops the walk and reports the real error | DE (DELFI, published Mondays) |
| `json_api` | `url` (may hold `{timetable_year}`), `items` (dotted path to the entries; lists are flattened at every level), `match` (field path → regex, all must match, up to 5), optional `sort` (field path; the highest wins), and **one of** `download` (URL template, `{field.path}` placeholders, same host as `url`) or `url_field` (field holding the file URL, must be on the same host) | Fetches the JSON catalogue, keeps the entries matching every regex, picks the highest `sort` value — or refuses if several match and there is no `sort` — then builds the file URL. Each flattened entry carries `_parent`, so a file can be matched on its dataset (`_parent.name`). **The only type whose lookup carries the provider's credential** | AT, ES (portals that need an account) |

`{timetable_year}` is the European timetable year in force today. It switches on the Sunday after
the second Saturday of December (2026-12-13 is the first day of 2027).

Config example (`sources.providers[]`):

```json
{"id": "CFL", "label": "CFL Luxembourg", "country_iso": "LU",
 "timetable": {"format": "netex_epip", "source": "nap",
   "resolver": {"type": "udata", "api": "https://data.public.lu/api/1",
                "dataset_id": "56fbd4e5855e9b6a1088f54e",
                "title_regex": "^netex-\\d{8}-\\d{8}\\.zip$"}}}
```

In the admin UI the provider card's **Source** menu has a "NAP resolver" option taking the
resolver as JSON.

## Portals that need an account

A provider's timetable can carry a credential (`timetable_credential_id`), chosen in the provider
card's **Credential** menu for the *URL* and *NAP resolver* sources. The secret itself is entered
only on the **/credentials** page, stored encrypted (AES-256-GCM), and never shown again.

| Scheme | Stored | Sent |
|---|---|---|
| `header`, `query`, `bearer`, `basic` | the key / token / `user:pass` | as is, on every request |
| `oauth2_password` (*Login*) | `{token_url, client_id, username, password[, scope]}` | an OAuth2 password grant is posted to `token_url` first; the access token goes out as `Authorization: Bearer`. Tokens are cached in-process until 30 s before they expire, so one refresh logs in once |

The credential is applied to the `json_api` catalogue lookup and to the download. The other
resolver types query public catalogues and never see it. Safety rules:

- the token URL, the lookup and every redirect hop pass the SSRF guard **before** anything is
  signed; the token request never follows redirects (that would re-post the password);
- a `json_api` file URL must be on the catalogue's host, so the credential cannot be carried to a
  host the operator did not configure;
- error messages name the HTTP status and the OAuth2 `error` field only — never the password,
  the token, or a signed URL's query string;
- a refused login, a deleted credential, or an undecryptable one (JWT_SECRET rotated) fails that
  one task with the reason; the previous file stays.

A login credential cannot be used where only a static header is possible: the OTP GTFS-RT
router config leaves that updater anonymous (and logs it), and NAP catalogue imports refuse it.
**Test login** on /credentials runs the exchange immediately, so a wrong password shows there
rather than at the next refresh. It checks the login only: whether the portal then serves a
given file is decided by the data server, and shows as an HTTP 401 on refresh.

**Presets** on /credentials fill the fixed, public parts:

- **Austria** (data.mobilitaetsverbuende.at): *Login*, token URL
  `https://user.mobilitaetsverbuende.at/auth/realms/dbp-public/protocol/openid-connect/token`,
  client id `dbp-public-ui` (the portal's web client). **Not** `dbp-script-download`: that
  client logs in, but `/data-sets/{id}/{year}/file` answers 401 to its token (probed
  2026-10-01; the catalogue is public, so a lookup succeeding proves nothing about the token).
  The account must have accepted each dataset's licence on the portal. The provider's documentation page is titled "only until November 22nd": the API may
  change after that date; re-run the probe if lookups start failing.
- **Spain** (nap.transportes.gob.es): *Custom header* `ApiKey`, the key from the portal's
  account page. Catalogue `GET /api/Fichero/GetList`; file `GET /api/Fichero/download/{id}`.
- **Belgium**: no credential needed for the SNCB/NMBS NeTEx (public blob URL); see "Still manual".

**Finishing a `json_api` config.** The portals block the development cloud, so the field names
were not probed. On the VPS run `python3 scripts/probe_nap_api.py at` (or `es`). It prompts for the
login or key without echoing it, and prints only the JSON shape and the entries matching OBB /
OUIGO / Iryo — no secret. Those field names go into `items`, `match`, `sort` and `download`.

## eu19 feed map (probed live 2026-09-29)

`app/data/eu19_nap_sources.json` holds the replacement for 25 of the 26 uploaded feeds, plus
TRENORD (added 2026-10-01). It lives under `app/` because the web image copies only `app/`.

**The map carries no credential.** For the entries that need one (OBB, the ES feeds) switching sets
the source only: select the credential on the provider card, save, then refresh. A credential
already selected on the card is kept.

**Switching a session to it, per country.** In the session's detail panel, **Automated NAP
sources** lists every provider that has an entry in the map, grouped by country:

- *can switch*: the provider still uses another source;
- *automated*: it already uses exactly the mapped source;
- *format differs*: the session and the map disagree on the format (GTFS vs NeTEx). It is never
  switched automatically; check it by hand.

Tick countries, then **Switch selected countries**. This is `POST /api/sessions/<sid>/nap-sources/apply`
(platform admin, like a config save). It changes the configuration only, with the same validation,
staleness flag and audit trail as a config save. The audit row (`session.nap_sources.applied`)
keeps each replaced timetable, so a switch can be undone by hand. Nothing is downloaded: click
**Refresh providers** next. Every current file stays until its replacement passes the format
check. Switching one country at a time lets you refresh and check it before moving on.

`scripts/switch_to_nap_sources.ps1` does the same for all countries at once, from a PC (dry run by
default; `-Apply` to save).

| Feeds | Source | Notes |
|---|---|---|
| 15 FR (BREIZHGO … TRENITAL-FR) | `nap/tdg` | 4 datasets have look-alike neighbours and are pinned by resource id: IDFM 80921 (not the Google/ITO rewrites 80931/83316), ZOU 83990 (not the Transdev "zou" dataset), FLUO 83635, ALEOP 80721. Never store the publisher's `original_url`: it rotates (ATOUMOD) or embeds an API key (LIO). HEAD is unreliable on tdg, so it is never used |
| FGC 1373, EUSKOTREN 1062, RENFE-AVLD 897, RENFE-CERC 929, OUIGO-ES 1515 | `nap/json_api` | ES NAP catalogue `GetList` (about 10 MB, mostly base64 logos), file `download/{ficheroId}`. Matched on the dataset id (`_parent.conjuntoDatoId`) and `tipoFicheroNombre` `^GTFS-ZIP$` — a looser `^GTFS` would also match `GTFS RT`. **Needs the ES `ApiKey` credential selected on the provider card.** Before 2026-10-01 the first four pointed at the operators' own URLs (same files); switched so every ES feed comes from the NAP. Iryo is not on the ES NAP |
| TRENITALIA | `url` | Italian NAP public catalogue, asset 1080596, `/checkedResource` = the last *validated* version. It serves `.xml.gz` with no validators, so change detection relies on the hash. **This corrects eu19-providers.md, which says CCISS is SPID-walled** |
| TRENORD | `url` | Italian NAP dataset IT-ITC4-TRENORD_336 (NeTEx, Italian profile level 1, Trenord only), asset 131494 `/checkedResource`. Anonymous, like TRENITALIA. The session declares GTFS (dati.lombardia.it), so the panel shows *format differs*: switch it by hand |
| OBB | `nap/permalink` | Dataset 67, "Railway Timetable Data (NeTEx) - Current Reference Data" (not 71, the changeover snapshot; not 66, GTFS). **Needs the AT *Login* credential** with client id `dbp-public-ui`, and the dataset licence accepted on the portal. The catalogue is public, so only the file request tests the token |
| SBB | `nap/permalink` | `timetablenetex_<year>/permalink` redirects to a 60-second presigned R2 URL; never store it. About 660 MB, new file roughly twice a week. The CKAN API is blocked (403) or needs a key; the permalink needs neither |
| CFL | `nap/udata` | Publisher is ATP (national multimodal), CC0. The local `netex-20260618-20260823.zip` is byte-size identical to this dataset's resource, which settles eu19-providers.md's "cannot be traced" |
| DB | `nap/dated` | `YYYYMMDD_fahrplaene_gesamtdeutschland.zip`, about 2 GB. No `latest` alias; old files are pruned. The file URL answers anonymously, but the dataset page states download is for registered users. **Register once on opendata-oepnv.de before relying on this** (CC-BY). Refresh at most weekly |

### Still manual (not in the map)

| Feed | Blocker | Next step |
|---|---|---|
| IRYO (new) | Not on the ES NAP | Find another source (e.g. a Transitous feed) |
| NMBS (BE) | The stable blob URL serves a **different export** (enRoute, 2,184 files, 7.8 GB uncompressed) from our local file. Also the licence is marked non-commercial, and our local file expired 2025-12-13 | Decide on licence; test the loader against the new structure (or use the anonymous GTFS feed) |

## Invariants & traps

- **Never HEAD** a NAP download URL. tdg answers 404 or "200, length 0", and R2 presigned URLs
  refuse HEAD. The `dated` resolver uses a 4-byte ranged GET and stops after one chunk, because
  some servers ignore Range.
- **Replay the ETag verbatim.** LIO's server sends it unquoted. Quoting it defeats the 304.
- **A resolver error, rejected file or failed download never empties a timetable slot.** The
  previous file stays until a replacement has passed the format check. A 304 or hash match is
  only trusted when the slot still holds this task's file.
- **OSM is the exception.** `POST /sources/osm/refresh` rotates `osm.pbf` to `osm.pbf.old.1`
  *before* downloading, so a failed OSM refresh does leave the slot empty (the previous file is
  recoverable from `.old.1`). That also means an OSM refresh is always a full download.
- Fetch state is keyed by the task label (`provider[<ID>].timetable(<fmt>)`). Renaming a provider
  id costs one full re-download, nothing more.
