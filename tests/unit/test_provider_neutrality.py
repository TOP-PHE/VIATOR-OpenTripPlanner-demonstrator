"""#303 AC2 — the canonical model knows no provider.

`app/journey/canonical/` imports no engine client, no adapter and no coverage module: only the
adapters (`app/journey/canonical_adapters/`) know a provider's format. Business modules join
this test as they move onto CanonicalJourney (#303 PR 3, PR 4).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"
NEUTRAL = sorted((APP / "journey" / "canonical").rglob("*.py"))

FORBIDDEN = (
    "app.journey.motis_client",
    "app.journey.otp_client",
    "app.journey.hafas_client",
    "app.journey.ojp_client",
    "app.journey.canonical_adapters",
    "app.network_coverage",
    "app.journey.signature",
)


def _imports(path: Path) -> set[str]:
    """Every module a file imports, relative imports resolved against its package, and each
    imported name as `module.name` (so `from .. import motis_client` is seen)."""
    package = list(path.relative_to(APP.parent).with_suffix("").parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_the_neutral_package_is_found() -> None:
    assert len(NEUTRAL) >= 15


@pytest.mark.parametrize("path", NEUTRAL, ids=lambda p: str(p.relative_to(APP)))
def test_the_canonical_package_imports_no_provider_code(path: Path) -> None:
    imported = _imports(path)
    leaks = sorted(m for m in imported for bad in FORBIDDEN if m == bad or m.startswith(bad + "."))
    assert not leaks, f"{path.name} imports provider code: {leaks}"
