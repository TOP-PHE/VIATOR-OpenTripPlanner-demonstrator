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

    assert _max_memory_stop_targets([]) == ["autoheal", *_OBSERVABILITY_SERVICES]


def test_autoheal_is_stopped_before_the_sessions():
    """Stopped first so it cannot restart a session container mid-stop."""
    from app.worker import _max_memory_stop_targets

    targets = _max_memory_stop_targets(["motis-eu19-transit-motis"])
    assert targets.index("autoheal") < targets.index("motis-eu19-transit-motis")


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


class _FakeCompose:
    """Stands in for `_compose`: records calls and answers `ps` from `running`."""

    def __init__(self, running_after_stop: list[set[str]]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._running = running_after_stop

    def __call__(self, *args: str):
        from subprocess import CompletedProcess

        self.calls.append(args)
        out = ""
        if args[0] == "ps":
            running = self._running[0] if len(self._running) == 1 else self._running.pop(0)
            out = "\n".join(s for s in args[4:] if s in running)
        return CompletedProcess(list(args), 0, stdout=out, stderr="")


def test_stop_checks_and_is_done_when_everything_stopped(monkeypatch):
    from app import worker

    fake = _FakeCompose([set()])
    monkeypatch.setattr(worker, "_compose", fake)
    worker._stop_services(["autoheal", "otp-a", "grafana"])
    assert [c[0] for c in fake.calls] == ["stop", "ps"]


def test_stop_retries_containers_that_came_back(monkeypatch, caplog):
    from app import worker

    fake = _FakeCompose([{"otp-a"}, set()])
    monkeypatch.setattr(worker, "_compose", fake)
    worker._stop_services(["autoheal", "otp-a", "grafana"])
    assert fake.calls[2] == ("stop", "otp-a")
    assert "still running after stop" in caplog.text
    assert "could not be stopped" not in caplog.text


def test_stop_reports_containers_it_cannot_stop(monkeypatch, caplog):
    from app import worker

    fake = _FakeCompose([{"otp-a"}])
    monkeypatch.setattr(worker, "_compose", fake)
    worker._stop_services(["otp-a", "grafana"])
    assert "could not be stopped" in caplog.text


def test_running_services_is_empty_when_compose_fails(monkeypatch):
    from subprocess import CompletedProcess

    from app import worker

    monkeypatch.setattr(
        worker, "_compose", lambda *a: CompletedProcess(list(a), 1, stdout="", stderr="boom")
    )
    assert worker._running_services(["otp-a"]) == []
