import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.rbac import can_delete, can_edit, can_view
from clinic import models as m
from core.models import AuditLog

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix="hamsa-test-media-")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def management(prefix, initial=0, total=None):
    return {
        f"{prefix}-TOTAL_FORMS": str(total if total is not None else initial),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }


@override_settings(MEDIA_ROOT=MEDIA)
class ClinicTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.users = {
            role: User.objects.create_user(username=role.lower(), password="Secret#12345", role=role)
            for role in (Role.ADMIN, Role.DOCTOR, Role.ATTENDER, Role.LAB)
        }
        self.patient = m.Patient.objects.create(full_name="Test Patient", age=30, gender="F", phone="9876543210")
        self.visit = m.Visit.objects.create(patient=self.patient, doctor=self.users[Role.DOCTOR])

    def login(self, role):
        self.client.force_login(self.users[role])

    def record_post(self, extra=None):
        """Minimal valid POST for an empty case record."""
        data = {
            "patient-registration_date": self.patient.registration_date.isoformat(),
            "patient-full_name": self.patient.full_name,
            "patient-age": str(self.patient.age),
            "patient-gender": self.patient.gender,
            "patient-phone": self.patient.phone,
            "visit-visit_date": self.visit.visit_date.isoformat(),
            "visit-visit_type": self.visit.visit_type,
            "visit-doctor": str(self.users[Role.DOCTOR].pk),
            **management("rep"), **management("rx"), **management("fu"),
        }
        data.update(extra or {})
        return data


class RbacMatrixTests(ClinicTestCase):
    def test_section_rules(self):
        doctor, attender, lab, admin = (self.users[r] for r in (Role.DOCTOR, Role.ATTENDER, Role.LAB, Role.ADMIN))
        self.assertTrue(can_view(doctor, "mental_generals"))
        self.assertFalse(can_view(attender, "mental_generals"))
        self.assertFalse(can_view(lab, "prescription"))
        self.assertTrue(can_view(lab, "diagnosis"))
        self.assertFalse(can_edit(lab, "diagnosis"))
        self.assertTrue(can_edit(lab, "investigation_reports"))
        self.assertTrue(can_edit(attender, "examination"))
        self.assertFalse(can_edit(attender, "case_history"))
        self.assertTrue(can_delete(admin, "patient"))
        self.assertFalse(can_delete(doctor, "patient"))
        self.assertFalse(can_delete(attender, "investigation_reports"))


