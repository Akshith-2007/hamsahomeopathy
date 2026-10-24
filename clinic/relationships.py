"""Two-way family links between patients, with automatic inference of close family.

Storage: a link is two rows, "A is B's MOTHER" (patient=B, related=A) and the
reverse "B is A's SON" (patient=A, related=B).

* MANUAL links are created by staff and always win.
* INFERRED links are recomputed by `rebuild_inferred_links()` from the manual
  links (and from inferred links derived from them) using INFERENCE_RULES.
  They're limited to close family: parents, siblings, grandparents, children
  and grandchildren.
* A REJECTED link (a user removed an inferred link) blocks that pair from ever
  being inferred again, until an admin restores it.

All writes go through this module so both rows and the inferred family stay
consistent.
"""
import logging
from collections import defaultdict

from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import AuditLog

from . import models as m
from .services import WorkflowError

logger = logging.getLogger(__name__)

R = m.Relationship
PR = m.PatientRelationship
MANUAL, INFERRED = PR.Source.MANUAL, PR.Source.INFERRED
ACTIVE, REJECTED = PR.LinkStatus.ACTIVE, PR.LinkStatus.REJECTED

# relationship -> category
CATEGORY = {
    R.FATHER: "parent", R.MOTHER: "parent", R.PARENT: "parent",
    R.SON: "child", R.DAUGHTER: "child", R.CHILD: "child",
    R.BROTHER: "sibling", R.SISTER: "sibling", R.SIBLING: "sibling",
    R.GRANDFATHER: "grandparent", R.GRANDMOTHER: "grandparent", R.GRANDPARENT: "grandparent",
    R.GRANDSON: "grandchild", R.GRANDDAUGHTER: "grandchild", R.GRANDCHILD: "grandchild",
    R.UNCLE: "uncle_aunt", R.AUNT: "uncle_aunt",
    R.NEPHEW: "nephew_niece", R.NIECE: "nephew_niece",
    R.SPOUSE: "spouse", R.OTHER: "other",
}
INVERSE_CATEGORY = {
    "parent": "child", "child": "parent", "sibling": "sibling", "grandparent": "grandchild",
    "grandchild": "grandparent", "uncle_aunt": "nephew_niece", "nephew_niece": "uncle_aunt",
    "spouse": "spouse", "other": "other",
}
# category -> (male, female, unknown) relationship; None = OTHER with a neutral label
GENDERED = {
    "parent": (R.FATHER, R.MOTHER, R.PARENT),
    "child": (R.SON, R.DAUGHTER, R.CHILD),
    "sibling": (R.BROTHER, R.SISTER, R.SIBLING),
    "grandparent": (R.GRANDFATHER, R.GRANDMOTHER, R.GRANDPARENT),
    "grandchild": (R.GRANDSON, R.GRANDDAUGHTER, R.GRANDCHILD),
    "uncle_aunt": (R.UNCLE, R.AUNT, None),
    "nephew_niece": (R.NEPHEW, R.NIECE, None),
    "spouse": (R.SPOUSE, R.SPOUSE, R.SPOUSE),
}
NEUTRAL_LABEL = {"uncle_aunt": "Uncle / Aunt", "nephew_niece": "Nephew / Niece"}

# If B is A's <first> and C is B's <second> (C ≠ A), then C is A's <result>.
# Close family only: no cousins, in-laws, uncles or aunts are ever inferred.
INFERENCE_RULES = {
    ("parent", "spouse"): "parent",          # mother's husband -> father
    ("parent", "child"): "sibling",          # mother's son -> brother
    ("parent", "parent"): "grandparent",     # mother's mother -> grandmother
    ("sibling", "parent"): "parent",         # brother's father -> father
    ("sibling", "sibling"): "sibling",       # brother's sister -> sister
    ("child", "child"): "grandchild",        # son's daughter -> granddaughter
    ("spouse", "child"): "child",            # wife's son -> son
}

# What reception picks from at registration / on the patient page.
CHOICES = [
    (value, label) for value, label in R.choices
    if value not in {R.PARENT, R.CHILD, R.SIBLING, R.GRANDPARENT, R.GRANDCHILD}
]


