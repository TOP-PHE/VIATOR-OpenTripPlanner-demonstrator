"""The tool versions outside requirements-dev.txt follow its pins (#336).

Dependabot bumps requirements-dev.txt and nothing else: it does not touch a
pre-commit hook's `rev:` nor a `pip install` line in a workflow. The ruff hook
had stayed at v0.15.12 while requirements-dev.txt pinned 0.16.10, so a commit
was linted by one ruff locally and another in CI's Python job. These tests fail
on such drift; the fix is to bump the other file to requirements-dev.txt's pin.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dev_pin(package: str) -> str:
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    pins = re.findall(rf"^{re.escape(package)}==([^\s#]+)", text, re.MULTILINE | re.IGNORECASE)
    assert len(pins) == 1, (package, pins)
    return pins[0]


def _hook_revs(repo_url: str) -> list[str]:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    return re.findall(
        rf"^\s*-\s*repo:\s*{re.escape(repo_url)}\s*\n\s*rev:\s*['\"]?([^'\"\s#]+)",
        config,
        re.MULTILINE,
    )


def test_ruff_hook_runs_the_pinned_ruff() -> None:
    revs = _hook_revs("https://github.com/astral-sh/ruff-pre-commit")
    assert revs == [f"v{_dev_pin('ruff')}"], (
        "bump the ruff-pre-commit rev in .pre-commit-config.yaml to "
        f"v{_dev_pin('ruff')} (requirements-dev.txt's pin); found {revs}"
    )


def test_ci_installs_the_pinned_pre_commit() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = re.findall(r"pip install\b[^\n]*\bpre-commit\b[^\n]*", workflow)
    # One install, and it reads the pin from requirements-dev.txt rather than
    # naming a version of its own (or none, as before #336).
    assert installs == [
        """pip install "$(grep -oE '^pre-commit==[^[:space:]]+' requirements-dev.txt)\""""
    ], installs
    # The pin it reads exists, once.
    assert _dev_pin("pre-commit")
