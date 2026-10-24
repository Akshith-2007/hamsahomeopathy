"""Backfill visit links/status and turn future follow-up dates into scheduled follow-ups."""
from django.db import migrations
from django.utils import timezone

VALID_TYPES = {"NEW", "FOLLOW_UP"}


def forwards(apps, schema_editor):
    Patient = apps.get_model("clinic", "Patient")
    Visit = apps.get_model("clinic", "Visit")
    PrescriptionItem = apps.get_model("clinic", "PrescriptionItem")
    FollowUp = apps.get_model("clinic", "FollowUp")
    ScheduledFollowUp = apps.get_model("clinic", "ScheduledFollowUp")
    today = timezone.localdate()

    prescribed = set(
        PrescriptionItem.objects.filter(is_deleted=False, prescription__is_deleted=False)
        .values_list("prescription__visit_id", flat=True)
    )

    for patient_id in Patient.objects.values_list("id", flat=True):
        previous = None
        for visit in Visit.objects.filter(patient_id=patient_id, is_deleted=False).order_by("visit_date", "id"):
            if visit.visit_type not in VALID_TYPES:
                # Only fill gaps; recorded types are real data and are kept.
                visit.visit_type = "NEW" if previous is None else "FOLLOW_UP"
            if visit.visit_type == "FOLLOW_UP" and previous is not None:
                visit.previous_visit_id = previous.id
            if visit.visit_date < today or (visit.visit_date == today and visit.id in prescribed):
                visit.status = "COMPLETED"
            else:
                visit.status = "WAITING"
            visit.save(update_fields=["visit_type", "previous_visit", "status"])
            previous = visit

        latest = (
            FollowUp.objects.filter(visit__patient_id=patient_id, visit__is_deleted=False, is_deleted=False,
                                    next_followup_date__gte=today)
            .order_by("-next_followup_date", "-id").first()
        )
        if latest is not None:
            ScheduledFollowUp.objects.create(
                patient_id=patient_id, source_visit_id=latest.visit_id, scheduled_date=latest.next_followup_date,
                status="SCHEDULED", scheduled_by_id=latest.created_by_id, created_by_id=latest.created_by_id,
                updated_by_id=latest.created_by_id,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0002_checkin_and_followup_scheduling"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
