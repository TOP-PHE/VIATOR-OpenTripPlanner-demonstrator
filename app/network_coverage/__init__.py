"""Network-coverage feature (v0.1.27).

Runs systematic A→B journey searches across a curated set of major
French rail hubs and persists the matrix for review + replay +
cross-session comparison.

Public API:
  hubs.HUBS            — the 23-station preset list (FR national-interest)
  runner.start_run()   — kick off a coverage run (background task)
  runner.collect_run() — assemble the matrix from persisted results

The admin UI lives at /admin/network-coverage and reuses the v0.1.26
journey trip-card components for the click-cell drilldown.
"""

from __future__ import annotations
