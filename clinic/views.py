import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Prefetch, Q, Value, When
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.rbac import (
    can_delete, can_edit, can_view, can_view_report, default_open, section_access, section_required,
)
from core import inbox
from core.models import AuditLog, Notification
from core.validators import document_validators, run_validators

from . import models as m
from . import pharmacy, relationships, services
from . import lab as labsvc
from .family import grouped
from .case_record import MENTAL_LAYOUT, PHYSICAL_LAYOUT, CaseRecord
from .forms import (
    PatientForm, PatientSearchForm, RegistrationVisitForm, RelationshipForm, RescheduleFollowUpForm,
    ScheduleFollowUpForm,
)
from .history import PatientHistory

STATUS_ORDER = Case(
    When(status=m.Visit.Status.WAITING, then=Value(0)),
    When(status=m.Visit.Status.IN_CONSULTATION, then=Value(1)),
    default=Value(2), output_field=IntegerField(),
)


def _next_url(request, default):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return target
    return default


def search_patients(query):
    patients = m.Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(opd_number__icontains=query) | Q(full_name__icontains=query) | Q(phone__icontains=query)
        )
    return patients


def _active_followups_prefetch():
    return Prefetch(
        "scheduled_followups",
        queryset=m.ScheduledFollowUp.objects.filter(status=m.ScheduledFollowUp.Status.SCHEDULED),
        to_attr="active_followups",
    )


# ----------------------------------------------------------------------------- patients & check-in
@login_required
def patient_list(request):
    query = request.GET.get("q", "").strip()
    if not can_view(request.user, "patient"):
        # Lab staff search from their worklist, which shows identity fields only.
        return redirect(f"{reverse('lab_worklist')}?q={query}" if query else reverse("lab_worklist"))
    mode = request.GET.get("mode", "")
    patients = (
        search_patients(query)
        .annotate(visit_count=Count("visits", filter=Q(visits__is_deleted=False)))
        .prefetch_related(_active_followups_prefetch())
        .order_by("-registration_date", "-id")
    )
    page = Paginator(patients, 20).get_page(request.GET.get("page"))
    return render(request, "clinic/patient_list.html", {
        "form": PatientSearchForm(request.GET or None), "page": page, "q": query,
        "mode": mode if mode in {"followup", "newcase"} else "",
        "can_checkin": can_edit(request.user, "checkin"),
    })


@section_required("checkin", "edit")
def checkin(request):
    """Reception entry point: choose New case or Follow-up."""
    today = timezone.localdate()
    waiting = (
        m.Visit.objects.filter(visit_date=today).select_related("patient", "doctor")
        .order_by(STATUS_ORDER, "created_at")
    )
    due = (
        m.ScheduledFollowUp.objects.filter(status=m.ScheduledFollowUp.Status.SCHEDULED, scheduled_date__lte=today)
        .select_related("patient")
    )
    return render(request, "clinic/checkin.html", {"waiting": waiting, "due_followups": due})


@section_required("checkin", "edit")
def patient_create(request):
    """Register a new patient; always creates today's New-case visit (WAITING)."""
    patient_form = PatientForm(request.POST or None, request.FILES or None, prefix="patient")
    visit_form = RegistrationVisitForm(request.POST or None, prefix="visit")
    if request.method == "POST" and patient_form.is_valid() and visit_form.is_valid():
        with transaction.atomic():
            patient = patient_form.save()
            result = services.check_in(
                patient, m.Visit.Type.NEW, request.user,
                doctor=visit_form.cleaned_data.get("doctor"), visit_date=visit_form.cleaned_data.get("visit_date"),
            )
            if request.POST.get("has_family") == "yes" and can_edit(request.user, "relationships"):
                _link_from_registration(request, patient)
        auto = [link for link in relationships.relatives_of(patient) if link.source == "INFERRED"]
        if auto:
            messages.info(request, "Also connected automatically: " + ", ".join(
                f"{link.related_patient.full_name} ({link.display_label})" for link in auto) + ".")
        if request.POST.get("action") == "save_only":
            services.record_patient_without_case_sheet(result.visit, request.user)
            messages.success(request, f"{patient.full_name} registered (OPD No. {patient.opd_number}) and checked in "
                                      "as a new case. The case sheet can be filled later.")
            return redirect("patient_detail", pk=patient.pk)
        messages.success(request, f"Patient registered with OPD No. {patient.opd_number}.")
        return redirect("visit_record", pk=result.visit.pk)
    return render(request, "clinic/patient_form.html", {
        "form": patient_form, "visit_form": visit_form,
        "relationship_choices": relationships.CHOICES, "can_link": can_edit(request.user, "relationships"),
    })


