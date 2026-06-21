"""Max-memory rebuild — the pure service-selection helper (v0.1.38).

`_max_memory_stop_targets` decides which compose services the worker stops to
free the box for a worst-case build: every serving session's per-engine
service plus the fixed observability stack. The core stack
(postgres/web/worker/nginx) must NEVER be in the list — stopping any of those
would kill the build itself or the UI.

Post-Phase-1 (v0.1.43.04): the helper now takes already-resolved compose
service names rather than raw session ids, so MOTIS sessions can be
included as `motis-<sid>`. The engine-aware name resolution lives in
`_serving_session_services`.
"""

from __future__ import annotations


def test_observability_services_always_included_even_with_no_sessions():
    from app.worker import _OBSERVABILITY_SERVICES, _max_memory_stop_targets

    assert _max_memory_stop_targets([]) == list(_OBSERVABILITY_SERVICES)


def test_passed_service_names_are_preserved_verbatim():
    """The helper no longer rewrites sids → service names. Caller passes the
    already-resolved per-engine names (`otp-<sid>`, `motis-<sid>`) via
    `_serving_session_services`."""
    from app.worker import _max_memory_stop_targets

    targets = _max_memory_stop_targets(["otp-nap-fr-rail", "motis-sp-rail-motis"])
    assert "otp-nap-fr-rail" in targets
    assert "motis-sp-rail-motis" in targets
    # And NOT mangled back into otp-* form for the MOTIS one:
    assert "otp-sp-rail-motis" not in targets


def test_core_stack_is_never_stopped():
    from app.worker import _max_memory_stop_targets

    targets = _max_memory_stop_targets(["otp-nap-fr-rail"])
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
