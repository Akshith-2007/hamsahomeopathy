"""Hosting: signed-URL media from object storage, env-driven settings."""
import json
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
    """Import settings in a fresh interpreter with server-style environment variables."""

    # Blank values win over the developer's local .env (it only fills in unset variables).
    BLANK = ("DOMAIN", "SERVER_IP", "DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS", "DJANGO_SECURE_COOKIES",
             "DJANGO_SSL_REDIRECT", "DJANGO_BEHIND_TLS_PROXY", "HSTS_SECONDS", "HSTS_INCLUDE_SUBDOMAINS", "HSTS_PRELOAD")

    def run_settings(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "SQLITE"))}
        base.update({k: "" for k in self.BLANK})
        base.update({"DJANGO_SECRET_KEY": "x" * 60, "DJANGO_DEBUG": "false", "DB_ENGINE": "postgres", **env})
        code = ("import os, json; os.environ['DJANGO_SETTINGS_MODULE']='hamsa.settings';"
                "from django.conf import settings as s;"
                "print(json.dumps({'hosts': s.ALLOWED_HOSTS, 'csrf': s.CSRF_TRUSTED_ORIGINS,"
                "'cookies': s.SESSION_COOKIE_SECURE and s.CSRF_COOKIE_SECURE, 'redirect': s.SECURE_SSL_REDIRECT,"
                "'hsts': s.SECURE_HSTS_SECONDS, 'preload': s.SECURE_HSTS_PRELOAD,"
                "'proxy': bool(getattr(s, 'SECURE_PROXY_SSL_HEADER', None))}))")
        return subprocess.run([sys.executable, "-c", code], env=base, capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent)

    def settings_with(self, **env):
        out = self.run_settings(**env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_ip_mode_is_plain_http(self):
        result = self.settings_with(SERVER_IP="203.0.113.10")
        self.assertEqual(result["hosts"], ["203.0.113.10", "localhost", "127.0.0.1"])
        self.assertIn("http://203.0.113.10", result["csrf"])
        self.assertFalse(result["cookies"] or result["redirect"] or result["proxy"])
        self.assertEqual(result["hsts"], 0)

    def test_domain_mode_is_https_only(self):
        result = self.settings_with(DOMAIN="Example.com", SERVER_IP="203.0.113.10")
        self.assertEqual(result["hosts"], ["example.com", "www.example.com"])
        self.assertEqual(result["csrf"], ["https://example.com", "https://www.example.com"])
        self.assertTrue(result["cookies"] and result["redirect"] and result["proxy"])
        self.assertEqual(result["hsts"], 3600)
        self.assertFalse(result["preload"])

    def test_explicit_values_override_the_mode(self):
        result = self.settings_with(DOMAIN="example.com", DJANGO_ALLOWED_HOSTS="a.test", DJANGO_SSL_REDIRECT="false",
                                    HSTS_SECONDS="31536000")
        self.assertEqual(result["hosts"], ["a.test"])
        self.assertFalse(result["redirect"])
        self.assertEqual(result["hsts"], 31536000)

    def test_refuses_to_start_unsafely(self):
        debug_on_domain = self.run_settings(DOMAIN="example.com", DJANGO_DEBUG="true")
        self.assertNotEqual(debug_on_domain.returncode, 0)
        self.assertIn("DJANGO_DEBUG must be off", debug_on_domain.stderr)
        no_key = self.run_settings(DJANGO_SECRET_KEY="")
        self.assertNotEqual(no_key.returncode, 0)
        self.assertIn("Set DJANGO_SECRET_KEY", no_key.stderr)
