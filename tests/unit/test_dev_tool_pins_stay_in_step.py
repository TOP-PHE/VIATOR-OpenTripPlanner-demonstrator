"""The pre-commit hooks run the tools of the dev lock, never versions of their own.

#336: Dependabot bumped requirements-dev.txt and nothing else, so the ruff
hook's `rev:` stayed at v0.15.12 while the lock pinned 0.16.10 and a commit was
linted by one ruff locally and another in CI. Since the hashed lock files
(VIATOR-technical-spec.md 15.2.5) every hook is `repo: local`,
`language: system`: it runs the program requirements-dev.txt installed, so
there is no second version to keep in step, pre-commit clones no hook
repository by a tag that can move, and pip-installs nothing unhashed. Each
hook is written as its project defines it (pre-commit-hooks v6.0.0,
ruff-pre-commit): the same program, the same kinds of file, the same stages.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# Each hook as its project defines it: the program it runs, and the keys that choose
# which files it reads and when. A key absent here must be absent from the hook, so that
# no read-only check is narrowed (no `files`, `exclude`, `types` or `stages` of our own).
HOOKS: dict[str, tuple[str, dict[str, object]]] = {
    "trailing-whitespace": (
        "trailing-whitespace-fixer",
        {"types": ["text"], "stages": ["pre-commit", "pre-push", "manual"]},
    ),
    "end-of-file-fixer": (
        "end-of-file-fixer",
        {"types": ["text"], "stages": ["pre-commit", "pre-push", "manual"]},
    ),
    "check-yaml": ("check-yaml", {"types": ["yaml"]}),
    "check-toml": ("check-toml", {"types": ["toml"]}),
    "check-added-large-files": (
        "check-added-large-files",
        {"stages": ["pre-commit", "pre-push", "manual"]},
    ),
    "check-merge-conflict": ("check-merge-conflict", {"types": ["text"]}),
    "detect-private-key": ("detect-private-key", {"types": ["text"]}),
    "ruff-check": (
        "ruff check --force-exclude",
        {"types_or": ["python", "pyi", "jupyter"], "require_serial": True},
    ),
    "ruff-format": (
        "ruff format --force-exclude",
        {"types_or": ["python", "pyi", "jupyter", "markdown"], "require_serial": True},
    ),
    # Ours (#327): CI's own command, before a push.
    "mypy": (
        "mypy app/",
        {
            "pass_filenames": False,
            "always_run": True,
            "require_serial": True,
            "stages": ["pre-push"],
        },
    ),
}
# The arguments each hook had before it became local.
ARGS = {"ruff-check": ["--fix"], "check-added-large-files": ["--maxkb=1024"]}
# The package of the dev lock that installs each program.
PACKAGES = {"ruff": "ruff", "mypy": "mypy"} | {
    entry: "pre-commit-hooks"
    for entry, _ in HOOKS.values()
    if entry.split()[0] not in ("ruff", "mypy")
}
# The pre-commit-hooks release whose hook definitions HOOKS copies. The remote hooks were
# at v5.0.0 when they became local. 6.0.0 removed two hooks VIATOR never ran
# (check-byte-order-marker, fix-encoding-pragma: their commands are gone, their stale
# modules still ship in the wheel); the definitions of the seven above, in its
# .pre-commit-hooks.yaml, and their programs are unchanged (the only code change is
# `Sequence` imported from collections.abc). The test below fails on any change of the
# pin, 6.0.1 included: check the new release the same way (no hook used here removed,
# renamed or redefined), then move this line with the pin.
HYGIENE_HOOKS_VERSION = "6.0.0"
SELECTING = (
    "types",
    "types_or",
    "exclude_types",
    "files",
    "exclude",
    "stages",
    "require_serial",
    "pass_filenames",
    "always_run",
)


def _dev_pins() -> dict[str, str]:
    text = (ROOT / "requirements-dev.in").read_text(encoding="utf-8")
    return {
        name.lower(): version
        for name, version in re.findall(r"^([A-Za-z0-9._-]+)==([^\s#]+)", text, re.MULTILINE)
    }


def _config() -> dict[str, object]:
    config: dict[str, object] = yaml.safe_load(
        (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    return config


def test_every_hook_is_local_and_runs_a_program_of_the_dev_lock() -> None:
    config = _config()
    repos = config["repos"]
    assert isinstance(repos, list)
    assert {repo["repo"] for repo in repos} == {"local"}
    assert all(set(repo) == {"repo", "hooks"} for repo in repos)
    # No exclude of the whole configuration either: VIATOR has none.
    assert set(config) == {"repos"}

    hooks = {hook["id"]: hook for repo in repos for hook in repo["hooks"]}
    assert set(hooks) == set(HOOKS)
    for name, (entry, kinds) in HOOKS.items():
        hook = hooks[name]
        assert hook["language"] == "system", name
        assert hook["entry"] == entry, name
        assert "additional_dependencies" not in hook, name
        assert hook.get("args") == ARGS.get(name), name
        assert {key: hook.get(key) for key in kinds} == kinds, name
        for key in SELECTING:
            assert key in kinds or key not in hook, (name, key)

    pins = _dev_pins()
    for package in set(PACKAGES.values()):
        assert package in pins, package
    assert {hook["entry"].split()[0] for hook in hooks.values()} == set(PACKAGES)


def test_the_hygiene_hooks_are_at_the_version_their_definitions_were_checked_against() -> None:
    """A new pre-commit-hooks major can remove or redefine a hook: it is checked by hand."""
    assert _dev_pins()["pre-commit-hooks"] == HYGIENE_HOOKS_VERSION
    # The configuration says which release its hooks are written from.
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert f"pre-commit-hooks v{HYGIENE_HOOKS_VERSION}" in config


def test_the_installed_hygiene_package_provides_every_hook_program() -> None:
    """The dev lock is installed (CI's Python job): each hook's program is one of its commands."""
    assert metadata.version("pre-commit-hooks") == HYGIENE_HOOKS_VERSION
    commands = {
        point.name
        for point in metadata.distribution("pre-commit-hooks").entry_points
        if point.group == "console_scripts"
    }
    programs = {entry for entry, package in PACKAGES.items() if package == "pre-commit-hooks"}
    assert programs <= commands, programs - commands


def test_ci_runs_pre_commit_from_the_hashed_dev_lock() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["pre-commit"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert runs == [
        "pip install --require-hashes --only-binary :all: -r requirements-dev.txt",
        "pre-commit run --all-files --show-diff-on-failure",
    ]
