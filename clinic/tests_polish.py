"""Professional finish: friendly error pages, no technical details for users, consistent wording."""
from unittest import mock

from django.test import RequestFactory
from django.urls import reverse

from accounts.models import Role
from clinic.tests import ClinicTestCase
from core import views as core_views


class ErrorPageTests(ClinicTestCase):
    def test_server_error_page_is_friendly_and_has_no_technical_details(self):
        response = core_views.server_error(RequestFactory().get("/anything"))
        body = response.content.decode()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something went wrong on our side", body)
        self.assertIn("Hamsa Homeopathy", body)
        self.assertNotIn("Traceback", body)

    def test_server_error_page_survives_a_broken_template(self):
        with mock.patch.object(core_views.loader, "get_template", side_effect=RuntimeError("boom")):
            response = core_views.server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom", response.content.decode())

    def test_unhandled_errors_render_the_friendly_page(self):
        self.login(Role.DOCTOR)
        self.client.raise_request_exception = False
        with mock.patch("clinic.views.PatientHistory", side_effect=RuntimeError("secret detail")):
            response = self.client.get(reverse("patient_history", args=[self.patient.pk]))
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Something went wrong on our side", status_code=500)
        self.assertNotContains(response, "secret detail", status_code=500)

    def test_readiness_probe_does_not_leak_database_errors(self):
        with mock.patch.object(core_views.connection, "cursor", side_effect=RuntimeError("password=hunter2 host=db")):
            response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_health_endpoints(self):
        for path in ("/health", "/healthz", "/readyz"):
            self.assertEqual(self.client.get(path, HTTP_HOST="10.1.2.3").status_code, 200, path)


class WordingTests(ClinicTestCase):
    def test_case_sheet_wording_and_clinic_name_in_titles(self):
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[self.visit.pk])).content.decode()
        self.assertIn(f"<title>Case sheet – {self.patient.full_name} · Hamsa Homeopathy</title>", html)
        self.assertIn("Homeopathic Case Sheet", html)
        self.assertIn("Save case sheet", html)
        self.assertNotIn("Case Record", html)
        self.assertNotIn("case record", html)
        page = self.client.get(reverse("patient_list")).content.decode()
        self.assertIn("· Hamsa Homeopathy</title>", page)
        printed = self.client.get(reverse("prescription_print", args=[self.visit.pk])).content.decode()
        self.assertIn("Back to case sheet", printed)
        self.assertIn("Hamsa Homeopathy", printed)
