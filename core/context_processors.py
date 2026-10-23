from django.conf import settings

from accounts.models import Role
from accounts.rbac import role_of


def app_context(request):
    role = role_of(getattr(request, "user", None))
    return {
        "CLINIC_NAME": settings.CLINIC_NAME,
        "CLINIC_ADDRESS": settings.CLINIC_ADDRESS,
        "CLINIC_PHONE": settings.CLINIC_PHONE,
        "user_role": role,
        "is_admin": role == Role.ADMIN,
        "is_doctor": role == Role.DOCTOR,
        "is_clinical": role in (Role.ADMIN, Role.DOCTOR),
        "can_register": role in (Role.ADMIN, Role.DOCTOR, Role.ATTENDER),
    }
