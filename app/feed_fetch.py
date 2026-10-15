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
  2. retries with backoff on transport errors and 429/5xx; a body cut part
     way is resumed with a range request from the first missing byte, under
     If-Range so that bytes of two versions are never mixed (`_download`);
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
# A retry downloads the file again from byte 0.
RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Seconds to wait before resume 1, 2, … of a body cut part way (a range
# request from the first missing byte, see `_download`). More resumes than
# retries: a national archive of ~2 GB that a server cuts at about the same
# point every time can never arrive from byte 0.
RESUME_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0, 30.0, 30.0, 30.0)
# Resume attempts in a row that bring no new byte before the download is given up.
MAX_STALLED_RESUMES = 2
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)

_STATE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# What a fetch-state file name may be once unsafe characters are replaced:
# a strict allow-list, short enough for any file system (255 bytes).
_STATE_NAME_MAX = 200
_STATE_NAME_OK = re.compile(rf"[A-Za-z0-9_.-]{{1,{_STATE_NAME_MAX}}}")
_DISPOSITION_RE = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)


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
    resumes: int = 0  # resume requests (a range from the first missing byte) on the way


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
        message = f"HTTP {response.status_code} {response.reason_phrase}".rstrip()
    else:
        message = str(exc) or type(exc).__name__
        if fetch_url != state_url:
            message = message.replace(fetch_url, state_url)
    return f"download failed: {message}"


# ──────────────────────────── download ────────────────────────────


class _Restart(Exception):
    """The partial file cannot be continued (the server sent another version,
    another range, or no range at all): download again from byte 0."""


@dataclass
class _Download:
    status: int  # 2xx, or 304 for "not modified"
    headers: httpx.Headers  # of the response that started the kept bytes
    sha256: str
    size: int
    resumes: int = 0


@dataclass
class _Transfer:
    """The bytes of one version of the file written so far to `dest`.

    `total` is the full size the server announced (Content-Length of a plain,
    not content-encoded 200), `validator` the strong ETag or Last-Modified that
    an If-Range must name so that a resume never appends bytes of another
    version. Without both, a cut body cannot be resumed."""

    dest: Path
    status: int = 0
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    digest: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    total: int | None = None
    validator: str | None = None
    ranges: bool = False
    limit: int | None = None  # byte offset the current response may not pass

    def start(self, response: httpx.Response) -> None:
        """A full response from byte 0, on a new `_Transfer` (a restart always
        makes one, so no earlier byte is ever counted or hashed)."""
        self.status = response.status_code
        self.headers = response.headers
        self.total = _full_length(response)
        self.validator = _range_validator(response.headers)
        self.ranges = _ranges_offered(response.headers)
        self.limit = self.total

    @property
    def resumable(self) -> bool:
        return (
            self.ranges
            and self.validator is not None
            and self.total is not None
            and 0 < self.size < self.total
        )

    def accept_range(self, response: httpx.Response) -> None:
        """Check a 206 continues exactly where the file stops, for the same
        full size; raise _Restart otherwise."""
        m = _CONTENT_RANGE_RE.fullmatch(response.headers.get("content-range", "").strip())
        if m is None:
            raise _Restart("unreadable Content-Range")
        start, end, total = int(m[1]), int(m[2]), m[3]
        if start != self.size:
            raise _Restart(f"range starts at byte {start}, not {self.size}")
        if total != str(self.total) or end < start or end >= int(total):
            raise _Restart(f"range {start}-{end}/{total} is not the announced file")
        self.limit = end + 1

    async def append(self, response: httpx.Response, mode: str) -> None:
        """Stream the body to disk (never held in memory) and into the hash."""
        out = await asyncio.to_thread(self.dest.open, mode)
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                if self.limit is not None and self.size + len(chunk) > self.limit:
                    raise FetchError(
                        f"download failed: size mismatch, the server sent more than "
                        f"the {self.limit} bytes it announced"
                    )
                # File I/O off the event loop: feeds run to ~2 GB (DE DELFI) and
                # a slow disk must not stall every other request the web serves.
                await asyncio.to_thread(out.write, chunk)
                self.digest.update(chunk)
                self.size += len(chunk)
        finally:
            await asyncio.to_thread(out.close)
        if self.total is not None and self.size < self.total:
            # The body ended early without a transport error (h11 normally
            # raises this itself): the same case as a cut connection.
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                f"(received {self.size} bytes, expected {self.total})"
            )

    def result(self) -> _Download:
        return _Download(self.status, self.headers, self.digest.hexdigest(), self.size)