def _link_from_registration(request, patient):
    """Create the relative links chosen on the registration form (relative_id/relationship/label lists)."""
    ids = request.POST.getlist("relative_id")
    kinds = request.POST.getlist("relative_relationship")
    labels = request.POST.getlist("relative_label")
    for index, relative_id in enumerate(ids):
        relative = m.Patient.objects.filter(pk=relative_id).first()
        kind = kinds[index] if index < len(kinds) else ""
        label = labels[index] if index < len(labels) else ""
        if relative is None:
            continue
        try:
            relationships.link(patient, relative, kind, request.user, label=label.strip())
        except services.WorkflowError as exc:
            messages.warning(request, f"{relative.full_name}: {exc}")


def _do_checkin(request, patient, visit_type):
    result = services.check_in(patient, visit_type, request.user)
    visit = result.visit
    parts = [f"{patient.full_name} checked in as {visit.get_visit_type_display().lower()}."]
    if result.downgraded_to_new:
        parts.append("No earlier visit was found, so this was recorded as a new case.")
    if result.carried_from:
        parts.append(f"Case details carried forward from {result.carried_from.visit_date:%d-%b-%Y}.")
    if result.completed_followup:
        parts.append(f"Scheduled follow-up of {result.completed_followup.scheduled_date:%d-%b-%Y} marked completed.")
    messages.success(request, " ".join(parts))
    if request.POST.get("open") == "1":
        return redirect("visit_record", pk=visit.pk)
    return redirect(_next_url(request, reverse("patient_detail", args=[patient.pk])))


@section_required("checkin", "edit")
@require_POST
def checkin_start(request, patient_pk):
    patient = get_object_or_404(m.Patient, pk=patient_pk)
    visit_type = request.POST.get("visit_type")
    if visit_type not in m.Visit.Type.values:
        messages.error(request, "Choose New case or Follow-up.")
        return redirect("patient_detail", pk=patient.pk)
    return _do_checkin(request, patient, visit_type)


@section_required("checkin", "edit")
@require_POST
def visit_create(request, patient_pk):
    """Legacy endpoint: start a follow-up (or first) visit and open the case sheet."""
    patient = get_object_or_404(m.Patient, pk=patient_pk)
    visit_type = request.POST.get("visit_type") or m.Visit.Type.FOLLOW_UP
    if "open" not in request.POST:
        request.POST = request.POST.copy()
        request.POST["open"] = "1"
    return _do_checkin(request, patient, visit_type)


@login_required
def patient_detail(request, pk):
    patient = get_object_or_404(m.Patient, pk=pk)
    if not can_view(request.user, "patient"):
        return redirect(f"{reverse('lab_dashboard')}?q={patient.opd_number}")
    access = section_access(request.user)
    visits = patient.visits.select_related("doctor", "reported_by")
    images = patient.images.select_related("created_by") if access["patient_images"]["view"] else []
    AuditLog.record(AuditLog.Action.VIEW, patient)
    return render(request, "clinic/patient_detail.html", {
        "patient": patient, "visits": visits, "images": images, "access": access,
        "can_delete_patient": can_delete(request.user, "patient"),
        "checkin_choices": [
            ("FOLLOW_UP", "Follow-up", "Continues the last visit; carries forward case details."),
            ("NEW", "New case", "New complaint; starts an empty case sheet."),
        ] if visits else [("NEW", "New case", "First visit for this patient.")],
        **_followup_card_context(request, patient),
        **_relatives_context(request, patient),
    })


def _relatives_context(request, patient):
    if not can_view(request.user, "relationships"):
        return {"relatives": None}
    return {
        "relatives": relationships.relatives_of(patient),
        "rejected_relatives": relationships.rejected_of(patient) if can_edit(request.user, "relationship_unreject") else [],
        "relationship_form": RelationshipForm(auto_id="rel_%s") if can_edit(request.user, "relationships") else None,
        "relationship_choices": relationships.CHOICES,
    }


@section_required("relationships", "edit")
@require_POST
def relationship_add(request, pk):
    patient = get_object_or_404(m.Patient, pk=pk)
    relative = m.Patient.objects.filter(pk=request.POST.get("relative_id")).first()
    form = RelationshipForm(request.POST)
    if relative is None:
        messages.error(request, "Choose the relative from the search results.")
    elif not form.is_valid():
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    else:
        try:
            relationships.link(patient, relative, form.cleaned_data["relationship"], request.user,
                               label=form.cleaned_data["label"])
        except services.WorkflowError as exc:
            messages.warning(request, str(exc))
        else:
            messages.success(request, f"{relative.full_name} linked.")
    return redirect(_next_url(request, f"{reverse('patient_detail', args=[patient.pk])}#family"))


