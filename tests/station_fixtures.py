"""Synthetic station files for the station panel tests.

The five offline files are never in this repository (it is public, and they
carry RNE-licensed rows). Everything built here is invented: the PLC prefix
`ZZ` does not exist, the names are made up, the codes use a `99` prefix.
"""

from __future__ import annotations

from collections.abc import Sequence


def csv_bytes(header: Sequence[str], *rows: dict[str, str]) -> bytes:
    """A station file as the offline chain writes it: UTF-8 with BOM, CRLF,
    RFC 4180 quoting. Columns a row does not name are empty."""
    lines = [",".join(header)]
    lines += [",".join(_cell(row.get(column, "")) for column in header) for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def _cell(value: str) -> str:
    if any(ch in value for ch in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value
