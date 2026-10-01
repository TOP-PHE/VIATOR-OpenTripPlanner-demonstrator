#!/usr/bin/env python3
"""Show what an account-only NAP catalogue API returns, without its secrets.

Run on the VPS (or any machine the portal does not block) to finish a
`json_api` resolver config for docs/nap-feed-resolvers.md:

    python3 scripts/probe_nap_api.py at          # Austria, login (prompts)
    python3 scripts/probe_nap_api.py es          # Spain, API key (prompts)
    python3 scripts/probe_nap_api.py es --grep 'ouigo|iryo'

Secrets are read with getpass (never echoed, never on the command line, so
never in shell history) and never printed. The output is the JSON *shape*
— keys, types, list lengths, and short sample values — plus every entry
whose text matches --grep. Paste that output back; it holds no secret.

Stdlib only, so it runs on the host's python3 as well as in the web image.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PORTALS: dict[str, dict[str, str]] = {
    "at": {
        "token_url": (
            "https://user.mobilitaetsverbuende.at/auth/realms/dbp-public"
            "/protocol/openid-connect/token"
        ),
        "client_id": "dbp-public-ui",
        "list_url": (
            "https://data.mobilitaetsverbuende.at/api/public/v1/data-sets"
            "?tagFilterModeInclusive=true"
        ),
        "grep": r"(?i)öbb|oebb|personenverkehr|rail",
    },
    "es": {
        "list_url": "https://nap.transportes.gob.es/api/Fichero/GetList",
        "grep": r"(?i)ouigo|iryo",
    },
}

_SAMPLE_CHARS = 60
_MAX_MATCHES = 40
# Base64 logos in the ES catalogue: megabytes of noise in every match.
_SKIP_KEYS = frozenset({"imagenByte"})


def _request(url: str, *, data: bytes | None = None, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 — fixed https URLs above
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            body = resp.read()
            print(f"HTTP {resp.status} {url} ({len(body)} bytes)")
    except urllib.error.HTTPError as exc:
        # Status and URL only — an error page can echo request headers.
        sys.exit(f"HTTP {exc.code} {url}")
    try:
        return json.loads(body)
    except ValueError:
        sys.exit(f"{url} did not answer JSON (first bytes: {body[:80]!r})")


def _at_headers(portal: dict[str, str]) -> dict[str, str]:
    username = input("AT NAP username (e-mail): ").strip()
    password = getpass.getpass("AT NAP password (not shown): ")
    form = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": portal["client_id"],
            "username": username,
            "password": password,
        }
    ).encode()
    answer = _request(
        portal["token_url"],
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    token = answer.get("access_token") if isinstance(answer, dict) else None
    if not token:
        sys.exit("login answer has no access_token")
    print(f"login OK — token valid for {answer.get('expires_in')} s (token not shown)")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _es_headers(portal: dict[str, str]) -> dict[str, str]:
    key = getpass.getpass("ES NAP API key (not shown): ").strip()
    return {"ApiKey": key, "Accept": "application/json"}


def _sample(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _SAMPLE_CHARS else text[:_SAMPLE_CHARS] + "…"


def shape(node: Any, path: str = "", out: dict[str, str] | None = None) -> dict[str, str]:
    """`path -> type / sample`, lists merged across their elements."""
    out = {} if out is None else out
    if isinstance(node, dict):
        out.setdefault(path or "(root)", "object")
        for key, value in node.items():
            if key in _SKIP_KEYS:
                continue
            shape(value, f"{path}.{key}" if path else key, out)
    elif isinstance(node, list):
        out[path or "(root)"] = f"list[{len(node)}]"
        for element in node[:50]:
            shape(element, f"{path}[]", out)
    else:
        out.setdefault(path, f"{type(node).__name__} e.g. {_sample(node)}")
    return out


def matches(node: Any, pattern: re.Pattern[str], path: str = "") -> list[tuple[str, Any]]:
    """Objects holding a string value matching `pattern` (deepest first)."""
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += matches(value, pattern, f"{path}.{key}" if path else key)
        own = any(
            isinstance(v, str) and pattern.search(v) for k, v in node.items() if k not in _SKIP_KEYS
        )
        if own:
            scalars = {
                k: v
                for k, v in node.items()
                if k not in _SKIP_KEYS and not isinstance(v, dict | list)
            }
            found.append((path or "(root)", scalars))
    elif isinstance(node, list):
        for i, element in enumerate(node):
            found += matches(element, pattern, f"{path}[{i}]")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("portal", choices=sorted(PORTALS))
    parser.add_argument("--grep", help="regex for the entries to show in full")
    parser.add_argument("--url", help="catalogue URL to list instead of the default")
    args = parser.parse_args()
    portal = PORTALS[args.portal]
    headers = _at_headers(portal) if args.portal == "at" else _es_headers(portal)
    data = _request(args.url or portal["list_url"], headers=headers)

    print("\n== shape ==")
    for path, desc in shape(data).items():
        print(f"{path}: {desc}")
    pattern = re.compile(args.grep or portal["grep"])
    found = matches(data, pattern)
    print(f"\n== entries matching {pattern.pattern!r} ({len(found)}) ==")
    for path, scalars in found[:_MAX_MATCHES]:
        print(f"{path}: {json.dumps(scalars, ensure_ascii=False)}")
    if len(found) > _MAX_MATCHES:
        print(f"… {len(found) - _MAX_MATCHES} more")


if __name__ == "__main__":
    main()