@section_required("relationships", "edit")
@require_POST
def relationship_edit(request, pk):
    link_row = get_object_or_404(m.PatientRelationship, pk=pk)
    form = RelationshipForm(request.POST)
    if form.is_valid():
        relationships.update(link_row, form.cleaned_data["relationship"], request.user, label=form.cleaned_data["label"])
        messages.success(request, "Relationship updated on both patients.")
    else:
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    return redirect(_next_url(request, f"{reverse('patient_detail', args=[link_row.patient_id])}#family"))


@section_required("relationships", "edit")
@require_POST
def relationship_delete(request, pk):
    link_row = get_object_or_404(m.PatientRelationship, pk=pk)
    was_inferred = link_row.source == m.PatientRelationship.Source.INFERRED
    relationships.unlink(link_row, request.user)
    messages.success(request, "Automatic link removed; it will not be suggested again." if was_inferred
                     else "Relative unlinked from both patients.")
    return redirect(_next_url(request, f"{reverse('patient_detail', args=[link_row.patient_id])}#family"))


@section_required("relationships", "edit")
@require_POST
def relationship_confirm(request, pk):
    link_row = get_object_or_404(m.PatientRelationship, pk=pk)
    relationships.confirm(link_row, request.user)
    messages.success(request, f"{link_row.related_patient.full_name} confirmed as {link_row.display_label}.")
    return redirect(_next_url(request, f"{reverse('patient_detail', args=[link_row.patient_id])}#family"))


@section_required("relationship_unreject", "edit")
@require_POST
def relationship_unreject(request, pk):
    link_row = get_object_or_404(m.PatientRelationship, pk=pk)
    relationships.unreject(link_row, request.user)
    messages.success(request, "Rejection undone; the link may be inferred again.")
    return redirect(_next_url(request, f"{reverse('patient_detail', args=[link_row.patient_id])}#family"))


@section_required("family_consult", "edit")
@require_POST
def consult_through_family(request, pk):
    """Doctor: start a consultation for a relative who isn't present, reported by the current patient."""
    relative = get_object_or_404(m.Patient, pk=pk)
    reporter = get_object_or_404(m.Patient, pk=request.POST.get("reported_by"))
    try:
        visit = services.consult_through_family(relative, reporter, request.user)
    except services.WorkflowError as exc:
        messages.error(request, str(exc))
        return redirect(_next_url(request, reverse("patient_detail", args=[reporter.pk])))
    messages.success(request, f"Consultation for {relative.full_name} started ({services.reported_by_text(visit)}).")
    back = request.POST.get("back_visit", "")
    query = f"?from={reporter.pk}" + (f"&back_visit={back}" if back.isdigit() else "")
    return redirect(f"{reverse('visit_record', args=[visit.pk])}{query}")


@section_required("relationships")
def patient_lookup(request):
    """Identity-only JSON search used to pick relatives (registration and patient page)."""
    query = request.GET.get("q", "").strip()
    if len(query) < 3:
        return JsonResponse({"results": []})
    patients = search_patients(query)
    if request.GET.get("exclude", "").isdigit():
        patients = patients.exclude(pk=int(request.GET["exclude"]))
    show_phone = can_view(request.user, "patient")
    return JsonResponse({"results": [{
        "id": p.pk, "opd_number": p.opd_number, "full_name": p.full_name, "age": p.age,
        "gender": p.get_gender_display(), **({"phone": p.phone} if show_phone else {}),
    } for p in patients.order_by("full_name")[:15]]})


@section_required("patient", "edit")
@require_POST
def patient_delete(request, pk):
    if not can_delete(request.user, "patient"):
        raise PermissionDenied
    patient = get_object_or_404(m.Patient, pk=pk)
    patient.delete()
    messages.success(request, f"Patient {patient.opd_number} deleted.")
    return redirect("patient_list")


# ----------------------------------------------------------------------------- case record
def _visit_or_404(pk):
    return get_object_or_404(m.Visit.objects.select_related("patient", "doctor", "previous_visit"), pk=pk)


