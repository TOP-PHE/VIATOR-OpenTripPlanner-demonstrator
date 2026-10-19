"""Monitoring configuration checks — issue #374.

The per-container panels of the "VIATOR — Resources" dashboard showed No data
for days without any test or alert noticing. These checks pin the static half
of the fix (the live half is the CadvisorNoContainerNames alert):

  * every dashboard and alert expression is well formed and uses only metrics
    and labels that cAdvisor, node-exporter and Prometheus itself export (the
    cAdvisor container labels come from the compose whitelist);
  * the rule files have the shape Prometheus loads (no promtool in CI, so a
    structural check);
  * every capped container in docker-compose.yml has no swap behind its cap;
  * cAdvisor is pinned to an exact release on ghcr.io, keeps its label
    whitelist and is told where containerd listens.

The expression check is a small structural reader, not a full PromQL parser:
it checks brackets, label matchers, grouping clauses, function names and
metric names, which is what went wrong in practice (a label that is not
exported matches nothing, silently).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
DASHBOARD = ROOT / "docker" / "grafana" / "dashboards" / "viator-resources.json"
RULES_DIR = ROOT / "docker" / "prometheus" / "rules"
PROMETHEUS_YML = ROOT / "docker" / "prometheus" / "prometheus.yml"

# ── what each exporter exports (the subset VIATOR queries) ───────────────────

CADVISOR_METRICS = {
    "container_cpu_usage_seconds_total",
    "container_memory_swap",
    "container_memory_working_set_bytes",
    "container_oom_events_total",
    "container_spec_memory_limit_bytes",
    "container_start_time_seconds",
}
NODE_METRICS = {
    "node_cpu_seconds_total",
    "node_filesystem_avail_bytes",
    "node_memory_MemAvailable_bytes",
    "node_memory_MemTotal_bytes",
    "node_memory_SwapFree_bytes",
    "node_memory_SwapTotal_bytes",
}
PROMETHEUS_METRICS = {"up"}

# Labels Prometheus adds to every scraped series, plus the static `service`
# label prometheus.yml sets on each job.
TARGET_LABELS = {"job", "instance", "service"}
# cAdvisor's own labels (lib/metrics/prometheus.go: id, name, image; `cpu` on
# the CPU counters). Container labels are added from the compose whitelist.
CADVISOR_BASE_LABELS = {"id", "name", "image", "cpu"}
NODE_LABELS = {"cpu", "mode", "mountpoint", "device", "fstype"}

FUNCTIONS = {"absent", "avg", "changes", "count", "increase", "irate", "max", "min", "rate", "sum"}
KEYWORDS = {"and", "or", "unless", "bool", "offset"}
GROUPING = ("by", "without", "on", "ignoring", "group_left", "group_right")

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_SELECTOR = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)?\s*\{([^{}]*)\}")
_MATCHER = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*")\s*(,|$)')
_GROUP_CLAUSE = re.compile(r"\b(" + "|".join(GROUPING) + r")\s*\(([^()]*)\)")
_RANGE = re.compile(r"\[\s*\d+[smhdw]\s*\]")
_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_NUMBER = re.compile(r"(?<![A-Za-z_:])\d+(?:\.\d+)?(?:e[+-]?\d+)?")


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
        return CADVISOR_BASE_LABELS | _whitelisted_labels() | TARGET_LABELS
    if metric in NODE_METRICS:
        return NODE_LABELS | TARGET_LABELS
    if metric in PROMETHEUS_METRICS:
        return set(TARGET_LABELS)
    raise AssertionError(f"metric {metric!r} is not exported by cadvisor/node-exporter/prometheus")


def _check_brackets(expr: str) -> None:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in _STRING.sub('""', expr):
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            assert stack, f"unbalanced {ch!r} in {expr!r}"
            assert stack[-1] == pairs[ch], f"unbalanced {ch!r} in {expr!r}"
            stack.pop()
    assert not stack, f"unclosed {stack} in {expr!r}"


def _check_matchers(metric: str, body: str, expr: str) -> None:
    """Each label matcher of a selector parses and names an exported label."""
    allowed = _labels_of(metric)
    pos = 0
    while body[pos:].strip():
        mm = _MATCHER.match(body, pos)
        assert mm, f"bad label matcher {body[pos:]!r} in {expr!r}"
        assert mm.group(1) in allowed, f"{metric} has no label {mm.group(1)!r}"
        if mm.group(2) in ("=~", "!~"):
            re.compile(json.loads(mm.group(3)))
        pos = mm.end()


def _bare_words(rest: str, expr: str) -> set[str]:
    """Metrics named outside selectors; every word before "(" is a function."""
    metrics: set[str] = set()
    for t in _IDENT.finditer(rest):
        word = t.group(0)
        if word in KEYWORDS:
            continue
        if rest[t.end() :].lstrip().startswith("("):
            assert word in FUNCTIONS, f"unknown function {word!r} in {expr!r}"
        else:
            _labels_of(word)
            metrics.add(word)
    return metrics


def check_expr(expr: str) -> set[str]:
    """Raise AssertionError unless `expr` is well formed and uses only exported
    metrics and labels. Returns the metric names it reads."""
    _check_brackets(expr)
    metrics: set[str] = set()
    # 1. Selectors with label matchers.
    for m in _SELECTOR.finditer(expr):
        assert m.group(1), f"selector without a metric name in {expr!r}"
        _check_matchers(m.group(1), m.group(2), expr)
        metrics.add(m.group(1))
    rest = _SELECTOR.sub(lambda m: f" {m.group(1)} ", _STRING.sub('""', expr))
    # 2. Grouping clauses: labels of the series being grouped or matched.
    grouping_labels = [
        x.strip() for g in _GROUP_CLAUSE.finditer(rest) for x in g.group(2).split(",") if x.strip()
    ]
    rest = _NUMBER.sub(" ", _RANGE.sub(" ", _GROUP_CLAUSE.sub(" ", rest)))
    # 3. What is left: functions (followed by "("), keywords and bare metrics.
    metrics |= _bare_words(rest, expr)
    assert metrics, f"no metric in {expr!r}"
    known = set().union(*(_labels_of(m) for m in metrics))
    for label in grouping_labels:
        assert label in known, f"grouping label {label!r} is not exported in {expr!r}"
    return metrics


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
    ],
)
def test_expression_checker_refuses(bad: str) -> None:
    with pytest.raises((AssertionError, re.error)):
        check_expr(bad)


def test_expression_checker_accepts_the_vector_matching_forms() -> None:
    expr = (
        'container_memory_working_set_bytes{name!=""} / on(name) '
        '((container_spec_memory_limit_bytes{name!=""} > 0) '
        "< on() group_left() max(node_memory_MemTotal_bytes)) * 100"
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
        ("worker", "MOTIS_BUILD_MEM_LIMIT"),
    ],
)
def test_memory_settings_reach_the_container_that_reads_them(service: str, variable: str) -> None:
    assert variable in _compose()["services"][service]["environment"]
