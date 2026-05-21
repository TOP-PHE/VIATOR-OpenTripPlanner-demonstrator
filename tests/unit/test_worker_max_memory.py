"""Max-memory rebuild — the pure service-selection helper (v0.1.38).

`_max_memory_stop_targets` decides which compose services the worker stops to
free the box for a worst-case build: every serving OTP session plus the fixed
observability stack. The core stack (postgres/web/worker/nginx) must NEVER be
in the list — stopping any of those would kill the build itself or the UI.
"""

from __future__ import annotations


def test_observability_services_always_included_even_with_no_sessions():
    from app.worker import _OBSERVABILITY_SERVICES, _max_memory_stop_targets

    assert _max_memory_stop_targets([]) == list(_OBSERVABILITY_SERVICES)


def test_serving_sessions_become_otp_service_names():
    from app.worker import _max_memory_stop_targets

    targets = _max_memory_stop_targets(["nap-fr-rail", "nap-ch-rail"])
    assert "otp-nap-fr-rail" in targets
    assert "otp-nap-ch-rail" in targets


def test_core_stack_is_never_stopped():
    from app.worker import _max_memory_stop_targets

    targets = _max_memory_stop_targets(["nap-fr-rail"])
    for core in ("postgres", "web", "worker", "nginx", "otp-build"):
        assert core not in targets


def test_observability_set_is_what_we_expect():
    from app.worker import _OBSERVABILITY_SERVICES

    assert set(_OBSERVABILITY_SERVICES) == {
        "grafana",
        "loki",
        "promtail",
        "prometheus",
        "cadvisor",
        "node-exporter",
        "tempo",
    }