@login_required
def visit_record(request, pk):
    visit = _visit_or_404(pk)
    if not can_view(request.user, "case_record"):
        if can_view(request.user, "lab_worklist"):
            return redirect("lab_visit", pk=visit.pk)
        raise PermissionDenied
    record = CaseRecord(request, visit)
    if request.method == "POST":
        if record.is_valid():
            record.save()
            messages.success(request, "Case record saved." + (
                " Consultation marked complete." if visit.status == m.Visit.Status.COMPLETED else ""))
            for note in record.notes:
                messages.success(request, note)
            for warning in record.warnings:
                messages.warning(request, warning)
            return redirect("visit_record", pk=visit.pk)
        messages.error(request, "Please correct the highlighted fields.")
    else:
        AuditLog.record(AuditLog.Action.VIEW, visit)
        if visit.status == m.Visit.Status.WAITING and can_edit(request.user, "consultation"):
            services.set_visit_status(visit, m.Visit.Status.IN_CONSULTATION)

    access = record.access
    patient = visit.patient
    history = PatientHistory(patient, visit) if access["history"]["view"] else None
    if history is not None:
        previous_visits = [v for v in history.visits if v.pk != visit.pk]  # already prefetched
    elif access["visit_history"]["view"]:
        previous_visits = list(patient.visits.exclude(pk=visit.pk).select_related("doctor", "reported_by"))
    else:
        previous_visits = []
    images = patient.images.select_related("created_by") if access["patient_images"]["view"] else []
    patient_followups = (
        m.FollowUp.objects.filter(visit__patient=patient, visit__is_deleted=False).exclude(visit=visit)
        .select_related("visit") if access["followups"]["view"] else []
    )
    compare_visit = None
    if history is not None and visit.visit_type == m.Visit.Type.FOLLOW_UP:
        compare_id = visit.previous_visit_id or (history.last_visit.pk if history.last_visit else None)
        compare_visit = next((v for v in history.visits if v.pk == compare_id), None)
    current_rx = record.prescription if record.prescription.pk else None
    family = record.family_entries
    if family is None and access["family_history"]["view"]:
        from .family import family_history

        family = family_history(patient)
    back_patient = back_visit = None
    if request.GET.get("from", "").isdigit():
        back_patient = m.Patient.objects.filter(pk=request.GET["from"]).first()
    if request.GET.get("back_visit", "").isdigit():
        back_visit = m.Visit.objects.filter(pk=request.GET["back_visit"]).select_related("patient").first()
    return render(request, "clinic/visit_record.html", {
        "family": family, "family_groups": grouped(family) if family else [],
        "family_new_info": record.family_new_info, "family_autofilled": bool(record.family_autofill_text),
        "prefill_payload": record.prefill_payload(),
        "past_summary": history.past_summary() if history is not None else None,
        "reported_by_text": services.reported_by_text(visit),
        "relatives": relationships.relatives_of(patient) if access["relationships"]["view"] else None,
        "back_patient": back_patient, "back_visit": back_visit,
        "dispense_orders": list(visit.dispense_orders.select_related("sent_by").prefetch_related("items")
                                .order_by("-version")) if access["pharmacy_status"]["view"] else [],
        "visit": visit, "patient": patient, "record": record, "access": access,
        "forms": record.forms, "formsets": record.formsets,
        "physical_layout": PHYSICAL_LAYOUT, "mental_layout": MENTAL_LAYOUT,
        "image_stages": [("BEFORE", "Before Treatment", "before_images", "danger"),
                         ("AFTER", "After Treatment", "after_images", "success")],
        "open_sections": default_open(request.user) | record.error_sections,
        "history": history, "compare_visit": compare_visit,
        "previous_visits": previous_visits, "images": images, "patient_followups": patient_followups,
        "investigation": record.investigation if record.investigation.pk else None,
        "repertory": record.repertory if record.repertory.pk else None,
        "past_treatment": record.forms["past_treatment"].instance if record.forms["past_treatment"].instance.pk else None,
        "current_rx_has_items": bool(current_rx and current_rx.items.exists()),
        "can_edit_any": any(a["edit"] for a in access.values() if a is not access["followup_schedule"]),
        "can_delete_visit": can_delete(request.user, "visit"),
        "medicine_suggestions": m.PrescriptionItem.objects.values_list("medicine_name", flat=True).distinct()[:300]
        if access["prescription"]["edit"] else [],
        **_followup_card_context(request, patient, source_visit=visit),
    })


@section_required("visit_history")
def visit_summary(request, pk):
    """Read-only visit details for the previous-visit modal."""
    visit = _visit_or_404(pk)
    AuditLog.record(AuditLog.Action.VIEW, visit)
    return render(request, "clinic/_visit_summary.html", {
        "visit": visit, "access": section_access(request.user),
        "case_history": visit.section("case_history"),
        "physical": visit.section("physical_generals"),
        "mental": visit.section("mental_generals"),
        "followup_note": visit.section("followup_note"),
        "examination": visit.section("examination"),
        "diagnosis": visit.section("diagnosis"),
        "investigations": visit.investigations.prefetch_related("reports"),
        "prescriptions": visit.prescriptions.prefetch_related("items"),
        "followups": visit.followups.all(),
    })


@section_required("visit", "edit")
@require_POST
def visit_delete(request, pk):
    if not can_delete(request.user, "visit"):
        raise PermissionDenied
    visit = _visit_or_404(pk)
    visit.delete()
    messages.success(request, "Visit deleted.")
    return redirect("patient_detail", pk=visit.patient_id)


