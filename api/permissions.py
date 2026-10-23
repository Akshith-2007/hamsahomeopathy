from rest_framework.permissions import SAFE_METHODS, BasePermission

from accounts.models import Role
from accounts.rbac import can_delete, can_edit, can_view, has_role


class SectionPermission(BasePermission):
    """Map HTTP verbs onto the RBAC table using the view's `section`."""

    def has_permission(self, request, view):
        user, section = request.user, view.section
        if request.method in SAFE_METHODS:
            return can_view(user, section)
        if request.method == "DELETE":
            return can_delete(user, section)
        return can_edit(user, section)


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return has_role(request.user, Role.ADMIN)
