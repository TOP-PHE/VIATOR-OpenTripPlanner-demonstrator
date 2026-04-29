#!/usr/bin/env bash
set -euo pipefail

MODE="${OTP_MODE:-serve}"
GRAPH_DIR="${OTP_GRAPH_DIR:-/var/otp/graph}"
INBOX_DIR="${OTP_INBOX_DIR:-/var/otp/inbox}"
HEAP="${OTP_HEAP:-12g}"

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
        # Street network
        if compgen -G "$INBOX_DIR/osm/*.pbf" > /dev/null; then
            cp "$INBOX_DIR"/osm/*.pbf "$BUILD_DIR/"
        fi
        # Optional elevation
        if compgen -G "$INBOX_DIR/dem/*.tif" > /dev/null; then
            cp "$INBOX_DIR"/dem/*.tif "$BUILD_DIR/"
        fi

        cp /opt/otp/build-config.json "$BUILD_DIR/"
        cp /opt/otp/router-config.json "$BUILD_DIR/"

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