def gendered(category, person_gender):
    """Relationship value (and label) for a person of `person_gender` in `category`."""
    if category == "other":
        return R.OTHER, ""
    male, female, unknown = GENDERED[category]
    choice = {"M": male, "F": female}.get(person_gender, unknown)
    if choice is None:
        return R.OTHER, NEUTRAL_LABEL[category]
    return choice, ""


def reverse_relationship(relationship, label, person):
    """How `person` relates to the other side, given that the other side is `person`'s <relationship>."""
    category = INVERSE_CATEGORY[CATEGORY[relationship]]
    if category == "other":
        return R.OTHER, label
    return gendered(category, person.gender)


# --------------------------------------------------------------------------- manual links
def _pair_rows(a_id, b_id):
    """Both live rows for a pair (either may be None)."""
    rows = {(r.patient_id, r.related_patient_id): r
            for r in PR.objects.filter(Q(patient_id=a_id, related_patient_id=b_id) | Q(patient_id=b_id, related_patient_id=a_id))}
    return rows.get((a_id, b_id)), rows.get((b_id, a_id))


@transaction.atomic
def link(patient, relative, relationship, user, *, label=""):
    """Manually record that `relative` is `patient`'s <relationship> (and the reverse)."""
    if patient.pk == relative.pk:
        raise WorkflowError("A patient cannot be linked to themselves.")
    if relationship not in R.values:
        raise WorkflowError("Choose a valid relationship.")
    label = label if relationship == R.OTHER else ""
    forward, reverse = _pair_rows(patient.pk, relative.pk)
    existing = forward or reverse
    if existing is not None and existing.source == MANUAL and existing.status == ACTIVE:
        shown = forward or existing
        raise WorkflowError(f"{relative.full_name} is already linked as {shown.display_label}. "
                            "Edit that link instead of adding a second one.")
    reverse_kind, reverse_label = reverse_relationship(relationship, label, patient)
    try:
        # A manual link replaces an inferred (or rejected) one for the same pair.
        forward = _write(forward, patient, relative, relationship, label, MANUAL)
        _write(reverse, relative, patient, reverse_kind, reverse_label, MANUAL)
    except IntegrityError as exc:
        raise WorkflowError("These patients are already linked with a different relationship.") from exc
    AuditLog.record(AuditLog.Action.RELATIONSHIP_LINKED, forward, user=user, changes={
        "patient": patient.pk, "relative": relative.pk, "relationship": relationship,
        "reverse": reverse_kind, "label": label, "replaced_inferred": existing is not None,
    })
    rebuild_inferred_links(patient, user=user)
    return forward


def _write(row, patient, relative, relationship, label, source, via=None):
    if row is None:
        row = PR(patient=patient, related_patient=relative)
    row.relationship, row.label, row.source, row.status, row.inferred_via = relationship, label, source, ACTIVE, via
    row.save()
    return row


@transaction.atomic
def update(link_row, relationship, user, *, label=""):
    """Change a link's relationship (editing an inferred link confirms it as manual)."""
    if relationship not in R.values:
        raise WorkflowError("Choose a valid relationship.")
    forward, reverse = _pair_rows(link_row.patient_id, link_row.related_patient_id)
    old = forward.relationship
    label = label if relationship == R.OTHER else ""
    _write(forward, forward.patient, forward.related_patient, relationship, label, MANUAL)
    reverse_kind, reverse_label = reverse_relationship(relationship, label, forward.patient)
    _write(reverse, forward.related_patient, forward.patient, reverse_kind, reverse_label, MANUAL)
    AuditLog.record(AuditLog.Action.RELATIONSHIP_EDITED, forward, user=user, changes={
        "relationship": {"old": old, "new": relationship}, "reverse": reverse_kind, "label": label,
    })
    rebuild_inferred_links(forward.patient, user=user)
    return forward


