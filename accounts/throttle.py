"""Per-IP limit on failed sign-ins, so one device cannot guess passwords for many accounts.

Counts live in Django's cache (per process with the default local-memory cache, which is enough for a
clinic-sized deployment; configure a shared cache to count across all workers).
"""
from django.conf import settings
from django.core.cache import cache

from core.middleware import get_client_ip


def window_minutes():
    return settings.LOGIN_RATE_WINDOW_MINUTES


def _key(request):
    return f"login-failures:{get_client_ip(request) or 'unknown'}"


def is_blocked(request):
    return cache.get(_key(request), 0) >= settings.LOGIN_RATE_LIMIT


def record_failure(request):
    key = _key(request)
    if cache.add(key, 1, timeout=window_minutes() * 60):
        return
    try:
        cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout=window_minutes() * 60)


def reset(request):
    cache.delete(_key(request))
