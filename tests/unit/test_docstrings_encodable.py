"""Every string literal in the application can be written as UTF-8.

The image runs Python 3.14, whose compiler cleans docstrings by encoding them
as UTF-8. A docstring that holds a lone surrogate (an unescaped `\\ud800` in a
non-raw string) makes the module fail to import there, while it imports fine
on the Python CI runs. v0.1.44.13 shipped exactly that in
`app/api/station_suggest.py` and the web container never started.

Regular expressions that match surrogates (`"[\\ud800-\\udfff]"`) are not
docstrings and compile everywhere, so only docstrings are checked here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
SOURCES = sorted(APP.rglob("*.py"))

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


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(APP)))
def test_every_docstring_can_be_written_as_utf8(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for lineno, doc in _docstrings(tree):
        try:
            doc.encode("utf-8")
        except UnicodeEncodeError:
            pytest.fail(f"{path.relative_to(APP)}:{lineno}: docstring holds a lone surrogate")
