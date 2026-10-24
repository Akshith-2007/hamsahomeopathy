"""Prescription → pharmacy hand-off (reception dispenses).

The doctor's Prescription stays editable; every send freezes its items into a
versioned DispenseOrder. Editing a sent prescription sends a new version
("Updated prescription"), and still-open older orders become SUPERSEDED.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.rbac import can_edit
from core import inbox
from core.models import AuditLog, Notification

from . import models as m
from .services import WorkflowError, family_link, set_visit_status

O = m.DispenseOrder.Status
SNAPSHOT_FIELDS = ("medicine_name", "potency", "form", "quantity", "dose", "duration", "instructions")

# action -> (allowed from, new status, timestamp field, user field)
TRANSITIONS = {
    "start-preparing": ({O.SENT}, O.PREPARING, "prepared_at", "prepared_by"),
    "mark-ready": ({O.SENT, O.PREPARING}, O.READY, "ready_at", "prepared_by"),
    "mark-dispensed": ({O.READY}, O.DISPENSED, "dispensed_at", "dispensed_by"),
    "cancel": ({O.SENT, O.PREPARING, O.READY}, O.CANCELLED, None, None),
}


def snapshot(prescription):
    return [{name: getattr(item, name) for name in SNAPSHOT_FIELDS} for item in prescription.items.all()]


def _key(row):
    return (row["medicine_name"].strip().lower(), row["potency"])


def _same(row_a, row_b):
    return all((row_a[f] or "") == (row_b[f] or "") for f in SNAPSHOT_FIELDS)


def latest_order(prescription):
    return prescription.dispense_orders.order_by("-version").first()


def pharmacy_recipients(exclude=None):
    users = get_user_model().objects.filter(is_active=True, is_locked=False)
    return [u for u in users if can_edit(u, "pharmacy_items") and u != exclude]


@transaction.atomic
def send_to_pharmacy(prescription, user, *, keep_open=False, reason=None):
    """Freeze the prescription into a new dispense order and notify reception.

    Returns (order, created). If nothing changed since the last live order, no
    new version is created.
    """
    rows = snapshot(prescription)
    if not rows:
        raise WorkflowError("Add at least one medicine before sending the prescription to the pharmacy.")
    visit = prescription.visit
    previous = latest_order(prescription)
    previous_rows = [{f: getattr(i, f) for f in SNAPSHOT_FIELDS} for i in previous.items.all()] if previous else []
    live_previous = previous is not None and previous.status not in (O.CANCELLED, O.SUPERSEDED)
    if live_previous and len(rows) == len(previous_rows) and all(map(_same, rows, previous_rows)):
        if not keep_open:
            set_visit_status(visit, m.Visit.Status.COMPLETED)
        return previous, False

    is_update = previous is not None and previous.status != O.CANCELLED
    if previous is not None and previous.is_active:
        previous.status = O.SUPERSEDED
        previous.save(update_fields=["status"])
    by_key = {_key(r): r for r in previous_rows}
    current_keys = {_key(r) for r in rows}
    order = m.DispenseOrder.objects.create(
        visit=visit, patient=visit.patient, prescription=prescription,
        version=(previous.version + 1) if previous else 1, is_update=is_update, previous_order=previous,
        removed_items=[f"{r['medicine_name']} {r['potency']}".strip() for r in previous_rows
                       if _key(r) not in current_keys] if is_update else [],
        sent_by=user, sent_at=timezone.now(),
    )
    if visit.consultation_mode == m.Visit.Mode.THROUGH_FAMILY and visit.reported_by_id:
        link = family_link(visit.patient, visit.reported_by)
        order.collect_by = visit.reported_by
        order.collect_by_label = link.display_label.lower() if link else "family member"
        order.save(update_fields=["collect_by", "collect_by_label"])
        AuditLog.record(AuditLog.Action.COLLECT_BY_ORDER, order, user=user, changes={
            "collect_by": visit.reported_by_id, "relationship": order.collect_by_label})
    for position, row in enumerate(rows):
        old = by_key.get(_key(row))
        change = ""
        if is_update:
            change = m.DispenseItem.Change.ADDED if old is None else (
                "" if _same(old, row) else m.DispenseItem.Change.CHANGED)
        m.DispenseItem.objects.create(order=order, position=position, change=change, **row)

    reason = reason or (AuditLog.Action.PRESCRIPTION_RESENT if is_update else AuditLog.Action.PRESCRIPTION_SENT)
    AuditLog.record(reason, order, user=user, changes={
        "version": order.version, "item_count": len(rows), "superseded": previous.pk if is_update and previous else None,
    })
    patient = visit.patient
    kind = Notification.Kind.PRESCRIPTION_UPDATED if is_update else Notification.Kind.PRESCRIPTION_SENT
    title = f"{'Updated prescription' if is_update else 'New prescription'}: {patient.full_name}"
    inbox.notify(
        pharmacy_recipients(exclude=user), title, kind=kind, target=order,
        message=f"{patient.opd_number} · {len(rows)} item(s) from {visit.doctor or user}"
                + (f" · collect by {order.collect_by.full_name} ({order.collect_by_label})" if order.collect_by_id else ""),
        url=f"{reverse('pharmacy_queue')}#order-{order.pk}",
    )
    if not keep_open:
        set_visit_status(visit, m.Visit.Status.COMPLETED)
    return order, True


def sync_after_edit(prescription, user):
    """Called after the doctor edits a prescription: re-send it if it was already sent."""
    previous = latest_order(prescription)
    if previous is None or previous.status == O.CANCELLED:
        return None
    if not prescription.items.exists():
        if previous.is_active:
            change_status(previous, "cancel", user)
        return None
    order, created = send_to_pharmacy(prescription, user, keep_open=True,
                                      reason=AuditLog.Action.PRESCRIPTION_UPDATED)
    return order if created else None


@transaction.atomic
def change_status(order, action, user):
    if action not in TRANSITIONS:
        raise WorkflowError("Unknown pharmacy action.")
    allowed, new_status, time_field, user_field = TRANSITIONS[action]
    if order.status not in allowed:
        raise WorkflowError(f"Order is {order.get_status_display().lower()}; cannot {action.replace('-', ' ')}.")
    old = order.status
    order.status = new_status
    if time_field:
        setattr(order, time_field, timezone.now())
    if user_field:
        setattr(order, user_field, user)
    order.save()
    AuditLog.record(AuditLog.Action.DISPENSE_STATUS_CHANGED, order, user=user,
                    changes={"status": {"old": old, "new": new_status}})
    return order


def _group_pickups(orders):
    """Place each "collect by" order right after the collecting patient's own order in the same stage."""
    by_patient = {}
    for order in orders:
        by_patient.setdefault(order.patient_id, order)
        order.collects_for = []
    attached = set()
    for order in orders:
        host = by_patient.get(order.collect_by_id) if order.collect_by_id else None
        if host is not None and host is not order:
            host.collects_for.append(order)
            attached.add(order.pk)
    result = []
    for order in orders:
        if order.pk in attached:
            continue
        result.append(order)
        result.extend(order.collects_for)
    return result


def queue(today=None):
    """Orders for the pharmacy screen, grouped by stage, oldest first (family pickups kept together)."""
    today = today or timezone.localdate()
    base = (m.DispenseOrder.objects.select_related("patient", "visit__doctor", "sent_by", "collect_by")
            .prefetch_related("items"))
    return {
        "new": _group_pickups(list(base.filter(status=O.SENT).order_by("sent_at"))),
        "preparing": _group_pickups(list(base.filter(status=O.PREPARING).order_by("sent_at"))),
        "ready": _group_pickups(list(base.filter(status=O.READY).order_by("sent_at"))),
        "dispensed": _group_pickups(list(base.filter(status=O.DISPENSED, dispensed_at__date=today)
                                         .order_by("dispensed_at"))),
    }
