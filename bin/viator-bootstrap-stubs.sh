#!/usr/bin/env sh
# Bootstrap the runtime stub files docker compose's `include:` directive
# requires at parse time.
#
# These files (docker/generated/{docker-compose.sessions.yml,nginx-sessions.conf})
# get overwritten by app/sessions_orchestrator on every web-container boot
# from current DB state. But compose parses include: BEFORE any container
# starts, so on a fresh clone — where neither file has been generated yet —
# `docker compose up` errors out without this script.
#
# Idempotent: leaves existing files alone. Safe to run defensively before
# any compose up. Closes audit-2026-05 #30.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GEN_DIR="$REPO_ROOT/docker/generated"

mkdir -p "$GEN_DIR"

COMPOSE_STUB="$GEN_DIR/docker-compose.sessions.yml"
NGINX_STUB="$GEN_DIR/nginx-sessions.conf"

if [ ! -f "$COMPOSE_STUB" ]; then
    cat > "$COMPOSE_STUB" <<'EOF'
# Bootstrap stub written by bin/viator-bootstrap-stubs.sh. Compose's `include:`
# requires this file at parse time. The web container's _startup hook calls
# app.sessions_orchestrator.regenerate() on every boot to overwrite it with
# current DB state — so this content is transient.
services: {}
EOF
    echo "created $COMPOSE_STUB"
fi

if [ ! -f "$NGINX_STUB" ]; then
    cat > "$NGINX_STUB" <<'EOF'
# Bootstrap stub written by bin/viator-bootstrap-stubs.sh. The web container's
# _startup hook overwrites this on every boot with one `location /otp/<sid>/`
# block per serving session — so this content is transient.
EOF
    echo "created $NGINX_STUB"
fi
