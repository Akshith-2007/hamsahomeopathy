"""Send SMS / WhatsApp reminders for scheduled follow-ups due on a given day (run daily as a CronJob)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from clinic.models import ScheduledFollowUp
from core.notifications import notify_patient


class Command(BaseCommand):
    help = "Notify patients whose scheduled follow-up is in N days (default: tomorrow)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1)

    def handle(self, *args, days, **options):
        due = timezone.localdate() + timedelta(days=days)
        followups = ScheduledFollowUp.objects.filter(
            status=ScheduledFollowUp.Status.SCHEDULED, scheduled_date=due
        ).select_related("patient")
        sent = 0
        for followup in followups:
            patient = followup.patient
            when = f"{due:%d %b %Y}" + (f" at {followup.scheduled_time:%H:%M}" if followup.scheduled_time else "")
            message = (f"Dear {patient.full_name}, this is a reminder of your follow-up at Hamsa Homeopathy "
                       f"on {when}. OPD No: {patient.opd_number}.")
            if notify_patient(patient, message):
                sent += 1
        self.stdout.write(f"{followups.count()} follow-ups due {due}; {sent} notified.")