def _full_length(response: httpx.Response) -> int | None:
    """Size of the full file, or None when unknown. A content-encoded body is
    decoded by httpx, so its byte count says nothing about the file's size
    and its ranges would count encoded bytes: never resumed."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        return None
    try:
        length = int(response.headers.get("content-length", ""))
    except ValueError:
        return None
    return length if length > 0 else None


def _range_validator(headers: httpx.Headers) -> str | None:
    """What If-Range names: a strong ETag, else Last-Modified. A weak ETag
    never matches an If-Range (RFC 9110 13.1.5), so it cannot serve."""
    etag = str(headers.get("etag") or "")
    if etag and not etag.startswith("W/"):
        return etag
    return str(headers.get("last-modified") or "") or None


def _ranges_offered(headers: httpx.Headers) -> bool:
    """`Accept-Ranges: bytes`, or no Accept-Ranges at all (then a Range
    request is tried and the answer, 206 or 200, decides); `none` refuses."""
    raw = headers.get("accept-ranges")
    if raw is None:
        return True
    return "bytes" in {token.strip().lower() for token in raw.split(",")}


def _resume_headers(headers: dict[str, str], transfer: _Transfer) -> dict[str, str]:
    """The request headers minus the conditional-GET pair (an If-None-Match
    could turn the resume into a 304), plus the range from the first missing
    byte under If-Range: a changed file answers 200 and is never mixed in."""
    kept = {
        k: v
        for k, v in headers.items()
        if k.lower() not in ("if-none-match", "if-modified-since", "range", "if-range")
    }
    kept["Range"] = f"bytes={transfer.size}-"
    kept["If-Range"] = transfer.validator or ""
    kept["Accept-Encoding"] = "identity"
    return kept


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


async def _get_full(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], transfer: _Transfer
) -> bool:
    """GET from byte 0. False for a 304 (nothing written)."""
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 304:
            transfer.status, transfer.headers = 304, response.headers
            return False
        response.raise_for_status()
        transfer.start(response)
        await transfer.append(response, "wb")
    return True


async def _get_rest(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], transfer: _Transfer
) -> None:
    """GET the missing bytes and append them; _Restart when the answer is
    not the continuation of the same file."""
    on_disk = await asyncio.to_thread(lambda: transfer.dest.stat().st_size)
    if on_disk != transfer.size:
        raise _Restart(f"partial file holds {on_disk} bytes, not {transfer.size}")
    async with client.stream("GET", url, headers=_resume_headers(headers, transfer)) as response:
        if response.status_code == 206:
            transfer.accept_range(response)
            await transfer.append(response, "ab")
            return
        # 200: the server ignores ranges, or the file changed (If-Range).
        # 416 or anything else but an error: start again rather than fail.
        if response.status_code >= 400 and response.status_code != 416:
            response.raise_for_status()
        raise _Restart(f"server answered {response.status_code} to a range request")


@dataclass
class _Attempts:
    """What the retries have used so far, for one download."""

    restarts: int = 0
    resumes: int = 0
    stalled: int = 0  # consecutive resume attempts that brought no byte


async def _download(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    dest: Path,
    *,
    state_url: str,
) -> _Download:
    """Download `url` into `dest`, riding out cuts. Raises FetchError (and
    deletes `dest`) when it gives up.

    A transport error or a retryable status restarts from byte 0 at most
    `len(RETRY_DELAYS)` times, as before. A body cut after some bytes is
    resumed instead when the server allows it (see `_Transfer.resumable`): up
    to `len(RESUME_DELAYS)` resumes, each with its wait, given up after
    `MAX_STALLED_RESUMES` resumes in a row that bring no new byte. Each
    request keeps the client's own timeouts.
    """
    try:
        return await _download_into(client, url, headers, dest, state_url)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


async def _download_into(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], dest: Path, state_url: str
) -> _Download:
    transfer = _Transfer(dest)
    tried = _Attempts()
    while True:
        before = transfer.size
        resuming = transfer.resumable
        try:
            result = await _attempt(client, url, headers, transfer, resuming)
            result.resumes = tried.resumes
            return result
        except _Restart as exc:
            log.info("fetch resume refused (%s), restarting from byte 0", exc)
            transfer = _Transfer(dest)
            await _next_restart(tried, f"download failed: {exc}", exc)
        except httpx.HTTPError as exc:
            reason = _download_error(exc, url, state_url)
            if not _retryable(exc):
                raise FetchError(f"{reason}{_resumed_note(tried.resumes)}") from exc
            if resuming or transfer.resumable:
                await _next_resume(tried, transfer, before, reason, exc)
                continue
            transfer = _Transfer(dest)
            await _next_restart(tried, reason, exc)


async def _attempt(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    transfer: _Transfer,
    resuming: bool,
) -> _Download:
    """One request: the rest of the file, or all of it."""
    if resuming:
        await _get_rest(client, url, headers, transfer)
    elif not await _get_full(client, url, headers, transfer):
        return _Download(304, transfer.headers, "", 0)
    return transfer.result()


async def _next_restart(tried: _Attempts, reason: str, exc: Exception) -> None:
    """Wait before the next download from byte 0, or give up with `reason`."""
    if tried.restarts >= len(RETRY_DELAYS):
        raise FetchError(f"{reason}{_resumed_note(tried.resumes)}") from exc
    log.info("fetch retry %d after %s", tried.restarts + 1, type(exc).__name__)
    await asyncio.sleep(RETRY_DELAYS[tried.restarts])
    tried.restarts += 1
    tried.stalled = 0


async def _next_resume(
    tried: _Attempts, transfer: _Transfer, before: int, reason: str, exc: Exception
) -> None:
    """Wait before resuming `transfer` at its first missing byte, or give up:
    no resume left, or `MAX_STALLED_RESUMES` resumes in a row brought nothing.
    `reason` is the operator-facing text of the last error."""
    tried.stalled = tried.stalled + 1 if transfer.size == before else 0
    where = f"stopped at byte {transfer.size} of {transfer.total}"
    if tried.stalled >= MAX_STALLED_RESUMES:
        raise FetchError(
            f"{reason}; no progress in {tried.stalled} resume attempts in a row, "
            f"{where}{_resumed_note(tried.resumes)}"
        ) from exc
    if tried.resumes >= len(RESUME_DELAYS):
        raise FetchError(f"{reason}; gave up after {tried.resumes} resumes, {where}") from exc
    log.info(
        "fetch resume %d from byte %d of %d after %s",
        tried.resumes + 1,
        transfer.size,
        transfer.total,
        type(exc).__name__,
    )
    await asyncio.sleep(RESUME_DELAYS[tried.resumes])
    tried.resumes += 1


def _resumed_note(resumes: int) -> str:
    if not resumes:
        return ""
    return f" (resumed {resumes} time{'s' if resumes > 1 else ''})"


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
    download = await _download(client, fetch_url, headers, raw, state_url=state_url)
    status, resp_headers, sha, size = (
        download.status,
        download.headers,
        download.sha256,
        download.size,
    )

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
    return FetchResult(
        status="fetched",
        path=final,
        size_bytes=size,
        sha256=sha,
        state=new_state,
        resumes=download.resumes,
    )


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
