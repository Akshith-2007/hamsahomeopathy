from django.contrib.auth.models import AbstractUser
from django.db import models

from core.validators import image_validators


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Administrator"
    DOCTOR = "DOCTOR", "Doctor"
    ATTENDER = "ATTENDER", "Attender / Receptionist"
    LAB = "LAB", "Lab Assistant"


class User(AbstractUser):
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.ATTENDER, db_index=True)
    phone = models.CharField(max_length=20, blank=True)
    registration_number = models.CharField(
        max_length=50, blank=True, help_text="Medical council registration number (printed on prescriptions)."
    )
    qualification = models.CharField(max_length=120, blank=True)
    signature = models.ImageField(
        upload_to="signatures/", blank=True, validators=image_validators(2),
        help_text="Scanned signature used on printed prescriptions.",
    )
    is_locked = models.BooleanField(default=False, help_text="Locked users cannot sign in.")
    locked_at = models.DateTimeField(null=True, blank=True)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "users"
        ordering = ["first_name", "last_name", "username"]

    @property
    def effective_role(self):
        return Role.ADMIN if self.is_superuser else self.role

    @property
    def display_name(self):
        name = self.get_full_name()
        if self.effective_role == Role.DOCTOR and name:
            return f"Dr. {name}"
        return name or self.username

    def __str__(self):
        return self.display_name
