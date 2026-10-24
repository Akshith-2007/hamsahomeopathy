"""Family history built from every active relative's own records (manual and inferred links).

Loads all relatives' visits with diagnoses, case history and generals in a
fixed number of queries. Only diagnoses and chronic conditions are summarised;
relatives' prescriptions and visits are never merged into the patient's own
history, and nothing is ever written to a relative's record.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from django.core.exceptions import ObjectDoesNotExist

from . import models as m
from .relationships import CATEGORY, relatives_of

# Summary groups, in display order.
GROUPS = [
    ("parent", "Parents"), ("sibling", "Siblings"), ("grandparent", "Grandparents"),
    ("child", "Children"), ("grandchild", "Grandchildren"), ("spouse", "Spouse"), ("other", "Other relatives"),
]
GROUP_OF = {"uncle_aunt": "other", "nephew_niece": "other"}


@dataclass
class RelativeHistory:
    link: m.PatientRelationship
    diagnoses: list = field(default_factory=list)  # [(text, latest date)], most recent first
    chronic: list = field(default_factory=list)    # short strings
    last_visit: m.Visit | None = None

    @property
    def relative(self):
        return self.link.related_patient

    @property
    def group(self):
        category = CATEGORY.get(self.link.relationship, "other")
        return GROUP_OF.get(category, category)

    @property
    def path(self):
        """e.g. 'via Mother' for inferred links."""
        return f"via {self.link.via_label}" if getattr(self.link, "via_label", "") else ""

    @property
    def heading(self):
        """'Mother (Lakshmi, 58)'"""
        return f"{self.link.display_label} ({self.relative.full_name}, {self.relative.age})"

    def facts(self):
        """Diagnoses with years; falls back to chronic notes if no diagnosis was recorded."""
        return [f"{text} ({when:%Y})" for text, when in self.diagnoses] or self.chronic[:2]

    @property
    def summary_line(self):
        facts = self.facts()
        return f"{self.heading} – {', '.join(facts)}" if facts else ""


def _section(visit, name):
    try:
        return getattr(visit, name)
    except ObjectDoesNotExist:
        return None


def family_history(patient):
    """[RelativeHistory] for every active relative, grouped order (parents, siblings, …)."""
    links = relatives_of(patient)
    if not links:
        return []
    visits_by_patient = defaultdict(list)
    visits = (m.Visit.objects.filter(patient_id__in=[link.related_patient_id for link in links])
              .select_related("diagnosis", "case_history", "physical_generals")
              .order_by("-visit_date", "-id"))
    for visit in visits:
        visits_by_patient[visit.patient_id].append(visit)

    result = []
    for link in links:
        entry = RelativeHistory(link=link)
        seen = {}
        for visit in visits_by_patient[link.related_patient_id]:
            entry.last_visit = entry.last_visit or visit
            diagnosis = _section(visit, "diagnosis")
            if diagnosis is not None:
                for text in (diagnosis.final, diagnosis.provisional):
                    text = (text or "").strip()
                    if text and text.lower() not in seen:
                        seen[text.lower()] = (text, visit.visit_date)
                        break  # final wins over provisional for the same visit
            case = _section(visit, "case_history")
            if case is not None and case.past_history.strip():
                note = f"Past history: {case.past_history.strip()[:120]}"
                if note not in entry.chronic:
                    entry.chronic.append(note)
            generals = _section(visit, "physical_generals")
            if generals is not None and generals.constitution.strip():
                note = f"Constitution: {generals.constitution.strip()[:60]}"
                if note not in entry.chronic:
                    entry.chronic.append(note)
        entry.diagnoses = list(seen.values())  # visits are newest first, so first sighting = latest
        entry.chronic = entry.chronic[:3]
        result.append(entry)
    order = {key: i for i, (key, _label) in enumerate(GROUPS)}
    result.sort(key=lambda e: (order.get(e.group, 99), e.relative.full_name))
    return result


def grouped(entries):
    """[(group label, [entries])] for the panel."""
    buckets = defaultdict(list)
    for entry in entries:
        buckets[entry.group].append(entry)
    return [(label, buckets[key]) for key, label in GROUPS if buckets.get(key)]


def summary_text(entries):
    """'Mother (Lakshmi, 58) – Diabetes (2024), Hypertension (2025)' lines for the family-history field."""
    return "\n".join(line for line in (e.summary_line for e in entries) if line)


# Phase 2 name kept for callers.
insert_text = summary_text


def new_information(entries, existing_text):
    """Lines describing facts not yet mentioned in `existing_text` (case-insensitive)."""
    existing = (existing_text or "").lower()
    lines = []
    for entry in entries:
        missing = [fact for fact in entry.facts() if fact.split(" (")[0].lower() not in existing]
        if missing:
            lines.append(f"{entry.heading} – {', '.join(missing)}")
    return "\n".join(lines)
