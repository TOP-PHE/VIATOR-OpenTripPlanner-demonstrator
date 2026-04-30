#!/usr/bin/env bash
set -euo pipefail

MODE="${OTP_MODE:-serve}"
GRAPH_DIR="${OTP_GRAPH_DIR:-/var/otp/graph}"
INBOX_DIR="${OTP_INBOX_DIR:-/var/otp/inbox}"
HEAP="${OTP_HEAP:-12g}"

# OSM filter scope (see app/osm_filter.py for the canonical preset
# definitions — these tag-filter strings MUST stay in sync with the
# Python `OSM_SCOPE_PRESETS` dict). At build time the operator's choice
# arrives as `OTP_OSM_SCOPE`; the entrypoint runs osmium-tool tags-filter
# accordingly. `comprehensive` skips filtering entirely.
OSM_SCOPE="${OTP_OSM_SCOPE:-transit-focused}"

# Build pipeline: 'two_phase' (default) builds the street graph and the transit
# graph in two separate JVMs. Peak heap is dramatically lower than --build (one
# shot) because raw OSM nodes are released between phases — France-wide PBF +
# nationwide GTFS fits in 24 GB this way vs. ~32 GB needed for one-shot. Set
# 'one_shot' to fall back to the legacy single-JVM path (debugging only).
BUILD_PHASES="${OTP_BUILD_PHASES:-two_phase}"

# JVM flags applied to every otp.jar invocation:
#   +UseStringDeduplication: 5-10% heap saving on duplicated OSM road names
#   MaxDirectMemorySize=2g:  bounds NIO/Netty direct buffers — predictable
#                            native footprint so `mem_limit` on the container
#                            actually corresponds to (heap + 2g + ~1-2g
#                            metaspace/threads/GC).
#   +ExitOnOutOfMemoryError: fail fast on OOM rather than degenerate into a
#                            multi-hour stop-the-world GC death spiral that
#                            looks like the build is "stuck" with no logs.
JVM_OPTS="-XX:+UseStringDeduplication -XX:MaxDirectMemorySize=2g -XX:+ExitOnOutOfMemoryError"

