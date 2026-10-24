"""Workflow operations shared by the HTML views and the REST API.

Each function performs one audited business action (check-in, scheduling,
repeating a prescription). Access control stays with the caller via
accounts.rbac; these functions assume the caller is allowed.
"""
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import Role
from core.models import AuditLog

from . import models as m

# Sections copied into a follow-up visit so the doctor only edits what changed.
CARRY_FORWARD = {
    "case_history": ["presenting_complaints", "past_history", "family_history"],
    "physical_generals": [f.name for f in m.PhysicalGeneral._meta.concrete_fields
                          if f.editable and f.name not in {"id", "visit"}],
    "mental_generals": [f.name for f in m.MentalGeneral._meta.concrete_fields
                        if f.editable and f.name not in {"id", "visit"}],
}


class WorkflowError(Exception):
    """A business rule refused the action; the message is safe to show users."""


@dataclass
class CheckInResult:
    visit: m.Visit
    carried_from: m.Visit | None = None
    completed_followup: m.ScheduledFollowUp | None = None
    downgraded_to_new: bool = False


def active_followup(patient):
    return patient.scheduled_followups.filter(status=m.ScheduledFollowUp.Status.SCHEDULED).first()


# --------------------------------------------------------------------------- check-in
@transaction.atomic
def check_in(patient, visit_type, user, *, doctor=None, visit_date=None):
    """Create today's visit as a New case or Follow-up (status WAITING).

    New case: empty case sheet, no carry-forward (history stays visible to the doctor).
    Follow-up: linked to the previous visit, case history and generals carried
    forward, and any SCHEDULED follow-up for the patient is marked COMPLETED.
    """
    previous = patient.visits.order_by("-visit_date", "-id").first()
    downgraded = False
    if visit_type == m.Visit.Type.FOLLOW_UP and previous is None:
        visit_type, downgraded = m.Visit.Type.NEW, True
    if doctor is None and user is not None and user.effective_role == Role.DOCTOR:
        doctor = user

    visit = m.Visit.objects.create(
        patient=patient,
        visit_date=visit_date or timezone.localdate(),
        visit_type=visit_type,
        status=m.Visit.Status.WAITING,
        previous_visit=previous if visit_type == m.Visit.Type.FOLLOW_UP else None,
        doctor=doctor or (previous.doctor if previous and visit_type == m.Visit.Type.FOLLOW_UP else None),
    )
    result = CheckInResult(visit=visit, downgraded_to_new=downgraded)

    if visit_type == m.Visit.Type.FOLLOW_UP:
        for section, fields in CARRY_FORWARD.items():
            source = previous.section(section)
            if source is not None:
                type(source).objects.create(visit=visit, **{name: getattr(source, name) for name in fields})
        result.carried_from = previous
        followup = active_followup(patient)
        if followup is not None:
            _set_followup_status(followup, m.ScheduledFollowUp.Status.COMPLETED, AuditLog.Action.FOLLOWUP_COMPLETED,
                                 checkin_visit=visit)
            result.completed_followup = followup

    AuditLog.record(AuditLog.Action.CHECK_IN, visit, user=user, changes={
        "visit_type": visit.visit_type,
        "previous_visit": previous.pk if visit.previous_visit_id else None,
        "completed_followup": result.completed_followup.pk if result.completed_followup else None,
    })
    return result


def record_patient_without_case_sheet(visit, user):
    AuditLog.record(AuditLog.Action.PATIENT_SAVED_NO_CASE_SHEET, visit.patient, user=user,
                    changes={"visit": visit.pk, "visit_type": visit.visit_type})


def record_visit_type_change(visit, old_type, user):
    AuditLog.record(AuditLog.Action.VISIT_TYPE_CHANGED, visit, user=user,
                    changes={"visit_type": {"old": old_type, "new": visit.visit_type}})


def set_visit_status(visit, status):
    if visit.status != status:
        visit.status = status
        visit.save(update_fields=["status"])


# --------------------------------------------------------------------------- follow-ups
def _set_followup_status(followup, status, action, **extra):
    followup.status = status
    followup.status_changed_at = timezone.now()
    for name, value in extra.items():
        setattr(followup, name, value)
    followup.save()
    AuditLog.record(action, followup, changes={"status": status, **{k: str(v.pk if hasattr(v, "pk") else v)
                                                                       for k, v in extra.items()}})


@transaction.atomic
def schedule_followup(patient, scheduled_date, user, *, scheduled_time=None, notes="", source_visit=None,
                      replace=False):
    """Schedule the patient's next follow-up. Only one may be SCHEDULED at a time."""
    if scheduled_date < timezone.localdate():
        raise WorkflowError("The follow-up date cannot be in the past.")
    existing = active_followup(patient)
    if existing is not None:
        if not replace:
            raise WorkflowError(
                f"{patient.full_name} already has a follow-up on {existing.scheduled_date:%d-%b-%Y}. "
                "Confirm to replace it."
            )
        _set_followup_status(existing, m.ScheduledFollowUp.Status.CANCELLED, AuditLog.Action.FOLLOWUP_CANCELLED)
    followup = m.ScheduledFollowUp.objects.create(
        patient=patient, source_visit=source_visit, scheduled_date=scheduled_date, scheduled_time=scheduled_time,
        notes=notes or "", scheduled_by=user,
    )
    AuditLog.record(AuditLog.Action.FOLLOWUP_SCHEDULED, followup, user=user, changes={
        "date": str(scheduled_date), "time": str(scheduled_time or ""),
        "replaced": existing.pk if existing is not None else None,
    })
    return followup


