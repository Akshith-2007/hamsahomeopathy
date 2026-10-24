"""Recompute automatic (inferred) family links for every connected family. Safe to run repeatedly."""
from django.core.management.base import BaseCommand

from clinic.models import PatientRelationship
from clinic.relationships import rebuild_inferred_links


class Command(BaseCommand):
    help = "Rebuild inferred family links (run once after migrating, or any time to repair links)."

    def handle(self, *args, **options):
        before = PatientRelationship.objects.filter(source="INFERRED", status="ACTIVE").count()
        done = set()
        patient_ids = (PatientRelationship.objects.filter(source="MANUAL", status="ACTIVE")
                       .values_list("patient_id", flat=True).distinct())
        families = 0
        for patient_id in patient_ids:
            if patient_id in done:
                continue
            rebuild_inferred_links(patient_id)
            # Mark the whole family as done so each one is rebuilt once.
            from clinic.relationships import _component

            done |= _component(patient_id)
            families += 1
        after = PatientRelationship.objects.filter(source="INFERRED", status="ACTIVE").count()
        self.stdout.write(f"Rebuilt {families} famil{'y' if families == 1 else 'ies'}; "
                          f"inferred links {before} -> {after}.")
