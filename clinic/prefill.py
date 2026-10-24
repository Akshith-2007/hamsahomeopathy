"""Pre-fill a visit's stable history from the patient's own earlier visits.

HISTORY_FIELDS is the single place that decides what counts as *stable history*
(copied forward from the most recent earlier visit that recorded it) versus
*visit-specific* data (never pre-filled). Values are only applied to the
unsaved form instances; nothing is written until the user saves.
"""
from django.db.models import Q

from . import models as m

# section -> {"model": ..., "stable": [...], "visit_specific": [...]}
HISTORY_FIELDS = {
    "case_history": {
        "model": m.CaseHistory,
        "stable": ["past_history", "family_history"],
        "visit_specific": ["presenting_complaints"],
    },
    "physical_generals": {
        "model": m.PhysicalGeneral,
        "stable": ["sleep", "dreams", "thirst", "appetite", "desires", "aversions", "urine", "stool", "sweat",
                   "thermals", "menses", "obstetric_history", "constitution", "habits"],
        "visit_specific": [],
    },
    "mental_generals": {
        "model": m.MentalGeneral,
        "stable": ["disposition", "company_social", "emotions", "relationships", "triggers", "confidence", "fears",
                   "responsibility_work", "anxiety", "concentration", "anger", "likes", "grief", "dislikes",
                   "sensitivity", "communication"],
        "visit_specific": [],
    },
    "past_treatment": {
        "model": m.PastTreatment,
        "stable": ["previous_medication", "duration", "outcome", "notes"],
        "visit_specific": [],
    },
    # Never pre-filled: they describe this visit only.
    "followup_assessment": {"model": m.FollowUpNote, "stable": [],
                            "visit_specific": ["changes_since_last", "remedy_response", "notes"]},
    "examination": {"model": m.Examination, "stable": [],
                    "visit_specific": ["pulse", "blood_pressure", "weight", "temperature", "respiratory_rate", "pallor"]},
    "diagnosis": {"model": m.Diagnosis, "stable": [], "visit_specific": ["provisional", "differential", "final"]},
}

STABLE_SECTIONS = {section: spec for section, spec in HISTORY_FIELDS.items() if spec["stable"]}


def _earlier(queryset, visit):
    if visit is None:
        return queryset
    return queryset.exclude(visit=visit).filter(
        Q(visit__visit_date__lt=visit.visit_date) | Q(visit__visit_date=visit.visit_date, visit_id__lt=visit.pk))


def latest_history(patient, before_visit=None, sections=None):
    """{section: {field: (value, source_visit)}} from the latest earlier visit that recorded each field.

    One query per section; looks across all earlier visits, not just the last one.
    """
    result = {}
    for section, spec in STABLE_SECTIONS.items():
        if sections is not None and section not in sections:
            continue
        rows = _earlier(spec["model"].objects.filter(visit__patient=patient, visit__is_deleted=False)
                        .select_related("visit"), before_visit).order_by("-visit__visit_date", "-visit_id")
        found = {}
        for row in rows:
            for name in spec["stable"]:
                value = getattr(row, name)
                if name not in found and value not in (None, ""):
                    found[name] = (value, row.visit)
            if len(found) == len(spec["stable"]):
                break
        result[section] = found
    return result


def source_label(visit):
    return f"From visit on {visit.visit_date:%d %b %Y}" if visit is not None else "From linked family records"
