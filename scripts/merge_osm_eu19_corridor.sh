#!/usr/bin/env bash
# Merge Geofabrik regional PBFs into one corridor-wide OSM file for the
# eu19-transit-motis session. Extends the eu11 (eurostar-corridor)
# script with 8 additional countries: DK + SE + NO + PL + CZ + SK + SI
# + HU.
#
# Country list (19 total):
#   eu11 base: ES + FR + BE + LU + NL + DE + AT + IT + LI + CH + GB-HS1
#   eu19 add:  DK + SE + NO + PL + CZ + SK + SI + HU
#
# Note on SK: included in OSM coverage even though the SK transit feed is
# excluded from the session (SK NAP is non-compliant per the EU PDF).
# Including SK in OSM still lets MOTIS route street/walking paths through
# Slovakia for cross-border journeys that touch it (e.g. AT→SK→HU
# Bratislava-Budapest road links). Cheap enough — Slovakia PBF is ~250 MB.
#
# Run on the VPS where docker + the otp-build image already live.
# The container's osmium-tool does all the heavy lifting; the host
# only needs curl and docker.
#
# Output: ${OUTPUT_PATH:-/tmp/osm-merge/eu19-corridor.osm.pbf}
#
# Plug the resulting file into the eu19 session via either:
#   - serve via nginx and set sources.osm_pbf=<URL>, OR
#   - drop into /data/inbox/<sid>/osm/osm.pbf and skip the URL refresh.
#
# Disk:   ~50 GB peak working space (raw downloads + merged output).
#         eu19 is ~2x eu11 due to PL + DE + IT being the largest extracts.
# Time:   ~60-90 min depending on bandwidth + CPU.
# Memory: streams — fits in a few hundred MB.

set -euo pipefail

# ────────────────────── config ──────────────────────
WORK_DIR="${WORK_DIR:-/tmp/osm-merge}"
OUTPUT_PATH="${OUTPUT_PATH:-${WORK_DIR}/eu19-corridor.osm.pbf}"

# Geofabrik regional extracts. Each entry is "<basename>|<url>".
# - Belgium / Netherlands / Luxembourg fetched separately (the benelux
#   composite was discontinued by Geofabrik — see eurostar-corridor
#   script comments for the HTML-stub trap).
# - switzerland = CH + LI (Liechtenstein is in the Switzerland extract).
# - great-britain = GB raw; we'll bbox-clip it below to the HS1 corridor.
# - eu19 additions: denmark, sweden, norway, poland, czech-republic,
#   slovakia, slovenia, hungary. All standard Geofabrik names.
REGIONS=(
    # eu11 base
    "spain|https://download.geofabrik.de/europe/spain-latest.osm.pbf"
    "france|https://download.geofabrik.de/europe/france-latest.osm.pbf"
    "belgium|https://download.geofabrik.de/europe/belgium-latest.osm.pbf"
    "netherlands|https://download.geofabrik.de/europe/netherlands-latest.osm.pbf"
    "luxembourg|https://download.geofabrik.de/europe/luxembourg-latest.osm.pbf"
    "germany|https://download.geofabrik.de/europe/germany-latest.osm.pbf"
    "austria|https://download.geofabrik.de/europe/austria-latest.osm.pbf"
    "italy|https://download.geofabrik.de/europe/italy-latest.osm.pbf"
    "switzerland|https://download.geofabrik.de/europe/switzerland-latest.osm.pbf"
    "great-britain|https://download.geofabrik.de/europe/great-britain-latest.osm.pbf"
    # eu19 additions
    "denmark|https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    "sweden|https://download.geofabrik.de/europe/sweden-latest.osm.pbf"
    "norway|https://download.geofabrik.de/europe/norway-latest.osm.pbf"
    "poland|https://download.geofabrik.de/europe/poland-latest.osm.pbf"
    "czech-republic|https://download.geofabrik.de/europe/czech-republic-latest.osm.pbf"
    "slovakia|https://download.geofabrik.de/europe/slovakia-latest.osm.pbf"
    "slovenia|https://download.geofabrik.de/europe/slovenia-latest.osm.pbf"
    "hungary|https://download.geofabrik.de/europe/hungary-latest.osm.pbf"
)

