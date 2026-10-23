from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.utils import timezone

from core.models import AuditLog


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    if user.failed_login_attempts:
        user.failed_login_attempts = 0
        user.save(update_fields=["failed_login_attempts"])
    AuditLog.record(AuditLog.Action.LOGIN, user, user=user)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        AuditLog.record(AuditLog.Action.LOGOUT, user, user=user)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    username = credentials.get("username", "")
    user = get_user_model().objects.filter(username=username).first()
    AuditLog.record(
        AuditLog.Action.LOGIN_FAILED, user, model_name="accounts.User",
        object_repr=username, changes={"username": username},
    )
    if user is None or user.is_locked:
        return
    user.failed_login_attempts += 1
    fields = ["failed_login_attempts"]
    if user.failed_login_attempts >= settings.MAX_FAILED_LOGINS:
        user.is_locked = True
        user.locked_at = timezone.now()
        fields += ["is_locked", "locked_at"]
        AuditLog.record(AuditLog.Action.LOCK, user, changes={"reason": "too many failed logins"})
    user.save(update_fields=fields)
