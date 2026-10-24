from rest_framework.permissions import SAFE_METHODS, BasePermission

from accounts.rbac import can_delete, can_edit, can_view


class SectionPermission(BasePermission):
    """Map HTTP verbs onto the RBAC table using the view's `section`."""

    def has_permission(self, request, view):
        user, section = request.user, view.section
        if request.method in SAFE_METHODS:
            return can_view(user, section)
        if request.method == "DELETE":
            return can_delete(user, section)
        return can_edit(user, section)


def section_permission(section, action="view"):
    """Permission class requiring access to a fixed RBAC section (for APIViews/actions)."""
    checker = can_edit if action == "edit" else can_view

    class _SectionAccess(BasePermission):
        def has_permission(self, request, view):
            return checker(request.user, section)

    _SectionAccess.__name__ = f"SectionAccess_{section}_{action}"
    return _SectionAccess


IsAdminRole = section_permission("user_admin", "edit")
