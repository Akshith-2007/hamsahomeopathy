"""Laboratory workflow: uploading and correcting reports, the lab dashboard and report history.

Lab users only ever see investigation data (test, requester, dates, files),
plus the patient identity fields (OPD No., name, age, sex).
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from accounts.rbac import can_edit, hides_superseded_reports, visible_report_sources
from core import inbox
from core.middleware import acting_as
from core.models import AuditLog, Notification

from . import models as m

# Quick filter for the dashboard and report history (matched against the request text).
TEST_TYPES = [
    ("cbc", "CBC / Haemogram", ["cbc", "haemogram", "hemogram", "blood count"]),
    ("thyroid", "Thyroid", ["thyroid", "tsh", "t3", "t4"]),
    ("sugar", "Blood sugar / HbA1c", ["sugar", "glucose", "hba1c", "fbs", "ppbs"]),
    ("lipid", "Lipid profile", ["lipid", "cholesterol"]),
    ("liver", "Liver (LFT)", ["lft", "liver"]),
    ("kidney", "Kidney (KFT/RFT)", ["kft", "rft", "kidney", "creatinine", "urea"]),
    ("urine", "Urine", ["urine"]),
    ("xray", "X-ray", ["x-ray", "xray"]),
    ("usg", "Ultrasound (USG)", ["usg", "ultrasound"]),
    ("other", "Other", []),
]
TEST_KEYWORDS = {key: words for key, _label, words in TEST_TYPES}


def test_filter(test_type, prefix=""):
    """Q() selecting investigations of a quick-filter test type (by keywords in the request text)."""
    field = f"{prefix}request_notes__icontains"
    words = TEST_KEYWORDS.get(test_type)
    if words is None:
        return Q()
    if test_type == "other":
        q = Q()
        for others in TEST_KEYWORDS.values():
            for word in others:
                q &= ~Q(**{field: word})
        return q
    q = Q()
    for word in words:
        q |= Q(**{field: word})
    return q


def requesting_doctor(investigation):
    """The doctor to notify: the visit's doctor, else whoever requested the test if they can prescribe."""
    visit = investigation.visit
    if visit.doctor_id:
        return visit.doctor
    requester = investigation.created_by
    return requester if requester is not None and can_edit(requester, "prescription") else None


def _notify_doctor(investigation, kind, title):
    doctor = requesting_doctor(investigation)
    if doctor is None or not doctor.is_active:
        return
    patient = investigation.visit.patient
    test = (investigation.request_notes or "investigation").strip().splitlines()[0][:60]
    inbox.notify([doctor], f"{title}: {patient.full_name} – {test}", kind=kind, target=investigation,
                 message=f"{patient.opd_number} · {investigation.requested_date:%d-%b-%Y}",
                 url=f"{reverse('visit_record', args=[investigation.visit_id])}#sec-investigations")


@transaction.atomic
def upload_reports(investigation, files, user, *, remarks="", source=m.InvestigationReport.Source.LAB):
    """Store lab reports, mark the investigation completed and tell the requesting doctor."""
    with acting_as(user):  # attribute to `user` even outside a request (API, scripts)
        reports = [m.InvestigationReport.objects.create(investigation=investigation, file=upload, source=source,
                                                        remarks=remarks) for upload in files]
    if source == m.InvestigationReport.Source.LAB:
        if investigation.status != m.Investigation.Status.COMPLETED:
            investigation.status = m.Investigation.Status.COMPLETED
            investigation.completed_at = timezone.now()
            investigation.save(update_fields=["status", "completed_at"])
        AuditLog.record(AuditLog.Action.REPORT_UPLOADED, investigation, user=user,
                        changes={"reports": [r.pk for r in reports], "remarks": remarks})
        _notify_doctor(investigation, Notification.Kind.REPORT_READY, "Report ready")
    return reports


@transaction.atomic
def correct_report(report, upload, user, *, remarks=""):
    """Upload a corrected version; the old report is kept as SUPERSEDED (visible to doctors/admins)."""
    with acting_as(user):
        corrected = m.InvestigationReport.objects.create(
            investigation=report.investigation, file=upload, source=report.source, title=report.title,
            remarks=remarks, version=report.version + 1, supersedes=report,
        )
    report.status = m.InvestigationReport.Version.SUPERSEDED
    report.save(update_fields=["status"])
    AuditLog.record(AuditLog.Action.REPORT_CORRECTED, corrected, user=user, changes={
        "supersedes": report.pk, "version": corrected.version, "remarks": remarks})
    _notify_doctor(report.investigation, Notification.Kind.REPORT_CORRECTED, "Corrected report")
    return corrected


def visible_reports(user, queryset=None):
    """Reports the user may list (lab: current, lab-produced only)."""
    queryset = queryset if queryset is not None else m.InvestigationReport.objects.all()
    sources = visible_report_sources(user)
    if sources is not None:
        queryset = queryset.filter(source__in=sources)
    if hides_superseded_reports(user):
        queryset = queryset.filter(status=m.InvestigationReport.Version.CURRENT)
    return queryset


def reports_prefetch(user, to_attr="visible_reports"):
    return Prefetch("reports", queryset=visible_reports(user).select_related("created_by"), to_attr=to_attr)


def pending(user, query="", test_type=""):
    investigations = (m.Investigation.objects.filter(status=m.Investigation.Status.REQUESTED, visit__is_deleted=False)
                      .select_related("visit__patient", "created_by").order_by("created_at", "id"))
    if query:
        investigations = investigations.filter(
            Q(visit__patient__opd_number__icontains=query) | Q(visit__patient__full_name__icontains=query)
            | Q(request_notes__icontains=query))
    if test_type:
        investigations = investigations.filter(test_filter(test_type))
    return investigations


def overdue_before():
    return timezone.now() - timedelta(days=settings.LAB_OVERDUE_DAYS)


def dashboard(user, query="", test_type=""):
    """Counts, the pending table (overdue flagged) and recent uploads for the lab dashboard."""
    now = timezone.now()
    cutoff = overdue_before()
    rows = list(pending(user, query, test_type))
    for inv in rows:
        inv.is_overdue = inv.created_at < cutoff
        inv.pending_for = now - inv.created_at
    all_pending = pending(user)
    today = timezone.localdate()
    recent = (visible_reports(user).filter(source=m.InvestigationReport.Source.LAB)
              .select_related("investigation__visit__patient", "created_by").order_by("-created_at")[:10])
    return {
        "counts": {
            "pending": all_pending.count(),
            "overdue": all_pending.filter(created_at__lt=cutoff).count(),
            "completed_today": m.Investigation.objects.filter(status=m.Investigation.Status.COMPLETED,
                                                              completed_at__date=today).count(),
        },
        "pending": rows,
        "recent": recent,
        "overdue_days": settings.LAB_OVERDUE_DAYS,
    }
