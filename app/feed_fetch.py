"""Download one feed file for a refresh task — safely enough to run unattended.

Before this module the refresh path streamed whatever the server sent into
the provider's inbox slot and queued a rebuild. Three failure modes that
bit (or would bite) an automated download:

  * an HTML error / landing page served with HTTP 200 got staged as
    `<feed>.zip` and queued a rebuild (the Geofabrik stub trap,
    docs/multi-country-runbook.md footguns table);
  * an unchanged file was re-rotated and queued a 30-minute rebuild anyway;
  * a transient 503 / connection reset failed the task outright.

`fetch_validated` fixes all three:

  1. conditional GET — the ETag / Last-Modified from the previous fetch of
     the same URL are replayed; a 304 means "unchanged";
  2. retries with backoff on transport errors and 429/5xx;
  3. a format check before anything touches the slot — zip / gzip / PBF
     magic bytes, then `detect.detect` must agree with the declared kind (a
     `.xml.gz` or plain `.xml` NeTEx, as the Italian NAP serves them, is
     re-wrapped as a zip).
     This is a format check only: it proves the file is the right *kind* of
     archive, not that its content is correct;
  4. a sha256 match against the previous fetch is also "unchanged" (for
     servers without validators, or whose ETag flaps between backends).

Per-task fetch state lives in `inbox/<sid>/_fetch_state/<key>.json`, outside
the timetable subdirs every build globs.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import detect, netex_structure

log = logging.getLogger(__name__)

# Kinds whose file must be a timetable archive `detect.detect` recognises.
TIMETABLE_KINDS: frozenset[str] = frozenset({"GTFS", "NeTEx-EPIP", "NeTEx-Nordic"})
# Both NeTEx profiles dispatch into the same netex/ slot and `detect` cannot
# tell them apart reliably — either one satisfies the other.
_NETEX_KINDS: frozenset[str] = frozenset({"NeTEx-EPIP", "NeTEx-Nordic"})
# CSV kinds, loaded into the DB rather than a build slot (a CSV or a zip of CSVs).
_CSV_KINDS: frozenset[str] = frozenset({"SNCF-MCT", "SNCF-Stations"})
_UTF8_BOM = b"\xef\xbb\xbf"

_ZIP_MAGIC = b"PK\x03\x04"
_EMPTY_ZIP_MAGIC = b"PK\x05\x06"
_GZIP_MAGIC = b"\x1f\x8b"

# Seconds to wait before retry 1, 2, … — module-level so tests can zero it.
RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_STATE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# What a fetch-state file name may be once unsafe characters are replaced:
# a strict allow-list, short enough for any file system (255 bytes).
_STATE_NAME_MAX = 200
_STATE_NAME_OK = re.compile(rf"[A-Za-z0-9_.-]{{1,{_STATE_NAME_MAX}}}")
_DISPOSITION_RE = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)


# The download progress of the refresh task that runs in this context
# (app/refresh_progress.py `RefreshRun.task`): called after each chunk with
# the bytes received so far and the bytes expected (the Content-Length of a
# body that is not content-encoded, else None). The callable throttles its own
# writes; an exception in it never stops a download.
download_progress: ContextVar[Callable[[int, int | None], None] | None] = ContextVar(
    "download_progress", default=None
)


class FetchError(Exception):
    """The download failed or the file was rejected. The slot is untouched."""


@dataclass
class FetchResult:
    status: str  # "fetched" | "unchanged"
    reason: str | None = None
    path: Path | None = None  # format-checked file, ready for dispatch ("fetched" only)
    size_bytes: int = 0
    sha256: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────── fetch state ────────────────────────────


def state_path(state_dir: Path, key: str) -> Path:
    """`<state_dir>/<key with unsafe characters replaced>.json`.

    The key is a task label built from a session's provider id, i.e. operator
    input. Two checks keep the file inside `state_dir`, each in a form static
    analysers recognise (pythonsecurity:S2083), and each still holds if the
    other is ever loosened:

      1. the file name must match a strict allow-list (letters, digits, `_`,
         `.`, `-`; at most `_STATE_NAME_MAX` characters);
      2. the resolved path (symlinks followed) must sit inside the resolved
         `state_dir`.

    Raises ValueError for a name that fails either of them.
    """
    name = f"{_STATE_KEY_RE.sub('_', key).strip('_')}.json"
    if not _STATE_NAME_OK.fullmatch(name):
        raise ValueError("fetch-state name is not a plain file name")
    base = Path(os.path.realpath(state_dir))
    target = Path(os.path.realpath(base / name))
    if not target.is_relative_to(base):
        raise ValueError(f"fetch-state name escapes {base}")
    return target


def load_state(state_dir: Path, key: str) -> dict[str, Any]:
    try:
        data = json.loads(state_path(state_dir, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: Path, key: str, state: dict[str, Any]) -> None:
    """Best-effort: a lost state file only costs one full re-download.

    The new content goes to a temporary file that `tempfile` creates inside
    the resolved state directory, then replaces the target in one rename, so a
    reader never sees half a file. Both names are checked to be inside that
    directory before anything is written.
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_path(state_dir, key)
        base = target.parent
        payload = json.dumps(state, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(dir=base, prefix=".fetch-state-", suffix=".tmp")
        tmp = Path(os.path.realpath(tmp_name))
        try:
            if not tmp.is_relative_to(base):
                raise ValueError(f"fetch-state temporary file escapes {base}")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(payload)
            tmp.replace(target)
        finally:
            if fd != -1:
                os.close(fd)
            tmp.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        log.warning("could not save fetch state %s: %s", key, exc)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ──────────────────────────── format check ────────────────────────────


def _describe_head(head: bytes) -> str:
    text = head.lstrip()[:15].lower()
    if text.startswith((b"<!doctype", b"<html", b"<?xml", b"<")):
        return "an HTML/XML page, not a file (landing page or error served with HTTP 200?)"
    if text.startswith((b"{", b"[")):
        return "a JSON document, not a file"
    if not head:
        return "empty"
    return f"unrecognised content (first bytes {head[:8].hex(' ')})"


def _inner_name(disposition: str | None, fallback: str) -> str:
    """Name for a gunzipped member: Content-Disposition filename minus `.gz`."""
    m = _DISPOSITION_RE.search(disposition or "")
    name = Path(m.group(1)).name if m else ""
    if name.lower().endswith(".gz"):
        name = name[:-3]
    name = _STATE_KEY_RE.sub("_", name)
    return name if name.lower().endswith(".xml") else f"{fallback}.xml"


def _wrap_in_zip(src: Path, dest: Path, member: str, *, gunzip: bool) -> None:
    opener = gzip.open if gunzip else open
    with (
        opener(src, "rb") as body,
        zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf,
        zf.open(member, "w", force_zip64=True) as out,
    ):
        shutil.copyfileobj(body, out, 1024 * 1024)


def _is_bare_xml(head: bytes) -> bool:
    """A lone XML document, not an HTML page. Whether it is NeTEx is left to
    `detect`, which rejects any XML outside the NeTEx namespace."""
    text = head.removeprefix(_UTF8_BOM).lstrip()[:15].lower()
    return text.startswith(b"<") and not text.startswith((b"<!doctype", b"<html"))


def _kind_matches(declared: str, detected: str) -> bool:
    if declared in _NETEX_KINDS:
        return detected in _NETEX_KINDS
    return detected == declared


def _validate_timetable_archive(
    raw: Path, head: bytes, kind: str, *, base_name: str, disposition: str | None
) -> Path:
    final = raw.with_name(f"{base_name}.zip")
    if head.startswith(_ZIP_MAGIC):
        raw.replace(final)
    elif head.startswith(_GZIP_MAGIC):
        try:
            _wrap_in_zip(raw, final, _inner_name(disposition, base_name), gunzip=True)
        except Exception as exc:  # zlib.error, EOFError, BadGzipFile, OSError…
            raise FetchError(f"gzip body could not be unpacked: {exc}") from exc
        raw.unlink(missing_ok=True)
    elif kind in _NETEX_KINDS and _is_bare_xml(head):
        # The Italian NAP serves some assets (Trenord) as plain XML.
        _wrap_in_zip(raw, final, _inner_name(disposition, base_name), gunzip=False)
        raw.unlink(missing_ok=True)
    elif head.startswith(_EMPTY_ZIP_MAGIC):
        raise FetchError("server sent an empty zip archive")
    else:
        raise FetchError(f"expected a {kind} zip, got {_describe_head(head)}")
    try:
        detected = detect.detect(final)
    except Exception as exc:  # BadZipFile, zlib.error, NotImplementedError (Deflate64)…
        raise FetchError(f"not a usable {kind} archive: {exc}") from exc
    if not _kind_matches(kind, detected):
        raise FetchError(f"declared {kind} but the file is {detected}")
    return final


def _validate_csv(head: bytes, kind: str) -> None:
    """CSV (or zipped-CSV) kinds: refuse an empty body, JSON, or an HTML page
    — including one behind a UTF-8 BOM."""
    text = head.removeprefix(_UTF8_BOM).lstrip()
    if not text or text[:1] in (b"<", b"{", b"["):
        raise FetchError(f"expected a {kind} file, got {_describe_head(text)}")


def _validate(
    raw: Path, kind: str, *, base_name: str, suffix: str, disposition: str | None
) -> Path:
    """Format check: is `raw` really a `kind` file? Returns the path to
    dispatch — `raw` renamed with the right suffix (dispatch and detect key
    off it), or a new zip wrapping a gzip body. Raises FetchError for a
    rejected file; any other exception means the archive itself is corrupt
    (the caller converts it). The caller deletes the leftovers either way."""
    with raw.open("rb") as f:
        head = f.read(64)

    if kind in TIMETABLE_KINDS:
        return _validate_timetable_archive(
            raw, head, kind, base_name=base_name, disposition=disposition
        )
    if kind == "OSM-PBF":
        # A PBF opens with the 4-byte big-endian length of a small BlobHeader.
        if not head.startswith(b"\x00\x00\x00"):
            raise FetchError(f"expected an OSM PBF, got {_describe_head(head)}")
        suffix = ".pbf"
    elif kind in _CSV_KINDS:
        _validate_csv(head, kind)
    elif head.lstrip()[:1] == b"<":
        raise FetchError(f"expected a {kind} file, got {_describe_head(head)}")
    final = raw.with_name(f"{base_name}{suffix}")
    raw.replace(final)
    return final


def _discard_staged(staging: Path, base_name: str) -> None:
    """Delete everything this fetch staged (`<base_name>.download`, the
    renamed or re-wrapped file). `base_name` is unique per task run."""
    for leftover in staging.glob(f"{base_name}.*"):
        leftover.unlink(missing_ok=True)


def _download_error(exc: httpx.HTTPError, fetch_url: str, state_url: str) -> str:
    """Operator-facing reason. Never echo `fetch_url`: a credential may be
    baked into it (query-param auth)."""
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"download failed: HTTP {response.status_code} {response.reason_phrase}".rstrip()
    message = str(exc) or type(exc).__name__
    if fetch_url != state_url:
        message = message.replace(fetch_url, state_url)
    return f"download failed: {message}"


# ──────────────────────────── download ────────────────────────────


def _expected_bytes(response: httpx.Response) -> int | None:
    """The body's announced size, or None: unknown, or a content-encoded
    body (httpx decodes it, so its byte count is not the Content-Length).
    A resumed download (206, PR #360) announces only the rest: its full size
    is the total of Content-Range, which this must then use."""
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        return None
    try:
        length = int(response.headers.get("content-length", ""))
    except ValueError:
        return None
    return length if length > 0 else None


def _report_progress(received: int, expected: int | None) -> None:
    report = download_progress.get()
    if report is None:
        return
    try:
        report(received, expected)
    except Exception as exc:  # progress is shown, never needed
        log.warning("download progress not reported: %s", type(exc).__name__)


async def _stream_to(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], dest: Path
) -> tuple[int, httpx.Headers, str, int]:
    """One GET attempt. Returns (status, headers, sha256, size); body lands in
    `dest` only for a 2xx."""
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 304:
            return 304, response.headers, "", 0
        response.raise_for_status()
        digest = hashlib.sha256()
        size = 0
        expected = _expected_bytes(response)
        # File I/O off the event loop: feeds run to ~2 GB (DE DELFI) and a
        # slow disk must not stall every other request the web process serves.
        out = await asyncio.to_thread(dest.open, "wb")
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                await asyncio.to_thread(out.write, chunk)
                digest.update(chunk)
                size += len(chunk)
                _report_progress(size, expected)
        finally:
            await asyncio.to_thread(out.close)
        return response.status_code, response.headers, digest.hexdigest(), size


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