@transaction.atomic
def unlink(link_row, user):
    """Remove a manual link from both patients (an inferred link is rejected instead)."""
    if link_row.source == INFERRED:
        return reject(link_row, user)
    forward, reverse = _pair_rows(link_row.patient_id, link_row.related_patient_id)
    AuditLog.record(AuditLog.Action.RELATIONSHIP_REMOVED, forward, user=user, changes={
        "patient": forward.patient_id, "relative": forward.related_patient_id, "relationship": forward.relationship,
    })
    for row in (forward, reverse):
        if row is not None:
            row.delete()
    rebuild_inferred_links(forward.patient, user=user)
    rebuild_inferred_links(forward.related_patient, user=user)


@transaction.atomic
def confirm(link_row, user):
    """Turn an inferred link into a manual one (both directions)."""
    if link_row.source == MANUAL:
        return link_row
    for row in _pair_rows(link_row.patient_id, link_row.related_patient_id):
        if row is not None:
            row.source, row.inferred_via, row.status = MANUAL, None, ACTIVE
            row.save()
    AuditLog.record(AuditLog.Action.RELATIONSHIP_CONFIRMED, link_row, user=user,
                    changes={"patient": link_row.patient_id, "relative": link_row.related_patient_id})
    rebuild_inferred_links(link_row.patient, user=user)
    return link_row


@transaction.atomic
def reject(link_row, user):
    """Remove an inferred link so it is never inferred again for this pair."""
    if link_row.source != INFERRED:
        raise WorkflowError("Only automatic links can be rejected; remove manual links instead.")
    for row in _pair_rows(link_row.patient_id, link_row.related_patient_id):
        if row is not None:
            row.status = REJECTED
            row.save()
    AuditLog.record(AuditLog.Action.RELATIONSHIP_REJECTED, link_row, user=user, changes={
        "patient": link_row.patient_id, "relative": link_row.related_patient_id,
        "relationship": link_row.relationship,
    })
    rebuild_inferred_links(link_row.patient, user=user)
    rebuild_inferred_links(link_row.related_patient, user=user)


@transaction.atomic
def unreject(link_row, user):
    """Admin: forget a rejection so the pair may be inferred again."""
    if link_row.status != REJECTED:
        return
    for row in _pair_rows(link_row.patient_id, link_row.related_patient_id):
        if row is not None:
            row.delete()
    AuditLog.record(AuditLog.Action.RELATIONSHIP_UNREJECTED, link_row, user=user,
                    changes={"patient": link_row.patient_id, "relative": link_row.related_patient_id})
    rebuild_inferred_links(link_row.patient, user=user)


# --------------------------------------------------------------------------- inference
def _component(patient_id):
    """Patients connected to `patient_id` through active manual links (one query per hop)."""
    seen, frontier = {patient_id}, {patient_id}
    while frontier:
        found = set(PR.objects.filter(patient_id__in=frontier, source=MANUAL, status=ACTIVE,
                                      related_patient__is_deleted=False).values_list("related_patient_id", flat=True))
        frontier = found - seen
        seen |= frontier
    return seen


def _infer(nodes, manual, blocked):
    """Fixed-point application of INFERENCE_RULES. Returns ({(a, c): (category, via)}, conflicts)."""
    conflicts = set()
    while True:
        restart = False
        edges = dict(manual)
        inferred = {}
        changed = True
        while changed and not restart:
            changed = False
            adjacency = defaultdict(list)
            for (a, b), cat in edges.items():
                adjacency[a].append((b, cat))
            candidates = defaultdict(set)
            via = {}
            for a in nodes:
                for b, first in adjacency[a]:
                    for c, second in adjacency[b]:
                        result = INFERENCE_RULES.get((first, second))
                        pair = frozenset((a, c))
                        if c == a or result is None or pair in blocked or pair in conflicts:
                            continue
                        if (a, c) in edges and (a, c) not in inferred:
                            continue  # a manual link exists for this pair: manual wins
                        candidates[(a, c)].add(result)
                        via[(a, c)] = min(via.get((a, c), b), b)
            for (a, c), results in candidates.items():
                results = results | {INVERSE_CATEGORY[r] for r in candidates.get((c, a), set())}
                if (a, c) in inferred:
                    results.add(inferred[(a, c)][0])
                if len(results) > 1:
                    conflicts.add(frozenset((a, c)))
                    restart = True  # recompute without the conflicting pair
                    break
                result = next(iter(results))
                if (a, c) not in inferred:
                    inferred[(a, c)] = (result, via[(a, c)])
                    inferred[(c, a)] = (INVERSE_CATEGORY[result], via.get((c, a), via[(a, c)]))
                    edges[(a, c)], edges[(c, a)] = result, INVERSE_CATEGORY[result]
                    changed = True
        if not restart:
            return inferred, conflicts


