from django.conf import settings

from accounts.rbac import section_access


def app_context(request):
    user = getattr(request, "user", None)
    return {
        "CLINIC_NAME": settings.CLINIC_NAME,
        "CLINIC_ADDRESS": settings.CLINIC_ADDRESS,
        "CLINIC_PHONE": settings.CLINIC_PHONE,
        # Per-section view/edit flags for navigation and widgets (see accounts/rbac.py).
        "nav": section_access(user) if user is not None and user.is_authenticated else {},
    }
