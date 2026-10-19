"""Write every Prometheus query of the Grafana dashboards as a rule file.

`promtool check rules` then parses each query with Prometheus's own parser
(issue #374): CI runs it on the output (.github/workflows/ci.yml, step
"promtool"), so a dashboard query that Prometheus cannot parse fails CI
instead of showing an empty panel. The rules are recording rules named after
the dashboard and panel; they are never loaded by Prometheus.

Usage: python scripts/dashboard_queries_as_rules.py <output.yml>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

DASHBOARDS = Path(__file__).resolve().parents[1] / "docker" / "grafana" / "dashboards"


def dashboard_rules(dashboards: Path = DASHBOARDS) -> dict[str, object]:
    """One recording rule per Prometheus target of every dashboard."""
    rules = []
    for path in sorted(dashboards.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        slug = re.sub(r"\W", "_", path.stem)
        for panel in board.get("panels", []):
            if (panel.get("datasource") or {}).get("type") != "prometheus":
                continue
            for target in panel.get("targets", []):
                name = f"dashboard:{slug}:panel{panel['id']}_{target.get('refId', 'A')}"
                rules.append({"record": name, "expr": target["expr"]})
    return {"groups": [{"name": "dashboard-queries", "rules": rules}]}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    Path(argv[1]).write_text(yaml.safe_dump(dashboard_rules(), sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
