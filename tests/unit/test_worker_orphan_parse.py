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

    # Explicit \n concatenation rather than three implicit-adjacent string
    # literals — ruff-format collapses adjacent literals onto one line when
    # they fit within line-length (here at ~95 chars), and SonarCloud's
    # S5799 then flags the result as ambiguous (could be three strings the
    # author forgot to comma-separate). Single literal sidesteps both.
    out = "\n".join(
        [
            "viator-otp-fr-rail-1",
            "viator-otp-eu-nap-network-1",
            "viator-otp-2026-W19_2026-W31-1",
            "",  # trailing newline to match docker ps's actual output shape
        ]
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


# ──────────────────── Non-compose OTP container detection (audit #27) ────────────────────


def test_non_compose_otp_container_is_detected():
    """The bug-of-record from audit #27: `wizardly_pasteur` ran our OTP
    image (`ghcr.io/top-phe/viator-otp:v0.1.30`) for 45 hours after a
    manual `docker run` debug invocation. Audit #25's `name=^viator-otp-`
    filter missed it. This pass catches it and logs a warning."""
    from app.worker import _find_non_compose_otp_containers

    out = "wizardly_pasteur\tghcr.io/top-phe/viator-otp:v0.1.30\tUp 45 hours\n"
    assert _find_non_compose_otp_containers(out) == [
        ("wizardly_pasteur", "ghcr.io/top-phe/viator-otp:v0.1.30", "Up 45 hours"),
    ]


def test_compose_otp_containers_are_not_flagged():
    """Compose-managed `viator-otp-<sid>-1` containers ARE running our OTP
    image but should be handled by audit #25's cleanup, not flagged as
    non-compose orphans. The non-compose check skips anything whose name
    starts with `viator-`."""
    from app.worker import _find_non_compose_otp_containers

    out = (
        "viator-otp-fr-rail-1\tghcr.io/top-phe/viator-otp:v0.1.32.7\tUp 2 hours\n"
        "viator-otp-eu-nap-network-1\tghcr.io/top-phe/viator-otp:v0.1.32.7\tUp 5 minutes\n"
    )
    assert _find_non_compose_otp_containers(out) == []


def test_other_images_are_not_flagged():
    """Don't flag containers using other images (postgres, nginx, web)."""
    from app.worker import _find_non_compose_otp_containers

    out = (
        "some-postgres\tpostgres:16-alpine\tUp 7 days\n"
        "some-nginx\tnginx:1.27-alpine\tUp 3 days\n"
        "viator-web-1\tghcr.io/top-phe/viator-web:v0.1.32.7\tUp 1 hour\n"
    )
    assert _find_non_compose_otp_containers(out) == []


def test_mixed_input_only_flags_non_compose_otp():
    """Realistic `docker ps -a` output: a mix of compose-managed services,
    other infra, and a manual debug container. Only the last gets flagged."""
    from app.worker import _find_non_compose_otp_containers

    out = (
        "viator-web-1\tghcr.io/top-phe/viator-web:v0.1.32.7\tUp 1 hour\n"
        "viator-worker-1\tghcr.io/top-phe/viator-web:v0.1.32.7\tUp 1 hour\n"
        "viator-otp-fr-rail-1\tghcr.io/top-phe/viator-otp:v0.1.32.7\tUp 1 hour\n"
        "viator-postgres-1\tpostgres:16-alpine\tUp 7 days\n"
        "wizardly_pasteur\tghcr.io/top-phe/viator-otp:v0.1.30\tExited (137) 45 hours ago\n"
    )
    assert _find_non_compose_otp_containers(out) == [
        (
            "wizardly_pasteur",
            "ghcr.io/top-phe/viator-otp:v0.1.30",
            "Exited (137) 45 hours ago",
        ),
    ]


def test_empty_or_malformed_input_yields_empty_list():
    from app.worker import _find_non_compose_otp_containers

    assert _find_non_compose_otp_containers("") == []
    assert _find_non_compose_otp_containers("\n\n   \n") == []
    # Lines with fewer than 3 tab-separated fields are skipped silently.
    assert _find_non_compose_otp_containers("partial-line\nname\timage\n") == []


def test_old_otp_image_versions_still_flagged():
    """The image-prefix check uses `ghcr.io/top-phe/viator-otp` (no
    version), so any tag matches — including ancient ones from before
    today's audit work. The 2026-05-07 incident's container was on
    v0.1.30; today's would be v0.1.32.7. Both get flagged."""
    from app.worker import _find_non_compose_otp_containers

    out = (
        "old-debug\tghcr.io/top-phe/viator-otp:v0.1.30\tUp 45 hours\n"
        "older-debug\tghcr.io/top-phe/viator-otp:v0.1.5\tUp 100 hours\n"
        "untagged-debug\tghcr.io/top-phe/viator-otp\tUp 10 hours\n"
    )
    result = _find_non_compose_otp_containers(out)
    assert len(result) == 3
