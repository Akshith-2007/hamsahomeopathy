"""Create the first administrator from environment variables (run by the container entrypoint on every start).

Uses DJANGO_ADMIN_USERNAME (default "admin") and DJANGO_ADMIN_PASSWORD. Does nothing if that
user already exists, so it is safe to run on every start and never resets a changed password.
"""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the first admin user from DJANGO_ADMIN_USERNAME / DJANGO_ADMIN_PASSWORD if missing."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_ADMIN_USERNAME", "admin")
        password = os.environ.get("DJANGO_ADMIN_PASSWORD", "")
        if not password:
            self.stdout.write("DJANGO_ADMIN_PASSWORD not set; skipping admin creation.")
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin '{username}' already exists.")
            return
        try:
            validate_password(password, User(username=username, role="ADMIN"))
        except ValidationError as exc:
            # Don't stop the app from starting; explain in the log why no admin was created.
            self.stderr.write(f"DJANGO_ADMIN_PASSWORD is too weak, admin not created: {' '.join(exc.messages)}")
            return
        User.objects.create_superuser(username, os.environ.get("DJANGO_ADMIN_EMAIL", ""), password,
                                      role="ADMIN", first_name="Clinic", last_name="Admin")
        self.stdout.write(f"Created admin '{username}'.")
