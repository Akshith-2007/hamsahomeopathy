from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import AuditLog

from .forms import LoginForm, PasswordResetByAdminForm, UserCreateForm, UserUpdateForm
from .models import Role
from .rbac import role_required

User = get_user_model()
admin_only = role_required(Role.ADMIN)


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    template_name = "registration/login.html"
    redirect_authenticated_user = True


@admin_only
def user_list(request):
    users = User.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        users = users.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    if role := request.GET.get("role"):
        users = users.filter(role=role)
    page = Paginator(users, 25).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html", {"page": page, "q": q, "roles": Role.choices})


@admin_only
def user_create(request):
    form = UserCreateForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        AuditLog.record(AuditLog.Action.CREATE, user, changes={"role": user.role})
        messages.success(request, f"User {user.username} created.")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": "New user"})


@admin_only
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    form = UserUpdateForm(request.POST or None, request.FILES or None, instance=target)
    if request.method == "POST" and form.is_valid():
        changed = {name: str(form.cleaned_data[name]) for name in form.changed_data if name != "signature"}
        form.save()
        AuditLog.record(AuditLog.Action.UPDATE, target, changes=changed)
        messages.success(request, f"User {target.username} updated.")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": f"Edit {target.username}", "target": target})


@admin_only
def user_password(request, pk):
    target = get_object_or_404(User, pk=pk)
    form = PasswordResetByAdminForm(target, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        target.is_locked = False
        target.failed_login_attempts = 0
        target.save(update_fields=["is_locked", "failed_login_attempts"])
        AuditLog.record(AuditLog.Action.PASSWORD_RESET, target)
        messages.success(request, f"Password reset for {target.username}; the account is unlocked.")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": f"Reset password – {target.username}", "target": target})


@admin_only
@require_POST
def user_toggle_lock(request, pk):
    target = get_object_or_404(User, pk=pk)
    if target == request.user:
        messages.error(request, "You cannot lock your own account.")
        return redirect("user_list")
    target.is_locked = not target.is_locked
    target.locked_at = timezone.now() if target.is_locked else None
    target.failed_login_attempts = 0
    target.save(update_fields=["is_locked", "locked_at", "failed_login_attempts"])
    AuditLog.record(AuditLog.Action.LOCK if target.is_locked else AuditLog.Action.UNLOCK, target)
    messages.success(request, f"{target.username} {'locked' if target.is_locked else 'unlocked'}.")
    return redirect("user_list")


@admin_only
@require_POST
def user_delete(request, pk):
    target = get_object_or_404(User, pk=pk)
    if target == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user_list")
    AuditLog.record(AuditLog.Action.DELETE, target)
    # Deactivate instead of dropping the row: clinical records keep pointing at it.
    target.is_active = False
    target.is_locked = True
    target.username = f"deleted-{target.pk}-{target.username}"[:150]
    target.save(update_fields=["is_active", "is_locked", "username"])
    messages.success(request, "User deleted.")
    return redirect("user_list")
