"""The per-session upload route answers 400, not 500, when the uploaded file
cannot be classified.

`detect.detect` raises on a file it does not know (an unknown extension, a CSV
that is neither SNCF stations nor MCT) and `zipfile` on a zip that is not one.
Both are the caller's mistake, and the staged copy must not be left behind.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.api.admin import sessions as sessions_api
from app.settings import settings

ACTOR = SimpleNamespace(id=uuid.uuid4(), username="ops@example.org", role="content_manager")
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "inbox"
    root.mkdir(exist_ok=True)  # the suite-wide settings fixture may have made it
    monkeypatch.setattr(settings, "inbox_dir", root)
    return root


@pytest.mark.parametrize(
    ("filename", "data", "fragment"),
    [
        ("unknown.csv", b"alpha,beta\n1,2\n", "CSV header does not match"),
        ("notes.txt", b"hello", "Unsupported extension"),
        ("broken.zip", b"this is not a zip archive", "Detection failed"),
    ],
)
async def test_session_upload_answers_400_when_detect_cannot_classify(
    inbox: Path, filename: str, data: bytes, fragment: str
) -> None:
    session = SimpleNamespace(id="xb-test", config={}, state="created")
    db = SimpleNamespace(get=lambda _model, _sid: session)
    upload = UploadFile(file=io.BytesIO(data), filename=filename)
    with pytest.raises(HTTPException) as exc:
        await sessions_api.upload_to_session(
            "xb-test",
            "GTFS",
            upload,
            REQUEST,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            ACTOR,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400
    assert exc.value.detail.startswith("Detection failed: ")
    assert fragment in exc.value.detail
    # The staged copy is removed: a refused upload leaves nothing in the inbox.
    assert list((inbox / "xb-test" / "_staging").iterdir()) == []


async def test_a_file_that_is_not_what_was_declared_is_still_refused(inbox: Path) -> None:
    # The check that was already there, now behind the same helper.
    session = SimpleNamespace(id="xb-test", config={}, state="created")
    db = SimpleNamespace(get=lambda _model, _sid: session)
    pbf = UploadFile(file=io.BytesIO(b"\x00\x00\x00\x0d\x0a\x09OSMHeader"), filename="x.osm.pbf")
    with pytest.raises(HTTPException) as exc:
        await sessions_api.upload_to_session(
            "xb-test",
            "GTFS",
            pbf,
            REQUEST,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            ACTOR,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400
    assert "declared as 'GTFS'" in exc.value.detail
    assert list((inbox / "xb-test" / "_staging").iterdir()) == []
