#!/usr/bin/env bash
# One-shot patcher: replace the single-shot "audit #30 v2" check in
# /opt/viator/bin/viator-deploy.sh with a 10-attempt retry that tolerates
# the ~50-200ms race between healthz returning 200 and the boot-regen log
# line landing in docker's log collector.
#
# Behaviour:
#   - Backs up the original to viator-deploy.sh.bak.<timestamp>
#   - Idempotent: re-running on an already-patched file is a no-op
#   - Validates the result with bash -n before installing
#
# Usage on the VPS:
#   sudo bash /tmp/patch_deploy_audit_retry.sh

set -euo pipefail

TARGET="/opt/viator/bin/viator-deploy.sh"

if [ ! -f "$TARGET" ]; then
    echo "ERROR: $TARGET not found"
    exit 1
fi

if grep -q "audit_found=0" "$TARGET"; then
    echo "Already patched (found 'audit_found=0' sentinel) - no-op"
    exit 0
fi

BACKUP="${TARGET}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$TARGET" "$BACKUP"
echo "Backup: $BACKUP"

# Use Python for the multi-line string replacement; sed's multi-line
# matching is fragile across backslash-continuation lines.
python3 - "$TARGET" <<'PY'
import pathlib, sys

p = pathlib.Path(sys.argv[1])
s = p.read_text()

# Old block — exact match including the line-continuation backslash + indentation.
OLD = '''echo ">>> structlog boot regen evidence (audit #30 v2)"
if ! docker compose logs --no-log-prefix --tail 200 web \\
     | grep -q "regenerated_at_boot"; then
    echo "ERROR: sessions_orchestrator.regenerated_at_boot event not found in web logs" >&2
    exit 5
fi
echo "  ✓ regenerated_at_boot event present"'''

NEW = '''echo ">>> structlog boot regen evidence (audit #30 v2)"
# Race-tolerant: the boot-regen log line can land in docker's log
# collector ~50-200ms after healthz returns 200. Retry briefly before
# declaring the event missing - a real regenerate-failure would still
# trip the audit after the full retry budget elapses (~5s).
audit_found=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if docker compose logs --no-log-prefix --tail 200 web \\
         | grep -q "regenerated_at_boot"; then
        audit_found=1
        echo "  ✓ regenerated_at_boot event present (attempt $attempt)"
        break
    fi
    sleep 0.5
done
if [ "$audit_found" = "0" ]; then
    echo "ERROR: sessions_orchestrator.regenerated_at_boot event not found in web logs after 10 attempts (~5s)" >&2
    exit 5
fi'''

if OLD not in s:
    print("ERROR: existing block did not match expected text -- not patching.")
    print("Manual inspection required; backup is intact.")
    sys.exit(2)

p.write_text(s.replace(OLD, NEW))
print("Replacement done.")
PY

# Validate the patched script parses as bash
if ! bash -n "$TARGET"; then
    echo "ERROR: patched file fails bash -n syntax check - rolling back"
    cp -a "$BACKUP" "$TARGET"
    exit 3
fi

echo ""
echo "OK. Patched section (lines around the audit):"
grep -n "audit #30 v2" "$TARGET" | head -3
echo ""
echo "If you want to roll back: sudo cp $BACKUP $TARGET"
