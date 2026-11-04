"""Patient attachments: old case sheets migrated from the paper / previous system."""
import io
import zipfile
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role
from clinic import models as m
from clinic.tests import MEDIA, PDF, ClinicTestCase
from core.models import AuditLog

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def docx_bytes(member="word/document.xml"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, "<w:document/>")
    return buffer.getvalue()


def upload(name="sheet.pdf", content=PDF, content_type="application/pdf"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class AttachmentTestCase(ClinicTestCase):
    def make_attachment(self, category=m.PatientAttachment.Category.OLD_CASE_SHEET, name="old.pdf", **extra):
        return m.PatientAttachment.objects.create(
            patient=self.patient, file=upload(name), original_filename=name, file_size=len(PDF),
            content_type="application/pdf", category=category, **extra,
        )

    def post_upload(self, *files, category="OLD_CASE_SHEET", **extra):
        return self.client.post(reverse("attachment_upload", args=[self.patient.pk]),
                                {"attachment_files": list(files), "category": category, **extra})

    def api(self, role):
        client = APIClient()
        client.force_authenticate(self.users[role])
        return client


# ============================================================================ model & storage
class AttachmentModelTests(AttachmentTestCase):
    def test_upload_path_uses_patient_id_and_random_name(self):
        attachment = self.make_attachment(name="Ramesh Kumar old sheet.pdf")
        path = Path(attachment.file.name)
        self.assertEqual(path.parts[:2], ("case_sheets", str(self.patient.pk)))
        self.assertEqual(path.suffix, ".pdf")
        self.assertNotIn("Ramesh", attachment.file.name)
        self.assertEqual(len(path.stem), 32)

    def test_soft_delete_keeps_row_and_file(self):
        attachment = self.make_attachment()
        stored = Path(MEDIA) / attachment.file.name
        attachment.delete()
        self.assertFalse(m.PatientAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(m.PatientAttachment.all_objects.get(pk=attachment.pk).is_deleted)
        self.assertTrue(stored.exists(), "soft delete must never remove the medical record file")

    def test_uploader_and_record_date(self):
        self.login(Role.ATTENDER)
        self.post_upload(upload(), document_date=(timezone.localdate() - timedelta(days=900)).isoformat())
        attachment = self.patient.attachments.get()
        self.assertEqual(attachment.uploaded_by, self.users[Role.ATTENDER])
        self.assertEqual(attachment.record_date, attachment.document_date)
        self.assertEqual(attachment.content_type, "application/pdf")


# ============================================================================ validation
class AttachmentValidationTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.login(Role.DOCTOR)

    def messages(self, response):
        return [str(msg) for msg in response.wsgi_request._messages]

    def test_accepts_pdf_jpg_png_and_word_in_one_upload(self):
        response = self.post_upload(upload("a.pdf"), upload("b.jpg", JPG, "image/jpeg"), upload("c.png", PNG, "image/png"),
                                    upload("d.docx", docx_bytes(), "application/octet-stream"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.patient.attachments.count(), 4)
        types = set(self.patient.attachments.values_list("content_type", flat=True))
        self.assertIn("application/vnd.openxmlformats-officedocument.wordprocessingml.document", types)

    def test_rejects_unsupported_extension(self):
        response = self.post_upload(upload("notes.txt", b"hello", "text/plain"))
        self.assertEqual(self.patient.attachments.count(), 0)
        self.assertIn("not a supported file type", " ".join(self.messages(response)))

    def test_rejects_renamed_executable_and_fake_word_file(self):
        self.post_upload(upload("sheet.pdf", b"MZ\x90\x00 fake", "application/pdf"))
        response = self.post_upload(upload("sheet.docx", docx_bytes("evil/payload.bin"), "application/msword"))
        self.assertEqual(self.patient.attachments.count(), 0)
        self.assertIn("does not look like a genuine Word file", " ".join(self.messages(response)))

    @override_settings(ATTACHMENT_MAX_UPLOAD_MB=1)
    def test_rejects_file_over_size_setting(self):
        big = PDF + b"0" * (1024 * 1024)
        response = self.post_upload(upload("big.pdf", big))
        self.assertEqual(self.patient.attachments.count(), 0)
        self.assertIn("over the 1 MB limit", " ".join(self.messages(response)))

    def test_rejects_empty_file_missing_file_future_date_and_too_many(self):
        self.post_upload(upload("empty.pdf", b""))
        self.post_upload()
        self.post_upload(upload(), document_date=(timezone.localdate() + timedelta(days=3)).isoformat())
        with self.settings(ATTACHMENT_MAX_FILES=2):
            response = self.post_upload(upload("1.pdf"), upload("2.pdf"), upload("3.pdf"))
        self.assertIn("up to 2 files", " ".join(self.messages(response)))
        self.assertEqual(self.patient.attachments.count(), 0)

    def test_one_bad_file_rejects_the_whole_batch(self):
        self.post_upload(upload("good.pdf"), upload("bad.png", b"not an image", "image/png"))
        self.assertEqual(self.patient.attachments.count(), 0)


# ============================================================================ permissions
class AttachmentPermissionTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.sheet = self.make_attachment()
        self.lab_report = self.make_attachment(category=m.PatientAttachment.Category.LAB_REPORT, name="cbc.pdf")
        self.view_url = reverse("attachment_file", args=[self.sheet.pk])

    def test_doctor_and_admin_can_view_download_and_delete(self):
        for role in (Role.DOCTOR, Role.ADMIN):
            self.login(role)
            response = self.client.get(self.view_url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response["Content-Disposition"].startswith("inline"))
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(b"".join(response.streaming_content), PDF)
            download = self.client.get(self.view_url + "?download=1")
            self.assertTrue(download["Content-Disposition"].startswith("attachment"))
        self.client.post(reverse("attachment_delete", args=[self.sheet.pk]))
        self.assertTrue(m.PatientAttachment.all_objects.get(pk=self.sheet.pk).is_deleted)

    def test_reception_can_upload_and_view_but_not_delete(self):
        self.login(Role.ATTENDER)
        self.assertEqual(self.client.get(self.view_url).status_code, 200)
        self.post_upload(upload())
        self.assertEqual(self.patient.attachments.count(), 3)
        self.assertEqual(self.client.post(reverse("attachment_delete", args=[self.sheet.pk])).status_code, 403)
        self.assertFalse(m.PatientAttachment.all_objects.get(pk=self.sheet.pk).is_deleted)

    def test_lab_sees_and_uploads_lab_reports_only(self):
        self.login(Role.LAB)
        self.assertEqual(self.client.get(reverse("attachment_file", args=[self.lab_report.pk])).status_code, 200)
        self.assertEqual(self.client.get(self.view_url).status_code, 404, "old case sheet must be invisible to lab")
        self.post_upload(upload(), category="OLD_CASE_SHEET")
        self.post_upload(upload("lft.pdf"), category="LAB_REPORT")
        self.assertEqual(list(self.patient.attachments.values_list("category", flat=True).order_by("id")),
                         ["OLD_CASE_SHEET", "LAB_REPORT", "LAB_REPORT"])
        self.assertEqual(self.client.post(reverse("attachment_delete", args=[self.lab_report.pk])).status_code, 403)

    def test_anonymous_users_and_guessed_ids_are_blocked(self):
        response = self.client.get(self.view_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.login(Role.DOCTOR)
        self.assertEqual(self.client.get(reverse("attachment_file", args=[999999])).status_code, 404)
        self.sheet.delete()
        self.assertEqual(self.client.get(self.view_url).status_code, 404, "deleted files are not served")

    def test_direct_media_urls_are_never_served(self):
        for role in (Role.ADMIN, Role.DOCTOR, Role.ATTENDER, Role.LAB):
            self.login(role)
            response = self.client.get(f"/media/{self.sheet.file.name}")
            self.assertEqual(response.status_code, 403, role)

    def test_views_and_downloads_are_audited(self):
        self.login(Role.DOCTOR)
        self.client.get(self.view_url)
        self.client.get(self.view_url + "?download=1")
        logs = AuditLog.objects.filter(model_name="clinic.PatientAttachment", object_id=str(self.sheet.pk))
        self.assertTrue(logs.filter(action=AuditLog.Action.VIEW).exists())
        self.assertTrue(logs.filter(action=AuditLog.Action.EXPORT).exists())


# ============================================================================ screens
class AttachmentScreenTests(AttachmentTestCase):
    def registration_data(self, **extra):
        return {
            "patient-registration_date": timezone.localdate().isoformat(), "patient-full_name": "Migrated Patient",
            "patient-age": "52", "patient-gender": "M", "patient-phone": "9000000001",
            "visit-visit_date": timezone.localdate().isoformat(), "visit-doctor": str(self.users[Role.DOCTOR].pk),
            "action": "save_only", "has_family": "no", **extra,
        }

    def test_reception_attaches_old_case_sheets_while_registering(self):
        self.login(Role.ATTENDER)
        page = self.client.get(reverse("patient_create")).content.decode()
        self.assertIn("Attach old case sheet", page)
        response = self.client.post(reverse("patient_create"), self.registration_data(
            attachment_files=[upload("2019.pdf"), upload("2020.jpg", JPG, "image/jpeg")]))
        patient = m.Patient.objects.get(full_name="Migrated Patient")
        self.assertRedirects(response, reverse("patient_detail", args=[patient.pk]))
        self.assertEqual(sorted(patient.attachments.values_list("category", flat=True)), ["OLD_CASE_SHEET"] * 2)

    def test_invalid_old_sheet_stops_registration_with_a_clear_message(self):
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("patient_create"), self.registration_data(
            attachment_files=[upload("virus.pdf", b"MZ...", "application/pdf")]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(m.Patient.objects.filter(full_name="Migrated Patient").exists())
        self.assertContains(response, "does not look like a genuine PDF file")

    def test_patient_page_lists_attachments_with_actions(self):
        self.make_attachment(name="paper-1998.pdf", document_date=timezone.localdate() - timedelta(days=9000))
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("patient_detail", args=[self.patient.pk])).content.decode()
        self.assertIn('id="attachments"', html)
        self.assertIn("paper-1998.pdf", html)
        self.assertIn("Old Case Sheet", html)
        self.assertIn("?download=1", html)
        self.assertIn(reverse("attachment_upload", args=[self.patient.pk]), html)
        attachment = self.patient.attachments.get()
        self.assertIn(reverse("attachment_delete", args=[attachment.pk]), html)

    def test_reception_page_has_no_delete_button_and_empty_state_is_friendly(self):
        self.login(Role.ATTENDER)
        html = self.client.get(reverse("patient_detail", args=[self.patient.pk])).content.decode()
        self.assertIn("No old case sheets or documents attached yet.", html)
        attachment = self.make_attachment()
        html = self.client.get(reverse("patient_detail", args=[self.patient.pk])).content.decode()
        self.assertIn(reverse("attachment_file", args=[attachment.pk]), html)
        self.assertNotIn(reverse("attachment_delete", args=[attachment.pk]), html)

    def test_doctor_sees_attachments_on_consultation_screen(self):
        self.make_attachment(name="old-case-2015.pdf")
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[self.visit.pk])).content.decode()
        self.assertIn('id="tab-attachments"', html)
        self.assertIn("old-case-2015.pdf", html)

    def test_lab_patient_reports_page_shows_lab_reports_only(self):
        self.make_attachment(name="private-case.pdf")
        self.make_attachment(category=m.PatientAttachment.Category.LAB_REPORT, name="outside-cbc.pdf")
        self.login(Role.LAB)
        html = self.client.get(reverse("lab_patient_reports", args=[self.patient.pk])).content.decode()
        self.assertIn("outside-cbc.pdf", html)
        self.assertNotIn("private-case.pdf", html)


# ============================================================================ API
class AttachmentApiTests(AttachmentTestCase):
    def test_upload_list_download_delete(self):
        api = self.api(Role.DOCTOR)
        response = api.post("/api/attachments/", {"patient": self.patient.pk, "category": "OLD_CASE_SHEET",
                                                  "document_date": "2018-05-01", "files": [upload("a.pdf"), upload("b.pdf")]},
                            format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()), 2)
        first = response.json()[0]
        self.assertEqual(first["category_display"], "Old Case Sheet")
        self.assertNotIn("file", first, "the raw storage path must not be exposed")
        listing = api.get(f"/api/attachments/?patient={self.patient.pk}").json()
        self.assertEqual(listing["count"], 2)
        file_response = api.get(first["download_url"])
        self.assertEqual(file_response.status_code, 200)
        self.assertTrue(file_response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(api.delete(f"/api/attachments/{first['id']}/").status_code, 204)
        self.assertTrue(m.PatientAttachment.all_objects.get(pk=first["id"]).is_deleted)

    def test_invalid_files_are_rejected_politely(self):
        response = self.api(Role.DOCTOR).post(
            "/api/attachments/", {"patient": self.patient.pk, "files": [upload("x.pdf", b"nope")]}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not look like a genuine PDF file", response.json()["files"][0])

    def test_role_rules_match_the_screens(self):
        sheet = self.make_attachment()
        report = self.make_attachment(category=m.PatientAttachment.Category.LAB_REPORT, name="r.pdf")
        lab = self.api(Role.LAB)
        self.assertEqual([row["id"] for row in lab.get("/api/attachments/").json()["results"]], [report.pk])
        self.assertEqual(lab.get(f"/api/attachments/{sheet.pk}/file/").status_code, 404)
        response = lab.post("/api/attachments/", {"patient": self.patient.pk, "category": "OLD_CASE_SHEET",
                                                  "files": [upload()]}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(lab.delete(f"/api/attachments/{report.pk}/").status_code, 403)
        reception = self.api(Role.ATTENDER)
        self.assertEqual(reception.delete(f"/api/attachments/{sheet.pk}/").status_code, 403)
        # Anonymous: DRF answers 403 because session auth (listed first) sends no WWW-Authenticate challenge.
        self.assertIn(APIClient().get(f"/api/attachments/{sheet.pk}/file/").status_code, (401, 403))

    def test_patient_serializer_includes_attachment_count(self):
        self.make_attachment()
        self.make_attachment(category=m.PatientAttachment.Category.LAB_REPORT, name="r.pdf")
        doctor_row = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/").json()
        self.assertEqual(doctor_row["attachment_count"], 2)
        listing = self.api(Role.ATTENDER).get("/api/patients/").json()["results"]
        self.assertEqual(listing[0]["attachment_count"], 2)
