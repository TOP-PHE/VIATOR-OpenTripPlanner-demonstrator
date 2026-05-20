# Provider Source Modes — design proposal

A design to let a **provider** in a session source its timetable content
in one of three explicit ways, instead of only by URL:

  1. **URL** — download from an `http(s)` endpoint (today's behaviour).
  2. **Upload** — the operator uploads a file, and it is **attached to
     that provider**.
  3. **Server file** — the provider references a file VIATOR generated and
     stored on the server itself (e.g. the SNCF cross-border GTFS produced
     by `app/gtfs_cross_border_filter.py`).

The goal is **traceability**. Today a provider card requires an `http(s)`
timetable URL, and the only way to use a local/generated file is the
separate per-session Upload form — which lands the file in the inbox with
**no link back to any provider**. The operator does the work "outside" the
provider model and loses the audit trail in the UI.

**Status**: Proposal. Phase 1 (uploads linked to a provider) is the
traceability win and is independently shippable. Phase 2 (server-file
source + cross-border-filter wiring) builds on it.
**Audience**: Platform admins, demonstrator product owners, implementers.

> Pairs with [cross-nap-federation-design.md](cross-nap-federation-design.md):
> the "corridors session" there wants to ingest the VIATOR-generated
> cross-border GTFS. The **server-file** source mode below is the clean UI
> path for doing exactly that — a corridors provider points at the
> filter's output instead of an external URL.

---

## 1. Motivation

### 1.1 The problem

A provider card requires an `http(s)` timetable URL
(`readProviderCard`, `app/templates/admin/sessions.html`):

```
if (!ttUrl) { showToast('error', `${id}: timetable URL is required for routing`); return null; }
```

So content that isn't behind a URL — a file on the operator's laptop, or a
file VIATOR itself generated (SNCF-XB) — cannot be expressed as a provider.
The workaround is the per-session Upload form
(`POST /api/sessions/{sid}/uploads`, `app/api/admin/sessions.py`), which:

  - detects the format, then calls `ingestion.dispatch()` **without** a
    `staged_filename`, so the file lands at the generic
    `inbox/<sid>/gtfs/gtfs.zip` rather than a provider's own slot;
  - records an `Upload` row (`app/models/ingestion.py`) with **no provider
    reference**.

Net effect: the file is in the graph, but the UI cannot answer "which
provider is this, and where did its data come from?"

### 1.2 The key finding

The data model already half-anticipates this. `_validate_provider`
(`app/ingestion.py`) accepts an **empty** timetable URL:

```
f"providers[{index}].timetable.url={url!r} must be an http(s) URL "
"(or empty if the operator will upload manually)"
```

And `ingestion.dispatch()` already accepts a `staged_filename` override so
a file can be placed at a provider's per-feed slot
`inbox/<sid>/gtfs/<feed_id>.zip` (multi-feed refresh uses this today). So
the back-end plumbing for "land an uploaded file at a provider's slot"
exists; the upload endpoint just never passes it, and the front-end blocks
the empty-URL case.

This means Phase 1 is mostly **wiring + one migration + UI**, not new
infrastructure.

---

## 2. Proposed model

Make the content source an explicit, discriminated property of each
provider's `timetable`:

| `source`      | provider stores                    | how content reaches the inbox slot                          |
|---------------|------------------------------------|-------------------------------------------------------------|
| `url`         | `url` (+ optional `credential_id`) | Refresh downloads it (today)                                |
| `upload`      | `upload_id` (last file for it)     | Upload endpoint lands it at `<feed_id>.zip`                 |
| `server_file` | `server_path` (allow-listed)       | Build/refresh copies the generated artifact into the slot   |

The inbox slot is unchanged: every provider resolves to
`inbox/<sid>/gtfs/<feed_id>.zip` (or `netex/` for NeTEx), so the OTP build
config generator is untouched — it still scans the subdir and assigns one
feedId per file.

**Back-compat (no migration of existing config needed):**
  - `timetable.url` present, no `source` → treat as `url`.
  - `timetable.url` empty, no `source` → treat as `upload` (pending a file).

`mct_url`, `stations_csv_url`, and `gtfs_rt.*` remain URL-only for now
(rarely local; out of scope — see §8).

---

## 3. Data-model changes

### 3.1 Provider schema (`app/ingestion.py` `_validate_provider`)
Add optional `timetable.source ∈ {url, upload, server_file}`:
  - `url`: existing `url` validation (must be `http(s)` or empty).
  - `upload`: optional `upload_id` (UUID of an `uploads` row); no URL.
  - `server_file`: required `server_path`, validated to live **inside an
    allow-listed root** (§7) and to exist.
Infer `source` when absent per the back-compat rules above.

### 3.2 `uploads` table (`app/models/ingestion.py`)
Add a nullable `provider_feed_id: str | None` column linking an upload to
the provider it satisfies (the OTP feedId, e.g. `SNCF-XB`). Alembic
migration required (additive, nullable — safe online). The existing
`stored_path` + `sha256` + `size_bytes` + `created_at` already give the
provenance the card needs to render a "current file" chip.

> Note: `Upload.session_id` carries a `FIXME(step-7): NOT NULL once the
> sessions UI lands`. The UI has landed; tightening it is a candidate
> clean-up but is **not** part of this work.

---

## 4. API changes

### 4.1 Upload endpoint (`upload_to_session`, `app/api/admin/sessions.py`)
Add an optional `provider_id` form field. When present:
  - resolve the provider, derive `fmt`, and call
    `dispatch(..., staged_filename=staged_filename_for_format(provider_id, fmt))`
    so the file lands at `<feed_id>.zip` (not generic `gtfs.zip`);
  - persist `Upload.provider_feed_id = provider_id`;
  - set the provider's `timetable.source = "upload"` and remember the
    `upload_id` (so the card can show the current file and the build knows
    the slot is satisfied without a URL).
  - keep the existing `detect()` format-match guard (an uploaded GTFS must
    detect as GTFS, etc.).

### 4.2 Generated-artifacts list endpoint (Phase 2)
`GET /api/generated-files` (session-agnostic — artifacts are shared, §10.1)
returns the allow-listed VIATOR-generated artifacts with provenance
metadata (filename, kind, size, sha256, generated-at, source description).
Feeds the server-file dropdown.

---

## 5. Ingestion / refresh changes

`_build_refresh_tasks` (`app/api/admin/sessions.py`) currently builds a
download task per provider URL. Change:
  - `source == "url"` → download as today.
  - `source == "upload"` → **skip** (the file is already at the slot, or
    the slot is pending; no URL to fetch — do not error).
  - `source == "server_file"` → copy the referenced artifact into the slot
    (so a rebuild always reflects the current generated file).

Staleness/freshness pills (`app/templates/admin/sessions.html`, the
`ok / stale / pending / error` pills) extend to:
  - `upload`: `ok` if a file is present at the slot, `pending` if not.
  - `server_file`: `ok` if the artifact exists and was copied, `error` if
    the referenced path is missing.

---

## 6. Front-end changes (`app/templates/admin/sessions.html`)

Per provider card (`providerCardHTML` / `readProviderCard` /
`populateProvidersList`):
  - A **source toggle** (segmented control / radio): `URL · Upload · Server file`.
  - `URL` → today's URL input.
  - `Upload` → file picker + a "current file" chip (filename, size, sha
    prefix, uploaded-at) sourced from the linked `Upload` row; the picker
    POSTs to the upload endpoint with `provider_id`.
  - `Server file` → a dropdown populated from §4.2.
  - Relax validation: instead of "URL required", require that the **chosen
    source is satisfied** (non-empty URL for `url`; a present file for
    `upload`; a selection for `server_file`).

---

## 7. Security — `server_file` path allow-list

`server_path` is operator-supplied and resolves to a filesystem read, so it
**must** be constrained:
  - Resolve against the fixed shared root (`settings.generated_dir` =
    `data/generated/`, §10.1) and reject anything that escapes it after
    `Path.resolve()` (no `..`, no absolute paths outside the root, no
    symlink escape).
  - Only expose files VIATOR itself wrote there; never accept an arbitrary
    server path from the client.

This is the one genuinely new risk surface; everything else reuses
existing, already-reviewed code paths.

---

## 8. The SNCF-XB end-to-end (server_file in action)

1. The cross-border filter (`app/gtfs_cross_border_filter.py`) writes its
   output into the shared `data/generated/` dir (§10.1) with provenance
   (source feed, filter version, kept-routes stats from `CrossBorderStats`).
2. `GET /generated-files` lists it as e.g.
   *"SNCF cross-border — 61 rail routes, generated 2026-05-20."*
3. A corridors-session provider picks it as its **server-file** source.
4. Rebuild copies it into the provider's slot; the card shows exactly which
   generated artifact (and its stats) the provider is serving.

This closes the loop the operator hit manually with SNCF-XB, with full UI
traceability.

---

## 9. Phasing

- **Phase 1 — uploads linked to a provider.** §3.1 (infer source), §3.2
  (`provider_feed_id` + migration), §4.1 (upload endpoint), §5 (skip
  refresh for `upload`), §6 (toggle with URL + Upload only). Delivers the
  traceability win on its own; no new security surface.
- **Phase 2 — server-file source.** §3.1 (`server_file` validation), §4.2
  (list endpoint), §5 (copy on refresh), §6 (dropdown), §7 (allow-list),
  §8 (filter writes to the generated dir with provenance).

Each phase is one reviewable PR (matching the project's one-concern-per-PR
norm), gated by CI + a release tag.

---

## 10. Resolved decisions

1. **Generated-file storage: shared.** Artifacts live in a single
   session-agnostic root (`data/generated/`, surfaced as
   `settings.generated_dir`), so SNCF-XB is built once and referenced from
   several corridors sessions. The list endpoint (§4.2) is therefore
   session-agnostic. A retention/GC story is still needed (deferred —
   §11).
2. **Re-upload: rotate.** Replacing an `upload` provider's file rotates the
   prior file to `.old` (today's `dispatch()` behaviour) and writes a new
   `Upload` row. The current file lives in the slot; full history stays in
   the `uploads` table.
3. **Source granularity: `timetable` only.** `mct_url`, `stations_csv_url`,
   and GTFS-RT stay URL-only until there's demand.

---

## 11. Out of scope

- Local `mct_url` / `stations_csv_url` / GTFS-RT (URL-only for now).
- Retention / garbage-collection of the shared `data/generated/` dir (it
  grows unbounded as artifacts are regenerated; a cleanup policy — keep-N,
  age-out, or reference-counting against providers — is a follow-up).
- Tightening `Upload.session_id` to NOT NULL (separate clean-up).
- Any change to the OTP build-config generator (the inbox slot contract is
  unchanged).
