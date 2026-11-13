"""Expose the current request to model code so audit fields fill themselves."""
import contextvars
from contextlib import contextmanager

_current_request = contextvars.ContextVar("current_request", default=None)
_acting_user = contextvars.ContextVar("acting_user", default=None)


class CurrentRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)


def get_current_request():
    return _current_request.get()


def get_current_user():
    """The authenticated user behind the current request (or `acting_as` block)."""
    override = _acting_user.get()
    if override is not None:
        return override
    request = _current_request.get()
    # DRF copies token-authenticated users onto the underlying HttpRequest, so
    # reading request.user lazily works for both sessions and API tokens.
    user = getattr(request, "user", None) if request is not None else None
    if user is not None and user.is_authenticated:
        return user
    return None


@contextmanager
def acting_as(user):
    """Attribute writes to `user` outside a request (management commands, jobs)."""
    token = _acting_user.set(user)
    try:
        yield
    finally:
        _acting_user.reset(token)


def get_client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class HealthCheckMiddleware:
    """Answer /healthz and /readyz before ALLOWED_HOSTS validation.

    Docker healthchecks and uptime monitors may send a Host header that is not in
    ALLOWED_HOSTS (e.g. 127.0.0.1); that must not turn into a 400 "unhealthy".
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in ("/health", "/healthz"):
            from . import views

            return views.healthz(request)
        if request.path == "/readyz":
            from . import views

            return views.readyz(request)
        return self.get_response(request)
