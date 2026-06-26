# ─────────────────────── stage 1: docker CLI binary ───────────────────────
# We need just the `docker` CLI (the worker container shells out to it to
# spawn otp-build jobs against the host's docker socket). We DO NOT need the
# daemon, swarm tools, containerd, etc. Pulling Debian's `docker.io` package
# brought all of that and >50 transitive deps — a big CVE surface for nothing.
# Solution: copy just the static CLI binary from Docker's official cli image.
#
# Track the major version (29-cli auto-resolves to the latest 29.x.y patch).
# The CLI is a statically-linked Go binary; Trivy scans its bundled Go deps,
# so an older docker:cli tag can ship CVEs even though the binary works fine.
FROM docker:29-cli AS dockercli

# ─────────────────────── stage 2: runtime image ───────────────────────────
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Apply pending Debian security patches at build time. The python:3.12-slim
# tag is rebuilt on Python's cadence, not Debian's, so it can lag behind
# debian-security-announce. `apt-get upgrade` here closes that gap and keeps
# Trivy's OS scan green between base-image refreshes.
#
# The `echo` line on the first step changes the RUN layer's cache-key
# string so BuildKit re-runs apt-get update/upgrade on the next build
# instead of reusing a stale cached layer. Without it, BuildKit happily
# reuses the previous layer (same Dockerfile + same parent) and never
# pulls fresh Debian fixes — exactly the failure mode behind PR #83's
# Trivy block:
#
#   - libcap2       CVE-2026-4878   fixed in 1:2.75-10+deb13u1
#   - libnghttp2-14 CVE-2026-27135  fixed in 1.64.0-1.1+deb13u1
#   - libsystemd0   CVE-2026-29111  fixed in 257.13-1~deb13u1
#   - (+1 of the same family)
#
# Bump the date on the echo whenever Trivy reports new HIGH/CRITICAL
# fixed-upstream Debian CVEs to force the apt layer to re-evaluate. The
# echo's stdout goes only to the build log; it does not affect the
# runtime image.
RUN echo "apt cache bust 2026-06-26 — refresh for libssh2 CVE-2026-55200 (OOB write in transport.c) + CVE-2026-7598 (integer overflow on username/password); fixed in libssh2-1t64 1.11.1-1+deb13u1. Previous bust 2026-05-29 for krb5 CVE-2026-40356." \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Copy the docker CLI binary AND its plugin directory.
#
# `docker compose` is NOT a builtin subcommand — it's a CLI plugin that lives
# at `/usr/local/libexec/docker/cli-plugins/docker-compose`. Without it,
# `docker compose <args>` falls through to the bare `docker <args>` parser,
# which fails on `-p` ("unknown shorthand flag: 'p' in -p"). The worker
# uses `docker compose -p viator run --rm otp-build` to spawn one-shot OTP
# builds, so we MUST copy the plugin directory in addition to the binary.
#
# Both are statically-linked Go binaries with no shared-library deps, so
# they run cleanly in the python:3.12-slim runtime without any apt
# installs.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

WORKDIR /srv

# Upgrade pip first — the python:3.12-slim base ships pip 25.0.1 which has
# CVE-2025-8869 (MEDIUM, tarfile symlink check) and CVE-2026-1703 (LOW, wheel
# path traversal). Pin to a fixed version that's also new enough for our deps.
RUN pip install --upgrade "pip>=26.0"

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY branding ./branding
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# ─────────────────────── non-root user ───────────────────────
# Runtime UID/GID 1000:1000. Two services run from this image:
#   - web    (uvicorn) — needs read access to /srv and rw on /data/*
#   - worker (app.worker) — also rw on /var/run/docker.sock; the docker
#                            socket GID is host-dependent, supplied at
#                            runtime via `group_add` in compose. See
#                            docker-compose.yml worker service + .env.example
#                            DOCKER_GID.
#
# Volume mount points are pre-created here owned by appuser so that on
# first `compose up` the named volumes (`inbox`, `graphs`) inherit
# appuser ownership rather than the docker default of root:root. Bind
# mounts (./generated) keep host perms — see admin-guide §6.12.
RUN groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/inbox /data/graphs /data/generated /srv/docker \
    && chown -R appuser:appuser /srv /data
USER appuser:appuser

# ─────────────────────── version stamp ───────────────────────
# Burned-in version label, surfaced in the UI header (badge next to the
# VIATOR lockup) and at /healthz/version. The CI workflow passes the git
# tag here (e.g. `v0.1.8`); local `docker compose build` inherits the
# default `dev` so you can tell at a glance the image isn't a release.
#
# Why ARG + ENV: the ARG is only set during build, but `Settings()` reads
# the value from the process environment at runtime. Promoting it to ENV
# is what makes it survive into the running container.
#
# Runtime override: docker-compose.yml passes `VIATOR_VERSION` from the
# host `.env`, which wins over this baked value. That's deliberate — local
# overrides for dev/debug work.
ARG VIATOR_VERSION=dev
ENV VIATOR_VERSION=${VIATOR_VERSION}
LABEL org.opencontainers.image.version="${VIATOR_VERSION}"

EXPOSE 8000

# Audit-2026-05 P1 #8 — orchestrator-truthful readiness signal.
#
# `/healthz/version` is the cheapest endpoint that proves uvicorn is running
# AND the FastAPI router is wired (it returns the burned-in version label).
# 60s start period covers the `alembic upgrade head` step in CMD on a fresh
# DB; in steady state the check costs < 50 ms.
#
# This HEALTHCHECK applies to both `web` and `worker` services since they
# share this image. The worker doesn't bind port 8000 — for that service,
# `docker-compose.yml` overrides `healthcheck: disable: true`. See
# `nginx.depends_on.web.condition: service_healthy` which is what makes
# rolling deploys actually wait for uvicorn before swapping nginx upstreams.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz/version || exit 1

# Web container: run pending migrations, then start uvicorn.
# The worker container overrides CMD (see docker-compose.yml) and skips the migrate step.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