@transaction.atomic
def rebuild_inferred_links(patient, user=None):
    """Recompute inferred links for the patient's whole connected family. Idempotent.

    Returns the newly created inferred rows that point *from* `patient`.
    """
    nodes = _component(patient.pk if hasattr(patient, "pk") else patient)
    rows = list(PR.objects.filter(Q(patient_id__in=nodes) | Q(related_patient_id__in=nodes))
                .select_related("patient", "related_patient"))
    manual = {(r.patient_id, r.related_patient_id): CATEGORY[r.relationship]
              for r in rows if r.source == MANUAL and r.status == ACTIVE}
    blocked = {frozenset((r.patient_id, r.related_patient_id)) for r in rows if r.status == REJECTED}
    existing = {(r.patient_id, r.related_patient_id): r for r in rows}
    people = {p.pk: p for p in m.Patient.objects.filter(pk__in=nodes)}

    inferred, conflicts = _infer(nodes, manual, blocked)

    created = []
    for (a, c), (category, via) in inferred.items():
        relationship, label = gendered(category, people[c].gender)
        row = existing.get((a, c))
        if row is None:
            row = PR.objects.create(patient=people[a], related_patient=people[c], relationship=relationship,
                                    label=label, source=INFERRED, inferred_via=people.get(via))
            created.append(row)
        elif row.source == INFERRED and row.status == ACTIVE and (
                row.relationship, row.label, row.inferred_via_id) != (relationship, label, via):
            row.relationship, row.label, row.inferred_via_id = relationship, label, via
            row.save()

    for key, row in existing.items():
        if row.source == INFERRED and row.status == ACTIVE and key not in inferred:
            AuditLog.record(AuditLog.Action.RELATIONSHIP_REMOVED, row, user=user,
                            changes={"reason": "no longer supported by manual links"})
            row.delete()

    for row in created:
        if row.patient_id < row.related_patient_id:  # one audit entry per pair
            AuditLog.record(AuditLog.Action.RELATIONSHIP_INFERRED, row, user=user, changes={
                "patient": row.patient_id, "relative": row.related_patient_id,
                "relationship": row.relationship, "via": row.inferred_via_id,
            })
    for pair in conflicts:
        a, b = sorted(pair)
        if not AuditLog.objects.filter(action=AuditLog.Action.INFERENCE_CONFLICT, object_repr=f"pair {a}-{b}").exists():
            logger.warning("Family inference conflict between patients %s and %s; no link created", a, b)
            AuditLog.record(AuditLog.Action.INFERENCE_CONFLICT, model_name="clinic.PatientRelationship",
                            object_repr=f"pair {a}-{b}", user=user,
                            changes={"patients": [a, b], "note": "rules gave different relationships"})
    start = patient.pk if hasattr(patient, "pk") else patient
    return [row for row in created if row.patient_id == start]


# --------------------------------------------------------------------------- reading
def relatives_of(patient):
    """Active links of `patient` (manual and inferred), relative and via loaded, with `via_label` set."""
    links = list(PR.objects.filter(patient=patient, status=ACTIVE, related_patient__is_deleted=False)
                 .select_related("related_patient", "inferred_via").order_by("relationship", "related_patient__full_name"))
    labels = {link.related_patient_id: link.display_label for link in links}
    for link in links:
        link.via_label = labels.get(link.inferred_via_id, "") if link.source == INFERRED else ""
    return links


def rejected_of(patient):
    return list(PR.objects.filter(patient=patient, status=REJECTED).select_related("related_patient"))
