"""Where the station panel keeps the files it acquires: `inbox/_stations/<key>/`.

There are two data volumes, `inbox` and `graphs`, and the station store lives
on the first. The Storage page treats any top-level inbox folder that is not a
session id as the leftover of a deleted session, so `_stations` is in
`app.storage.INBOX_ROOT_RESERVED`; a session id cannot start with an
underscore, so the name cannot collide with one.

A file is streamed to `<key>/_incoming/` while its sha256 is computed, and is
moved to `<key>/<sha256 prefix>-<name>` only once the caller has decided to
keep it. Nothing here reads a database or knows what a station file contains.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from ..settings import settings

STORE_DIRNAME = "_stations"
_INCOMING = "_incoming"
_CHUNK = 1024 * 1024

# A source key becomes a folder name, so it is a closed alphabet: no slash, no dot.
SOURCE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")

_FULL_DATE_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})(?!\d)")
_MONTH_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?!\d)")


class UploadTooLarge(Exception):
    """The stream exceeded the size limit. Nothing is left on disk."""


class _Readable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class Received:
    """A file streamed to the incoming folder, not yet kept."""

    path: Path
    sha256: str
    size: int


def store_root() -> Path:
    return settings.inbox_dir / STORE_DIRNAME


def source_dir(key: str) -> Path:
    if not SOURCE_KEY_RE.fullmatch(key):
        raise ValueError(f"not a station source key: {key!r}")
    return store_root() / key


def safe_filename(name: str | None) -> str:
    """Strip path components and anything outside a conservative alphabet.

    Both separators are handled by hand: the server runs on Linux, where
    `Path("C:\\data\\x.csv").name` keeps the whole string, and a browser on
    Windows may send exactly that.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:200] or "upload.bin"


def as_of_from_filename(name: str) -> date | None:
    """The date a file name carries, if it carries one.

    `station_master_crd_2026-09.csv` is the September 2026 issue (first of the
    month); `crd_locations_2026-09-14.csv` and `..._20260914.csv` are that day.
    A name with no plausible date gives None: the operator can state it.
    """
    for pattern, has_day in ((_FULL_DATE_RE, True), (_MONTH_RE, False)):
        for match in pattern.finditer(name):
            found = _plausible_date(match, has_day)
            if found is not None:
                return found
    return None


def _plausible_date(match: re.Match[str], has_day: bool) -> date | None:
    year, month = int(match.group(1)), int(match.group(2))
    if not 2000 <= year <= 2100:  # eight digits in a name are not always a date
        return None
    try:
        return date(year, month, int(match.group(3)) if has_day else 1)
    except ValueError:
        return None


async def receive(stream: _Readable, key: str, *, max_bytes: int) -> Received:
    """Stream an upload into the source's incoming folder, hashing as it goes."""
    incoming = source_dir(key) / _INCOMING
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / f"{secrets.token_hex(8)}.part"
    sha = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := await stream.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge
                sha.update(chunk)
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Received(path=path, sha256=sha.hexdigest(), size=size)


def keep(received: Received, key: str, filename: str) -> Path:
    """Move a received file to its place. The sha256 prefix makes the name unique
    per content, so two issues with the same file name never overwrite each other."""
    target = source_dir(key) / f"{received.sha256[:16]}-{safe_filename(filename)}"
    received.path.replace(target)
    return target


def discard(received: Received) -> None:
    received.path.unlink(missing_ok=True)