# Minimum byte size for a cached PBF to be trusted. Same rationale as
# the eu11 script — catches HTML stubs from retired extracts. Luxembourg
# remains the smallest legit region at ~40 MB; 5 MB is a comfortable
# lower bound. None of the new eu19 countries push below this (Slovenia
# is the next-smallest at ~150 MB).
MIN_PBF_BYTES=$((5 * 1024 * 1024))

# UK bbox — the HS1 corridor: London St Pancras → Ashford → Folkestone /
# Channel Tunnel portal. Generous enough to catch all Eurostar UK termini
# (St Pancras, Ebbsfleet, Stratford International, Ashford) plus the
# walking environment around each. Output ~50-80 MB once rail-filtered.
#
# bbox = min_lon,min_lat,max_lon,max_lat
UK_BBOX="-0.5,50.8,1.5,51.8"

# Docker image with osmium-tool. Built locally on first run from a
# minimal Ubuntu base — no docker-hub auth, no external image dep, no
# coupling to the platform's otp image tag (which varies per release).
# ~20 s one-time build, cached on rerun.
OSMIUM_IMAGE="${OSMIUM_IMAGE:-viator-osmium-helper:latest}"

# Run docker as the host user so files written under WORK_DIR are
# readable / cleanable by the viator user afterwards (otherwise root
# would own everything the container writes).
DOCKER_USER="$(id -u):$(id -g)"

# ────────────────────── helpers ──────────────────────
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# Run osmium inside a docker container with WORK_DIR mounted at /work.
osmium() {
    docker run --rm \
        --user "${DOCKER_USER}" \
        -v "${WORK_DIR}:/work" \
        -w /work \
        "${OSMIUM_IMAGE}" \
        osmium "$@"
}

# Ensure the osmium helper image exists; build it from a minimal Ubuntu
# base if not. Takes ~20 s on first run, instant on reruns.
ensure_osmium_image() {
    if docker image inspect "${OSMIUM_IMAGE}" >/dev/null 2>&1; then
        log "Using existing osmium image: ${OSMIUM_IMAGE}"
        return
    fi
    log "Building osmium helper image '${OSMIUM_IMAGE}' (one-time, ~20 s)"
    local build_dir
    build_dir="$(mktemp -d)"
    cat > "${build_dir}/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
DOCKERFILE
    docker build -t "${OSMIUM_IMAGE}" "${build_dir}"
    rm -rf "${build_dir}"
}

# Resumable download; skip when target already exists AND is plausibly
# big enough to be a real PBF (catches the "Geofabrik 200-OK HTML stub"
# trap where a previous run cached a ~10 KB error page).
download() {
    local name="$1" url="$2" dst="${WORK_DIR}/raw/${1}.osm.pbf"
    if [ -s "${dst}" ]; then
        local sz
        sz=$(stat -c %s "${dst}")
        if [ "$sz" -ge "$MIN_PBF_BYTES" ]; then
            log "  ${name}: cached at ${dst} ($(du -h "${dst}" | cut -f1)), skipping download"
            return
        fi
        log "  ${name}: cached file at ${dst} is suspiciously small ($sz bytes) — re-downloading"
        rm -f "${dst}" "${dst}.part"
    fi
    mkdir -p "${WORK_DIR}/raw"
    log "  ${name}: downloading from ${url}"
    curl --fail --location --retry 3 --retry-delay 5 \
         --continue-at - --output "${dst}.part" "${url}"
    # Sanity-check the just-downloaded file before promoting. Geofabrik
    # sometimes returns 200 OK with an HTML stub for retired extracts
    # — curl --fail can't detect that. A real PBF is always > 5 MB.
    local got_sz
    got_sz=$(stat -c %s "${dst}.part")
    if [ "$got_sz" -lt "$MIN_PBF_BYTES" ]; then
        log "  ${name}: downloaded file is only $got_sz bytes — abort (URL likely retired or HTML stub)"
        head -c 200 "${dst}.part" >&2 || true; echo "" >&2
        rm -f "${dst}.part"
        return 1
    fi
    mv "${dst}.part" "${dst}"
    log "  ${name}: $(du -h "${dst}" | cut -f1) downloaded"
}

# ────────────────────── pipeline ──────────────────────
mkdir -p "${WORK_DIR}/raw" "${WORK_DIR}/staged"
ensure_osmium_image

