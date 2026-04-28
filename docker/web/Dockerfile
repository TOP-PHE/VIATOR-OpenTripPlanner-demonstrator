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
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Apply pending Debian security patches at build time. The python:3.12-slim
# tag is rebuilt on Python's cadence, not Debian's, so it can lag behind
# debian-security-announce. `apt-get upgrade` here closes that gap and keeps
# Trivy's OS scan green between base-image refreshes.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Copy just the docker CLI binary — single static go binary, no daemon, no apt deps.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

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

EXPOSE 8000

# Web container: run pending migrations, then start uvicorn.
# The worker container overrides CMD (see docker-compose.yml) and skips the migrate step.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