class CaseRecordViewTests(ClinicTestCase):
    def test_sections_hidden_by_role(self):
        url = reverse("visit_record", args=[self.visit.pk])
        self.login(Role.ATTENDER)
        html = self.client.get(url).content.decode()
        self.assertNotIn('id="sec-mental_generals"', html)
        self.assertNotIn('id="sec-prescription"', html)
        self.assertNotIn('id="sec-diagnosis"', html)

        self.login(Role.LAB)
        html = self.client.get(url).content.decode()
        self.assertNotIn('id="sec-mental_generals"', html)
        self.assertNotIn('id="sec-prescription"', html)
        self.assertIn('id="sec-diagnosis"', html)

        self.login(Role.DOCTOR)
        html = self.client.get(url).content.decode()
        self.assertIn('id="body-mental_generals" class="collapse show"', html)
        self.assertIn('id="body-prescription" class="collapse show"', html)

    def test_doctor_saves_full_record(self):
        self.login(Role.DOCTOR)
        data = self.record_post({
            "case_history-presenting_complaints": "Migraine",
            "mental_generals-fears": "Of dark",
            "diagnosis-final": "Migraine",
            "examination-blood_pressure": "120/80",
            **management("rx", total=2),
            "rx-0-medicine_name": "Nux Vomica", "rx-0-potency": "30C",
            "rx-1-medicine_name": "Sulphur", "rx-1-potency": "200C",
            **management("rep", total=1),
            "rep-0-rubric": "HEAD - PAIN", "rep-0-book": "KENT", "rep-0-grade": "2", "rep-0-remedies": "Nat-m:3",
            **management("fu", total=1),
            "fu-0-visit_date": "2026-10-23", "fu-0-progress_notes": "Better",
        })
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), data)
        self.assertEqual(response.status_code, 302, response.content.decode()[:2000])
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.mental_generals.fears, "Of dark")
        self.assertEqual(self.visit.prescription_summary, "Nux Vomica 30C, Sulphur 200C")
        self.assertEqual(self.visit.repertory.entries.count(), 1)
        self.assertEqual(self.visit.followups.count(), 1)
        self.assertEqual(self.visit.case_history.created_by, self.users[Role.DOCTOR])

    def test_attender_cannot_write_restricted_sections(self):
        self.login(Role.ATTENDER)
        data = self.record_post({
            "mental_generals-fears": "should be ignored",
            "case_history-presenting_complaints": "should be ignored",
            "examination-pulse": "72",
            **management("rx", total=1), "rx-0-medicine_name": "Ignored",
        })
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(m.MentalGeneral.objects.filter(visit=self.visit).exists())
        self.assertFalse(m.CaseHistory.objects.filter(visit=self.visit).exists())
        self.assertFalse(m.PrescriptionItem.objects.exists())
        self.assertEqual(m.Examination.objects.get(visit=self.visit).pulse, 72)

    def test_lab_uploads_report_but_cannot_edit_patient(self):
        m.Investigation.objects.create(visit=self.visit, request_notes="CBC")
        self.login(Role.LAB)
        data = self.record_post({"patient-full_name": "Hacked"})
        data["investigation_files"] = [SimpleUploadedFile("cbc.pdf", PDF, content_type="application/pdf")]
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.full_name, "Test Patient")
        investigation = self.visit.investigations.get()
        self.assertEqual(investigation.status, m.Investigation.Status.COMPLETED)
        self.assertEqual(investigation.reports.get().source, m.InvestigationReport.Source.LAB)

    def test_upload_with_fake_extension_rejected(self):
        self.login(Role.ATTENDER)
        data = self.record_post()
        data["investigation_files"] = [SimpleUploadedFile("evil.pdf", b"MZ\x90\x00binary", content_type="application/pdf")]
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not look like a .pdf file")
        self.assertFalse(m.InvestigationReport.objects.exists())

    def test_followup_visit_carries_forward_case(self):
        m.CaseHistory.objects.create(visit=self.visit, presenting_complaints="Eczema")
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("visit_create", args=[self.patient.pk]))
        new_visit = self.patient.visits.order_by("-id").first()
        self.assertRedirects(response, reverse("visit_record", args=[new_visit.pk]))
        self.assertEqual(new_visit.visit_type, m.Visit.Type.FOLLOW_UP)
        self.assertEqual(new_visit.case_history.presenting_complaints, "Eczema")

    def test_history_modal_respects_role(self):
        m.MentalGeneral.objects.create(visit=self.visit, fears="Secret fear")
        self.login(Role.LAB)
        response = self.client.get(reverse("visit_summary", args=[self.visit.pk]))
        self.assertNotContains(response, "Secret fear")
        self.login(Role.DOCTOR)
        response = self.client.get(reverse("visit_summary", args=[self.visit.pk]))
        self.assertContains(response, "Secret fear")


class AuditAndSoftDeleteTests(ClinicTestCase):
    def test_soft_delete_patient_admin_only(self):
        self.login(Role.ATTENDER)
        self.assertEqual(self.client.post(reverse("patient_delete", args=[self.patient.pk])).status_code, 403)
        self.login(Role.ADMIN)
        self.client.post(reverse("patient_delete", args=[self.patient.pk]))
        self.assertFalse(m.Patient.objects.filter(pk=self.patient.pk).exists())
        deleted = m.Patient.all_objects.get(pk=self.patient.pk)
        self.assertTrue(deleted.is_deleted)
        self.assertEqual(deleted.deleted_by, self.users[Role.ADMIN])
        self.assertFalse(m.Visit.objects.filter(pk=self.visit.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="DELETE", model_name="clinic.Patient").exists())

    def test_update_is_audited_with_diff(self):
        self.login(Role.DOCTOR)
        self.client.post(reverse("visit_record", args=[self.visit.pk]), self.record_post({"diagnosis-final": "Asthma"}))
        self.client.post(reverse("visit_record", args=[self.visit.pk]), self.record_post({"diagnosis-final": "Bronchial asthma"}))
        log = AuditLog.objects.filter(action="UPDATE", model_name="clinic.Diagnosis").latest("timestamp")
        self.assertEqual(log.changes["final"], {"old": "Asthma", "new": "Bronchial asthma"})
        self.assertEqual(log.user, self.users[Role.DOCTOR])

    def test_opd_numbers_are_sequential(self):
        second = m.Patient.objects.create(full_name="B", age=1, gender="M", phone="9876543211")
        first_no = int(self.patient.opd_number.rsplit("-", 1)[1])
        self.assertEqual(int(second.opd_number.rsplit("-", 1)[1]), first_no + 1)


