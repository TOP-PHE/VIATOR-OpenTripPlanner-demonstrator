"""Monitoring configuration checks — issue #374.

The per-container panels of the "VIATOR — Resources" dashboard showed No data
for days without any test or alert noticing. These checks pin the static half
of the fix (the live half is the CadvisorNoContainerNames alert):

  * every expression of the Resources dashboard and of the alert rules is read
    by tests/unit/promql_reader.py (a recursive-descent reader of the PromQL
    subset used here: operators between operands, range functions with a
    range, known functions) and uses only metrics and labels cAdvisor,
    node-exporter and Prometheus export, label by label per metric (the
    cAdvisor container labels come from the compose whitelist);
  * the rule files have the shape Prometheus loads. Prometheus's own parser
    runs in CI: the "promtool" step checks prometheus.yml, every rule file and
    every dashboard query (scripts/dashboard_queries_as_rules.py); the tests
    below keep that step and its image tag in place;
  * every capped container in docker-compose.yml has no swap behind its cap;
  * cAdvisor is pinned to an exact release on ghcr.io, keeps its label
    whitelist and is told where containerd listens;
  * the deploy script waits for web to rewrite the session compose file
    before it recreates the session containers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.dashboard_queries_as_rules import dashboard_rules
from tests.unit.promql_reader import read_expr

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
DASHBOARD = ROOT / "docker" / "grafana" / "dashboards" / "viator-resources.json"
RULES_DIR = ROOT / "docker" / "prometheus" / "rules"
PROMETHEUS_YML = ROOT / "docker" / "prometheus" / "prometheus.yml"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_SH = ROOT / "bin" / "viator-deploy.sh"

# ── what each exporter exports (the subset VIATOR queries), per metric ───────

# Labels Prometheus adds to every scraped series, plus the static `service`
# label prometheus.yml sets on each job.
TARGET_LABELS = {"job", "instance", "service"}
# cAdvisor's own labels (lib/metrics/prometheus.go: id, name, image) plus the
# whitelisted container labels; `cpu` only on the CPU counter.
CADVISOR_BASE_LABELS = {"id", "name", "image"}
CADVISOR_METRICS: dict[str, set[str]] = {
    "container_cpu_usage_seconds_total": {"cpu"},
    "container_memory_swap": set(),
    "container_memory_working_set_bytes": set(),
    "container_oom_events_total": set(),
    "container_spec_memory_limit_bytes": set(),
    "container_start_time_seconds": set(),
}
NODE_METRICS: dict[str, set[str]] = {
    "node_cpu_seconds_total": {"cpu", "mode"},
    "node_filesystem_avail_bytes": {"mountpoint", "device", "fstype"},
    "node_memory_MemAvailable_bytes": set(),
    "node_memory_MemTotal_bytes": set(),
    "node_memory_SwapFree_bytes": set(),
    "node_memory_SwapTotal_bytes": set(),
}
PROMETHEUS_METRICS: dict[str, set[str]] = {"up": set()}


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data


def _cadvisor_command() -> list[str]:
    command: list[str] = _compose()["services"]["cadvisor"]["command"]
    return command


def _whitelisted_labels() -> set[str]:
    for arg in _cadvisor_command():
        if arg.startswith("--whitelisted_container_labels="):
            names = arg.split("=", 1)[1].split(",")
            return {"container_label_" + re.sub(r"[^A-Za-z0-9_]", "_", n) for n in names}
    raise AssertionError("cadvisor has no --whitelisted_container_labels")


def _labels_of(metric: str) -> set[str]:
    if metric in CADVISOR_METRICS:
        return (
            CADVISOR_BASE_LABELS | _whitelisted_labels() | CADVISOR_METRICS[metric] | TARGET_LABELS
        )
    if metric in NODE_METRICS:
        return NODE_METRICS[metric] | TARGET_LABELS
    if metric in PROMETHEUS_METRICS:
        return PROMETHEUS_METRICS[metric] | TARGET_LABELS
    raise AssertionError(f"metric {metric!r} is not exported by cadvisor/node-exporter/prometheus")


def check_expr(expr: str) -> set[str]:
    """Raise AssertionError unless `expr` reads and uses only exported metrics
    and labels. Returns the metric names it reads."""
    return read_expr(expr, _labels_of)


# ── the checker itself refuses what it must refuse ──────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        'container_memory_working_set_bytes{container_name="zz"}',  # label not exported
        'container_memory_working_set_bytes{name="zz"',  # unclosed brace
        "sum by (pod) (rate(container_cpu_usage_seconds_total[2m]))",  # grouping label
        "zz_made_up_metric_total",  # metric no exporter has
        "summ(container_memory_swap)",  # unknown function
        'container_memory_swap{name=~"("}',  # regex that does not compile
        'node_cpu_seconds_total{mode="idle" name="zz"}',  # matchers without comma
        "rate(container_cpu_usage_seconds_total)",  # range function without a range
        "absent(up[5m])",  # instant function given a range
        "container_memory_swap[5m]",  # a range as the result
        "container_memory_swap[5m] > 0",  # operator on a range
        "1 + container_memory_swap[5m]",  # range on the right of an operator
        "container_memory_swap container_memory_swap",  # two operands, no operator
        "container_memory_swap + * 2",  # two operators in a row
        "container_memory_swap +",  # operator without its right operand
        'node_memory_MemTotal_bytes{mode="idle"}',  # label of another node metric
        'container_memory_swap{cpu="total"}',  # cpu is on the CPU counter only
        "sum by (mountpoint) (node_memory_MemTotal_bytes)",  # grouping label of no metric here
        "container_memory_swap + bool 1",  # bool after a non-comparison
        "sum(rate(container_cpu_usage_seconds_total[2m])",  # unclosed paren
    ],
)
def test_expression_checker_refuses(bad: str) -> None:
    with pytest.raises((AssertionError, re.error)):
        check_expr(bad)


@pytest.mark.parametrize(
    "good",
    [
        'container_memory_working_set_bytes{name!=""} / on(name) '
        '((container_spec_memory_limit_bytes{name!=""} > 0) '
        "< on() group_left() max(node_memory_MemTotal_bytes)) * 100",
        '100 - (avg(irate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)',
        'up{job="zz"} == 0 or absent(up{job="zz"})',
        "sum by (name) (changes(container_start_time_seconds[1h])) > 3",
        "count(container_memory_swap) by (name) >= bool 1",
    ],
)
def test_expression_checker_accepts(good: str) -> None:
    assert check_expr(good)


def test_expression_checker_returns_the_metrics() -> None:
    expr = (
        'container_memory_working_set_bytes{name!=""} / on(name) '
        '(container_spec_memory_limit_bytes{name!=""} < on() group_left() '
        "max(node_memory_MemTotal_bytes))"
    )
    assert check_expr(expr) == {
        "container_memory_working_set_bytes",
        "container_spec_memory_limit_bytes",
        "node_memory_MemTotal_bytes",
    }


# ── dashboard ────────────────────────────────────────────────────────────────


def _dashboard() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return data


def _dashboard_exprs() -> list[str]:
    return [t["expr"] for p in _dashboard()["panels"] for t in p.get("targets", [])]


@pytest.mark.parametrize("index", range(len(_dashboard_exprs())))
def test_every_dashboard_expression_uses_exported_metrics_and_labels(index: int) -> None:
    check_expr(_dashboard_exprs()[index])


def test_container_panels_select_every_named_container() -> None:
    """`name!=""` rather than a naming pattern: every container cAdvisor
    names (VIATOR, MSMM, build containers) shows, and an empty panel then
    means only one thing — cAdvisor lost the Docker names."""
    container_exprs = [e for e in _dashboard_exprs() if "container_" in e]
    assert container_exprs
    for expr in container_exprs:
        assert 'name=~"' not in expr
        assert 'name!=""' in expr


def test_swap_by_container_and_iowait_panels_exist() -> None:
    by_title = {p["title"]: p for p in _dashboard()["panels"]}
    swap = by_title["Swap by container"]
    assert swap["targets"][0]["expr"] == 'container_memory_swap{name!=""}'
    assert swap["targets"][0]["legendFormat"] == "{{name}}"
    iowait = by_title["Host iowait"]
    assert 'mode="iowait"' in iowait["targets"][0]["expr"]
    assert iowait["fieldConfig"]["defaults"]["unit"] == "percent"


def test_dashboard_panels_have_unique_ids_and_do_not_overlap() -> None:
    panels = _dashboard()["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    cells: set[tuple[int, int]] = set()
    for p in panels:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, f"panel {p['id']} overlaps another"
                cells.add((x, y))


# ── alert rules ──────────────────────────────────────────────────────────────


def _rule_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.yml"))


def _rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for path in _rule_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(doc) == {"groups"}, path
        for group in doc["groups"]:
            assert set(group) <= {"name", "interval", "rules"}, path
            assert group["name"], path
            assert group["rules"], path
            rules += group["rules"]
    return rules


def test_prometheus_loads_every_rule_file() -> None:
    cfg = yaml.safe_load(PROMETHEUS_YML.read_text(encoding="utf-8"))
    assert cfg["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    compose_volumes = _compose()["services"]["prometheus"]["volumes"]
    assert "./prometheus/rules:/etc/prometheus/rules:ro" in compose_volumes


@pytest.mark.parametrize("index", range(len(_rules())))
def test_every_rule_is_well_formed(index: int) -> None:
    rule = _rules()[index]
    assert set(rule) <= {"alert", "expr", "for", "labels", "annotations"}, rule
    assert re.fullmatch(r"[A-Z][A-Za-z0-9]+", rule["alert"])
    if "for" in rule:
        assert re.fullmatch(r"\d+[smh]", rule["for"]), rule["for"]
    assert rule["labels"]["severity"] in {"page", "warning"}
    assert rule["annotations"]["summary"]
    check_expr(rule["expr"])


def test_alert_names_are_unique() -> None:
    names = [r["alert"] for r in _rules()]
    assert len(names) == len(set(names))


def _rule(name: str) -> dict[str, Any]:
    return next(r for r in _rules() if r["alert"] == name)


@pytest.mark.parametrize(
    "name,threshold,duration",
    [("HostSwapUsageHigh", "> 50", "10m"), ("HostIowaitHigh", "> 30", "10m")],
)
def test_host_alerts_thresholds(name: str, threshold: str, duration: str) -> None:
    rule = _rule(name)
    assert rule["expr"].rstrip().endswith(threshold)
    assert rule["for"] == duration


def test_swap_alert_ignores_hosts_without_swap() -> None:
    assert "(node_memory_SwapTotal_bytes > 0)" in _rule("HostSwapUsageHigh")["expr"]


def test_iowait_alert_reads_the_iowait_mode() -> None:
    assert 'node_cpu_seconds_total{mode="iowait"}' in _rule("HostIowaitHigh")["expr"]


def test_cadvisor_down_alert_covers_a_missing_target() -> None:
    expr = _rule("CadvisorDown")["expr"]
    assert 'up{job="cadvisor"} == 0' in expr
    assert 'absent(up{job="cadvisor"})' in expr


def test_blind_spot_alert_needs_named_series_while_cadvisor_is_up() -> None:
    rule = _rule("CadvisorNoContainerNames")
    assert rule["for"] == "10m"
    assert 'absent(container_memory_working_set_bytes{name!=""})' in rule["expr"]
    assert 'up{job="cadvisor"} == 1' in rule["expr"]


def test_oom_alert_reads_cadvisor_oom_events() -> None:
    assert "container_oom_events_total" in _rule("ContainerOOMKilled")["expr"]
    disabled = next(a for a in _cadvisor_command() if a.startswith("--disable_metrics="))
    groups = disabled.split("=", 1)[1].split(",")
    assert "oom_event" not in groups
    assert "memory" not in groups


@pytest.mark.parametrize("path", _rule_files(), ids=lambda p: p.name)
def test_every_rule_file_keeps_the_no_alertmanager_note(path: Path) -> None:
    assert "no Alertmanager" in path.read_text(encoding="utf-8")


# ── compose: caps without swap, cAdvisor pin ─────────────────────────────────


def capped_without_swap_problems(services: dict[str, Any]) -> list[str]:
    """Names of services whose memory cap leaves swap open."""
    problems = []
    for name, svc in services.items():
        if "mem_limit" in svc and svc.get("memswap_limit") != svc["mem_limit"]:
            problems.append(name)
        if "memswap_limit" in svc and "mem_limit" not in svc:
            problems.append(name)
    return problems


def test_every_capped_service_has_no_swap() -> None:
    services = _compose()["services"]
    assert "mem_limit" in services["otp-build"]
    assert capped_without_swap_problems(services) == []


def test_swap_check_catches_a_cap_with_swap() -> None:
    services = {
        "zz-a": {"mem_limit": "8g"},
        "zz-b": {"mem_limit": "8g", "memswap_limit": "16g"},
        "zz-c": {"memswap_limit": "8g"},
        "zz-d": {"mem_limit": "8g", "memswap_limit": "8g"},
    }
    assert capped_without_swap_problems(services) == ["zz-a", "zz-b", "zz-c"]


def test_otp_build_cap_and_swap_follow_the_same_variable() -> None:
    build = _compose()["services"]["otp-build"]
    assert build["mem_limit"] == "${OTP_BUILD_MEM_LIMIT:-12g}"
    assert build["memswap_limit"] == "${OTP_BUILD_MEM_LIMIT:-12g}"


def test_cadvisor_is_pinned_to_an_exact_ghcr_release() -> None:
    image = _compose()["services"]["cadvisor"]["image"]
    assert re.fullmatch(r"ghcr\.io/google/cadvisor:v\d+\.\d+\.\d+", image), image
    major, minor, _patch = (int(x) for x in image.rsplit(":v", 1)[1].split("."))
    # containerd image store support (0.54) + its fix for hosts without it (0.56)
    assert (major, minor) >= (0, 56)


def test_cadvisor_keeps_its_label_whitelist_and_finds_containerd() -> None:
    command = _cadvisor_command()
    assert "--store_container_labels=false" in command
    assert (
        "--whitelisted_container_labels="
        "com.docker.compose.service,viator.session_id,viator.category,viator.autoheal"
    ) in command
    assert "--containerd=/var/run/containerd/containerd.sock" in command
    volumes = _compose()["services"]["cadvisor"]["volumes"]
    assert "/var/run:/var/run:rw" in volumes
    assert "/var/lib/docker/:/var/lib/docker:ro" in volumes
    assert _compose()["services"]["cadvisor"]["cgroup"] == "host"


def test_the_autoheal_alert_label_is_whitelisted() -> None:
    assert "container_label_viator_autoheal" in _whitelisted_labels()


@pytest.mark.parametrize(
    "service,variable",
    [
        ("web", "OTP_SERVE_MEM_CAP"),
        ("web", "OTP_SERVE_MEM_HEADROOM_GB"),
        ("web", "MOTIS_SERVE_MEM_LIMIT"),
        ("web", "OTP_SERVE_GC_MODE"),
        ("web", "OTP_SERVE_SOFT_MAX_HEAP"),
        ("web", "PROMETHEUS_URL"),
        ("worker", "MOTIS_BUILD_MEM_LIMIT"),
        ("worker", "OTP_BUILD_MEM_HEADROOM_GB"),
    ],
)
def test_memory_settings_reach_the_container_that_reads_them(service: str, variable: str) -> None:
    assert variable in _compose()["services"][service]["environment"]


def test_builds_are_killed_first_when_the_host_runs_out_of_memory() -> None:
    from app.memory_caps import BUILD_OOM_SCORE_ADJ

    services = _compose()["services"]
    assert BUILD_OOM_SCORE_ADJ == 500
    assert services["otp-build"]["oom_score_adj"] == BUILD_OOM_SCORE_ADJ
    for name in ("web", "worker", "postgres", "nginx"):
        assert "oom_score_adj" not in services[name]


# ── restart signal for long-running containers ──────────────────────────────


def test_restart_alert_watches_the_serve_containers_only() -> None:
    rule = _rule("SessionContainerRestarted")
    assert rule["expr"] == (
        'changes(container_start_time_seconds{name=~"viator-(otp|motis)-.+"}[15m]) > 0'
    )
    pattern = re.compile(r"viator-(otp|motis)-.+")
    for name in ("viator-otp-zz-rail-1", "viator-motis-zz-motis-1"):
        assert pattern.fullmatch(name)
    for name in ("viator-web-1", "viator-build-99", "viator-postgres-1"):
        assert not pattern.fullmatch(name)


def test_restart_alert_does_not_duplicate_the_autoheal_alert() -> None:
    autoheal = _rule("AutohealExcessiveRestarts")
    assert autoheal["expr"].rstrip().endswith("> 3")
    assert "[1h]" in autoheal["expr"]
    assert "[15m]" in _rule("SessionContainerRestarted")["expr"]


# ── promtool in CI ───────────────────────────────────────────────────────────


def _promtool_step() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["python"]["steps"]
    step: dict[str, Any] = next(s for s in steps if s.get("name", "").startswith("promtool"))
    return step


def test_ci_checks_config_rules_and_dashboard_queries_with_promtool() -> None:
    run = _promtool_step()["run"]
    assert "python scripts/dashboard_queries_as_rules.py" in run
    assert "check config /etc/prometheus/prometheus.yml" in run
    assert 'check rules "${rule_files[@]}" /promql/dashboard-queries.yml' in run
    assert "set -euo pipefail" in run


def test_ci_promtool_is_the_prometheus_that_runs() -> None:
    image = _compose()["services"]["prometheus"]["image"]
    assert re.fullmatch(r"prom/prometheus:v\d+\.\d+\.\d+", image)
    assert f" {image})" in _promtool_step()["run"]


def test_every_dashboard_query_becomes_a_rule_for_promtool() -> None:
    rules = dashboard_rules()["groups"][0]["rules"]  # type: ignore[index]
    exprs = [r["expr"] for r in rules]
    assert set(_dashboard_exprs()) <= set(exprs)
    names = [r["record"] for r in rules]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", n) for n in names)


def test_dashboard_rules_skip_non_prometheus_panels(tmp_path: Path) -> None:
    board = {
        "panels": [
            {"id": 1, "datasource": {"type": "loki"}, "targets": [{"expr": '{zz="a"}'}]},
            {
                "id": 2,
                "datasource": {"type": "prometheus"},
                "targets": [{"refId": "B", "expr": "up"}],
            },
        ]
    }
    (tmp_path / "zz-board.json").write_text(json.dumps(board), encoding="utf-8")
    rules = dashboard_rules(tmp_path)["groups"][0]["rules"]  # type: ignore[index]
    assert rules == [{"record": "dashboard:zz_board:panel2_B", "expr": "up"}]


# ── deploy: caps reach the session containers at deploy time ─────────────────


def _deploy_lines() -> list[str]:
    script = DEPLOY_SH.read_text(encoding="utf-8")
    return [line.split("#", 1)[0].strip() for line in script.splitlines()]


def test_deploy_waits_for_web_before_recreating_the_sessions() -> None:
    lines = _deploy_lines()
    recreate_web = lines.index("docker compose up -d --force-recreate web worker")
    wait = next(i for i, line in enumerate(lines) if line.startswith("WEB_REGEN_TIMEOUT="))
    plain_up = lines.index("docker compose up -d")
    assert recreate_web < wait
    assert wait < plain_up
    assert lines.index("web_recreated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)") < recreate_web


def _wait_block() -> str:
    script = DEPLOY_SH.read_text(encoding="utf-8")
    start = script.index("WEB_REGEN_TIMEOUT=")
    end = script.index("done\n", start) + len("done\n")
    return (
        script[start:end]
        .replace("WEB_REGEN_TIMEOUT=300", "WEB_REGEN_TIMEOUT=2")
        .replace("sleep 5", "sleep 0.01")
    )


@pytest.mark.parametrize(
    "web_log,code,said",
    [
        ("zz\nsessions_orchestrator.regenerated_at_boot\n", 0, ""),
        ("zz\nsessions_orchestrator.boot_regenerate_failed\n", 5, "boot_regenerate_failed"),
        ("zz still starting\n", 5, "no regenerated_at_boot from web after 2s"),
    ],
)
def test_deploy_wait_block(tmp_path: Path, web_log: str, code: int, said: str) -> None:
    fake = tmp_path / "docker"
    # Answers only `docker compose logs ... web`: reading another service's
    # log (or none) makes the fake fail, so the wait would time out.
    fake.write_text(
        '#!/bin/sh\ncase "$*" in\n  "compose logs "*" web") printf "%s" "$ZZ_WEB_LOG" ;;\n'
        '  *) echo "zz unexpected: $*" >&2; exit 9 ;;\nesac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    script = tmp_path / "wait.sh"
    script.write_text(
        "set -euo pipefail\nweb_recreated_at=2026-01-01T00:00:00Z\n" + _wait_block(),
        encoding="utf-8",
    )
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ZZ_WEB_LOG": web_log}
    done = subprocess.run(
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == code, done.stderr
    assert said in done.stderr