@section_required("prescription")
def prescription_print(request, pk):
    visit = _visit_or_404(pk)
    prescription = visit.prescriptions.prefetch_related("items").first()
    return render(request, "clinic/prescription_print.html", {
        "visit": visit, "patient": visit.patient, "prescription": prescription,
        "diagnosis": visit.section("diagnosis"), "examination": visit.section("examination"),
        "doctor": visit.doctor or request.user,
    })


@section_required("prescription", "edit")
@require_POST
def prescription_repeat(request, pk, rx_pk):
    """Copy an earlier prescription's medicines into this visit's prescription."""
    visit = _visit_or_404(pk)
    source = get_object_or_404(m.Prescription.objects.select_related("visit"), pk=rx_pk, visit__patient=visit.patient)
    try:
        target = services.repeat_prescription(source, visit, request.user, replace=request.POST.get("replace") == "1")
    except services.WorkflowError as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, f"Prescription of {source.visit.visit_date:%d-%b-%Y} copied. "
                                  "Review and save the case record.")
        order = pharmacy.sync_after_edit(target, request.user)
        if order is not None:
            messages.info(request, f"Updated prescription sent to the pharmacy (version {order.version}).")
    return redirect(f"{reverse('visit_record', args=[visit.pk])}#sec-prescription")


@section_required("history")
def patient_history(request, pk):
    """Full-page patient timeline (same content as the case-record History tab)."""
    patient = get_object_or_404(m.Patient, pk=pk)
    start, end = _optional_date(request, "start"), _optional_date(request, "end")
    history = PatientHistory(patient, start=start, end=end)
    AuditLog.record(AuditLog.Action.VIEW, patient, changes={"view": "history"})
    back_patient = None
    if request.GET.get("from", "").isdigit():
        back_patient = m.Patient.objects.filter(pk=request.GET["from"]).first()
    return render(request, "clinic/patient_history.html", {
        "patient": patient, "history": history, "start": start, "end": end, "back_patient": back_patient,
        "access": section_access(request.user),
    })


# ----------------------------------------------------------------------------- uploads
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
    return redirect(_next_url(request, reverse("home")))


@section_required("repertory", "edit")
@require_POST
def repertory_sheet_delete(request, pk):
    if not can_delete(request.user, "repertory"):
        raise PermissionDenied
    analysis = get_object_or_404(m.RepertoryAnalysis, pk=pk)
    analysis.sheet = ""
    analysis.save()
    messages.success(request, "Repertorization sheet removed.")
    return redirect("visit_record", pk=analysis.visit_id)


# ----------------------------------------------------------------------------- follow-up scheduling
def _followup_card_context(request, patient, source_visit=None):
    """Context for the "Next follow-up" card (patient page and case record)."""
    if not can_view(request.user, "followup_schedule"):
        return {"followup_card": False}
    show_notes = can_view(request.user, "followup_notes")
    active = services.active_followup(patient)
    base = source_visit.visit_date if source_visit else timezone.localdate()
    return {
        "followup_card": True,
        "active_followup": active,
        "followup_history": patient.scheduled_followups.exclude(status=m.ScheduledFollowUp.Status.SCHEDULED)
        .select_related("scheduled_by")[:5],
        "schedule_form": ScheduleFollowUpForm(show_notes=show_notes, prefix="sched")
        if can_edit(request.user, "followup_schedule") else None,
        "show_followup_notes": show_notes,
        "followup_source_visit": source_visit,
        "quick_picks": [(label, (base + timedelta(days=days)).isoformat())
                        for label, days in (("1 week", 7), ("2 weeks", 14), ("1 month", 30), ("3 months", 91))],
    }


@section_required("followup_schedule", "edit")
@require_POST
def followup_schedule(request, patient_pk):
    patient = get_object_or_404(m.Patient, pk=patient_pk)
    form = ScheduleFollowUpForm(request.POST, prefix="sched", show_notes=can_edit(request.user, "followup_notes"))
    default_next = reverse("patient_detail", args=[patient.pk])
    if not form.is_valid():
        messages.error(request, "Choose a valid follow-up date. " + " ".join(
            e for errors in form.errors.values() for e in errors))
        return redirect(_next_url(request, default_next))
    source_visit = None
    if request.POST.get("source_visit"):
        source_visit = m.Visit.objects.filter(pk=request.POST["source_visit"], patient=patient).first()
    try:
        followup = services.schedule_followup(
            patient, form.cleaned_data["scheduled_date"], request.user,
            scheduled_time=form.cleaned_data.get("scheduled_time"), notes=form.cleaned_data.get("notes", ""),
            source_visit=source_visit, replace=form.cleaned_data.get("replace", False),
        )
    except services.WorkflowError as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, f"Follow-up scheduled for {followup.scheduled_date:%d-%b-%Y}.")
    return redirect(_next_url(request, default_next))


