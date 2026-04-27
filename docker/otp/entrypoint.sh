#!/usr/bin/env bash
set -euo pipefail

MODE="${OTP_MODE:-serve}"
GRAPH_DIR="${OTP_GRAPH_DIR:-/var/otp/graph}"
INBOX_DIR="${OTP_INBOX_DIR:-/var/otp/inbox}"
HEAP="${OTP_HEAP:-12g}"

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

        echo "Building graph (heap=$HEAP) ..."
        java -Xmx"$HEAP" -jar /opt/otp/otp.jar --build --save "$BUILD_DIR"

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
        exec java -Xmx"$HEAP" -jar /opt/otp/otp.jar --load --serve "$GRAPH_DIR/current"
        ;;

    *)
        echo "Unknown OTP_MODE: $MODE" >&2
        exit 1
        ;;
esac
