#!/usr/bin/env bash
# Diagnose which raw OSM PBF is corrupt after a failed osmium merge.
# Reports each file under /tmp/osm-merge/raw/ as OK or BAD, then lists
# any leftover .part files from interrupted downloads.

WORK="${WORK_DIR:-/tmp/osm-merge}"
IMG="${OSMIUM_IMAGE:-viator-osmium-helper:latest}"

for f in "$WORK"/raw/*.pbf; do
    [ -e "$f" ] || continue
    name=$(basename "$f")
    size=$(du -h "$f" | cut -f1)
    result=$(docker run --rm -v "$WORK:/work" "$IMG" \
                osmium fileinfo "/work/raw/$name" 2>&1 | head -10)
    if echo "$result" | grep -qiE "error|invalid|premature|truncated"; then
        echo "BAD  $name  ($size)"
        echo "$result" | sed 's/^/      /'
    else
        nodes=$(echo "$result" | grep -i "Number of nodes" | head -1)
        echo "OK   $name  ($size)  ${nodes#*:}"
    fi
done

echo "---"
echo "Staged files:"
ls -lh "$WORK"/staged/*.pbf 2>/dev/null || echo "(no staged files)"

echo "---"
echo "Leftover .part files (interrupted downloads):"
ls -lh "$WORK"/raw/*.part 2>/dev/null || echo "(none)"
