#!/usr/bin/env bash
# Update to the latest code and restart:  scripts/deploy.sh
# Database migrations run automatically when the web container starts.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_env_file
cd "$APP_DIR"

[ "$(stat -c %a "$ENV_FILE")" = "600" ] || warn ".env should be private: chmod 600 $ENV_FILE"
if [ -z "$(env_get DOMAIN)" ]; then
    warn "IP mode (no DOMAIN): plain HTTP, for testing only. No real patient data until HTTPS is on."
    [ -n "$(env_get SERVER_IP)" ] || warn "SERVER_IP is empty in .env; the site will only answer on localhost."
fi

info "Pulling the latest code"
git pull --ff-only

info "Building the app image"
compose build --pull web

info "Starting containers"
compose up -d --remove-orphans

info "Waiting for the app to be ready"
if ! wait_ready 240; then
    compose ps
    die "The app did not become ready. Recent logs:  docker compose -f docker-compose.prod.yml logs --tail 100 web"
fi
docker image prune -f > /dev/null
compose ps
ok "Deployed $(git log -1 --format='%h %s')"
