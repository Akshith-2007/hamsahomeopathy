"""Existing completed investigations get completed_at = latest report upload (or last update)."""
from django.db import migrations


def forwards(apps, schema_editor):
    Investigation = apps.get_model("clinic", "Investigation")
    InvestigationReport = apps.get_model("clinic", "InvestigationReport")
    for investigation in Investigation.objects.filter(status="COMPLETED", completed_at__isnull=True):
        latest = (InvestigationReport.objects.filter(investigation=investigation, is_deleted=False)
                  .order_by("-created_at").values_list("created_at", flat=True).first())
        investigation.completed_at = latest or investigation.updated_at
        investigation.save(update_fields=["completed_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0005_family_inference_prefill_lab"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
