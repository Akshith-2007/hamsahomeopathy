from django.contrib import admin

from . import models as m


@admin.register(m.Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("opd_number", "full_name", "age", "gender", "phone", "registration_date", "is_deleted")
    search_fields = ("opd_number", "full_name", "phone")
    list_filter = ("gender", "is_deleted")

    def get_queryset(self, request):
        return m.Patient.all_objects.all()


@admin.register(m.Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ("patient", "visit_date", "visit_type", "doctor", "is_deleted")
    list_filter = ("visit_type", "is_deleted")
    raw_id_fields = ("patient",)

    def get_queryset(self, request):
        return m.Visit.all_objects.select_related("patient", "doctor")


for model in (m.CaseHistory, m.PhysicalGeneral, m.MentalGeneral, m.Examination, m.Diagnosis, m.Investigation,
              m.InvestigationReport, m.PastTreatment, m.PastTreatmentAttachment, m.PatientImage,
              m.RepertoryAnalysis, m.RepertoryEntry, m.Prescription, m.PrescriptionItem, m.FollowUp):
    admin.site.register(model)