case "$MODE" in
    build)
        BUILD_DIR="$(mktemp -d)"
        trap 'rm -rf "$BUILD_DIR"' EXIT

        echo "Staging build inputs from $INBOX_DIR ..."
        # GTFS bundles
        if compgen -G "$INBOX_DIR/gtfs/*.zip" > /dev/null; then
            cp "$INBOX_DIR"/gtfs/*.zip "$BUILD_DIR/"
        fi
        # NeTEx bundles (only Nordic / EPIP — never NeTEx-FR until a converter exists)
        if compgen -G "$INBOX_DIR/netex/*.zip" > /dev/null; then
            cp "$INBOX_DIR"/netex/*.zip "$BUILD_DIR/"
        fi
        # Street network — staged into BUILD_DIR. If the operator picked
        # an OSM scope other than 'comprehensive', we run osmium-tool
        # tags-filter to drop non-routing-relevant ways before OTP sees
        # the file. Cuts ~30-40% of OSM data for transit-focused, ~10-20%
        # for multi-modal — bringing France-wide build heap from ~40 GB
        # down to ~22-26 GB.
        if compgen -G "$INBOX_DIR/osm/*.pbf" > /dev/null; then
            for pbf_in in "$INBOX_DIR"/osm/*.pbf; do
                pbf_out="$BUILD_DIR/$(basename "$pbf_in")"
                case "$OSM_SCOPE" in
                    transit-focused)
                        echo "OSM filter: transit-focused — running osmium tags-filter on $(basename "$pbf_in")"
                        osmium tags-filter --quiet --overwrite \
                            -o "$pbf_out" "$pbf_in" \
                            'highway=motorway,trunk,primary,secondary,tertiary,unclassified,residential,living_street,pedestrian,footway,path,steps,cycleway,road,motorway_link,trunk_link,primary_link,secondary_link,tertiary_link' \
                            'railway' \
                            'public_transport' \
                            'amenity=parking,parking_entrance' \
                            'highway=bus_stop'
                        ;;
                    multi-modal)
                        echo "OSM filter: multi-modal — running osmium tags-filter on $(basename "$pbf_in")"
                        osmium tags-filter --quiet --overwrite \
                            -o "$pbf_out" "$pbf_in" \
                            'highway' \
                            'railway' \
                            'public_transport' \
                            'amenity=parking,parking_entrance'
                        ;;
                    comprehensive)
                        echo "OSM filter: comprehensive — passing $(basename "$pbf_in") through unchanged"
                        cp "$pbf_in" "$pbf_out"
                        ;;
                    *)
                        echo "Unknown OTP_OSM_SCOPE='$OSM_SCOPE' (expected: transit-focused | multi-modal | comprehensive)" >&2
                        exit 1
                        ;;
                esac
                # Quick before/after summary so the build log shows the
                # operator what got dropped.
                in_sz=$(stat -c %s "$pbf_in")
                out_sz=$(stat -c %s "$pbf_out")
                if [ "$in_sz" -gt 0 ]; then
                    pct=$(( (out_sz * 100) / in_sz ))
                    echo "  $(basename "$pbf_in"): ${in_sz} → ${out_sz} bytes (${pct}% of original)"
                fi
            done
        fi
        # Optional elevation
        if compgen -G "$INBOX_DIR/dem/*.tif" > /dev/null; then
            cp "$INBOX_DIR"/dem/*.tif "$BUILD_DIR/"
        fi

        cp /opt/otp/router-config.json "$BUILD_DIR/"

        # Generate build-config.json from the GTFS files that landed in
        # BUILD_DIR. One transitFeeds entry per .zip; feedId derived from the
        # filename stem, uppercased. Single-feed sessions stage at `gtfs.zip`
        # and produce feedId=GTFS. Multi-feed sessions stage as `<feed_id_lower>.zip`
        # (e.g. `sncf.zip`, `idfm.zip`, `trenitalia.zip`) and produce one
        # feedId per file (SNCF, IDFM, TRENITALIA). The OSM section is fixed
        # — exactly one PBF per session, staged as `osm.pbf`.
        #
        # The bundled `/opt/otp/build-config.json` from the image is only
        # used as a fallback when no GTFS zips exist (keeps the entrypoint
        # working for an OSM-only build, even though that's a degenerate
        # case OTP itself rejects with "no transit data").
        TRANSIT_FEEDS_JSON=""
        for zip in "$BUILD_DIR"/*.zip; do
            [ -f "$zip" ] || continue
            stem="$(basename "$zip" .zip)"
            feed_id="$(printf %s "$stem" | tr '[:lower:]' '[:upper:]')"
            entry="{\"type\":\"gtfs\",\"feedId\":\"$feed_id\",\"source\":\"$stem.zip\"}"
            if [ -n "$TRANSIT_FEEDS_JSON" ]; then
                TRANSIT_FEEDS_JSON="$TRANSIT_FEEDS_JSON,$entry"
            else
                TRANSIT_FEEDS_JSON="$entry"
            fi
        done
        if [ -n "$TRANSIT_FEEDS_JSON" ]; then
            echo "Generating build-config.json with feeds: $TRANSIT_FEEDS_JSON"
            cat > "$BUILD_DIR/build-config.json" <<JSON
{
  "osmDefaults": {"osmTagMapping": "default"},
  "transitFeeds": [$TRANSIT_FEEDS_JSON],
  "osm": [{"source": "osm.pbf"}],
  "transitServiceStart": "-P1M",
  "transitServiceEnd": "P5M",
  "subwayAccessTime": 2.0,
  "streetGraph": "streetGraph.obj",
  "graph": "graph.obj"
}
JSON
        else
            echo "No GTFS zips found; falling back to baked build-config.json"
            cp /opt/otp/build-config.json "$BUILD_DIR/"
        fi

        case "$BUILD_PHASES" in
            two_phase)
                # Phase 1 — read OSM PBF, build street graph, save streetGraph.obj.
                # JVM exits at the end of this command, releasing the OSM-parse peak.
                echo "Phase 1/2 — building street graph (heap=$HEAP) ..."
                # shellcheck disable=SC2086  # JVM_OPTS is intentionally word-split
                java -Xmx"$HEAP" $JVM_OPTS -jar /opt/otp/otp.jar --buildStreet --save "$BUILD_DIR"

                # Phase 2 — load the saved streetGraph.obj, layer GTFS/NeTEx on top,
                # save graph.obj. Peak heap is much lower than the combined
                # OSM-parse + transit-link working set the one-shot mode requires.
                echo "Phase 2/2 — overlaying transit (heap=$HEAP) ..."
                # shellcheck disable=SC2086
                java -Xmx"$HEAP" $JVM_OPTS -jar /opt/otp/otp.jar --loadStreet --save "$BUILD_DIR"
                ;;
            one_shot)
                echo "Single-phase build (heap=$HEAP) ..."
                # shellcheck disable=SC2086
                java -Xmx"$HEAP" $JVM_OPTS -jar /opt/otp/otp.jar --build --save "$BUILD_DIR"
                ;;
            *)
                echo "Unknown OTP_BUILD_PHASES='$BUILD_PHASES' (use 'two_phase' or 'one_shot')" >&2
                exit 1
                ;;
        esac

        echo "Promoting graph to $GRAPH_DIR ..."
        mkdir -p "$GRAPH_DIR"
        mv "$BUILD_DIR/graph.obj" "$GRAPH_DIR/graph.obj"
        # The worker (host side) will move this into a timestamped subdir + flip the symlink.
        ;;

    serve)
        if [ ! -e "$GRAPH_DIR/current/graph.obj" ]; then
            echo "No graph at $GRAPH_DIR/current/graph.obj — waiting for first build."
            # Block instead of exiting so docker doesn't restart-loop.
            while [ ! -e "$GRAPH_DIR/current/graph.obj" ]; do sleep 30; done
        fi
        echo "Serving graph from $GRAPH_DIR/current (heap=$HEAP) ..."
        # shellcheck disable=SC2086
        exec java -Xmx"$HEAP" $JVM_OPTS -jar /opt/otp/otp.jar --load --serve "$GRAPH_DIR/current"
        ;;

    *)
        echo "Unknown OTP_MODE: $MODE" >&2
        exit 1
        ;;
esac
