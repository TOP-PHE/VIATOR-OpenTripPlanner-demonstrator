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
COPY entrypoint.sh /opt/otp/entrypoint.sh
RUN chmod +x /opt/otp/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/opt/otp/entrypoint.sh"]
