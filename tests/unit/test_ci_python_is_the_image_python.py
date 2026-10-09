"""CI checks the code on the Python the web image runs (#325).

v0.1.44.13 passed every check on Python 3.12 and crash-looped in production on
the image's 3.14: a docstring that 3.12 compiles and 3.13+ refuses (#324). The
Dockerfile is the one source of truth; CI's `setup-python`, mypy's
`python_version` and SonarCloud's `sonar.python.version` must name its version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _image_python() -> str:
    dockerfile = (ROOT / "docker" / "web" / "Dockerfile").read_text(encoding="utf-8")
    versions = re.findall(r"^FROM python:(\d+\.\d+)[-.\w]*\s*$", dockerfile, re.MULTILINE)
    assert len(versions) == 1, versions
    return versions[0]


def test_every_ci_python_is_the_image_python() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    found = {
        (path.name, version)
        for path in workflows
        for version in re.findall(
            r"""^\s*python-version:\s*["']?([^"'\s#]+)""",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }
    # ci.yml: the Python lint+type+test job and the pre-commit job.
    assert {name for name, _ in found} >= {"ci.yml"}
    assert {version for _, version in found} == {_image_python()}, sorted(found)


def test_mypy_checks_against_the_image_python() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["mypy"]["python_version"] == _image_python()


def test_sonar_analyses_for_the_image_python() -> None:
    properties = (ROOT / "sonar-project.properties").read_text(encoding="utf-8")
    assert re.findall(r"^sonar\.python\.version=(\S+)$", properties, re.MULTILINE) == [
        _image_python()
    ]
