#!/usr/bin/env bash
# First-time setup of a fresh Ubuntu 24.04 VPS for Hamsa Homeopathy.
#   sudo scripts/setup_server.sh
# Installs Docker, Caddy (IP-mode config), ufw (22/80/443), fail2ban, unattended-upgrades, rclone.
# Safe to run again. It does NOT change SSH login settings; it prints those steps at the end.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0"
. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || warn "Written for Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}. Continuing."
export DEBIAN_FRONTEND=noninteractive

info "Updating the system and installing base packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl gnupg git ufw fail2ban python3-systemd unattended-upgrades \
    bind9-dnsutils rclone debian-keyring debian-archive-keyring apt-transport-https

info "Installing Docker (official repository)"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
info "Installing Caddy (official repository)"
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin caddy
systemctl enable --now docker caddy

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    usermod -aG docker "$SUDO_USER"
    ok "Added $SUDO_USER to the docker group (log out and back in for it to take effect)."
fi

info "Installing the IP-mode Caddyfile"
# Re-running after set_domain.sh must not throw the HTTPS config away.
if grep -q '127.0.0.1:8080' /etc/caddy/Caddyfile 2>/dev/null && ! grep -q '^:80 {' /etc/caddy/Caddyfile; then
    warn "/etc/caddy/Caddyfile is already set up for a domain. Leaving it alone."
else
    install -m 0644 "$APP_DIR/deploy/Caddyfile.ip" /etc/caddy/Caddyfile
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile > /dev/null
    systemctl reload caddy
fi

info "Firewall: allow SSH (22), HTTP (80), HTTPS (443); block everything else"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

info "fail2ban: ban IPs that keep failing SSH logins"
cat > /etc/fail2ban/jail.d/hamsa.local <<'EOF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable fail2ban
systemctl restart fail2ban

info "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

ok "Server setup complete: Docker $(docker --version | cut -d' ' -f3 | tr -d ,), Caddy $(caddy version | cut -d' ' -f1)."
cat <<EOF

------------------------------------------------------------------------------------------
NEXT: lock down SSH yourself (this script does not, so you cannot be locked out).
  1. From YOUR computer, confirm key login works for your sudo user in a NEW terminal:
         ssh ${SUDO_USER:-<user>}@<server-ip>
  2. Only then, on the server:
         sudo nano /etc/ssh/sshd_config.d/01-hardening.conf
     and put in (the name must sort first: sshd keeps the FIRST value it reads,
     and Hostinger's 50-cloud-init.conf may turn passwords back on):
         PermitRootLogin no
         PasswordAuthentication no
         KbdInteractiveAuthentication no
  3. Check and apply:   sudo sshd -t && sudo systemctl restart ssh
     Confirm:           sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication'
  4. Keep your current session open and test a NEW login before closing it.
------------------------------------------------------------------------------------------
EOF
