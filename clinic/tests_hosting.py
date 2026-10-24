"""Hosting adaptations: Vercel cron endpoint, signed-URL media from object storage, env-driven settings."""
import os
import subprocess
import sys
from pathlib import Path

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from accounts.models import Role
from clinic import models as m
from clinic.tests import MEDIA, PDF, ClinicTestCase


class SignedUrlStorage(FileSystemStorage):
    """Stands in for ProtectedS3Storage: local files, but exposes signed_url() like the S3 backend."""

    def signed_url(self, name):
        return f"https://bucket.example.com/{name}?X-Amz-Signature=test"


SIGNED_STORAGES = {
    "default": {"BACKEND": "clinic.tests_hosting.SignedUrlStorage", "OPTIONS": {"location": MEDIA}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class CronEndpointTests(ClinicTestCase):
    @override_settings(CRON_SECRET="s3cret")
    def test_requires_bearer_secret(self):
        url = reverse("cron_daily")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url, headers={"authorization": "Bearer wrong"}).status_code, 403)
        response = self.client.get(url, headers={"authorization": "Bearer s3cret"})
        self.assertEqual(response.json()["status"], "ok")

    @override_settings(CRON_SECRET="")
    def test_disabled_without_secret(self):
        self.assertEqual(self.client.get(reverse("cron_daily"), headers={"authorization": "Bearer "}).status_code, 403)


@override_settings(STORAGES=SIGNED_STORAGES)
class ObjectStorageMediaTests(ClinicTestCase):
    def test_access_checked_then_redirected_to_signed_url(self):
        investigation = m.Investigation.objects.create(visit=self.visit, request_notes="CBC")
        report = m.InvestigationReport.objects.create(investigation=investigation, source="LAB",
                                                      file=SimpleUploadedFile("r.pdf", PDF))
        self.login(Role.DOCTOR)
        response = self.client.get(report.file.url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://bucket.example.com/patients/"))
        self.login(Role.ATTENDER)
        sheet = m.RepertoryAnalysis.objects.create(visit=self.visit, sheet=SimpleUploadedFile("s.pdf", PDF))
        self.assertEqual(self.client.get(sheet.sheet.url).status_code, 403)   # role check still applies

    def test_path_traversal_rejected(self):
        self.login(Role.ADMIN)
        self.assertEqual(self.client.get("/media/patients/x/photo/../../../.env").status_code, 403)


class SettingsFromEnvironmentTests(ClinicTestCase):
    """Import settings in a fresh interpreter with host-style environment variables."""

    def settings_with(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("VERCEL", "RENDER", "DB_", "SQLITE", "DATABASE"))}
        # DB_ENGINE=postgres overrides the local .env (Wi-Fi hosting uses SQLite); Vercel has no .env.
        base.update({"DJANGO_SECRET_KEY": "x" * 60, "DJANGO_DEBUG": "false", "DB_ENGINE": "postgres", **env})
        code = ("import os, json; os.environ['DJANGO_SETTINGS_MODULE']='hamsa.settings';"
                "from django.conf import settings as s; d=s.DATABASES['default'];"
                "print(json.dumps({'hosts': s.ALLOWED_HOSTS, 'csrf': s.CSRF_TRUSTED_ORIGINS, 'opts': d.get('OPTIONS'),"
                "'age': d.get('CONN_MAX_AGE'), 'redirect': s.SECURE_SSL_REDIRECT, 'maxmb': s.MAX_REQUEST_MB,"
                "'proxy': bool(getattr(s, 'SECURE_PROXY_SSL_HEADER', None))}))")
        out = subprocess.run([sys.executable, "-c", code], env=base, capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent.parent, check=True)
        import json

        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_vercel_environment(self):
        result = self.settings_with(VERCEL="1", VERCEL_URL="hamsa-abc.vercel.app",
                                    VERCEL_PROJECT_PRODUCTION_URL="hamsa.vercel.app",
                                    DATABASE_URL="postgres://u:p@ep-x.ap-south-1.aws.neon.tech/db?sslmode=require")
        self.assertIn("hamsa.vercel.app", result["hosts"])
        self.assertIn("https://hamsa-abc.vercel.app", result["csrf"])
        self.assertEqual(result["opts"], {"sslmode": "require"})
        self.assertEqual(result["age"], 0)
        self.assertTrue(result["redirect"] and result["proxy"])
        self.assertAlmostEqual(result["maxmb"], 4.4)
