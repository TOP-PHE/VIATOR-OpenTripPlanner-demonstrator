"""Provider refresh that outlasts nginx's proxy timeout (sessions.html).

A slow source (nap.si at ~120 KB/s for a 57 MB file) can run past nginx's
600 s `proxy_read_timeout`. The browser then gets a 504 while the server keeps
downloading, and the status pills used to stay on the old state until a page
reload. The page now treats a gateway error as "still running" and polls the
provider status until the server records a new attempt.

The JS block is executed in Node with fakes for the page's globals; skipped
where Node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "admin" / "sessions.html"
_START = "const _GATEWAY_STATUSES"
_END = "// v0.1.37 — extracted from the config-form"

_HARNESS = """
const toasts = []; const showToast = (k, t) => toasts.push([k, t]);
let marked = 0; const markRefreshCompletedLocally = () => marked++;
const _feedStatusCache = new Map([['s', {SI: {source: 'nap', last_attempt:
  {at: '2026-10-02T15:40:00+00:00', status: 'skipped', reason: 'HTTP 403'}}}]]);
const NEXT = %(next)s;
let calls = 0;
async function loadProviderStatuses() {
  calls++;
  return calls < 3 ? _feedStatusCache.get('s') : {SI: {last_attempt: NEXT}};
}
%(block)s
(async () => {
  await handleRefreshFailure(%(response)s, %(body)s, 's', null, attemptMarks('s', ['SI']));
  console.log(JSON.stringify({toasts, marked, calls}));
})();
"""


def _run(response: dict, body: dict, next_attempt: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text[text.index(_START) : text.index(_END)]
    block = block.replace("_REFRESH_POLL_MS = 20000", "_REFRESH_POLL_MS = 1")
    script = _HARNESS % {
        "next": json.dumps(next_attempt),
        "block": block,
        "response": json.dumps(response),
        "body": json.dumps(body),
    }
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_gateway_timeout_waits_for_the_server_side_outcome() -> None:
    result = _run(
        {"status": 504},
        {},
        {"at": "2026-10-02T15:58:15+00:00", "status": "fetched", "reason": None},
    )
    kinds = [k for k, _ in result["toasts"]]
    assert kinds == ["warning", "success"]
    assert result["toasts"][1][1] == "SI: fetched"
    # Polled past the unchanged attempt instead of stopping on the old one.
    assert result["calls"] == 3
    assert result["marked"] == 1


def test_a_failed_attempt_after_a_timeout_is_reported_with_its_reason() -> None:
    result = _run(
        {"status": 504},
        {},
        {"at": "2026-10-02T16:10:00+00:00", "status": "skipped", "reason": "HTTP 403"},
    )
    assert result["toasts"][-1] == ["error", "SI: skipped (HTTP 403)"]
    assert result["marked"] == 0


def test_a_real_error_is_shown_and_the_pills_reread_without_polling() -> None:
    result = _run(
        {"status": 400},
        {"detail": "bad request"},
        {"at": "2026-10-02T16:10:00+00:00", "status": "fetched", "reason": None},
    )
    assert result["toasts"] == [["error", "bad request"]]
    assert result["calls"] == 1