def reschedule_followup(followup, scheduled_date, user, *, scheduled_time=None):
    if followup.status != m.ScheduledFollowUp.Status.SCHEDULED:
        raise WorkflowError("Only scheduled follow-ups can be rescheduled.")
    if scheduled_date < timezone.localdate():
        raise WorkflowError("The follow-up date cannot be in the past.")
    old = (followup.scheduled_date, followup.scheduled_time)
    followup.scheduled_date = scheduled_date
    followup.scheduled_time = scheduled_time
    followup.save()
    AuditLog.record(AuditLog.Action.FOLLOWUP_RESCHEDULED, followup, user=user, changes={
        "date": {"old": str(old[0]), "new": str(scheduled_date)},
        "time": {"old": str(old[1] or ""), "new": str(scheduled_time or "")},
    })
    return followup


def cancel_followup(followup, user):
    if followup.status != m.ScheduledFollowUp.Status.SCHEDULED:
        raise WorkflowError("Only scheduled follow-ups can be cancelled.")
    _set_followup_status(followup, m.ScheduledFollowUp.Status.CANCELLED, AuditLog.Action.FOLLOWUP_CANCELLED)
    return followup


def mark_missed_followups(today=None):
    """Mark SCHEDULED follow-ups dated before today as MISSED. Returns how many."""
    today = today or timezone.localdate()
    overdue = list(m.ScheduledFollowUp.objects.filter(status=m.ScheduledFollowUp.Status.SCHEDULED,
                                                      scheduled_date__lt=today))
    for followup in overdue:
        _set_followup_status(followup, m.ScheduledFollowUp.Status.MISSED, AuditLog.Action.FOLLOWUP_MISSED)
    return len(overdue)


# --------------------------------------------------------------------------- prescriptions
@transaction.atomic
def repeat_prescription(source, target_visit, user, *, replace=False):
    """Copy `source` prescription's items into the target visit's prescription for editing."""
    if source.visit_id == target_visit.pk:
        raise WorkflowError("That prescription already belongs to this visit.")
    target = target_visit.prescriptions.first() or m.Prescription.objects.create(visit=target_visit)
    existing = list(target.items.all())
    if existing and not replace:
        raise WorkflowError("This visit already has medicines. Confirm to replace them.")
    for item in existing:
        item.delete()
    copied = []
    for position, item in enumerate(source.items.all()):
        copied.append(m.PrescriptionItem.objects.create(
            prescription=target, medicine_name=item.medicine_name, potency=item.potency, form=item.form,
            quantity=item.quantity, dose=item.dose, duration=item.duration, instructions=item.instructions,
            order=position,
        ))
    if not target.dietary_advice and source.dietary_advice:
        target.dietary_advice = source.dietary_advice
        target.save(update_fields=["dietary_advice"])
    AuditLog.record(AuditLog.Action.PRESCRIPTION_REPEATED, target, user=user, changes={
        "from_prescription": source.pk, "from_visit": source.visit_id, "item_count": len(copied),
        "replaced_items": len(existing),
    })
    return target


# --------------------------------------------------------------------------- consulting through family
def family_link(relative, reporter):
    """The active link saying how `reporter` is related to `relative` (None if they aren't linked)."""
    return (m.PatientRelationship.objects.filter(patient=relative, related_patient=reporter, status="ACTIVE")
            .select_related("related_patient").first())


@transaction.atomic
def consult_through_family(relative, reported_by, user):
    """Start a visit for `relative`, who is not present, reported by family member `reported_by`.

    The visit belongs only to the relative. Requires an active (manual or inferred) relationship.
    """
    if relative.pk == reported_by.pk:
        raise WorkflowError("Choose a family member other than the current patient.")
    link = family_link(relative, reported_by)
    if link is None:
        raise WorkflowError(f"{reported_by.full_name} is not linked to {relative.full_name} as family.")
    previous = relative.visits.order_by("-visit_date", "-id").first()
    visit = m.Visit.objects.create(
        patient=relative,
        visit_type=m.Visit.Type.FOLLOW_UP if previous else m.Visit.Type.NEW,
        previous_visit=previous,
        status=m.Visit.Status.IN_CONSULTATION,
        consultation_mode=m.Visit.Mode.THROUGH_FAMILY,
        reported_by=reported_by,
        doctor=user if user is not None and user.effective_role == Role.DOCTOR else (previous.doctor if previous else None),
    )
    AuditLog.record(AuditLog.Action.VISIT_THROUGH_FAMILY, visit, user=user, changes={
        "relative": relative.pk, "reported_by": reported_by.pk, "relationship": link.display_label,
        "visit_type": visit.visit_type,
    })
    return visit


def reported_by_text(visit):
    """'Reported by son, Ravi' for through-family visits ('' otherwise)."""
    if visit.consultation_mode != m.Visit.Mode.THROUGH_FAMILY or visit.reported_by_id is None:
        return ""
    link = family_link(visit.patient, visit.reported_by)
    relation = link.display_label.lower() if link else "family member"
    return f"Reported by {relation}, {visit.reported_by.full_name}"
