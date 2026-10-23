"""Block until the database accepts connections (used by the container entrypoint)."""
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Wait for the database to become available."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=60)

    def handle(self, *args, timeout, **options):
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection.ensure_connection()
                self.stdout.write("Database available.")
                return
            except OperationalError as exc:
                if time.monotonic() > deadline:
                    raise CommandError(f"Database not available after {timeout}s: {exc}") from exc
                self.stdout.write("Waiting for database…")
                time.sleep(2)