class SecurityTests(ClinicTestCase):
    def test_account_locks_after_failed_logins(self):
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "doctor", "password": "wrong"})
        self.users[Role.DOCTOR].refresh_from_db()
        self.assertTrue(self.users[Role.DOCTOR].is_locked)
        response = self.client.post(reverse("login"), {"username": "doctor", "password": "Secret#12345"})
        self.assertContains(response, "locked")

    def test_media_requires_permission(self):
        analysis = m.RepertoryAnalysis.objects.create(
            visit=self.visit, sheet=SimpleUploadedFile("sheet.pdf", PDF, content_type="application/pdf"))
        url = analysis.sheet.url
        self.assertEqual(self.client.get(url).status_code, 302)  # anonymous -> login
        self.login(Role.LAB)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(Role.DOCTOR)
        self.assertEqual(self.client.get(url).status_code, 200)


class ApiTests(ClinicTestCase):
    def api(self, role):
        client = APIClient()
        client.force_authenticate(self.users[role])
        return client

    def test_visit_detail_filters_sections(self):
        m.MentalGeneral.objects.create(visit=self.visit, fears="x")
        url = f"/api/visits/{self.visit.pk}/"
        lab = self.api(Role.LAB).get(url).json()
        self.assertNotIn("mental_generals", lab)
        self.assertNotIn("prescriptions", lab)
        self.assertIn("diagnosis", lab)
        doctor = self.api(Role.DOCTOR).get(url).json()
        self.assertEqual(doctor["mental_generals"]["fears"], "x")

    def test_prescription_permissions_and_nested_items(self):
        payload = {"visit": self.visit.pk, "items": [{"medicine_name": "Sulphur", "potency": "30C"}]}
        self.assertEqual(self.api(Role.ATTENDER).post("/api/prescriptions/", payload, format="json").status_code, 403)
        response = self.api(Role.DOCTOR).post("/api/prescriptions/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["items"][0]["medicine_name"], "Sulphur")

    def test_patient_search_pagination(self):
        response = self.api(Role.ATTENDER).get("/api/patients/?search=987654&ordering=full_name")
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertIn("results", body)

    def test_token_login(self):
        response = APIClient().post("/api/auth/login/", {"username": "doctor", "password": "Secret#12345"}, format="json")
        token = response.json()["token"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.assertEqual(client.get("/api/auth/me/").json()["role"], Role.DOCTOR)

    def test_lab_report_upload_api(self):
        investigation = m.Investigation.objects.create(visit=self.visit, request_notes="LFT")
        upload = SimpleUploadedFile("lft.pdf", PDF, content_type="application/pdf")
        response = self.api(Role.LAB).post("/api/investigation-reports/", {"investigation": investigation.pk, "file": upload})
        self.assertEqual(response.status_code, 201, response.content)
        investigation.refresh_from_db()
        self.assertEqual(investigation.status, m.Investigation.Status.COMPLETED)

    def test_user_management_admin_only(self):
        self.assertEqual(self.api(Role.DOCTOR).get("/api/users/").status_code, 403)
        client = self.api(Role.ADMIN)
        target = self.users[Role.LAB]
        self.assertTrue(client.post(f"/api/users/{target.pk}/lock/").json()["is_locked"])
        self.assertFalse(client.post(f"/api/users/{target.pk}/unlock/").json()["is_locked"])
