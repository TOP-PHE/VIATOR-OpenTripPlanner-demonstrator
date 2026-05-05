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

        # Pre-check: if there's no PBF or no transit feed, OTP fails ~30 s
        # in with "no OSM data available" (or a similarly opaque error)
        # after spinning up the JVM. Surface a clear message early instead
        # — usually the operator forgot to click Refresh sources before
        # Rebuild graph. The API also guards this at enqueue time (since
        # v0.1.7); this check covers manual operator-driven invocations.
        has_pbf=0
        has_transit=0
        compgen -G "$INBOX_DIR/osm/*.pbf"   > /dev/null && has_pbf=1
        compgen -G "$INBOX_DIR/gtfs/*.zip"  > /dev/null && has_transit=1
        compgen -G "$INBOX_DIR/netex/*.zip" > /dev/null && has_transit=1
        if [ "$has_pbf" -eq 0 ] || [ "$has_transit" -eq 0 ]; then
            echo "ERROR: missing inputs in $INBOX_DIR/" >&2
            echo "  OSM PBF (osm/*.pbf):       $([ $has_pbf    -eq 1 ] && echo present || echo MISSING)" >&2
            echo "  Transit feed (gtfs/netex): $([ $has_transit -eq 1 ] && echo present || echo MISSING)" >&2
            echo "Did you click 'Refresh all sources' before 'Rebuild graph'?" >&2
            exit 1
        fi
        # Transit feeds — staged with per-format type tracking so the
        # build-config generator below picks the right OTP `transitFeeds.type`
        # value per file. GTFS files land in inbox/<sid>/gtfs/, NeTEx (Nordic
        # / EPIP) in inbox/<sid>/netex/. NeTEx-FR is intentionally never
        # plumbed into BUILD_DIR — it's archive-only because OTP can't read
        # it; operators wanting NeTEx-FR for compliance use the manual
        # upload path which dispatches to inbox/<sid>/archive/.
        TRANSIT_FEEDS_JSON=""
        _append_feed_entry() {
            local entry="$1"
            if [ -n "$TRANSIT_FEEDS_JSON" ]; then
                TRANSIT_FEEDS_JSON="$TRANSIT_FEEDS_JSON,$entry"
            else
                TRANSIT_FEEDS_JSON="$entry"
            fi
        }
        if compgen -G "$INBOX_DIR/gtfs/*.zip" > /dev/null; then
            for src in "$INBOX_DIR"/gtfs/*.zip; do
                cp "$src" "$BUILD_DIR/"
                stem="$(basename "$src" .zip)"
                feed_id="$(printf %s "$stem" | tr '[:lower:]' '[:upper:]')"
                _append_feed_entry "{\"type\":\"gtfs\",\"feedId\":\"$feed_id\",\"source\":\"$stem.zip\"}"
            done
        fi
        if compgen -G "$INBOX_DIR/netex/*.zip" > /dev/null; then
            for src in "$INBOX_DIR"/netex/*.zip; do
                cp "$src" "$BUILD_DIR/"
                stem="$(basename "$src" .zip)"
                feed_id="$(printf %s "$stem" | tr '[:lower:]' '[:upper:]')"
                _append_feed_entry "{\"type\":\"netex\",\"feedId\":\"$feed_id\",\"source\":\"$stem.zip\"}"
            done
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
                        # Note on flags: `--quiet` was tried in v0.1.5 but
                        # osmium 1.16.x's `tags-filter` doesn't accept it
                        # — only `--no-progress` exists, and even that's
                        # noise-suppression we don't actually need (the
                        # progress bar prints periodic %-done lines to
                        # stderr that are useful for build observability).
                        # Default verbosity is fine.
                        osmium tags-filter --overwrite \
                            -o "$pbf_out" "$pbf_in" \
                            'highway=motorway,trunk,primary,secondary,tertiary,unclassified,residential,living_street,pedestrian,footway,path,steps,cycleway,road,motorway_link,trunk_link,primary_link,secondary_link,tertiary_link' \
                            'railway' \
                            'public_transport' \
                            'amenity=parking,parking_entrance' \
                            'highway=bus_stop'
                        ;;
                    multi-modal)
                        echo "OSM filter: multi-modal — running osmium tags-filter on $(basename "$pbf_in")"
                        osmium tags-filter --overwrite \
                            -o "$pbf_out" "$pbf_in" \
                            'highway' \
                            'railway' \
                            'public_transport' \
                            'amenity=parking,parking_entrance'
                        ;;
                    rail-focused)
                        # v0.1.30 — drops ALL driving infrastructure (no
                        # motorway/primary/residential/service/cycleway).
                        # ~80 % smaller than raw PBF; the only scope that
                        # lets a 10-country EU merge fit in ~24-28 GB build
                        # heap on a 47 GB box. See app/osm_filter.py for
                        # the rationale and trade-offs.
                        echo "OSM filter: rail-focused — running osmium tags-filter on $(basename "$pbf_in")"
                        osmium tags-filter --overwrite \
                            -o "$pbf_out" "$pbf_in" \
                            'railway' \
                            'public_transport' \
                            'highway=footway,path,steps,pedestrian,corridor,elevator' \
                            'amenity=parking_entrance'
                        ;;
                    comprehensive)
                        echo "OSM filter: comprehensive — passing $(basename "$pbf_in") through unchanged"
                        cp "$pbf_in" "$pbf_out"
                        ;;
                    *)
                        echo "Unknown OTP_OSM_SCOPE='$OSM_SCOPE' (expected: transit-focused | multi-modal | rail-focused | comprehensive)" >&2
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

        # Per-session router-config.json (v0.1.7): the worker generates it
        # from `session.config.sources.providers[*].gtfs_rt` before each
        # build and writes it to `inbox/<sid>/router-config.json`. Used for
        # OTP's real-time updater plumbing — alerts / trip-updates / vehicle-
        # positions URLs per provider, all in one config. We prefer it over
        # the baked image default; falling back keeps the entrypoint
        # working for sessions without GTFS-RT URLs configured.
        if [ -f "$INBOX_DIR/router-config.json" ]; then
            echo "Using per-session router-config.json from $INBOX_DIR"
            cp "$INBOX_DIR/router-config.json" "$BUILD_DIR/"
        else
            cp /opt/otp/router-config.json "$BUILD_DIR/"
        fi

        # Generate build-config.json from the transit feeds we staged above.
        # The TRANSIT_FEEDS_JSON variable was populated by the staging loops
        # — each `_append_feed_entry` call added one `{"type":..., "feedId":...,
        # "source":...}` entry, with type="gtfs" or type="netex" depending on
        # which inbox subdir the file came from. OTP merges them all into a
        # single graph at build time.
        #
        # The bundled `/opt/otp/build-config.json` is the no-feeds fallback
        # (degenerate case OTP itself rejects with "no transit data").
        # v0.1.21 — `transitModelTimeZone` is required by OTP 2.9 when the
        # graph mixes agencies declaring different timezones (SNCF says
        # Europe/Paris, Eurostar says Europe/Brussels — OTP refuses to pick
        # for us and aborts with "agencies with different time zones"). The
        # worker passes it as $OTP_TIMEZONE; default in app/otp_timezone.py
        # is Europe/Paris so single-FR sessions keep building unchanged.
        OTP_TIMEZONE_LINE=""
        if [ -n "$OTP_TIMEZONE" ]; then
            OTP_TIMEZONE_LINE="\"transitModelTimeZone\": \"$OTP_TIMEZONE\","
        fi

        if [ -n "$TRANSIT_FEEDS_JSON" ]; then
            echo "Generating build-config.json with feeds: $TRANSIT_FEEDS_JSON"
            echo "  transitModelTimeZone=$OTP_TIMEZONE"
            cat > "$BUILD_DIR/build-config.json" <<JSON
{
  "osmDefaults": {"osmTagMapping": "default"},
  $OTP_TIMEZONE_LINE
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
            echo "No transit feeds found; falling back to baked build-config.json"
            cp /opt/otp/build-config.json "$BUILD_DIR/"
        fi

        # streetGraph.obj cache (v0.1.7) — phase 1 is the heaviest step and
        # only depends on the OSM PBF + filter scope. If neither has changed
        # since the last successful build, skip phase 1 and reuse the cached
        # streetGraph.obj. Cache lives at GRAPH_DIR/.cache/<sid>/ alongside a
        # `.key` file containing `sha256(osm.pbf):<scope>`.
        #
        # Why GRAPH_DIR and not INBOX_DIR? The inbox volume is mounted
        # read-only in the otp-build container by design — protects staged
        # GTFS files from a buggy build. The graphs volume is rw, and the
        # cache logically belongs alongside the graph anyway. The session
        # id comes from the OTP_INBOX_DIR's basename so the cache stays
        # per-session even with shared GRAPH_DIR.
        #
        # Effect: adding a new GTFS provider to a France-wide session goes
        # from ~30 min (full rebuild) → ~10-12 min (phase-2 only). OSM
        # itself unchanged, only transit overlay re-runs.
        OSM_INPUT="$BUILD_DIR/osm.pbf"
        SID="$(basename "$INBOX_DIR")"
        CACHE_DIR="$GRAPH_DIR/.cache/$SID"
        CACHE_OBJ="$CACHE_DIR/streetGraph.obj"
        CACHE_KEY="$CACHE_DIR/streetGraph.key"
        CURRENT_KEY=""
        if [ -f "$OSM_INPUT" ]; then
            # sha256 takes ~1 min on a 5 GB PBF; we eat that cost only once
            # per (PBF, scope) combo because we cache the streetGraph it
            # produces. Key format: `<sha256>:<scope>` — both pieces invalidate
            # the cache when they change.
            OSM_SHA="$(sha256sum "$OSM_INPUT" | awk '{print $1}')"
            CURRENT_KEY="${OSM_SHA}:${OSM_SCOPE}"
        fi
        CACHED_KEY=""
        [ -f "$CACHE_KEY" ] && CACHED_KEY="$(cat "$CACHE_KEY")"
        STREETGRAPH_FROM_CACHE=0
        if [ -n "$CURRENT_KEY" ] && [ "$CURRENT_KEY" = "$CACHED_KEY" ] && [ -f "$CACHE_OBJ" ]; then
            echo "streetGraph.obj cache hit (key=$CURRENT_KEY) — copying $CACHE_OBJ into BUILD_DIR"
            cp "$CACHE_OBJ" "$BUILD_DIR/streetGraph.obj"
            STREETGRAPH_FROM_CACHE=1
        else
            if [ -n "$CACHED_KEY" ]; then
                echo "streetGraph.obj cache miss (key changed: $CACHED_KEY → $CURRENT_KEY) — rebuilding"
            else
                echo "streetGraph.obj cache empty — building from scratch"
            fi
        fi

        case "$BUILD_PHASES" in
            two_phase)
                if [ "$STREETGRAPH_FROM_CACHE" = "1" ]; then
                    echo "Phase 1/2 — SKIPPED (using cached streetGraph.obj)"
                else
                    # Phase 1 — read OSM PBF, build street graph, save streetGraph.obj.
                    # JVM exits at the end of this command, releasing the OSM-parse peak.
                    echo "Phase 1/2 — building street graph (heap=$HEAP) ..."
                    # shellcheck disable=SC2086  # JVM_OPTS is intentionally word-split
                    java -Xmx"$HEAP" $JVM_OPTS -jar /opt/otp/otp.jar --buildStreet --save "$BUILD_DIR"
                    # Persist for next time, only after phase 1 succeeded.
                    # Best-effort: we'd rather log a warning and let phase 2
                    # proceed than abort the entire build because the cache
                    # write hit a permissions or disk-space issue. The cache
                    # is purely a performance optimisation — its absence
                    # means the next build re-does phase 1, not that today's
                    # build is broken.
                    if [ -n "$CURRENT_KEY" ] && [ -f "$BUILD_DIR/streetGraph.obj" ]; then
                        if mkdir -p "$CACHE_DIR" 2>/dev/null \
                           && cp "$BUILD_DIR/streetGraph.obj" "$CACHE_OBJ" 2>/dev/null \
                           && printf '%s' "$CURRENT_KEY" > "$CACHE_KEY" 2>/dev/null; then
                            echo "streetGraph.obj cache updated (key=$CURRENT_KEY) at $CACHE_DIR"
                        else
                            echo "WARNING: streetGraph.obj cache write to $CACHE_DIR failed — next build will redo phase 1. Continuing." >&2
                        fi
                    fi
                fi

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
        # router-config.json travels with the graph — OTP's serve mode loads
        # it from the same directory as graph.obj, and the per-session
        # otp-<sid> serving container only has the graphs volume mounted
        # (not inbox), so the file has to live alongside the graph rather
        # than being read from inbox at serve time.
        if [ -f "$BUILD_DIR/router-config.json" ]; then
            cp "$BUILD_DIR/router-config.json" "$GRAPH_DIR/router-config.json"
        fi
        # The worker (host side) will move both into a timestamped subdir + flip the symlink.
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
