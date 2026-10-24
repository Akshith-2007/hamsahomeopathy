"""Single source of truth for role-based access to every screen and section.

Both the HTML views and the REST API consult this table, so a rule changed
here changes everywhere. Views, templates and serializers must ask
`can_view` / `can_edit` (or use `section_required`) instead of comparing roles.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Role

A, D, AT, L = Role.ADMIN, Role.DOCTOR, Role.ATTENDER, Role.LAB
ALL = frozenset({A, D, AT, L})
STAFF = frozenset({A, D, AT})  # everyone who works with the case sheet
CLINICAL = frozenset({A, D})
ADMIN = frozenset({A})
NOBODY = frozenset()

# section -> (roles that can view, roles that can edit)
SECTION_RULES = {
    # --- patient & visit ------------------------------------------------------
    "patient_identity": (ALL, NOBODY),            # OPD No., name, age, sex
    "patient": (STAFF, STAFF),                    # contact details, address, photo
    "visit": (ALL, STAFF),                        # visit date / doctor / status
    "visit_type": (STAFF, CLINICAL),              # change New/Follow-up after check-in
    "checkin": (STAFF, STAFF),                    # New case / Follow-up check-in
    "consultation": (STAFF, CLINICAL),            # move a visit to In consultation / Completed
    "case_record": (STAFF, STAFF),                # the full case-sheet screen
    "visit_history": (STAFF, NOBODY),             # previous-visit grid + modal
    "history": (CLINICAL, CLINICAL),              # summary bar, timeline, previous prescriptions, past history summary
    "history_prefill": (STAFF, STAFF),            # pre-fill stable history (only in sections the user can edit)
    # --- clinical sections ----------------------------------------------------
    "case_history": (STAFF, CLINICAL),
    "physical_generals": (STAFF, CLINICAL),
    "mental_generals": (CLINICAL, CLINICAL),
    "followup_assessment": (CLINICAL, CLINICAL),  # changes since last visit, remedy response
    "examination": (STAFF, STAFF),
    "diagnosis": (CLINICAL, CLINICAL),
    "investigations": (ALL, STAFF),               # requested tests
    "investigation_reports": (ALL, ALL),          # upload / list reports
    "external_reports": (STAFF, STAFF),           # reports not produced by the clinic lab
    "past_treatment": (STAFF, STAFF),
    "patient_images": (STAFF, CLINICAL),
    "repertory": (CLINICAL, CLINICAL),
    "prescription": (CLINICAL, CLINICAL),
    "followups": (CLINICAL, CLINICAL),            # follow-up progress-notes grid
    # --- follow-up scheduling ---------------------------------------------------
    "followup_schedule": (STAFF, STAFF),          # date, time, status
    "followup_notes": (CLINICAL, CLINICAL),       # doctor's notes on a scheduled follow-up
    # --- family links -------------------------------------------------------------
    "relationships": (STAFF, STAFF),              # relatives (incl. inferred): view, link, confirm, reject
    "relationship_unreject": (ADMIN, ADMIN),      # undo a rejected inferred link
    "family_consult": (CLINICAL, CLINICAL),       # consult & prescribe for a relative (through family)
    "family_history": (CLINICAL, CLINICAL),       # relatives' diagnoses + "Insert into family history"
    # --- pharmacy (reception doubles as the dispensary) -----------------------------
    "pharmacy_send": (CLINICAL, CLINICAL),        # send / re-send a prescription to the pharmacy
    "pharmacy_items": ({A, AT}, {A, AT}),         # queue with medicine items, status updates, labels
    "pharmacy_status": (STAFF, NOBODY),           # order status badges (case record, today's patients)
    "notifications": (STAFF, STAFF),              # in-app notification bell
    # --- other screens ------------------------------------------------------------
    "lab_worklist": ({A, L}, {A, L}),             # lab dashboard (lab home) and lab visit view
    "lab_reports": ({A, D, L}, {A, D, L}),        # previous reports, per-patient report history, corrected upload
    "report_versions": (CLINICAL, NOBODY),        # see superseded report versions
    "reports": (CLINICAL, NOBODY),
    "user_admin": (ADMIN, ADMIN),
    "audit_log": (ADMIN, NOBODY),
}

# Panels expanded when the case record opens, per role (may include UI-only panels).
DEFAULT_OPEN = {
    A: {"patient", "case_history", "mental_generals", "diagnosis", "prescription", "previous_prescriptions",
        "followup_assessment", "compare", "family_history"},
    D: {"patient", "case_history", "mental_generals", "diagnosis", "prescription", "previous_prescriptions",
        "followup_assessment", "compare", "family_history"},
    AT: {"patient", "examination", "investigations"},
    L: {"investigations"},
}

# Where each role lands after signing in.
HOME_VIEW = {L: "lab_dashboard"}

# Top-level records only an administrator may delete.
ADMIN_ONLY_DELETE = {"patient", "visit", "investigation_reports"}


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


def can_view_report(user, report):
    """Lab staff see current lab-produced reports; doctors/admins also see superseded versions."""
    if getattr(report, "status", "CURRENT") == "SUPERSEDED" and not can_view(user, "report_versions"):
        return False
    if can_view(user, "external_reports"):
        return True
    return can_view(user, "investigation_reports") and report.source == "LAB"


def visible_report_sources(user):
    """Report sources the user may list, or None for no restriction."""
    if can_view(user, "external_reports"):
        return None
    return ["LAB"] if can_view(user, "investigation_reports") else []


def hides_superseded_reports(user):
    return not can_view(user, "report_versions")


def default_open(user):
    return set(DEFAULT_OPEN.get(role_of(user), set()))


def home_view_name(user):
    return HOME_VIEW.get(role_of(user), "home")


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


def _guard(check):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not check(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def section_required(section, action="view"):
    """View decorator: require view (or edit) access to an RBAC section."""
    checker = can_edit if action == "edit" else can_view
    return _guard(lambda user: checker(user, section))


def role_required(*roles):
    """Deprecated: prefer section_required so the rule lives in SECTION_RULES."""
    return _guard(lambda user: has_role(user, *roles))
