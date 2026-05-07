"""Worker orphan-cleanup parsing — `_parse_otp_service_names`.

Audit-2026-05 #25. The previous orphan-cleanup logic in `handle_reload_trigger`
used `docker ps` (running only) which missed Exited (143) containers from
deleted sessions. This module validates the parsing helper now used to turn
`docker ps -a --format {{.Names}}` output into the set of compose service
names the orchestrator should compare against `expected_otp_services`.
"""

from __future__ import annotations


def test_per_session_serve_container_yields_service_name():
    from app.worker import _parse_otp_service_names

    out = "viator-otp-fr-rail-1\n"
    assert _parse_otp_service_names(out) == {"otp-fr-rail"}


def test_multiple_sessions_dedup_into_distinct_services():
    from app.worker import _parse_otp_service_names

    out = (
        "viator-otp-fr-rail-1\n" "viator-otp-eu-nap-network-1\n" "viator-otp-2026-W19_2026-W31-1\n"
    )
    assert _parse_otp_service_names(out) == {
        "otp-fr-rail",
        "otp-eu-nap-network",
        "otp-2026-W19_2026-W31",
    }


def test_exited_containers_are_extracted_same_as_running():
    """`docker ps -a` lists exited containers by name only; the parser is
    state-agnostic. This is the bug-of-record from audit #25 — the prior
    code used `docker ps` without `-a` so Exited (143) orphans never made
    it to this parsing step."""
    from app.worker import _parse_otp_service_names

    # Same name format whether the container is Up or Exited; only the
    # listing scope matters at the docker ps level.
    out = "viator-otp-deleted-session-1\n"
    assert _parse_otp_service_names(out) == {"otp-deleted-session"}


def test_ephemeral_build_container_is_skipped():
    """Build containers spawned via `docker compose run --rm otp-build` get
    auto-named `viator-otp-build-run-<hex>`. They match the name filter but
    are NOT compose services we manage; trying to `compose rm` them
    produces noisy errors. The parser must skip them."""
    from app.worker import _parse_otp_service_names

    out = (
        "viator-otp-fr-rail-1\n"
        "viator-otp-build-run-15f454b50111abcdef\n"  # ephemeral build, skip
        "viator-otp-eu-nap-network-1\n"
    )
    assert _parse_otp_service_names(out) == {
        "otp-fr-rail",
        "otp-eu-nap-network",
    }


def test_session_id_with_underscores_and_digits_round_trips():
    """Session ids allow `-`, `_`, and digits. A session id like
    `2026-W19_2026-W31` should round-trip cleanly through the parser
    (it was the actual session that triggered audit-2026-05 #25)."""
    from app.worker import _parse_otp_service_names

    out = "viator-otp-2026-W19_2026-W31-1\n"
    assert _parse_otp_service_names(out) == {"otp-2026-W19_2026-W31"}


def test_empty_output_yields_empty_set():
    from app.worker import _parse_otp_service_names

    assert _parse_otp_service_names("") == set()
    assert _parse_otp_service_names("\n\n   \n") == set()


def test_unrelated_viator_containers_are_ignored():
    """The `name=^viator-otp-` docker filter should already exclude these,
    but the parser is defensive — non-`viator-otp-` lines (or non-`viator-`
    lines somehow leaking through) are silently skipped."""
    from app.worker import _parse_otp_service_names

    out = (
        "viator-web-1\n"  # filtered upstream, but defensive skip here
        "viator-otp-fr-rail-1\n"
        "viator-postgres-1\n"
        "some-unrelated-container\n"  # not viator at all
    )
    assert _parse_otp_service_names(out) == {"otp-fr-rail"}


def test_replica_suffix_is_stripped_only_when_numeric():
    """Standard compose replica naming uses `-1`, `-2`, … as the trailing
    segment. A non-numeric trailing segment is unusual; the parser keeps
    the whole inner so the orphan cleanup at least surfaces the anomaly
    rather than dropping it silently."""
    from app.worker import _parse_otp_service_names

    out = "viator-otp-someweirdname-abc\n"  # `abc` not numeric
    # Parser keeps the whole inner so the unexpected shape surfaces in logs.
    assert _parse_otp_service_names(out) == {"otp-someweirdname-abc"}