async def _stream_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], dest: Path
) -> tuple[int, httpx.Headers, str, int]:
    attempt = 0
    while True:
        try:
            return await _stream_to(client, url, headers, dest)
        except httpx.HTTPError as exc:
            dest.unlink(missing_ok=True)
            if attempt >= len(RETRY_DELAYS) or not _retryable(exc):
                raise
            log.info("fetch retry %d after %s", attempt + 1, type(exc).__name__)
            await asyncio.sleep(RETRY_DELAYS[attempt])
            attempt += 1


async def fetch_validated(
    client: httpx.AsyncClient,
    *,
    fetch_url: str,
    state_url: str,
    extra_headers: dict[str, str],
    kind: str,
    staging: Path,
    base_name: str,
    suffix: str,
    previous: dict[str, Any],
    have_current: bool,
) -> FetchResult:
    """Download `fetch_url` and format-check it as `kind`.

    `state_url` is the URL without credential material (what gets stored and
    compared across runs). `previous` is the stored state of the last
    successful fetch; `have_current` says the slot still holds that file —
    without it a 304 or hash match would leave the slot empty, so both are
    ignored.
    """
    headers = dict(extra_headers)
    same_source = have_current and previous.get("url") == state_url
    if same_source:
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]  # verbatim: some servers send it unquoted
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]

    raw = staging / f"{base_name}.download"
    try:
        status, resp_headers, sha, size = await _stream_with_retry(client, fetch_url, headers, raw)
    except httpx.HTTPError as exc:
        raise FetchError(_download_error(exc, fetch_url, state_url)) from exc

    checked = {**previous, "checked_at": _now_iso()}
    if status == 304:
        if not same_source:
            raise FetchError("server answered 304 to an unconditional request")
        return FetchResult(status="unchanged", reason="not modified (HTTP 304)", state=checked)

    new_state = {
        "url": state_url,
        "etag": resp_headers.get("etag"),
        "last_modified": resp_headers.get("last-modified"),
        "sha256": sha,
        "size_bytes": size,
        "fetched_at": _now_iso(),
        "checked_at": _now_iso(),
    }
    if have_current and sha and sha == previous.get("sha256"):
        raw.unlink(missing_ok=True)
        return FetchResult(
            status="unchanged",
            reason="identical content (sha256 match)",
            state={**previous, **new_state, "fetched_at": previous.get("fetched_at")},
        )

    try:
        final = await asyncio.to_thread(
            _validate,
            raw,
            kind,
            base_name=base_name,
            suffix=suffix,
            disposition=resp_headers.get("content-disposition"),
        )
    except FetchError:
        _discard_staged(staging, base_name)
        raise
    except Exception as exc:
        # A corrupt archive surfaces as zlib.error, EOFError, BadZipFile,
        # NotImplementedError (Deflate64), RuntimeError (encrypted member)…
        # — all mean "rejected", never a 500 for the whole refresh.
        _discard_staged(staging, base_name)
        raise FetchError(f"not a usable {kind} file ({type(exc).__name__}: {exc})") from exc
    # What the slot will hold: the downloaded bytes themselves, or the zip a
    # `.xml.gz` or bare `.xml` was re-wrapped into. The stop extract checks a
    # snapshot of the slot against these two (app/station_extract.py), so
    # that a slot replaced before its state is saved is never paired with the
    # older sha256.
    new_state.update(await asyncio.to_thread(_slot_identity, final, size, sha))
    if kind in _NETEX_KINDS:
        new_state["structure"] = await asyncio.to_thread(
            _structure_check, final, previous.get("structure")
        )
    return FetchResult(status="fetched", path=final, size_bytes=size, sha256=sha, state=new_state)


