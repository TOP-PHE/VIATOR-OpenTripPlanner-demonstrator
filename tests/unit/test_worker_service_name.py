"""P1 MOTIS — engine-aware compose service name resolution.

Pre-Phase-1, the worker hard-coded `otp-{sid}` everywhere it needed a
per-session compose service name. That broke when a MOTIS session was
created (service is actually `motis-{sid}`): the reload-trigger handler
kept retrying `compose up -d otp-sp-rail-motis` and failing with
`no such service`. `_service_name_for` is the helper that closes that gap.
"""

from __future__ import annotations

from types import SimpleNamespace


def test_service_name_for_otp_session():
    from app.worker import _service_name_for

    s = SimpleNamespace(id="nap-fr-rail", engine="otp")
    assert _service_name_for(s) == "otp-nap-fr-rail"


def test_service_name_for_motis_session():
    from app.worker import _service_name_for

    s = SimpleNamespace(id="sp-rail-motis", engine="motis")
    assert _service_name_for(s) == "motis-sp-rail-motis"


def test_service_name_defaults_to_otp_when_engine_missing():
    """Legacy session rows (pre-Phase-1 migration backfill) shouldn't crash
    the worker if their engine attr is somehow None / empty / unset."""
    from app.worker import _service_name_for

    for legacy_engine in (None, ""):
        s = SimpleNamespace(id="legacy", engine=legacy_engine)
        assert _service_name_for(s) == "otp-legacy"

    # Truly missing attr (some unit-test fixtures don't set it):
    s = SimpleNamespace(id="legacy")
    assert _service_name_for(s) == "otp-legacy"
