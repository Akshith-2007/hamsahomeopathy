from django.conf import settings
from django.db import models
from django.utils import timezone

from .middleware import get_client_ip, get_current_request, get_current_user

AUDIT_VALUE_MAX = 300
AUDIT_SKIP_FIELDS = {"created_at", "updated_at", "created_by_id", "updated_by_id"}


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        VIEW = "VIEW", "View"
        LOGIN = "LOGIN", "Login"
        LOGOUT = "LOGOUT", "Logout"
        LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
        LOCK = "LOCK", "Lock user"
        UNLOCK = "UNLOCK", "Unlock user"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"
        EXPORT = "EXPORT", "Export"
        CHECK_IN = "CHECK_IN", "Check-in"
        VISIT_TYPE_CHANGED = "VISIT_TYPE_CHANGED", "Visit type changed"
        FOLLOWUP_SCHEDULED = "FOLLOWUP_SCHEDULED", "Follow-up scheduled"
        FOLLOWUP_RESCHEDULED = "FOLLOWUP_RESCHEDULED", "Follow-up rescheduled"
        FOLLOWUP_CANCELLED = "FOLLOWUP_CANCELLED", "Follow-up cancelled"
        FOLLOWUP_COMPLETED = "FOLLOWUP_COMPLETED", "Follow-up completed"
        FOLLOWUP_MISSED = "FOLLOWUP_MISSED", "Follow-up missed"
        PRESCRIPTION_REPEATED = "PRESCRIPTION_REPEATED", "Prescription repeated"
        PATIENT_SAVED_NO_CASE_SHEET = "PATIENT_SAVED_NO_CASE_SHEET", "Patient saved without case sheet"
        RELATIONSHIP_LINKED = "RELATIONSHIP_LINKED", "Relative linked"
        RELATIONSHIP_EDITED = "RELATIONSHIP_EDITED", "Relationship edited"
        RELATIONSHIP_REMOVED = "RELATIONSHIP_REMOVED", "Relative unlinked"
        FAMILY_HISTORY_INSERTED = "FAMILY_HISTORY_INSERTED", "Family history inserted"
        PRESCRIPTION_SENT = "PRESCRIPTION_SENT", "Prescription sent to pharmacy"
        PRESCRIPTION_RESENT = "PRESCRIPTION_RESENT", "Prescription re-sent to pharmacy"
        PRESCRIPTION_UPDATED = "PRESCRIPTION_UPDATED", "Updated prescription sent to pharmacy"
        DISPENSE_STATUS_CHANGED = "DISPENSE_STATUS_CHANGED", "Dispense status changed"
        LABEL_PRINTED = "LABEL_PRINTED", "Dispensing label printed"
        RELATIONSHIP_INFERRED = "RELATIONSHIP_INFERRED", "Relative auto-linked"
        RELATIONSHIP_CONFIRMED = "RELATIONSHIP_CONFIRMED", "Auto-link confirmed"
        RELATIONSHIP_REJECTED = "RELATIONSHIP_REJECTED", "Auto-link rejected"
        RELATIONSHIP_UNREJECTED = "RELATIONSHIP_UNREJECTED", "Rejected auto-link restored"
        INFERENCE_CONFLICT = "INFERENCE_CONFLICT", "Family inference conflict"
        HISTORY_PREFILLED = "HISTORY_PREFILLED", "History pre-filled"
        FAMILY_HISTORY_AUTOFILLED = "FAMILY_HISTORY_AUTOFILLED", "Family history auto-filled"
        VISIT_THROUGH_FAMILY = "VISIT_THROUGH_FAMILY", "Visit created through family member"
        COLLECT_BY_ORDER = "COLLECT_BY_ORDER", "Collect-by pharmacy order"
        REPORT_UPLOADED = "REPORT_UPLOADED", "Lab report uploaded"
        REPORT_CORRECTED = "REPORT_CORRECTED", "Lab report corrected"

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs"
    )
    username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=40, choices=Action.choices, db_index=True)
    model_name = models.CharField(max_length=100, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    path = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "audit_logs"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.username} {self.action} {self.model_name}#{self.object_id}"

    @classmethod
    def record(cls, action, obj=None, changes=None, user=None, model_name=None, object_repr=None):
        request = get_current_request()
        user = user or get_current_user()
        return cls.objects.create(
            user=user if user is not None and user.pk else None,
            username=getattr(user, "username", "") or "",
            action=action,
            model_name=model_name or (obj._meta.label if obj is not None else ""),
            object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
            object_repr=(object_repr or (str(obj) if obj is not None else ""))[:255],
            changes=changes or {},
            ip_address=get_client_ip(request),
            path=(request.path[:255] if request is not None else ""),
        )


def _audit_value(value):
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= AUDIT_VALUE_MAX else text[:AUDIT_VALUE_MAX] + "…"


class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        return self.update(is_deleted=True, deleted_at=timezone.now(), deleted_by=get_current_user())

    def hard_delete(self):
        return super().delete()


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: hides soft-deleted rows everywhere, including reverse relations."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class AuditedModel(models.Model):
    """Base for every clinical entity: who/when columns, soft delete, audit trail."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    is_deleted = models.BooleanField(default=False, editable=False, db_index=True)

    objects = AliveManager()
    all_objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        user = get_current_user()
        creating = self._state.adding
        before = {}
        if not creating:
            before = type(self).all_objects.filter(pk=self.pk).values().first() or {}
        if user is not None:
            if creating and self.created_by_id is None:
                self.created_by = user
            self.updated_by = user
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"updated_by", "updated_at"}
        super().save(*args, **kwargs)

        changes = {}
        for field in self._meta.concrete_fields:
            name = field.attname
            if name in AUDIT_SKIP_FIELDS:
                continue
            new = _audit_value(getattr(self, name))
            old = _audit_value(before.get(name)) if not creating else None
            if creating:
                if new not in (None, ""):
                    changes[field.name] = new
            elif old != new:
                changes[field.name] = {"old": old, "new": new}
        if creating or changes:
            AuditLog.record(AuditLog.Action.CREATE if creating else AuditLog.Action.UPDATE, self, changes, user=user)

    def delete(self, using=None, keep_parents=False):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = get_current_user()
        models.Model.save(self, update_fields=["is_deleted", "deleted_at", "deleted_by"])
        AuditLog.record(AuditLog.Action.DELETE, self)
        return 1, {self._meta.label: 1}

    def hard_delete(self):
        return super().delete()



class Notification(models.Model):
    """In-app message for one user (delivered by polling; see core.inbox)."""

    class Kind(models.TextChoices):
        PRESCRIPTION_SENT = "PRESCRIPTION_SENT", "New prescription"
        PRESCRIPTION_UPDATED = "PRESCRIPTION_UPDATED", "Updated prescription"
        REPORT_READY = "REPORT_READY", "Report ready"
        REPORT_CORRECTED = "REPORT_CORRECTED", "Corrected report"
        GENERAL = "GENERAL", "General"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.GENERAL)
    title = models.CharField(max_length=150)
    message = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=255, blank=True)
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "read_at"], name="notif_recipient_unread")]

    def __str__(self):
        return f"{self.recipient} – {self.title}"
