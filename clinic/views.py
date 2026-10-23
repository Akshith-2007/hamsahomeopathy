import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role
from accounts.rbac import can_delete, can_edit, has_role, role_required, section_access
from core.models import AuditLog

from . import models as m
from .case_record import MENTAL_LAYOUT, PHYSICAL_LAYOUT, CaseRecord
from .forms import PatientForm, PatientSearchForm, VisitForm

CAN_REGISTER = (Role.ADMIN, Role.DOCTOR, Role.ATTENDER)

# Sections copied into a new follow-up visit so the doctor only edits what changed.
CARRY_FORWARD = {
    "case_history": ["presenting_complaints", "past_history", "family_history"],
    "physical_generals": [f.name for f in m.PhysicalGeneral._meta.concrete_fields if f.editable and f.name not in {"id", "visit"}],
    "mental_generals": [f.name for f in m.MentalGeneral._meta.concrete_fields if f.editable and f.name not in {"id", "visit"}],
}


def search_patients(query):
    patients = m.Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(opd_number__icontains=query) | Q(full_name__icontains=query) | Q(phone__icontains=query)
        )
    return patients


@login_required
def patient_list(request):
    form = PatientSearchForm(request.GET or None)
    query = request.GET.get("q", "").strip()
    followup_mode = request.GET.get("mode") == "followup"
    patients = (
        search_patients(query)
        .annotate(visit_count=Count("visits", filter=Q(visits__is_deleted=False)))
        .order_by("-registration_date", "-id")
    )
    page = Paginator(patients, 20).get_page(request.GET.get("page"))
    return render(request, "clinic/patient_list.html", {
        "form": form, "page": page, "q": query, "followup_mode": followup_mode,
        "can_register": has_role(request.user, *CAN_REGISTER),
    })


@role_required(*CAN_REGISTER)
def patient_create(request):
    patient_form = PatientForm(request.POST or None, request.FILES or None, prefix="patient")
    visit_form = VisitForm(request.POST or None, prefix="visit",
                           initial={"doctor": request.user if has_role(request.user, Role.DOCTOR) else None})
    if request.method == "POST" and patient_form.is_valid() and visit_form.is_valid():
        with transaction.atomic():
            patient = patient_form.save()
            visit = visit_form.save(commit=False)
            visit.patient = patient
            visit.visit_type = m.Visit.Type.NEW
            visit.save()
        messages.success(request, f"Patient registered with OPD No. {patient.opd_number}.")
        return redirect("visit_record", pk=visit.pk)
    return render(request, "clinic/patient_form.html", {"form": patient_form, "visit_form": visit_form})


@login_required
def patient_detail(request, pk):
    patient = get_object_or_404(m.Patient, pk=pk)
    access = section_access(request.user)
    visits = patient.visits.select_related("doctor")
    images = patient.images.select_related("created_by") if access["patient_images"]["view"] else []
    AuditLog.record(AuditLog.Action.VIEW, patient)
    return render(request, "clinic/patient_detail.html", {
        "patient": patient, "visits": visits, "images": images, "access": access,
        "can_add_visit": can_edit(request.user, "visit"), "can_delete_patient": can_delete(request.user, "patient"),
    })


@role_required(Role.ADMIN)
@require_POST
def patient_delete(request, pk):
    patient = get_object_or_404(m.Patient, pk=pk)
    patient.delete()
    messages.success(request, f"Patient {patient.opd_number} deleted.")
    return redirect("patient_list")


@role_required(*CAN_REGISTER)
@require_POST
def visit_create(request, patient_pk):
    """Start a follow-up visit, carrying forward the last recorded case details."""
    patient = get_object_or_404(m.Patient, pk=patient_pk)
    previous = patient.latest_visit
    with transaction.atomic():
        visit = m.Visit.objects.create(
            patient=patient,
            visit_type=m.Visit.Type.FOLLOW_UP if previous else m.Visit.Type.NEW,
            doctor=request.user if has_role(request.user, Role.DOCTOR) else (previous.doctor if previous else None),
        )
        if previous:
            for section, fields in CARRY_FORWARD.items():
                source = previous.section(section)
                if source is not None:
                    model = type(source)
                    model.objects.create(visit=visit, **{name: getattr(source, name) for name in fields})
    note = f" Case details carried forward from {previous.visit_date:%d-%b-%Y}." if previous else ""
    messages.success(request, f"New visit started for {patient.full_name}.{note}")
    return redirect("visit_record", pk=visit.pk)


