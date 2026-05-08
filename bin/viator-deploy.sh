#!/usr/bin/env bash
# Server-side deploy script — invoked via SSH forced-command from
# .github/workflows/deploy.yml (audit #17 Part B). The version arrives
# via $SSH_ORIGINAL_COMMAND, set by SSH server when the forced-command
# in ~/.ssh/authorized_keys overrides the client-supplied command.
#
# This script is the SOLE thing the deploy SSH key can run, so it:
#   1. Validates the version arg strictly (no shell metachars).
#   2. Runs the documented admin-guide §5.1 deploy procedure.
#   3. Verifies post-deploy state and exits non-zero on any anomaly.
#
# Operator setup is documented in admin-guide §2.4. Run as `otpadmin`
# (the user that owns /opt/viator and is in the docker group).

set -euo pipefail

# ─────────────────────────────── Validate input ───────────────────────────────
# $SSH_ORIGINAL_COMMAND is the *exact* command the client requested, before
# the forced-command override kicked in. We expect the deploy.yml workflow
# to ssh as: ssh -i <key> otpadmin@host "vX.Y.Z.W"
ARG="${SSH_ORIGINAL_COMMAND:-${1:-}}"
if [[ -z "$ARG" ]]; then
    echo "ERROR: no version supplied (expected vX.Y.Z.W in \$SSH_ORIGINAL_COMMAND)" >&2
    exit 2
fi
if ! [[ "$ARG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: invalid version format: $ARG (must match vX.Y.Z.W)" >&2
    exit 2
fi
VERSION="$ARG"

echo "============================================================"
echo "VIATOR deploy — $VERSION"
echo "started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# ─────────────────────────────── Update working tree ──────────────────────────
cd /opt/viator

echo ">>> git fetch + pull"
git fetch --all -v
git pull --ff-only
echo ""
echo ">>> repo state after pull:"
git log --oneline -3
echo ""

# Confirm the tag we're deploying is on origin (defence-in-depth — deploy.yml
# already verified the GHCR image exists, but we want this script to refuse
# if someone bypasses the workflow).
if ! git rev-parse --verify "refs/tags/$VERSION" >/dev/null 2>&1; then
    git fetch --tags origin "$VERSION" 2>&1 || {
        echo "ERROR: tag $VERSION not on origin" >&2
        exit 3
    }
fi

# ─────────────────────────────── Pin .env + pull images ───────────────────────
cd /opt/viator/docker

echo ">>> pin VIATOR_VERSION in .env"
sed -i "s/^VIATOR_VERSION=.*/VIATOR_VERSION=${VERSION}/" .env
grep '^VIATOR_VERSION' .env
echo ""

echo ">>> docker compose pull web worker otp-build"
docker compose pull web worker otp-build
echo ""

# ─────────────────────────────── Recreate web + worker ────────────────────────
echo ">>> docker compose up -d --force-recreate web worker"
docker compose up -d --force-recreate web worker
echo ""

# ─────────────────────────────── Verify ───────────────────────────────────────
echo ">>> waiting 8 sec for web container readiness"
sleep 8

echo ">>> healthz/version"
got=$(curl -sf http://localhost/healthz/version 2>/dev/null \
       || curl -skf https://localhost/healthz/version 2>/dev/null \
       || echo "")
echo "  $got"
if ! echo "$got" | grep -q "\"version\":\"${VERSION}\""; then
    echo "ERROR: /healthz/version did not return $VERSION" >&2
    exit 4
fi
echo ""

echo ">>> container UIDs (must be uid=1000 since v0.1.32.3)"
docker exec viator-web-1 id
docker exec viator-worker-1 id
echo ""

echo ">>> structlog boot regen evidence (audit #30 v2)"
if ! docker compose logs --no-log-prefix --tail 200 web \
     | grep -q "regenerated_at_boot"; then
    echo "ERROR: sessions_orchestrator.regenerated_at_boot event not found in web logs" >&2
    exit 5
fi
echo "  ✓ regenerated_at_boot event present"
echo ""

echo ">>> per-session OTP sanity (audit §6.11 step 7)"
db_sessions=$(docker exec viator-web-1 \
    python -c "from app.worker import _list_serving_sessions; print(_list_serving_sessions())")
running=$(docker ps --filter "name=^viator-otp-" --format "{{.Names}}")
echo "  DB serving sessions: $db_sessions"
echo "  Running OTP containers:"
echo "$running" | sed 's/^/    /'
echo ""

# Cross-check (advisory — don't fail the deploy on mismatch since the
# orchestrator's auto-reconcile may still be in flight; operator gets the
# warning in the workflow log).
if [[ -n "$db_sessions" && "$db_sessions" != "[]" ]]; then
    while IFS= read -r sid; do
        sid_clean=$(echo "$sid" | tr -d "'\"[], ")
        [[ -z "$sid_clean" ]] && continue
        if ! echo "$running" | grep -q "viator-otp-${sid_clean}"; then
            echo "WARN: session '$sid_clean' is in DB but no matching viator-otp-* container is running" >&2
        fi
    done < <(echo "$db_sessions" | tr ',' '\n')
fi

echo "============================================================"
echo "DEPLOY OK — $VERSION live on $(hostname)"
echo "ended at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"
