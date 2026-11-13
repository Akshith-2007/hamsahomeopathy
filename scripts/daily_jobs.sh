#!/usr/bin/env bash
# Daily follow-up jobs (run from cron, see deploy/crontab.example):
# mark yesterday's missed follow-ups, then send today's reminders.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

compose exec -T web python manage.py mark_missed_followups
compose exec -T web python manage.py send_followup_reminders
