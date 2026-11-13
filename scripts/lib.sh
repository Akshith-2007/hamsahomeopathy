# Shared helpers for the scripts in this folder. Sourced, not run.
# shellcheck shell=bash

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$APP_DIR/.env"
COMPOSE_FILE="$APP_DIR/docker-compose.prod.yml"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32mOK:\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Read one value from .env without executing it (values like "Hamsa Homeopathy" are not shell-safe).
env_get() {
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']$//"
}

# Set KEY=value in .env, replacing an existing line or appending one.
# Rewrites in place (not sed -i) so the file keeps its owner and 600 permissions when run with sudo.
env_set() {
    if grep -qE "^$1=" "$ENV_FILE"; then
        local tmp; tmp="$(mktemp)"
        sed -E "s|^$1=.*|$1=$2|" "$ENV_FILE" > "$tmp"
        cat "$tmp" > "$ENV_FILE"
        rm -f "$tmp"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    fi
}

require_env_file() {
    [ -f "$ENV_FILE" ] || die "$ENV_FILE not found. Copy .env.example to .env and fill it in (see DEPLOY.md)."
}

compose() { docker compose -f "$COMPOSE_FILE" --project-directory "$APP_DIR" "$@"; }

# Wait until the app answers /readyz through nginx (database reachable). $1 = seconds to wait.
wait_ready() {
    local timeout="${1:-180}" waited=0
    until curl -fsS --max-time 5 http://127.0.0.1:8080/readyz > /dev/null 2>&1; do
        [ "$waited" -ge "$timeout" ] && return 1
        sleep 3; waited=$((waited + 3))
    done
}
