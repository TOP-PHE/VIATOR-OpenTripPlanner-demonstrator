"""Every docstring in the application and its migrations can be written as UTF-8.

The image runs Python 3.14; since Python 3.13 the compiler cleans docstrings by
encoding them as UTF-8. A docstring that holds a lone surrogate (an unescaped `\\ud800` in a
non-raw string) makes the module fail to import there, while it imports fine
on the Python CI runs. v0.1.44.13 shipped exactly that in
`app/api/station_suggest.py` and the web container never started.

Regular expressions that match surrogates (`"[\\ud800-\\udfff]"`) are not
docstrings and compile everywhere, so only docstrings are checked here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# app/ is imported by uvicorn; alembic/ runs at web startup too (alembic upgrade head).
SOURCES = sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "alembic").rglob("*.py")])

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstrings(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, _DOC_OWNERS):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                found.append((getattr(node, "lineno", 1), doc))
    return found


def test_there_are_sources_to_check() -> None:
    assert SOURCES


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_docstring_can_be_written_as_utf8(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    unwritable = [lineno for lineno, doc in _docstrings(tree) if _LONE_SURROGATE.search(doc)]
    assert unwritable == [], (
        f"{path.relative_to(ROOT)}: lone surrogate in docstrings at {unwritable}"
    )
