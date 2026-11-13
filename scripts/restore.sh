#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database:
#   sudo scripts/restore.sh <hamsa-db-...dump.gpg> <hamsa-media-...tar.gz.gpg> [--wipe]
# Refuses if the database already has tables, unless --wipe is given (then it is dropped and recreated
# after a second confirmation). Unencrypted .dump / .tar.gz files are accepted too.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo $0 ...)."
wipe=false; files=()
for arg in "$@"; do
    if [ "$arg" = "--wipe" ]; then wipe=true; else files+=("$arg"); fi
done
[ "${#files[@]}" -eq 2 ] || die "Usage: sudo $0 <db-backup.dump.gpg> <media-backup.tar.gz.gpg> [--wipe]"
dump="${files[0]}"; media="${files[1]}"
[ -f "$dump" ] || die "Not found: $dump"
[ -f "$media" ] || die "Not found: $media"
require_env_file

PASS_FILE="${BACKUP_PASSPHRASE_FILE:-/root/.hamsa-backup-passphrase}"
decrypt() {
    case "$1" in
        *.gpg) gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" --decrypt "$1" ;;
        *) cat "$1" ;;
    esac
}
if [[ "$dump$media" == *.gpg* ]] && [ ! -f "$PASS_FILE" ]; then
    die "$PASS_FILE is missing. Put the backup passphrase there first (chmod 600)."
fi

info "Checking the backup files"
magic="$( { decrypt "$dump" 2> /dev/null || true; } | head -c 5)"
[ "$magic" = "PGDMP" ] || die "$dump is not a database backup, or the passphrase is wrong."
{ decrypt "$media" | tar -tzf - > /dev/null; } || die "$media is not a readable uploads archive, or the passphrase is wrong."

db="$(env_get POSTGRES_DB)"; db="${db:-hamsa}"
user="$(env_get POSTGRES_USER)"; user="${user:-hamsa}"
compose up -d db
for _ in $(seq 1 30); do compose exec -T db pg_isready -q -U "$user" -d "$db" && break; sleep 2; done
tables="$(compose exec -T db psql -U "$user" -d "$db" -tAc "select count(*) from pg_tables where schemaname = 'public'" | tr -d '[:space:]')"

cat <<EOF

$(printf '\033[1;31m')#####################################################################
#                         DATABASE RESTORE                          #
#####################################################################$(printf '\033[0m')
  Database : $db  (currently $tables tables)
  From     : $dump
  Uploads  : $media  (extracted into the uploads volume)
  The app is STOPPED during the restore and started again afterwards.
EOF
if [ "$tables" != "0" ]; then
    $wipe || die "Database '$db' is not empty. Nothing was changed.
       To ERASE it and restore, run again with --wipe (only if you are sure this is what you want)."
    printf '\033[1;31m  --wipe: ALL CURRENT DATA IN "%s" WILL BE PERMANENTLY DELETED.\033[0m\n' "$db"
    read -r -p "  Type the database name ($db) to confirm the wipe: " answer
    [ "$answer" = "$db" ] || die "Cancelled. Nothing was changed."
fi
read -r -p "  Type RESTORE to continue: " answer
[ "$answer" = "RESTORE" ] || die "Cancelled. Nothing was changed."

trap 'warn "Restore FAILED part-way. The app is stopped. Fix the problem above, then run again with --wipe."' ERR
info "Stopping the app"
compose stop web nginx
if [ "$tables" != "0" ]; then
    info "Dropping and recreating database '$db'"
    compose exec -T db dropdb -U "$user" --force "$db"
    compose exec -T db createdb -U "$user" -O "$user" "$db"
fi

info "Restoring the database"
decrypt "$dump" | compose exec -T db pg_restore -U "$user" -d "$db" --no-owner --exit-on-error

info "Restoring uploaded files"
decrypt "$media" | compose run --rm --no-deps -T --entrypoint tar web -xzf - -C /app/media

trap - ERR
info "Starting the app (migrations for newer code run automatically)"
compose up -d
wait_ready 240 || die "The app did not become ready. See: docker compose -f docker-compose.prod.yml logs web"
ok "Restore complete. Log in and check a few patients and their attachments."
