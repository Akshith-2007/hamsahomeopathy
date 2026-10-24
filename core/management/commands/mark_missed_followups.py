"""Mark SCHEDULED follow-ups whose date has passed as MISSED (run daily)."""
from django.core.management.base import BaseCommand

from clinic.services import mark_missed_followups


class Command(BaseCommand):
    help = "Mark past-date scheduled follow-ups as MISSED."

    def handle(self, *args, **options):
        count = mark_missed_followups()
        self.stdout.write(f"{count} follow-up(s) marked missed.")
