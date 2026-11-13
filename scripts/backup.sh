#!/usr/bin/env bash
# Encrypted backup of the database and uploaded files:  sudo scripts/backup.sh
#   ~/backups/hamsa-db-<date>.dump.gpg       (pg_dump custom format)
#   ~/backups/hamsa-media-<date>.tar.gz.gpg  (uploads volume)
# Nothing unencrypted is ever written to disk. Local copies older than 30 days are deleted.
# If BACKUP_RCLONE_REMOTE is set in .env (e.g. "b2:hamsa-backups"), the files are also copied there.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo $0): the passphrase file is root-only."
require_env_file
umask 077

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
PASS_FILE="${BACKUP_PASSPHRASE_FILE:-/root/.hamsa-backup-passphrase}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
[ -f "$PASS_FILE" ] || die "Passphrase file $PASS_FILE is missing. Create it (see DEPLOY.md, 'Backups'):
    sudo sh -c 'openssl rand -base64 32 > $PASS_FILE && chmod 600 $PASS_FILE'
and keep a copy of the passphrase OFF the server: without it the backups cannot be opened."
[ "$(stat -c '%U %a' "$PASS_FILE")" = "root 600" ] || die "$PASS_FILE must be owned by root with mode 600."

db="$(env_get POSTGRES_DB)"; db="${db:-hamsa}"
user="$(env_get POSTGRES_USER)"; user="${user:-hamsa}"
stamp="$(date +%Y-%m-%d_%H%M)"
mkdir -p "$BACKUP_DIR"

encrypt() {  # stdin -> $1
    gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" \
        --symmetric --cipher-algo AES256 -o "$1"
}
# Write to .part and rename only after the whole pipeline succeeded (pipefail stops the script before
# the mv), so a failed dump never leaves a file that looks like a good backup.
trap 'rm -f "$BACKUP_DIR"/*.part' EXIT

info "Backing up database '$db'"
db_file="$BACKUP_DIR/hamsa-db-$stamp.dump.gpg"
compose exec -T db pg_dump -U "$user" -d "$db" -Fc | encrypt "$db_file.part"
mv "$db_file.part" "$db_file"

info "Backing up uploaded files"
media_file="$BACKUP_DIR/hamsa-media-$stamp.tar.gz.gpg"
compose exec -T web tar -C /app/media -czf - . | encrypt "$media_file.part"
mv "$media_file.part" "$media_file"
ok "Saved $(du -h "$db_file" | cut -f1) $db_file"
ok "Saved $(du -h "$media_file" | cut -f1) $media_file"

info "Deleting local backups older than $KEEP_DAYS days"
find "$BACKUP_DIR" -maxdepth 1 -name 'hamsa-*.gpg' -mtime "+$KEEP_DAYS" -print -delete

remote="$(env_get BACKUP_RCLONE_REMOTE)"
if [ -z "$remote" ]; then
    warn "BACKUP_RCLONE_REMOTE is not set in .env: backups exist ONLY on this server. Set up an off-site copy (DEPLOY.md)."
    exit 0
fi
info "Copying to off-site storage: $remote"
rclone copy "$BACKUP_DIR" "$remote" --include 'hamsa-*.gpg'
ok "Off-site copy done."
