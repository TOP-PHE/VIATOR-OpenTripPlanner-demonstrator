"""MOTIS version resolution (app/engine_versions.py).

docker-compose forwards an unset `.env` variable as an empty string, so an
empty override must fall back to the pin — a `.get(name, default)` would
render the image `ghcr.io/motis-project/motis:` and every MOTIS container
would fail to pull. The build version follows the serve version unless it is
split on purpose.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType

import pytest

from app import engine_versions

_ENV = ("VIATOR_MOTIS_VERSION", "VIATOR_MOTIS_BUILD_VERSION")


@pytest.fixture
def reload_versions(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    importlib.reload(engine_versions)


def _reload() -> ModuleType:
    return importlib.reload(engine_versions)


@pytest.mark.usefixtures("reload_versions")
def test_unset_uses_the_pin() -> None:
    v = _reload()
    assert v.MOTIS_VERSION == v._MOTIS_PINNED
    assert f"ghcr.io/motis-project/motis:{v._MOTIS_PINNED}" == v.MOTIS_IMAGE
    assert v.MOTIS_BUILD_IMAGE == v.MOTIS_IMAGE


def test_empty_strings_from_compose_use_the_pin(reload_versions: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        reload_versions.setenv(name, "")
    v = _reload()
    assert v.MOTIS_IMAGE.endswith(f":{v._MOTIS_PINNED}")
    assert v.MOTIS_BUILD_IMAGE == v.MOTIS_IMAGE


def test_serve_override_also_moves_the_build(reload_versions: pytest.MonkeyPatch) -> None:
    reload_versions.setenv("VIATOR_MOTIS_VERSION", "2.10.2")
    v = _reload()
    assert v.MOTIS_IMAGE == "ghcr.io/motis-project/motis:2.10.2"
    assert v.MOTIS_BUILD_IMAGE == "ghcr.io/motis-project/motis:2.10.2"


def test_build_override_alone_leaves_serving_on_the_pin(
    reload_versions: pytest.MonkeyPatch,
) -> None:
    reload_versions.setenv("VIATOR_MOTIS_BUILD_VERSION", "2.12.0")
    v = _reload()
    assert v.MOTIS_IMAGE.endswith(f":{v._MOTIS_PINNED}")
    assert v.MOTIS_BUILD_IMAGE == "ghcr.io/motis-project/motis:2.12.0"


def test_worker_builds_with_the_build_image() -> None:
    from app import worker

    assert worker._MOTIS_IMAGE == engine_versions.MOTIS_BUILD_IMAGE
