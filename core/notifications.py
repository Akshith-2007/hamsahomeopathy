"""Pluggable patient notifications (SMS / WhatsApp).

Callers use `notify_patient()`; the transport is chosen by
settings.NOTIFICATION_BACKEND. Add a provider (Twilio, Gupshup, Meta Cloud API…)
by subclassing NotificationBackend and pointing the setting at it.
"""
import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


class NotificationBackend:
    def send_sms(self, phone, message):
        raise NotImplementedError

    def send_whatsapp(self, phone, message):
        raise NotImplementedError


class ConsoleBackend(NotificationBackend):
    def send_sms(self, phone, message):
        logger.info("SMS to %s: %s", phone, message)
        return True

    def send_whatsapp(self, phone, message):
        logger.info("WhatsApp to %s: %s", phone, message)
        return True


def get_backend():
    return import_string(settings.NOTIFICATION_BACKEND)()


def notify_patient(patient, message):
    """Send `message` over every enabled channel. Returns the channels used."""
    if not patient.phone:
        return []
    backend = get_backend()
    sent = []
    if settings.SMS_ENABLED and backend.send_sms(patient.phone, message):
        sent.append("sms")
    if settings.WHATSAPP_ENABLED and backend.send_whatsapp(patient.phone, message):
        sent.append("whatsapp")
    return sent