@section_required("followup_schedule", "edit")
@require_POST
def followup_reschedule(request, pk):
    followup = get_object_or_404(m.ScheduledFollowUp, pk=pk)
    form = RescheduleFollowUpForm(request.POST)
    default_next = reverse("patient_detail", args=[followup.patient_id])
    if form.is_valid():
        try:
            services.reschedule_followup(followup, form.cleaned_data["scheduled_date"], request.user,
                                         scheduled_time=form.cleaned_data.get("scheduled_time"))
        except services.WorkflowError as exc:
            messages.warning(request, str(exc))
        else:
            messages.success(request, f"Follow-up moved to {followup.scheduled_date:%d-%b-%Y}.")
    else:
        messages.error(request, "Choose a valid date.")
    return redirect(_next_url(request, default_next))


@section_required("followup_schedule", "edit")
@require_POST
def followup_cancel(request, pk):
    followup = get_object_or_404(m.ScheduledFollowUp, pk=pk)
    try:
        services.cancel_followup(followup, request.user)
    except services.WorkflowError as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, "Follow-up cancelled.")
    return redirect(_next_url(request, reverse("patient_detail", args=[followup.patient_id])))


@section_required("checkin", "edit")
@require_POST
def followup_checkin(request, pk):
    """Check the patient in for this scheduled follow-up (starts the Follow-up flow)."""
    followup = get_object_or_404(m.ScheduledFollowUp.objects.select_related("patient"), pk=pk)
    return _do_checkin(request, followup.patient, m.Visit.Type.FOLLOW_UP)


def followup_buckets(queryset, today=None):
    today = today or timezone.localdate()
    scheduled = queryset.filter(status=m.ScheduledFollowUp.Status.SCHEDULED)
    return {
        "overdue": scheduled.filter(scheduled_date__lt=today),
        "today": scheduled.filter(scheduled_date=today),
        "week": scheduled.filter(scheduled_date__gt=today, scheduled_date__lte=today + timedelta(days=7)),
    }


@section_required("followup_schedule")
def followup_list(request):
    status = request.GET.get("status", m.ScheduledFollowUp.Status.SCHEDULED)
    followups = m.ScheduledFollowUp.objects.select_related("patient", "scheduled_by")
    if status in m.ScheduledFollowUp.Status.values:
        followups = followups.filter(status=status)
    start, end = _optional_date(request, "start"), _optional_date(request, "end")
    if start:
        followups = followups.filter(scheduled_date__gte=start)
    if end:
        followups = followups.filter(scheduled_date__lte=end)
    page = Paginator(followups.order_by("scheduled_date", "scheduled_time", "id"), 30).get_page(request.GET.get("page"))
    return render(request, "clinic/followup_list.html", {
        "page": page, "status": status, "statuses": m.ScheduledFollowUp.Status.choices, "start": start, "end": end,
        "show_followup_notes": can_view(request.user, "followup_notes"),
        "can_manage": can_edit(request.user, "followup_schedule"),
        "can_checkin": can_edit(request.user, "checkin"),
    })


# ----------------------------------------------------------------------------- lab
@section_required("lab_worklist")
def lab_dashboard(request):
    """Lab home: counts, pending reports (upload in place) and recent uploads."""
    query = request.GET.get("q", "").strip()
    test_type = request.GET.get("test", "")
    data = labsvc.dashboard(request.user, query, test_type)
    return render(request, "clinic/lab_dashboard.html", {
        **data, "q": query, "test": test_type, "test_types": labsvc.TEST_TYPES,
        "can_upload": can_edit(request.user, "investigation_reports"),
    })


# Phase 1 name: the worklist is now the dashboard.
lab_worklist = lab_dashboard


def _upload_errors(files):
    errors = [] if files else ["Choose at least one file to upload."]
    for upload in files:
        problems = run_validators(upload, document_validators(20))
        if problems:
            errors.append(f"{upload.name}: {' '.join(problems)}")
    return errors


@section_required("investigation_reports", "edit")
@require_POST
def lab_upload(request, pk):
    """Upload from the dashboard modal (AJAX or plain POST)."""
    investigation = get_object_or_404(m.Investigation.objects.select_related("visit__patient"), pk=pk)
    files = request.FILES.getlist("report_files")
    errors = _upload_errors(files)
    wants_json = request.headers.get("x-requested-with") == "fetch"
    if errors:
        if wants_json:
            return JsonResponse({"ok": False, "errors": errors}, status=400)
        for error in errors:
            messages.error(request, error)
        return redirect(_next_url(request, reverse("lab_dashboard")))
    labsvc.upload_reports(investigation, files, request.user, remarks=request.POST.get("remarks", "").strip())
    if wants_json:
        return JsonResponse({"ok": True, "counts": labsvc.dashboard(request.user)["counts"]})
    messages.success(request, f"Report uploaded for {investigation.visit.patient.full_name}.")
    return redirect(_next_url(request, reverse("lab_dashboard")))


