"""Create the first administrator from environment variables (for hosts without a shell, e.g. Render).

Uses DJANGO_ADMIN_USERNAME (default "admin") and DJANGO_ADMIN_PASSWORD. Does nothing if that
user already exists, so it is safe to run on every start and never resets a changed password.
"""
import os

from django.contrib.auth import get_user_model
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
        User.objects.create_superuser(username, os.environ.get("DJANGO_ADMIN_EMAIL", ""), password,
                                      role="ADMIN", first_name="Clinic", last_name="Admin")
        self.stdout.write(f"Created admin '{username}'.")