def _visit_or_404(pk):
    return get_object_or_404(m.Visit.objects.select_related("patient", "doctor"), pk=pk)


@login_required
def visit_record(request, pk):
    visit = _visit_or_404(pk)
    record = CaseRecord(request, visit)
    if request.method == "POST":
        if record.is_valid():
            record.save()
            messages.success(request, "Case record saved.")
            return redirect("visit_record", pk=visit.pk)
        messages.error(request, "Please correct the highlighted fields.")
    else:
        AuditLog.record(AuditLog.Action.VIEW, visit)

    access = record.access
    history = visit.patient.visits.exclude(pk=visit.pk).select_related("doctor")
    images = visit.patient.images.select_related("created_by") if access["patient_images"]["view"] else []
    patient_followups = (
        m.FollowUp.objects.filter(visit__patient=visit.patient, visit__is_deleted=False).exclude(visit=visit)
        if access["followups"]["view"] else []
    )
    return render(request, "clinic/visit_record.html", {
        "visit": visit, "patient": visit.patient, "record": record, "access": access,
        "forms": record.forms, "formsets": record.formsets,
        "physical_layout": PHYSICAL_LAYOUT, "mental_layout": MENTAL_LAYOUT,
        "image_stages": [("BEFORE", "Before Treatment", "before_images", "danger"),
                         ("AFTER", "After Treatment", "after_images", "success")],
        "open_sections": {s for s, a in access.items() if a["open"]} | record.error_sections,
        "history": history, "images": images, "patient_followups": patient_followups,
        "investigation": record.investigation if record.investigation.pk else None,
        "repertory": record.repertory if record.repertory.pk else None,
        "past_treatment": record.forms["past_treatment"].instance if record.forms["past_treatment"].instance.pk else None,
        "can_edit_any": any(a["edit"] for a in access.values()),
        "can_add_visit": can_edit(request.user, "visit"),
        "can_delete_visit": can_delete(request.user, "visit"),
        "medicine_suggestions": m.PrescriptionItem.objects.values_list("medicine_name", flat=True).distinct()[:300]
        if access["prescription"]["edit"] else [],
    })


@login_required
def visit_summary(request, pk):
    """Read-only visit details for the previous-visit modal."""
    visit = _visit_or_404(pk)
    AuditLog.record(AuditLog.Action.VIEW, visit)
    return render(request, "clinic/_visit_summary.html", {
        "visit": visit, "access": section_access(request.user),
        "case_history": visit.section("case_history"),
        "physical": visit.section("physical_generals"),
        "mental": visit.section("mental_generals"),
        "examination": visit.section("examination"),
        "diagnosis": visit.section("diagnosis"),
        "investigations": visit.investigations.prefetch_related("reports"),
        "prescriptions": visit.prescriptions.prefetch_related("items"),
        "followups": visit.followups.all(),
    })


@role_required(Role.ADMIN)
@require_POST
def visit_delete(request, pk):
    visit = _visit_or_404(pk)
    visit.delete()
    messages.success(request, "Visit deleted.")
    return redirect("patient_detail", pk=visit.patient_id)


@role_required(Role.ADMIN, Role.DOCTOR)
def prescription_print(request, pk):
    visit = _visit_or_404(pk)
    prescription = visit.prescriptions.prefetch_related("items").first()
    return render(request, "clinic/prescription_print.html", {
        "visit": visit, "patient": visit.patient, "prescription": prescription,
        "diagnosis": visit.section("diagnosis"), "examination": visit.section("examination"),
        "doctor": visit.doctor or request.user,
    })


# kind -> (model, rbac section)
UPLOAD_KINDS = {
    "report": (m.InvestigationReport, "investigation_reports"),
    "past-treatment": (m.PastTreatmentAttachment, "past_treatment"),
    "image": (m.PatientImage, "patient_images"),
}


@login_required
@require_POST
def upload_delete(request, kind, pk):
    if kind not in UPLOAD_KINDS:
        raise PermissionDenied
    model, section = UPLOAD_KINDS[kind]
    if not can_delete(request.user, section):
        raise PermissionDenied
    obj = get_object_or_404(model, pk=pk)
    obj.delete()
    messages.success(request, "File removed.")
    return redirect(request.POST.get("next") or "home")


@role_required(Role.ADMIN, Role.DOCTOR)
@require_POST
def repertory_sheet_delete(request, pk):
    analysis = get_object_or_404(m.RepertoryAnalysis, pk=pk)
    analysis.sheet = ""
    analysis.save()
    messages.success(request, "Repertorization sheet removed.")
    return redirect("visit_record", pk=analysis.visit_id)