@section_required("lab_reports")
def lab_reports(request):
    """Previous (completed) reports with filters. Investigation data only."""
    reports = labsvc.visible_reports(request.user).select_related(
        "investigation__visit__patient", "investigation__created_by", "created_by")
    filters = {key: request.GET.get(key, "").strip() for key in ("q", "test", "start", "end", "uploaded_by")}
    if filters["q"]:
        reports = reports.filter(Q(investigation__visit__patient__opd_number__icontains=filters["q"])
                                 | Q(investigation__visit__patient__full_name__icontains=filters["q"]))
    if filters["test"]:
        reports = reports.filter(labsvc.test_filter(filters["test"], prefix="investigation__"))
    if _optional_date(request, "start"):
        reports = reports.filter(created_at__date__gte=_optional_date(request, "start"))
    if _optional_date(request, "end"):
        reports = reports.filter(created_at__date__lte=_optional_date(request, "end"))
    if filters["uploaded_by"] == "me":
        reports = reports.filter(created_by=request.user)
    page = Paginator(reports.order_by("-created_at", "-id"), 30).get_page(request.GET.get("page"))
    return render(request, "clinic/lab_reports.html", {
        "page": page, "filters": filters, "test_types": labsvc.TEST_TYPES,
        "can_correct": can_edit(request.user, "lab_reports"),
    })


@section_required("lab_reports")
def lab_patient_reports(request, pk):
    """Per-patient report history for comparing results over time (no case-sheet data)."""
    patient = get_object_or_404(m.Patient, pk=pk)
    investigations = (m.Investigation.objects.filter(visit__patient=patient, visit__is_deleted=False)
                      .select_related("visit", "created_by")
                      .prefetch_related(labsvc.reports_prefetch(request.user)).order_by("-requested_date", "-id"))
    return render(request, "clinic/lab_patient_reports.html", {
        "patient": patient, "investigations": investigations,
        "can_correct": can_edit(request.user, "lab_reports"),
    })


@section_required("lab_reports", "edit")
@require_POST
def report_correct(request, pk):
    """Upload a corrected version of a report; the old one is kept as superseded."""
    report = get_object_or_404(m.InvestigationReport.objects.select_related("investigation__visit__patient"), pk=pk)
    if not can_view_report(request.user, report) or report.status != m.InvestigationReport.Version.CURRENT:
        raise PermissionDenied
    files = request.FILES.getlist("report_files")[:1]
    errors = _upload_errors(files)
    if errors:
        for error in errors:
            messages.error(request, error)
    else:
        labsvc.correct_report(report, files[0], request.user, remarks=request.POST.get("remarks", "").strip())
        messages.success(request, "Corrected report uploaded; the previous version is kept as superseded.")
    return redirect(_next_url(request, reverse("lab_reports")))


@section_required("lab_worklist")
def lab_visit(request, pk):
    """Lab-only view of a visit: patient identity, requested tests, lab uploads."""
    visit = get_object_or_404(m.Visit.objects.select_related("patient"), pk=pk)
    investigations = list(visit.investigations.select_related("created_by").prefetch_related(
        labsvc.reports_prefetch(request.user)))
    upload_errors = []
    if request.method == "POST":
        if not can_edit(request.user, "investigation_reports"):
            raise PermissionDenied
        investigation = next((i for i in investigations if str(i.pk) == request.POST.get("investigation")), None)
        files = request.FILES.getlist("report_files")
        if investigation is None:
            upload_errors.append("Choose the investigation this report belongs to.")
        if not files:
            upload_errors.append("Choose at least one file to upload.")
        for upload in files:
            errors = run_validators(upload, document_validators(20))
            if errors:
                upload_errors.append(f"{upload.name}: {' '.join(errors)}")
        if not upload_errors:
            labsvc.upload_reports(investigation, files, request.user, remarks=request.POST.get("remarks", "").strip())
            messages.success(request, f"{len(files)} report(s) uploaded; investigation marked completed.")
            return redirect("lab_visit", pk=visit.pk)
    else:
        AuditLog.record(AuditLog.Action.VIEW, visit, changes={"view": "lab"})
    return render(request, "clinic/lab_visit.html", {
        "visit": visit, "patient": visit.patient, "investigations": investigations, "upload_errors": upload_errors,
        "can_upload": can_edit(request.user, "investigation_reports"),
    })


