"""The stop extracts are made at refresh time, after the answer is sent.

`refresh_sources` and `refresh_provider` (app/api/admin/sessions.py) queue
`station_extract.queue_warm` as a FastAPI background task when a
timetable was fetched or found unchanged, so that sending the extracts later
(and the MOTIS wait of the station build) never waits for one to be made.
The upload route does the same for an `upload` provider; its integration
test is in tests/integration/test_provider_upload.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks

from app import station_extract
from app.api.admin import sessions as sessions_api

ACTOR = SimpleNamespace(id=uuid.uuid4(), username="ops@example.org", role="content_manager")
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
CONFIG = {
    "sources": {
        "providers": [
            {
                "id": "ZZRAIL",
                "timetable": {"format": "gtfs", "url": "https://zz.example.invalid/zz.zip"},
            }
        ]
    }
}


def _session() -> SimpleNamespace:
    return SimpleNamespace(id="zz-session", config=dict(CONFIG), state="populated")


def _db(session: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        get=lambda _model, _sid: session, commit=lambda: None, refresh=lambda *_a, **_k: None
    )


def _response(**buckets: list[dict[str, Any]]) -> sessions_api.RefreshSourcesResponse:
    return sessions_api.RefreshSourcesResponse(
        fetched=buckets.get("fetched", []),
        skipped=buckets.get("skipped", []),
        unchanged=buckets.get("unchanged", []),
        orphaned=[],
        cascaded=[],
    )


@pytest.fixture
def no_progress_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """`refresh_sources` claims a progress record in the database (issue
    #359, app/refresh_progress.py); these tests have no database."""
    from app import refresh_progress

    def claim(sid: str, scope: str, total: int, **_k: Any) -> refresh_progress.RefreshRun:
        detail = refresh_progress._initial_detail(total, None)
        return refresh_progress.RefreshRun(sid, scope, uuid.uuid4(), detail)

    monkeypatch.setattr(refresh_progress, "claim", claim)
    monkeypatch.setattr(refresh_progress.RefreshRun, "_execute", lambda _self, _stmt, **_k: None)


@pytest.mark.parametrize(
    ("buckets", "queued"),
    [
        ({"fetched": [{"key": "t"}]}, True),
        ({"unchanged": [{"key": "t"}]}, True),
        ({"skipped": [{"key": "t"}]}, False),
    ],
)
@pytest.mark.usefixtures("no_progress_record")
async def test_refresh_provider_queues_the_extract(
    monkeypatch: pytest.MonkeyPatch, buckets: dict[str, list[dict[str, Any]]], queued: bool
) -> None:
    async def refreshed(*_args: Any, **_kwargs: Any) -> sessions_api.RefreshSourcesResponse:
        return _response(**buckets)

    monkeypatch.setattr(sessions_api, "_refresh_provider_urls", refreshed)
    background = BackgroundTasks()
    session = _session()
    await sessions_api.refresh_provider(
        "zz-session",
        "ZZRAIL",
        REQUEST,  # type: ignore[arg-type]
        _db(session),  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
        background,
    )
    if not queued:
        assert background.tasks == []
        return
    [task] = background.tasks
    assert task.func is station_extract.queue_warm
    assert task.args == ("zz-session", CONFIG)
    assert task.kwargs == {"only_provider": "ZZRAIL"}


@pytest.mark.usefixtures("no_progress_record")
@pytest.mark.parametrize("bucket", ["fetched", "unchanged", "skipped"])
async def test_refresh_sources_queues_the_extracts(
    monkeypatch: pytest.MonkeyPatch, bucket: str
) -> None:
    outcome = {"fetched": [], "unchanged": [], "skipped": []}
    outcome[bucket] = [{"key": "provider[ZZRAIL].timetable(gtfs)"}]

    async def gathered(*_args: Any, **_kwargs: Any) -> tuple[list[Any], ...]:
        return outcome["fetched"], outcome["unchanged"], outcome["skipped"]

    async def no_cascade(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(sessions_api, "_gather_refresh_outcomes", gathered)
    monkeypatch.setattr(sessions_api, "_cascade_derived_refresh", no_cascade)
    monkeypatch.setattr(sessions_api, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(sessions_api.audit, "record", lambda *_a, **_k: None)
    background = BackgroundTasks()
    await sessions_api.refresh_sources(
        "zz-session",
        REQUEST,  # type: ignore[arg-type]
        _db(_session()),  # type: ignore[arg-type]
        ACTOR,  # type: ignore[arg-type]
        background,
    )
    if bucket == "skipped":
        assert background.tasks == []
        return
    [task] = background.tasks
    assert task.func is station_extract.queue_warm
    assert task.args[0] == "zz-session"
    assert task.kwargs == {"only_provider": None}
