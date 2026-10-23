from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm

from core.forms import BootstrapMixin

User = get_user_model()

PROFILE_FIELDS = ["first_name", "last_name", "email", "phone", "role", "qualification", "registration_number",
                  "signature", "is_active"]


class LoginForm(BootstrapMixin, AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "locked": "This account is locked. Please contact the administrator.",
    }

    def clean(self):
        username = self.cleaned_data.get("username")
        if username and User.objects.filter(username=username, is_locked=True).exists():
            raise forms.ValidationError(self.error_messages["locked"], code="locked")
        return super().clean()


class UserCreateForm(BootstrapMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["username", *PROFILE_FIELDS]


class UserUpdateForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = PROFILE_FIELDS


class PasswordResetByAdminForm(BootstrapMixin, SetPasswordForm):
    pass
