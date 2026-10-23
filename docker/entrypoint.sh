#!/bin/sh
set -e

python manage.py wait_for_db --timeout "${DB_WAIT_TIMEOUT:-60}"

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  python manage.py migrate --noinput
fi

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
  python manage.py seed_demo --password "${DEMO_PASSWORD:-Hamsa@2026}"
fi

exec "$@"