# ----------------------------------------------------------------------------- reports
def _optional_date(request, name):
    try:
        return timezone.datetime.strptime(request.GET.get(name, ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_range(request):
    today = timezone.localdate()
    return _optional_date(request, "start") or today.replace(day=1), _optional_date(request, "end") or today


@section_required("reports")
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


# ----------------------------------------------------------------------------- dashboard
def dashboard_context(user):
    """Widgets for the home page, limited to what the role may see."""
    access = section_access(user)
    today = timezone.localdate()
    pending = m.Investigation.objects.filter(status=m.Investigation.Status.REQUESTED, visit__is_deleted=False)
    ctx = {
        "pending_investigations": pending.select_related("visit__patient").order_by("requested_date")[:10],
        "stats": {"pending_labs": pending.count()},
    }
    if access["case_record"]["view"]:
        today_visits = m.Visit.objects.filter(visit_date=today)
        listed = today_visits.select_related("patient", "doctor", "reported_by").order_by(STATUS_ORDER, "created_at")
        if access["pharmacy_status"]["view"]:
            listed = listed.prefetch_related(Prefetch(
                "dispense_orders", queryset=m.DispenseOrder.objects.order_by("-version"), to_attr="orders"))
        ctx["today_visits"] = listed[:25]
        ctx["stats"]["patients"] = m.Patient.objects.count()
        ctx["stats"]["today"] = today_visits.count()
        ctx["stats"]["waiting"] = today_visits.filter(status=m.Visit.Status.WAITING).count()
    if access["followup_schedule"]["view"]:
        buckets = followup_buckets(m.ScheduledFollowUp.objects.select_related("patient"), today)
        ctx["followup_buckets"] = [
            ("overdue", "Overdue", "danger", buckets["overdue"][:10]),
            ("today", "Today", "primary", buckets["today"][:10]),
            ("week", "Next 7 days", "success", buckets["week"][:10]),
        ]
        ctx["stats"]["followups"] = buckets["today"].count() + buckets["week"].count()
        ctx["stats"]["overdue_followups"] = buckets["overdue"].count()
    if access["pharmacy_items"]["view"]:
        stages = pharmacy.queue(today)
        ctx["pharmacy_stages"] = [
            ("new", "New", "danger", stages["new"]), ("preparing", "Preparing", "warning", stages["preparing"]),
            ("ready", "Ready", "success", stages["ready"]),
        ]
        ctx["stats"]["pharmacy_pending"] = len(stages["new"]) + len(stages["preparing"])
    if access["prescription"]["view"]:
        ctx["recent_prescriptions"] = (
            m.Prescription.objects.filter(visit__is_deleted=False).select_related("visit__patient", "created_by")
            .prefetch_related("items").order_by("-created_at")[:8]
        )
    return ctx



# ----------------------------------------------------------------------------- pharmacy
@section_required("pharmacy_items")
def pharmacy_queue(request):
    stages = pharmacy.queue()
    return render(request, "clinic/pharmacy_queue.html", {
        "stages": [
            ("new", "New", "danger", "bi-bell", stages["new"]),
            ("preparing", "Preparing", "warning", "bi-hourglass-split", stages["preparing"]),
            ("ready", "Ready", "success", "bi-bag-check", stages["ready"]),
            ("dispensed", "Dispensed today", "secondary", "bi-check2-all", stages["dispensed"]),
        ],
        "can_update": can_edit(request.user, "pharmacy_items"),
    })


@section_required("pharmacy_items", "edit")
@require_POST
def pharmacy_action(request, pk, action):
    order = get_object_or_404(m.DispenseOrder.objects.select_related("patient"), pk=pk)
    try:
        pharmacy.change_status(order, action, request.user)
    except services.WorkflowError as exc:
        messages.warning(request, str(exc))
    else:
        messages.success(request, f"{order.patient.full_name}: {order.get_status_display()}.")
    return redirect(_next_url(request, f"{reverse('pharmacy_queue')}#order-{order.pk}"))


@section_required("pharmacy_items")
def pharmacy_label(request, pk):
    order = get_object_or_404(m.DispenseOrder.objects.select_related("patient", "visit__doctor")
                              .prefetch_related("items"), pk=pk)
    AuditLog.record(AuditLog.Action.LABEL_PRINTED, order, changes={"size": request.GET.get("size", "a4")})
    return render(request, "clinic/pharmacy_label.html", {
        "order": order, "patient": order.patient, "size": "label" if request.GET.get("size") == "label" else "a4",
    })


@section_required("notifications")
def notification_open(request, pk):
    """Mark a notification read and go to what it points at."""
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    inbox.mark_read(notification)
    target = notification.url
    if not (target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure())):
        target = reverse("home")
    return redirect(target)
