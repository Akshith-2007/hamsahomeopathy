"""Fast "contains" search on patient name, phone and OPD No. (PostgreSQL trigram indexes).

Patient search uses icontains, which ordinary B-tree indexes cannot serve; pg_trgm GIN indexes can.
SQLite (tests, quick local runs) has no pg_trgm, so this migration does nothing there. If the database
user may not create the extension, the indexes are skipped with a warning instead of failing the deploy.
"""
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)

COLUMNS = ("full_name", "phone", "opd_number")


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    try:
        with transaction.atomic():
            schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as exc:  # noqa: BLE001 - managed hosts may forbid CREATE EXTENSION
        logger.warning("pg_trgm unavailable (%s); patient search works but without trigram indexes.", exc)
        return
    for column in COLUMNS:
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS patients_{column}_trgm ON patients USING gin (UPPER({column}) gin_trgm_ops)")


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for column in COLUMNS:
        schema_editor.execute(f"DROP INDEX IF EXISTS patients_{column}_trgm")


class Migration(migrations.Migration):
    dependencies = [("clinic", "0007_patient_attachments")]

    operations = [migrations.RunPython(create_indexes, drop_indexes)]
