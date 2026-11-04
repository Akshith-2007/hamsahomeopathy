"""Production hardening: sign-in throttling, safe first admin, security headers."""
import io
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from clinic.tests import ClinicTestCase

User = get_user_model()


class SignInThrottleTests(ClinicTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    @override_settings(LOGIN_RATE_LIMIT=3)
    def test_too_many_failed_sign_ins_from_one_device_are_refused(self):
        url = reverse("login")
        for n in range(3):
            # Different (non-existent) usernames: the per-account lock would never trigger.
            self.client.post(url, {"username": f"guess{n}", "password": "wrong"})
        response = self.client.post(url, {"username": "doctor", "password": "Secret#12345"})
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many sign-in attempts from this device", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(LOGIN_RATE_LIMIT=3)
    def test_successful_sign_in_clears_the_counter(self):
        url = reverse("login")
        self.client.post(url, {"username": "x", "password": "wrong"})
        self.client.post(url, {"username": "x", "password": "wrong"})
        self.assertEqual(self.client.post(url, {"username": "doctor", "password": "Secret#12345"}).status_code, 302)
        self.client.logout()
        self.client.post(url, {"username": "x", "password": "wrong"})
        self.assertEqual(self.client.post(url, {"username": "doctor", "password": "Secret#12345"}).status_code, 302)

    def test_api_login_is_rate_limited(self):
        client = APIClient()
        with mock.patch("rest_framework.throttling.ScopedRateThrottle.THROTTLE_RATES", {"login": "3/min"}):
            codes = [client.post("/api/auth/login/", {"username": "nobody", "password": "x"}).status_code
                     for _ in range(5)]
        self.assertEqual(codes[:3], [400, 400, 400])
        self.assertEqual(codes[3:], [429, 429])


class EnsureAdminTests(ClinicTestCase):
    def run_command(self, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env):
            call_command("ensure_admin", stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def test_weak_password_is_refused_without_crashing(self):
        output = self.run_command(DJANGO_ADMIN_USERNAME="clinicadmin", DJANGO_ADMIN_PASSWORD="12345678")
        self.assertIn("too weak", output)
        self.assertFalse(User.objects.filter(username="clinicadmin").exists())

    def test_strong_password_creates_admin_once(self):
        self.run_command(DJANGO_ADMIN_USERNAME="clinicadmin", DJANGO_ADMIN_PASSWORD="Tulsi-Garden#2026")
        admin = User.objects.get(username="clinicadmin")
        self.assertEqual(admin.role, "ADMIN")
        self.assertIn("already exists", self.run_command(DJANGO_ADMIN_USERNAME="clinicadmin",
                                                         DJANGO_ADMIN_PASSWORD="Other#Pass2026"))
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("Tulsi-Garden#2026"), "an existing password is never reset")


class SecurityHeaderTests(ClinicTestCase):
    def test_pages_cannot_be_framed_and_referrer_is_limited(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
