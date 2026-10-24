"""Everything the doctor needs about a patient's past, loaded in a fixed number of queries.

Used by the case-record summary bar, the "Previous prescriptions" panel, the
timeline (HTML and /api/patients/<id>/history/). Visibility is decided by the
caller through accounts.rbac ("history" section).
"""
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch

from . import models as m


def history_visits(patient):
    """All live visits of the patient, newest first, with every related row prefetched."""
    return (
        patient.visits.select_related("doctor", "case_history", "diagnosis", "followup_note", "reported_by")
        .prefetch_related(
            Prefetch("prescriptions", queryset=m.Prescription.objects.select_related("created_by").prefetch_related(
                Prefetch("items", queryset=m.PrescriptionItem.objects.order_by("order", "id")))),
            Prefetch("investigations", queryset=m.Investigation.objects.select_related("created_by").prefetch_related(
                Prefetch("reports", queryset=m.InvestigationReport.objects.select_related("created_by")))),
            Prefetch("followups", queryset=m.FollowUp.objects.order_by("visit_date", "id")),
            Prefetch("images", queryset=m.PatientImage.objects.select_related("created_by")),
        )
        .order_by("-visit_date", "-id")
    )


def _section(visit, name):
    try:
        return getattr(visit, name)
    except ObjectDoesNotExist:
        return None


def diagnosis_text(visit):
    diagnosis = _section(visit, "diagnosis")
    if diagnosis is None:
        return ""
    return diagnosis.final or diagnosis.provisional or diagnosis.differential


def chief_complaint(visit):
    case = _section(visit, "case_history")
    return case.presenting_complaints if case is not None else ""


def prescriptions_with_items(visit):
    return [rx for rx in visit.prescriptions.all() if rx.items.all()]


def remedy_text(visit):
    return ", ".join(
        f"{item.medicine_name} {item.potency}".strip()
        for rx in prescriptions_with_items(visit) for item in rx.items.all()
    )


class PatientHistory:
    def __init__(self, patient, current_visit=None, *, start=None, end=None):
        self.patient = patient
        self.current = current_visit
        self.visits = list(history_visits(patient))  # newest first
        key = (lambda v: (v.visit_date, v.pk))
        if current_visit is not None:
            current_key = key(current_visit)
            self.previous = [v for v in self.visits if key(v) < current_key]
        else:
            self.previous = list(self.visits)
        self.timeline = [
            v for v in self.visits
            if (start is None or v.visit_date >= start) and (end is None or v.visit_date <= end)
        ]
        self.next_followup = patient.scheduled_followups.filter(status=m.ScheduledFollowUp.Status.SCHEDULED).first()

    # ------------------------------------------------------------- summary bar
    @property
    def visit_number(self):
        if self.current is None:
            return len(self.visits)
        ascending = list(reversed(self.visits))
        return next((i for i, v in enumerate(ascending, start=1) if v.pk == self.current.pk), len(ascending))

    @property
    def first_visit_date(self):
        return self.visits[-1].visit_date if self.visits else None

    @property
    def last_visit(self):
        return self.previous[0] if self.previous else None

    @property
    def last_diagnosis(self):
        return next((text for text in map(diagnosis_text, self.previous) if text), "")

    @property
    def last_remedy(self):
        return next((text for text in map(remedy_text, self.previous) if text), "")

    def past_summary(self):
        """Built live from earlier visits: diagnoses (merged), remedies with potency and dates, investigations."""
        diagnoses = {}
        for visit in self.previous:  # newest first
            text = diagnosis_text(visit).strip()
            if text:
                entry = diagnoses.setdefault(text.lower(), {"text": text, "dates": []})
                entry["dates"].append(visit.visit_date)
        remedies = {}
        for visit in self.previous:
            for rx in prescriptions_with_items(visit):
                for item in rx.items.all():
                    name = f"{item.medicine_name} {item.potency}".strip()
                    entry = remedies.setdefault(name.lower(), {"text": name, "dates": []})
                    if visit.visit_date not in entry["dates"]:
                        entry["dates"].append(visit.visit_date)
        investigations = [(visit, inv) for visit in self.previous for inv in visit.investigations.all()]
        return {
            "diagnoses": list(diagnoses.values()),
            "remedies": list(remedies.values()),
            "investigations": investigations,
        }

    @property
    def previous_prescriptions(self):
        """[(visit, prescription)] for earlier visits that prescribed something, newest first."""
        return [(visit, rx) for visit in self.previous for rx in prescriptions_with_items(visit)]

    def as_dict(self, report_filter=None):
        """Serialisable history for the API. `report_filter(report) -> bool` limits reports."""
        def person(user):
            return str(user) if user else None

        def visit_dict(v):
            return {
                "id": v.pk,
                "visit_date": v.visit_date,
                "visit_type": v.visit_type,
                "status": v.status,
                "consultation_mode": v.consultation_mode,
                "reported_by": person(v.reported_by),
                "doctor": person(v.doctor),
                "chief_complaint": chief_complaint(v),
                "diagnosis": diagnosis_text(v),
                "prescriptions": [{
                    "id": rx.pk,
                    "prescribed_on": rx.prescribed_on,
                    "prescribed_by": person(v.doctor or rx.created_by),
                    "dietary_advice": rx.dietary_advice,
                    "followup_advice": rx.followup_advice,
                    "items": [{"medicine_name": i.medicine_name, "potency": i.potency, "duration": i.duration,
                               "dose": i.instructions} for i in rx.items.all()],
                } for rx in v.prescriptions.all()],
                "investigations": [{
                    "id": inv.pk, "request_notes": inv.request_notes, "requested_date": inv.requested_date,
                    "status": inv.status, "requested_by": person(inv.created_by),
                    "reports": [{"id": r.pk, "url": r.file.url, "source": r.source, "title": r.title or r.filename}
                                for r in inv.reports.all() if report_filter is None or report_filter(r)],
                } for inv in v.investigations.all()],
                "followup_notes": [{"date": fu.visit_date, "progress": fu.progress_notes,
                                    "medicines_given": fu.medicines_given,
                                    "next_followup_date": fu.next_followup_date} for fu in v.followups.all()],
                "images": [{"id": img.pk, "stage": img.stage, "url": img.image.url, "uploaded": img.created_at}
                           for img in v.images.all()],
            }

        return {
            "patient": {"id": self.patient.pk, "opd_number": self.patient.opd_number,
                        "full_name": self.patient.full_name, "age": self.patient.age, "gender": self.patient.gender},
            "summary": {
                "total_visits": len(self.visits),
                "first_visit_date": self.first_visit_date,
                "last_visit_date": self.last_visit.visit_date if self.last_visit else None,
                "last_diagnosis": self.last_diagnosis,
                "last_remedy": self.last_remedy,
                "next_followup": {
                    "id": self.next_followup.pk, "date": self.next_followup.scheduled_date,
                    "time": self.next_followup.scheduled_time,
                } if self.next_followup else None,
            },
            "visits": [visit_dict(v) for v in self.timeline],
        }