# ----------------------------------------------------------------------------- reports
def _date_range(request):
    today = timezone.localdate()
    try:
        start = timezone.datetime.strptime(request.GET.get("start", ""), "%Y-%m-%d").date()
    except ValueError:
        start = today.replace(day=1)
    try:
        end = timezone.datetime.strptime(request.GET.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        end = today
    return start, end


@role_required(Role.ADMIN, Role.DOCTOR)
def reports(request):
    start, end = _date_range(request)
    visits = m.Visit.objects.filter(visit_date__range=(start, end))
    if request.GET.get("export") == "csv":
        return _export_visits(visits, start, end)

    new_patients = m.Patient.objects.filter(registration_date__range=(start, end))
    diagnoses = (
        m.Diagnosis.objects.filter(visit__in=visits).exclude(final="")
        .values("final").annotate(total=Count("id")).order_by("-total")[:10]
    )
    medicines = (
        m.PrescriptionItem.objects.filter(prescription__visit__in=visits, prescription__is_deleted=False)
        .values("medicine_name").annotate(total=Count("id")).order_by("-total")[:10]
    )
    by_doctor = visits.values("doctor__first_name", "doctor__last_name", "doctor__username").annotate(total=Count("id")).order_by("-total")
    gender = new_patients.values("gender").annotate(total=Count("id")).order_by("gender")
    gender_labels = dict(m.Gender.choices)
    return render(request, "clinic/reports.html", {
        "start": start, "end": end,
        "totals": {
            "visits": visits.count(),
            "new_cases": visits.filter(visit_type=m.Visit.Type.NEW).count(),
            "follow_ups": visits.filter(visit_type=m.Visit.Type.FOLLOW_UP).count(),
            "new_patients": new_patients.count(),
            "pending_investigations": m.Investigation.objects.filter(
                visit__in=visits, status=m.Investigation.Status.REQUESTED).count(),
        },
        "diagnoses": diagnoses, "medicines": medicines, "by_doctor": by_doctor,
        "gender": [(gender_labels.get(g["gender"], g["gender"]), g["total"]) for g in gender],
    })


def _export_visits(visits, start, end):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="visits_{start}_{end}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Visit date", "OPD No.", "Patient", "Age", "Sex", "Phone", "Visit type", "Doctor", "Diagnosis", "Prescription"])
    for visit in visits.select_related("patient", "doctor").order_by("visit_date"):
        p = visit.patient
        writer.writerow([visit.visit_date, p.opd_number, p.full_name, p.age, p.get_gender_display(), p.phone,
                         visit.get_visit_type_display(), visit.doctor or "", visit.diagnosis_summary, visit.prescription_summary])
    AuditLog.record(AuditLog.Action.EXPORT, model_name="clinic.Visit", object_repr=f"Visits {start}..{end}")
    return response


def dashboard_context(user):
    """Widgets for the home page, limited to what the role may see."""
    access = section_access(user)
    today = timezone.localdate()
    ctx = {
        "today_visits": m.Visit.objects.filter(visit_date=today).select_related("patient", "doctor")[:20],
        "pending_investigations": m.Investigation.objects.filter(status=m.Investigation.Status.REQUESTED, visit__is_deleted=False)
        .select_related("visit__patient").order_by("requested_date")[:10],
        "stats": {
            "patients": m.Patient.objects.count(),
            "today": m.Visit.objects.filter(visit_date=today).count(),
            "pending_labs": m.Investigation.objects.filter(status=m.Investigation.Status.REQUESTED, visit__is_deleted=False).count(),
        },
    }
    if access["patient"]["edit"]:
        ctx["upcoming_followups"] = (
            m.FollowUp.objects.filter(next_followup_date__range=(today, today + timedelta(days=7)), visit__is_deleted=False)
            .select_related("visit__patient").order_by("next_followup_date")[:10]
        )
        ctx["stats"]["followups"] = m.FollowUp.objects.filter(
            next_followup_date__range=(today, today + timedelta(days=7)), visit__is_deleted=False).count()
    if access["prescription"]["view"]:
        ctx["recent_prescriptions"] = (
            m.Prescription.objects.filter(visit__is_deleted=False).select_related("visit__patient", "created_by")
            .prefetch_related("items").order_by("-created_at")[:8]
        )
    return ctx
