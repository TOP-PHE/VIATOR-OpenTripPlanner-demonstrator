"""What a session's id and its settings may be, checked in one place.

A session's id and several keys of its `config` end up in text other programs
read: the compose fragment and the nginx fragment `sessions_orchestrator`
renders, and the arguments of the build containers. The API checks them here
when a session is created and when it is saved, and the orchestrator checks
again what it is about to render.
"""

from __future__ import annotations

import re
from typing import Any

from . import (
    ingestion,
    osm_filter,
    osm_geo,
    otp_api_timeout,
    otp_heap,
    otp_start_period,
    otp_timezone,
)

# A lower-case letter, then 1 to 62 letters, digits or hyphens. Matched with
# `fullmatch`: `$` in a `match` also accepts a line feed at the end, and the
# id is written as is into a YAML key and an nginx `location`.
_SESSION_ID_RE = re.compile(r"[a-z][a-z0-9-]{1,62}")


def is_session_id(value: object) -> bool:
    """Whether `value` is a session id the fragments can carry as is."""
    return isinstance(value, str) and _SESSION_ID_RE.fullmatch(value) is not None


def validate_settings(config: dict[str, Any]) -> None:
    """Check the per-session settings of `config`; raise ValueError on the first bad one.

    Each setting present is replaced by its canonical form (`24G` becomes
    `24g`). A key that is absent is left absent: the worker and the
    orchestrator apply the default. The providers under `sources` are not
    looked at here (`ingestion.normalize_providers`).
    """
    if "osm_scope" in config:
        config["osm_scope"] = osm_filter.validate_scope(config["osm_scope"])
    if "otp_timezone" in config:
        config["otp_timezone"] = otp_timezone.validate_timezone(config["otp_timezone"])
    if "otp_build_heap" in config:
        config["otp_build_heap"] = otp_heap.validate_heap(config["otp_build_heap"])
    if "otp_heap" in config:
        config["otp_heap"] = otp_heap.validate_heap(config["otp_heap"])
    if "otp_api_timeout" in config:
        config["otp_api_timeout"] = otp_api_timeout.validate_timeout(config["otp_api_timeout"])
    if "otp_serve_start_period" in config:
        # Checked, not rewritten: the orchestrator reads an int or its digits alike.
        otp_start_period.validate_start_period(config["otp_serve_start_period"])
    # True or false only, so a stray string never turns max-memory builds on
    # (`ingestion.wants_max_memory` reads only a literal true).
    if ingestion.BUILD_MAX_MEMORY in config and not isinstance(
        config[ingestion.BUILD_MAX_MEMORY], bool
    ):
        raise ValueError(f"{ingestion.BUILD_MAX_MEMORY} must be true or false")
    if "osm_countries" in config:
        config["osm_countries"] = osm_geo.validate_countries(config["osm_countries"])
