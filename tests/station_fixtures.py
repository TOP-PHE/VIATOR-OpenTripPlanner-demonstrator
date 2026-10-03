"""Synthetic station files for the station panel tests.

The five offline files are never in this repository (it is public, and they
carry RNE-licensed rows). Everything built here is invented: the PLC prefix
`ZZ` does not exist, the names are made up, the codes use a `99` prefix.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

STATION_TABS = (
    "/admin/stations/nap",
    "/admin/stations/registers",
    "/admin/stations/trainline",
    "/admin/stations/reference",
    "/admin/stations/sources",
)


def page_request(path: str, role: str | None) -> Request:
    """A browser request for a page, as `role` (None: not logged in).

    The page guard reads the JWT only, so a page can be rendered in a unit
    test without a database or a running app.
    """
    headers: list[tuple[bytes, bytes]] = []
    if role is not None:
        from app.auth import tokens

        jwt = tokens.issue_jwt(uuid.uuid4(), f"{role}@viator.example", role)
        headers.append((b"authorization", f"Bearer {jwt}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": headers,
        }
    )


_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)


def inline_scripts(html: str) -> list[tuple[bool, str]]:
    """The inline scripts of a rendered page: (is an ES module, source)."""
    out = []
    for match in _SCRIPT_RE.finditer(html):
        attrs = match.group("attrs")
        if "src=" in attrs or not match.group("body").strip():
            continue
        out.append(('type="module"' in attrs, match.group("body")))
    return out


def node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; skipping the JavaScript checks")
    return node


def assert_scripts_parse(html: str, tmp_path: Path) -> int:
    """Run `node --check` on every inline script of a rendered page.

    The templates' JavaScript is never executed by the Python tests, so a
    syntax error would only show in a browser. Returns how many were checked.
    """
    node = node_or_skip()
    scripts = inline_scripts(html)
    for index, (is_module, source) in enumerate(scripts):
        path = tmp_path / f"inline_{index}.{'mjs' if is_module else 'cjs'}"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"script {index} does not parse:\n{result.stderr}"
    return len(scripts)


def run_in_node(script: str, expression: str) -> Any:
    """Evaluate `expression` after running a classic inline script, in Node.

    Enough for helpers that do not touch the DOM until they are called.
    """
    node = node_or_skip()
    program = script + "\nprocess.stdout.write(JSON.stringify(" + expression + "));\n"
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, check=False, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