def _slot_identity(final: Path, size: int, sha: str) -> dict[str, Any]:
    """`slot_size_bytes` and `slot_sha256` of the file dispatched to the slot:
    the download's own when the file was only renamed (same size), else
    those of the re-wrapped zip, hashed here."""
    slot_size = final.stat().st_size
    if slot_size == size:
        return {"slot_size_bytes": size, "slot_sha256": sha}
    digest = hashlib.sha256()
    with final.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return {"slot_size_bytes": slot_size, "slot_sha256": digest.hexdigest()}


def _structure_check(path: Path, previous: Any) -> dict[str, Any]:
    """Fingerprint + assessment of a downloaded NeTEx file (app/netex_structure.py).
    Never fails the download: a check that cannot run is itself a warning."""
    before = previous.get("fingerprint") if isinstance(previous, dict) else None
    try:
        fp = netex_structure.fingerprint(path)
        result = netex_structure.assess(fp, before)
    except Exception as exc:  # corrupt member, odd encoding… the format check already passed
        log.warning("NeTEx structure check failed for %s: %s", path.name, exc)
        failed = netex_structure.Assessment()
        failed.add(netex_structure.WARN, f"structure check could not run: {exc}")
        return {"checked_at": _now_iso(), "fingerprint": before, "assessment": failed.to_state()}
    return {"checked_at": _now_iso(), "fingerprint": fp.to_state(), "assessment": result.to_state()}