log "Phase 1/4 — downloading ${#REGIONS[@]} regional PBFs to ${WORK_DIR}/raw"
for entry in "${REGIONS[@]}"; do
    name="${entry%%|*}"
    url="${entry#*|}"
    download "${name}" "${url}"
done

log "Phase 2/4 — staging country PBFs into ${WORK_DIR}/staged"
# Most regions get a verbatim copy (hard link) into staged/. GB is the
# exception: bbox-clip the raw GB PBF down to the HS1 corridor. Everything
# else stays raw — the session build's geo-crop polygon will later trim
# each country to its national outline anyway. (Doing the GB clip here
# keeps the merge cheap and the merged file's GB footprint tiny from
# the start, rather than carrying 1.5 GB of UK we don't want through
# the merge then trimming it later.)
for entry in "${REGIONS[@]}"; do
    name="${entry%%|*}"
    src="raw/${name}.osm.pbf"
    dst="staged/${name}.osm.pbf"

    if [ "${name}" = "great-britain" ]; then
        log "  great-britain: bbox-clip to ${UK_BBOX} (Eurostar HS1 corridor)"
        # `-s smart` keeps ways that cross the bbox boundary, so HS1
        # tracks running into/out of the box stay intact at the edge.
        osmium extract --overwrite -s smart \
            --bbox "${UK_BBOX}" \
            -o "${dst}" "${src}"
        clipped_sz=$(du -h "${WORK_DIR}/${dst}" | cut -f1)
        log "  great-britain: clipped to ${clipped_sz}"
    else
        # Hard-link to avoid duplicating multi-GB files on disk.
        # Falls back to cp if hard links aren't supported (different fs).
        rm -f "${WORK_DIR}/${dst}"
        if ! ln "${WORK_DIR}/${src}" "${WORK_DIR}/${dst}" 2>/dev/null; then
            cp "${WORK_DIR}/${src}" "${WORK_DIR}/${dst}"
        fi
    fi
done

log "Phase 3/4 — osmium merge → $(basename "${OUTPUT_PATH}")"
# osmium merge wants sorted inputs but Geofabrik extracts are sorted;
# `--overwrite` lets us re-run the script idempotently.
merge_inputs=()
for entry in "${REGIONS[@]}"; do
    name="${entry%%|*}"
    merge_inputs+=("staged/${name}.osm.pbf")
done
mkdir -p "$(dirname "${OUTPUT_PATH}")"
# Output path inside container needs to be under /work too.
out_basename="$(basename "${OUTPUT_PATH}")"
osmium merge --overwrite -o "${out_basename}" "${merge_inputs[@]}"
# Move from /work-mounted location to the final OUTPUT_PATH — but skip the
# move when osmium already wrote it there (default case: OUTPUT_PATH lives
# inside WORK_DIR). Without this guard, `mv X X` errors with "same file"
# at the very end of an otherwise-successful merge.
if [ "${WORK_DIR}/${out_basename}" != "${OUTPUT_PATH}" ]; then
    mv "${WORK_DIR}/${out_basename}" "${OUTPUT_PATH}"
fi

log "Phase 4/4 — summary"
log "  Output:     ${OUTPUT_PATH}"
log "  Size:       $(du -h "${OUTPUT_PATH}" | cut -f1)"
log "  fileinfo:"
osmium_for_summary() {
    docker run --rm \
        -v "$(dirname "${OUTPUT_PATH}"):/out" \
        -w /out \
        "${OSMIUM_IMAGE}" \
        osmium fileinfo --extended "$(basename "${OUTPUT_PATH}")"
}
osmium_for_summary || log "  (fileinfo failed — file is still usable; this is just summary output)"

log "Done. Next steps:"
log "  • Serve this file (nginx route or other), then set"
log "    sources.osm_pbf=<URL> on the new eu19 session, OR"
log "  • Copy directly into the session's inbox:"
log "      cp ${OUTPUT_PATH} /data/inbox/eu19-transit-motis/osm/osm.pbf"
log "    and skip the URL-refresh step in the session form."
log ""
log "  • For the eu19 session bootstrap, see:"
log "      scripts/create_eu19_transit_motis_session.ps1"
