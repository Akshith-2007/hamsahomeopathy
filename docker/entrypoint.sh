#!/bin/sh
set -e

# Started as root (docker-compose, Render): make sure the uploads folder - possibly a freshly
# mounted persistent disk owned by root - belongs to the app user, then re-run as that user.
# Kubernetes already starts us as uid 1000 (see k8s/30-web-deployment.yaml), so this is skipped there.
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${MEDIA_ROOT:-/app/media}"
  chown -R app:app "${MEDIA_ROOT:-/app/media}"
  exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

python manage.py wait_for_db --timeout "${DB_WAIT_TIMEOUT:-60}"

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  python manage.py migrate --noinput
  python manage.py rebuild_family_links
fi

# First administrator from DJANGO_ADMIN_USERNAME / DJANGO_ADMIN_PASSWORD (no-op once it exists).
python manage.py ensure_admin

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
  python manage.py seed_demo --password "${DEMO_PASSWORD:-Hamsa@2026}"
fi

exec "$@"
