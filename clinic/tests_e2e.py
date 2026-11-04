"""End-to-end clinic workflows across roles (reception -> doctor -> pharmacy -> lab -> follow-up -> admin),
role blocking, and edge cases."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from clinic import models as m
from clinic.tests import PDF, management
from clinic.tests_attachments import JPG, AttachmentTestCase, upload
from core.models import AuditLog, Notification

User = get_user_model()


class JourneyMixin:
    def register(self, name="Savitri Devi", phone="9000012345", **extra):
        return self.client.post(reverse("patient_create"), {
            "patient-registration_date": timezone.localdate().isoformat(), "patient-full_name": name,
            "patient-age": "47", "patient-gender": "F", "patient-phone": phone,
            "visit-visit_date": timezone.localdate().isoformat(), "visit-doctor": str(self.users[Role.DOCTOR].pk),
            "action": "save_only", "has_family": "no", **extra,
        })

    def consultation_post(self, patient, visit, extra=None):
        data = {
            "patient-registration_date": patient.registration_date.isoformat(), "patient-full_name": patient.full_name,
            "patient-age": str(patient.age), "patient-gender": patient.gender, "patient-phone": patient.phone,
            "visit-visit_date": visit.visit_date.isoformat(), "visit-visit_type": visit.visit_type,
            "visit-doctor": str(self.users[Role.DOCTOR].pk),
            **management("rep"), **management("fu"), **management("rx", total=1),
            "case_history-presenting_complaints": "Recurrent headache, worse in sun",
            "mental_generals-fears": "Of being alone",
            "diagnosis-final": "Migraine",
            "investigations-request_notes": "CBC, TSH",
            "rx-0-medicine_name": "Natrum Muriaticum", "rx-0-potency": "200C", "rx-0-form": "PILLS",
            "rx-0-quantity": "1 dram", "rx-0-dose": "4 pills once a week", "rx-0-duration": "4 weeks",
            "prescription-followup_advice": "Come back after a month",
            "send_to_pharmacy": "complete",
        }
        data.update(extra or {})
        return data


class ClinicJourneyTests(JourneyMixin, AttachmentTestCase):
    def test_complete_journey_from_registration_to_follow_up(self):
        doctor = self.users[Role.DOCTOR]

        # 1. Reception registers a migrating patient with an old case sheet; the patient is queued for the doctor.
        self.login(Role.ATTENDER)
        response = self.register(attachment_files=[upload("1999-sheet.pdf"), upload("2005-sheet.jpg", JPG, "image/jpeg")])
        patient = m.Patient.objects.get(full_name="Savitri Devi")
        self.assertRedirects(response, reverse("patient_detail", args=[patient.pk]))
        self.assertEqual(patient.attachments.count(), 2)
        visit = patient.visits.get()
        self.assertEqual((visit.status, visit.doctor), (m.Visit.Status.WAITING, doctor))

        self.login(Role.DOCTOR)
        home = self.client.get(reverse("home")).content.decode()
        self.assertIn("Savitri Devi", home)
        self.assertIn(reverse("visit_record", args=[visit.pk]), home)

        # 2. Doctor opens the case sheet (moves to In consultation) and sees the old case sheets beside history.
        page = self.client.get(reverse("visit_record", args=[visit.pk])).content.decode()
        visit.refresh_from_db()
        self.assertEqual(visit.status, m.Visit.Status.IN_CONSULTATION)
        self.assertIn("1999-sheet.pdf", page)
        self.assertIn("2005-sheet.jpg", page)

        response = self.client.post(reverse("visit_record", args=[visit.pk]), self.consultation_post(patient, visit))
        self.assertEqual(response.status_code, 302, response.content.decode()[:1500])
        visit.refresh_from_db()
        self.assertEqual(visit.status, m.Visit.Status.COMPLETED)
        self.assertEqual(visit.diagnosis_summary, "Migraine")
        self.assertEqual(visit.prescription_summary, "Natrum Muriaticum 200C")
        follow_date = timezone.localdate() + timedelta(days=28)
        self.client.post(reverse("followup_schedule", args=[patient.pk]),
                         {"sched-scheduled_date": follow_date.isoformat(), "source_visit": visit.pk})
        self.assertEqual(patient.scheduled_followups.get().scheduled_date, follow_date)

        # 3. Reception (pharmacy) sees the prescription and dispenses it.
        order = m.DispenseOrder.objects.get(visit=visit)
        self.login(Role.ATTENDER)
        queue = self.client.get(reverse("pharmacy_queue")).content.decode()
        self.assertIn("Natrum Muriaticum", queue)
        self.assertNotIn("Migraine", queue, "the pharmacy queue must not show clinical data")
        for action in ("start-preparing", "mark-ready", "mark-dispensed"):
            self.client.post(reverse("pharmacy_action", args=[order.pk, action]))
        order.refresh_from_db()
        self.assertEqual(order.status, m.DispenseOrder.Status.DISPENSED)
        self.assertContains(self.client.get(reverse("pharmacy_label", args=[order.pk])), "Natrum Muriaticum")

        # 4. Lab sees the request, uploads the report; the doctor is notified and sees it in the history.
        investigation = visit.investigations.get()
        self.login(Role.LAB)
        self.assertContains(self.client.get(reverse("lab_dashboard")), "CBC, TSH")
        self.client.post(reverse("lab_upload", args=[investigation.pk]),
                         {"report_files": [SimpleUploadedFile("cbc.pdf", PDF)], "remarks": "Hb 11.2"})
        investigation.refresh_from_db()
        self.assertEqual(investigation.status, m.Investigation.Status.COMPLETED)
        self.assertTrue(Notification.objects.filter(recipient=doctor, kind=Notification.Kind.REPORT_READY).exists())
        self.login(Role.DOCTOR)
        self.assertContains(self.client.get(reverse("patient_history", args=[patient.pk])), "cbc.pdf")

        # 5. The patient returns: follow-up check-in links to the first visit; the doctor sees the full history.
        self.login(Role.ATTENDER)
        self.client.post(reverse("checkin_start", args=[patient.pk]), {"visit_type": "FOLLOW_UP", "open": "0"})
        follow_up = patient.visits.exclude(pk=visit.pk).get()
        self.assertEqual(follow_up.previous_visit, visit)
        self.assertEqual(patient.scheduled_followups.get().status, m.ScheduledFollowUp.Status.COMPLETED)
        self.login(Role.DOCTOR)
        page = self.client.get(reverse("visit_record", args=[follow_up.pk])).content.decode()
        for text in ("Migraine", "Natrum Muriaticum", "Come back after a month", "Imported / Old record", "1999-sheet.pdf"):
            self.assertIn(text, page)

        # 6. Admin manages users and roles and can view every record and the audit trail.
        self.login(Role.ADMIN)
        self.client.post(reverse("user_create"), {
            "username": "drsharma", "password1": "Clinic#Strong2026", "password2": "Clinic#Strong2026",
            "first_name": "Anil", "last_name": "Sharma", "role": Role.DOCTOR, "is_active": "on",
        })
        new_doctor = User.objects.get(username="drsharma")
        self.assertEqual(new_doctor.role, Role.DOCTOR)
        self.client.post(reverse("user_edit", args=[new_doctor.pk]), {
            "first_name": "Anil", "last_name": "Sharma", "role": Role.ATTENDER, "is_active": "on"})
        new_doctor.refresh_from_db()
        self.assertEqual(new_doctor.role, Role.ATTENDER)
        self.assertContains(self.client.get(reverse("patient_list")), "Savitri Devi")
        self.assertContains(self.client.get(reverse("visit_record", args=[visit.pk])), "Of being alone")
        self.assertEqual(self.client.get(reverse("audit_list")).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(model_name="clinic.PatientAttachment", action="CREATE").exists())


# ============================================================================ role blocking
class RoleBlockingTests(JourneyMixin, AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.sheet = self.make_attachment()
        self.report = m.InvestigationReport.objects.create(
            investigation=m.Investigation.objects.create(visit=self.visit, request_notes="CBC"),
            source="EXTERNAL", file=SimpleUploadedFile("outside.pdf", PDF))

    def status(self, role, method, name, *args, data=None):
        self.login(role)
        call = self.client.post if method == "post" else self.client.get
        return call(reverse(name, args=args), data or {}).status_code

    def test_reception_is_blocked_from_clinical_and_admin_actions(self):
        role = Role.ATTENDER
        self.assertEqual(self.status(role, "get", "user_list"), 403)
        self.assertEqual(self.status(role, "get", "audit_list"), 403)
        self.assertEqual(self.status(role, "get", "patient_history", self.patient.pk), 403)
        self.assertEqual(self.status(role, "get", "reports"), 403)
        self.assertEqual(self.status(role, "post", "patient_delete", self.patient.pk), 403)
        self.assertEqual(self.status(role, "post", "attachment_delete", self.sheet.pk), 403)
        html = self.client.get(reverse("visit_record", args=[self.visit.pk])).content.decode()
        self.assertNotIn('id="sec-prescription"', html)

    def test_lab_is_blocked_from_patient_records_case_sheets_and_files(self):
        role = Role.LAB
        self.assertEqual(self.status(role, "get", "patient_history", self.patient.pk), 403)
        self.assertEqual(self.status(role, "get", "pharmacy_queue"), 403)
        self.assertEqual(self.status(role, "get", "user_list"), 403)
        self.assertEqual(self.status(role, "get", "attachment_file", self.sheet.pk), 404)
        self.assertEqual(self.client.get(f"/media/{self.report.file.name}").status_code, 403,
                         "lab must not open reports that came from outside the clinic lab")
        self.assertRedirects(self.client.get(reverse("patient_detail", args=[self.patient.pk])),
                             f"{reverse('lab_dashboard')}?q={self.patient.opd_number}")
        self.assertRedirects(self.client.get(reverse("visit_record", args=[self.visit.pk])),
                             reverse("lab_visit", args=[self.visit.pk]))

    def test_doctor_is_blocked_from_admin_screens(self):
        self.assertEqual(self.status(Role.DOCTOR, "get", "user_list"), 403)
        self.assertEqual(self.status(Role.DOCTOR, "get", "audit_list"), 403)
        self.assertEqual(self.status(Role.DOCTOR, "post", "patient_delete", self.patient.pk), 403)

    def test_guessed_file_paths_and_traversal_are_refused(self):
        self.login(Role.ADMIN)
        for path in ("/media/case_sheets/1/anything.pdf", "/media/../hamsa/settings.py",
                     "/media/patients/%2e%2e/%2e%2e/.env", "/media/db.sqlite3"):
            self.assertIn(self.client.get(path).status_code, (403, 404), path)

    def test_signed_out_users_are_sent_to_login_everywhere(self):
        for name, args in (("home", ()), ("patient_list", ()), ("patient_detail", (self.patient.pk,)),
                           ("visit_record", (self.visit.pk,)), ("attachment_file", (self.sheet.pk,)),
                           ("pharmacy_queue", ()), ("lab_dashboard", ())):
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"], name)


# ============================================================================ edge cases
class EdgeCaseTests(JourneyMixin, AttachmentTestCase):
    def test_duplicate_submission_does_not_create_a_second_patient(self):
        self.login(Role.ATTENDER)
        self.register()
        second = self.register()
        patients = m.Patient.objects.filter(full_name="Savitri Devi")
        self.assertEqual(patients.count(), 1)
        self.assertRedirects(second, reverse("patient_detail", args=[patients.get().pk]))
        self.assertEqual(patients.get().visits.count(), 1)

    def test_same_phone_different_person_is_still_allowed(self):
        self.login(Role.ATTENDER)
        self.register(name="Savitri Devi")
        self.register(name="Raghav Devi")  # family members often share one mobile number
        self.assertEqual(m.Patient.objects.filter(phone="9000012345").count(), 2)

    def test_expired_session_does_not_save_and_asks_to_sign_in(self):
        self.login(Role.DOCTOR)
        data = self.consultation_post(self.patient, self.visit)
        self.client.logout()
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertIsNone(self.visit.section("diagnosis"))

    def test_case_record_post_redirects_so_refresh_cannot_resubmit(self):
        self.login(Role.DOCTOR)
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]),
                                    self.consultation_post(self.patient, self.visit))
        self.assertRedirects(response, reverse("visit_record", args=[self.visit.pk]))
        self.assertEqual(m.DispenseOrder.objects.filter(visit=self.visit).count(), 1)

    @override_settings(ATTACHMENT_MAX_UPLOAD_MB=0.01)
    def test_very_large_upload_is_refused_with_a_clear_message(self):
        self.login(Role.DOCTOR)
        response = self.post_upload(upload("huge.pdf", PDF + b"0" * 50_000), )
        self.assertEqual(self.patient.attachments.count(), 0)
        self.assertIn("over the 0.01 MB limit", " ".join(str(m_) for m_ in response.wsgi_request._messages))

    def test_unknown_records_show_the_friendly_404_page(self):
        self.login(Role.DOCTOR)
        response = self.client.get(reverse("patient_detail", args=[987654]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)

    def test_forbidden_pages_show_the_friendly_403_page(self):
        self.login(Role.ATTENDER)
        response = self.client.get(reverse("user_list"))
        self.assertContains(response, "You don't have access to this page", status_code=403)
