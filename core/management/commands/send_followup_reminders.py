"""Send SMS / WhatsApp reminders for follow-ups due on a given day (run daily as a CronJob)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from clinic.models import FollowUp
from core.notifications import notify_patient


class Command(BaseCommand):
    help = "Notify patients whose next follow-up is due in N days (default: tomorrow)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1)

    def handle(self, *args, days, **options):
        due = timezone.localdate() + timedelta(days=days)
        followups = FollowUp.objects.filter(next_followup_date=due, visit__is_deleted=False).select_related("visit__patient")
        sent = 0
        for followup in followups:
            patient = followup.visit.patient
            message = (f"Dear {patient.full_name}, this is a reminder of your follow-up at Hamsa Homeopathy "
                       f"on {due:%d %b %Y}. OPD No: {patient.opd_number}.")
            if notify_patient(patient, message):
                sent += 1
        self.stdout.write(f"{followups.count()} follow-ups due {due}; {sent} notified.")
