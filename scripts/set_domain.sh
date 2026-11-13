#!/usr/bin/env bash
# Switch the site to a domain with automatic HTTPS (or change to another domain later).
#   sudo scripts/set_domain.sh example.com
# Checks DNS first; stops without changing anything if the domain doesn't point at this server.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0 <domain>"
[ $# -eq 1 ] || die "Usage: sudo $0 example.com"
domain="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's#^https?://##; s#/.*$##; s#^www\.##')"
[[ "$domain" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "'$1' is not a valid domain name."
require_env_file

server_ip="$(env_get SERVER_IP)"
[ -n "$server_ip" ] || server_ip="$(hostname -I | awk '{print $1}')"
info "Checking DNS: $domain and www.$domain must point to this server ($server_ip)"
for name in "$domain" "www.$domain"; do
    found="$(dig +short A "$name" @1.1.1.1 | grep -E '^[0-9.]+$' | sort -u | tr '\n' ' ' | sed 's/ $//')"
    if [ "$found" != "$server_ip" ]; then
        warn "$name resolves to '${found:-nothing}', expected '$server_ip'."
        die "Fix the A record in Hostinger (hPanel > Domains > DNS), wait for DNS to update, then run this again. Nothing was changed."
    fi
    if [ -n "$(dig +short AAAA "$name" @1.1.1.1)" ]; then
        warn "$name also has an AAAA (IPv6) record. If it doesn't point to this server, delete it or HTTPS certificates may fail."
    fi
done
ok "DNS points to this server."

info "Writing DOMAIN=$domain to .env"
env_set DOMAIN "$domain"
[ -n "$(env_get SERVER_IP)" ] || env_set SERVER_IP "$server_ip"

info "Installing the HTTPS Caddyfile"
tmp="$(mktemp)"
sed "s/{\$DOMAIN}/$domain/g" "$APP_DIR/deploy/Caddyfile.domain.template" > "$tmp"
caddy validate --config "$tmp" --adapter caddyfile > /dev/null || die "Generated Caddyfile is invalid; nothing was changed in Caddy."
[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
install -m 0644 "$tmp" /etc/caddy/Caddyfile
rm -f "$tmp"
systemctl reload caddy

info "Restarting the app with the new settings"
compose up -d --force-recreate web nginx
wait_ready 180 || die "The app did not become ready. See: docker compose -f docker-compose.prod.yml logs web"

info "Waiting for the HTTPS certificate and checking https://$domain/readyz (can take a minute)"
for _ in $(seq 1 24); do
    if curl -fsS --max-time 10 "https://$domain/readyz" > /dev/null 2>&1; then
        ok "https://$domain is live (and https://www.$domain)."
        echo "Next: run the deploy check  ->  docker compose -f docker-compose.prod.yml exec web python manage.py check --deploy"
        exit 0
    fi
    sleep 5
done
die "https://$domain/readyz did not answer. Check Caddy:  sudo journalctl -u caddy --since '10 min ago'"
