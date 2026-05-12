FROM eclipse-temurin:25-jre-noble

ARG OTP_VERSION=2.9.0
ENV OTP_VERSION=${OTP_VERSION}

# Apply pending Ubuntu security patches at build time. The base image tag
# (eclipse-temurin:25-jre-noble) is rebuilt on Temurin's cadence, not Ubuntu's,
# so it can lag behind ubuntu-security-announce. `apt-get upgrade` here closes
# that gap and keeps Trivy's OS scan green between base-image refreshes.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        osmium-tool \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# osmium-tool (~5 MB installed, plus ~10 MB of libs) is the OSM-PBF
# pre-filter. We run it in entrypoint.sh against the staged osm.pbf to
# drop non-routing-relevant ways before OTP builds the graph. Lets us
# fit France-wide builds in 24 GB heap instead of 40+. Source of preset
# tag-filter expressions: app/osm_filter.py (Python is the single source
# of truth; entrypoint.sh reads the chosen preset by env var).

# Note: starting with OTP 2.7.0 the Maven artifact ID was renamed from
# `otp` (with classifier `shaded`) to `otp-shaded` (no classifier — the
# shaded jar IS the artifact). This URL is correct for 2.7.0+; for older
# versions, use https://repo1.maven.org/maven2/org/opentripplanner/otp/<v>/otp-<v>-shaded.jar
RUN mkdir -p /opt/otp \
    && curl -fSL -o /opt/otp/otp.jar \
        "https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/${OTP_VERSION}/otp-shaded-${OTP_VERSION}.jar"

COPY build-config.json /opt/otp/build-config.json
COPY router-config.json /opt/otp/router-config.json
# `--chmod=755` sets the executable bit at copy time (BuildKit feature).
# Replaces a separate `RUN chmod +x …` step — saves one image layer and
# silences SonarCloud rule "merge consecutive RUN instructions" (audit
# 2026-05 SonarCloud finding). Requires BuildKit, which the GHA matrix
# uses via docker/setup-buildx-action — `docker compose build` locally
# also enables it by default since Docker Engine 23.
COPY --chmod=755 entrypoint.sh /opt/otp/entrypoint.sh

# ─────────────────────── non-root user ───────────────────────
# Runtime UID/GID 1000:1000 (matches the web image so a single host UID
# owns both service trees and the shared `graphs` named volume — using
# different UIDs would break cross-container read/write on that volume).
# The build container also writes /tmp/<random> via mktemp inside
# entrypoint.sh and the cache dir at /var/otp/graph/.cache/<sid>/, both
# under appuser ownership.
#
# `eclipse-temurin:25-jre-noble` is built on Ubuntu 24.04 (noble), which
# ships with a default `ubuntu` user at UID/GID 1000:1000. We delete it
# first so our `appuser` can own that slot. The `|| true` guards against
# a future base-image rebuild that drops the default user — userdel
# fails-noisy if the user doesn't exist, and we'd rather not break the
# build on a benign upstream change.
RUN userdel --remove ubuntu 2>/dev/null || true \
    && groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/otp/inbox /var/otp/graph \
    && chown -R appuser:appuser /opt/otp /var/otp
USER appuser:appuser

EXPOSE 8080

# Audit-2026-05 P1 #8 — readiness signal for per-session OTP serve containers.
# Audit-2026-05 #32 (PR #32) — moved the HEALTHCHECK to the per-session
# generated compose fragment (app/sessions_orchestrator.py
# `_OTP_SVC_TEMPLATE`). The hardcoded `--start-period=120s` here was too
# tight for graphs that take >2 min to do their Raptor mapping — the OTP
# serve container would be marked unhealthy mid-startup, killed by the
# orchestration, restarted, never finish, repeat. Surfaced 2026-05-11 on
# nap-fr-rail with SBB included. The new location lets the operator size
# start_period per session via the admin UI.
#
# Build-only invocations of this image (`docker compose run --rm otp-build`)
# never see a healthcheck because the container exits before docker would
# evaluate one, so dropping the directive here is safe — build semantics
# are unchanged.

ENTRYPOINT ["/opt/otp/entrypoint.sh"]
