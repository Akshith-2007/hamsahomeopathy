from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "last_name", "role", "is_active", "is_locked", "last_login")
    list_filter = ("role", "is_active", "is_locked")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Clinic", {"fields": ("role", "phone", "qualification", "registration_number", "signature",
                               "is_locked", "locked_at", "failed_login_attempts")}),
    )
