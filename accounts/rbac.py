"""Single source of truth for role-based access to every case-record section.

Both the HTML views and the REST API consult this table, so a rule changed
here changes everywhere.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Role

A, D, AT, L = Role.ADMIN, Role.DOCTOR, Role.ATTENDER, Role.LAB
ALL = frozenset({A, D, AT, L})
CLINICAL = frozenset({A, D})

# section -> (roles that can view, roles that can edit)
SECTION_RULES = {
    "patient": (ALL, {A, D, AT}),
    "visit": (ALL, {A, D, AT}),
    "case_history": (ALL, CLINICAL),
    "physical_generals": (ALL, CLINICAL),
    "mental_generals": (CLINICAL, CLINICAL),
    "examination": (ALL, {A, D, AT}),
    "diagnosis": ({A, D, L}, CLINICAL),
    "investigations": (ALL, {A, D, AT}),
    "investigation_reports": (ALL, {A, D, AT, L}),
    "past_treatment": (ALL, {A, D, AT}),
    "patient_images": ({A, D, AT}, CLINICAL),
    "repertory": (CLINICAL, CLINICAL),
    "prescription": (CLINICAL, CLINICAL),
    "followups": (CLINICAL, CLINICAL),
}

# Sections expanded when the case record opens, per role.
DEFAULT_OPEN = {
    A: {"patient", "case_history", "mental_generals", "diagnosis", "prescription"},
    D: {"patient", "case_history", "mental_generals", "diagnosis", "prescription"},
    AT: {"patient", "examination", "investigations"},
    L: {"patient", "investigations"},
}

# Top-level records only an administrator may delete.
ADMIN_ONLY_DELETE = {"patient", "visit"}


def role_of(user):
    if user is None or not user.is_authenticated:
        return None
    return user.effective_role


def can_view(user, section):
    return role_of(user) in SECTION_RULES[section][0]


def can_edit(user, section):
    return role_of(user) in SECTION_RULES[section][1]


def can_delete(user, section):
    """Admins delete anything; doctors may remove clinical child rows and uploads."""
    role = role_of(user)
    if role == A:
        return True
    return role == D and section not in ADMIN_ONLY_DELETE and can_edit(user, section)


def section_access(user):
    role = role_of(user)
    open_sections = DEFAULT_OPEN.get(role, set())
    return {
        name: {
            "view": can_view(user, name),
            "edit": can_edit(user, name),
            "delete": can_delete(user, name),
            "open": name in open_sections,
        }
        for name in SECTION_RULES
    }


def has_role(user, *roles):
    return role_of(user) in roles


def role_required(*roles):
    """View decorator: allow only the given roles (login is checked first)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not has_role(request.user, *roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
