"""In-app notifications.

Callers use `notify()`; browsers fetch with `unread_for()` by polling
(/api/notifications/unread/). To switch to WebSockets later, point
settings.NOTIFICATION_PUSH_BACKEND at a class whose `push(notification)` sends
to a channel layer. Nothing else needs to change.
"""
from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from .models import Notification


class PollingPushBackend:
    """Default: nothing to push, clients poll for unread notifications."""

    def push(self, notification):
        return None


def _push_backend():
    return import_string(getattr(settings, "NOTIFICATION_PUSH_BACKEND", "core.inbox.PollingPushBackend"))()


def notify(recipients, title, *, message="", url="", kind=Notification.Kind.GENERAL, target=None):
    """Create one notification per recipient and hand each to the push backend."""
    backend = _push_backend()
    created = []
    for user in recipients:
        notification = Notification.objects.create(
            recipient=user, kind=kind, title=title[:150], message=message[:500], url=url,
            target_model=target._meta.label if target is not None else "",
            target_id=str(target.pk) if target is not None else "",
        )
        backend.push(notification)
        created.append(notification)
    return created


def unread_for(user):
    return Notification.objects.filter(recipient=user, read_at__isnull=True)


def mark_read(notification):
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    return notification


def mark_all_read(user):
    return unread_for(user).update(read_at=timezone.now())
