"""Tests for doctor history, check-in, follow-up scheduling and lab restrictions."""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role
from clinic import models as m
from clinic.tests import PDF, ClinicTestCase
from core.models import AuditLog

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def audited(action, **filters):
    return AuditLog.objects.filter(action=action, **filters).exists()


class HistoryFixtureMixin:
    """Patient with three earlier visits (each with diagnosis + prescription) and today's visit."""

    def make_previous_visits(self, count=3, start_days_ago=60):
        doctor = self.users[Role.DOCTOR]
        today = timezone.localdate()
        visits = []
        for n in range(count):
            visit = m.Visit.objects.create(
                patient=self.patient, doctor=doctor, status=m.Visit.Status.COMPLETED,
                visit_date=today - timedelta(days=start_days_ago - n * (start_days_ago // (count + 1))),
                visit_type=m.Visit.Type.NEW if n == 0 else m.Visit.Type.FOLLOW_UP,
            )
            m.CaseHistory.objects.create(visit=visit, presenting_complaints=f"Complaint{n + 1}")
            m.Diagnosis.objects.create(visit=visit, final=f"Diagnosis{n + 1}")
            rx = m.Prescription.objects.create(visit=visit, dietary_advice="No coffee")
            m.PrescriptionItem.objects.create(prescription=rx, medicine_name=f"Remedy{n + 1}", potency="30C",
                                              instructions="4 pills twice daily", duration="7 days")
            inv = m.Investigation.objects.create(visit=visit, request_notes=f"Test{n + 1}")
            m.InvestigationReport.objects.create(investigation=inv, source="LAB",
                                                 file=SimpleUploadedFile(f"r{n}.pdf", PDF))
            visits.append(visit)
        return visits

    def api(self, role):
        client = APIClient()
        client.force_authenticate(self.users[role])
        return client


# ============================================================================ 1. doctor history
class DoctorHistoryTests(HistoryFixtureMixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.previous = self.make_previous_visits()
        self.url = reverse("visit_record", args=[self.visit.pk])

    def test_doctor_sees_summary_prescriptions_newest_first_and_timeline(self):
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        self.assertIn("summary-bar", html)
        self.assertIn("Visit 4", html)
        self.assertIn("Diagnosis3", html)  # last diagnosis in the summary
        panel = html[html.index('id="sec-previous_prescriptions"'):html.index('id="sec-prescription"')]
        self.assertIn('id="body-previous_prescriptions" class="collapse show"', panel)
        positions = [panel.index(f"Remedy{n}") for n in (3, 2, 1)]
        self.assertEqual(positions, sorted(positions), "previous prescriptions must be newest first")
        self.assertIn("Repeat this prescription", panel)
        self.assertIn('id="tab-timeline"', html)
        for n in (1, 2, 3):
            self.assertIn(f"Complaint{n}", html)
            self.assertIn(f"Test{n}", html)

    def test_repeat_prescription_copies_items_and_confirms_replacement(self):
        self.login(Role.DOCTOR)
        source = self.previous[-1].prescriptions.get()
        url = reverse("prescription_repeat", args=[self.visit.pk, source.pk])
        self.client.post(url)
        items = list(m.PrescriptionItem.objects.filter(prescription__visit=self.visit).values_list("medicine_name", flat=True))
        self.assertEqual(items, ["Remedy3"])
        self.assertTrue(audited("PRESCRIPTION_REPEATED"))

        older = self.previous[0].prescriptions.get()
        older_url = reverse("prescription_repeat", args=[self.visit.pk, older.pk])
        response = self.client.post(older_url, follow=True)  # no confirmation -> refused
        self.assertContains(response, "Confirm to replace")
        self.assertEqual(list(m.PrescriptionItem.objects.filter(prescription__visit=self.visit)
                              .values_list("medicine_name", flat=True)), ["Remedy3"])
        self.client.post(older_url, {"replace": "1"})
        self.assertEqual(list(m.PrescriptionItem.objects.filter(prescription__visit=self.visit)
                              .values_list("medicine_name", flat=True)), ["Remedy1"])

    def test_page_shows_confirmation_when_items_exist(self):
        rx = m.Prescription.objects.create(visit=self.visit)
        m.PrescriptionItem.objects.create(prescription=rx, medicine_name="Current")
        self.login(Role.DOCTOR)
        self.assertContains(self.client.get(self.url), "This visit already has medicines entered")

    def test_reception_and_lab_never_see_history(self):
        self.login(Role.ATTENDER)
        html = self.client.get(self.url).content.decode()
        for marker in ("summary-bar", 'id="sec-previous_prescriptions"', 'id="tab-timeline"', "Remedy1", "Diagnosis1"):
            self.assertNotIn(marker, html)
        history_url = reverse("patient_history", args=[self.patient.pk])
        self.assertEqual(self.client.get(history_url).status_code, 403)
        self.login(Role.LAB)
        self.assertEqual(self.client.get(history_url).status_code, 403)
        api_url = f"/api/patients/{self.patient.pk}/history/"
        self.assertEqual(self.api(Role.ATTENDER).get(api_url).status_code, 403)
        self.assertEqual(self.api(Role.LAB).get(api_url).status_code, 403)

    def test_history_api_for_doctor(self):
        body = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/").json()
        self.assertEqual(body["summary"]["total_visits"], 4)
        remedies = [v["prescriptions"][0]["items"][0]["medicine_name"] for v in body["visits"] if v["prescriptions"]]
        self.assertEqual(remedies, ["Remedy3", "Remedy2", "Remedy1"])
        self.assertEqual(body["visits"][1]["investigations"][0]["reports"][0]["source"], "LAB")
        start = (timezone.localdate() - timedelta(days=20)).isoformat()
        filtered = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/?start={start}").json()
        self.assertTrue(all(v["visit_date"] >= start for v in filtered["visits"]))

    def test_history_queries_do_not_grow_with_visits(self):
        self.login(Role.DOCTOR)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        self.make_previous_visits(count=6, start_days_ago=400)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertLessEqual(len(large) - len(small), 2, "history must not issue per-visit queries")

    def test_followup_visit_shows_previous_visit_side_by_side(self):
        self.visit.visit_type = m.Visit.Type.FOLLOW_UP
        self.visit.previous_visit = self.previous[-1]
        self.visit.save()
        m.FollowUpNote.objects.create(visit=self.previous[-1], notes="Watch sleep pattern")
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        compare = html[html.index('id="sec-compare"'):html.index('id="sec-case_history"')]
        for text in ("Complaint3", "Remedy3", "Watch sleep pattern", "Changes since last visit"):
            self.assertIn(text, compare)


# ============================================================================ 2. check-in
class CheckInTests(HistoryFixtureMixin, ClinicTestCase):
    SECTION_MODELS = (m.CaseHistory, m.PhysicalGeneral, m.MentalGeneral, m.FollowUpNote, m.Examination, m.Diagnosis,
                      m.PastTreatment, m.RepertoryAnalysis, m.Investigation, m.Prescription, m.FollowUp)

    def section_rows(self, visit):
        return sum(model.objects.filter(visit=visit).count() for model in self.SECTION_MODELS)

    def registration(self, action):
        return {
            "patient-registration_date": timezone.localdate().isoformat(), "patient-full_name": "New Person",
            "patient-age": "40", "patient-gender": "M", "patient-phone": "9000000001",
            "visit-visit_date": timezone.localdate().isoformat(), "action": action,
        }

    def test_new_patient_save_only_creates_waiting_visit_without_sections(self):
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("patient_create"), self.registration("save_only"))
        patient = m.Patient.objects.get(full_name="New Person")
        self.assertRedirects(response, reverse("patient_detail", args=[patient.pk]))
        visit = patient.visits.get()
        self.assertEqual((visit.visit_type, visit.status), (m.Visit.Type.NEW, m.Visit.Status.WAITING))
        self.assertTrue(patient.opd_number.startswith("OPD-"))
        self.assertEqual(self.section_rows(visit), 0)
        self.assertTrue(audited("PATIENT_SAVED_NO_CASE_SHEET", object_id=str(patient.pk)))
        self.assertTrue(audited("CHECK_IN", object_id=str(visit.pk)))

    def test_new_patient_save_and_fill_opens_case_sheet(self):
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("patient_create"), self.registration("save_open"))
        visit = m.Patient.objects.get(full_name="New Person").visits.get()
        self.assertRedirects(response, reverse("visit_record", args=[visit.pk]))

    def test_saving_empty_case_sheet_creates_no_rows(self):
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("visit_record", args=[self.visit.pk]), self.record_post({
            "investigations-requested_date": timezone.localdate().isoformat(), "investigations-status": "REQUESTED",
        }))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.section_rows(self.visit), 0)

    def test_existing_patient_new_case_is_empty_but_history_visible(self):
        self.make_previous_visits()
        self.login(Role.ATTENDER)
        self.client.post(reverse("checkin_start", args=[self.patient.pk]), {"visit_type": "NEW", "open": "0"})
        visit = self.patient.visits.order_by("-id").first()
        self.assertEqual(visit.visit_type, m.Visit.Type.NEW)
        self.assertIsNone(visit.previous_visit)
        self.assertEqual(self.section_rows(visit), 0)
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[visit.pk])).content.decode()
        self.assertIn("Remedy3", html)
        self.assertIn("Complaint1", html)

    def test_followup_checkin_carries_forward_links_and_completes_schedule(self):
        m.CaseHistory.objects.create(visit=self.visit, presenting_complaints="Eczema")
        scheduled = m.ScheduledFollowUp.objects.create(patient=self.patient, scheduled_date=timezone.localdate())
        self.login(Role.ATTENDER)
        self.client.post(reverse("checkin_start", args=[self.patient.pk]), {"visit_type": "FOLLOW_UP", "open": "1"})
        visit = self.patient.visits.order_by("-id").first()
        self.assertEqual(visit.visit_type, m.Visit.Type.FOLLOW_UP)
        self.assertEqual(visit.previous_visit, self.visit)
        self.assertEqual(visit.status, m.Visit.Status.WAITING)
        self.assertEqual(visit.case_history.presenting_complaints, "Eczema")
        scheduled.refresh_from_db()
        self.assertEqual(scheduled.status, m.ScheduledFollowUp.Status.COMPLETED)
        self.assertEqual(scheduled.checkin_visit, visit)
        self.assertTrue(audited("FOLLOWUP_COMPLETED", object_id=str(scheduled.pk)))

    def test_followup_without_previous_visit_becomes_new_case(self):
        patient = m.Patient.objects.create(full_name="First Timer", age=20, gender="F", phone="9111111111")
        self.login(Role.ATTENDER)
        self.client.post(reverse("checkin_start", args=[patient.pk]), {"visit_type": "FOLLOW_UP"})
        self.assertEqual(patient.visits.get().visit_type, m.Visit.Type.NEW)

    def test_only_doctor_changes_visit_type_and_it_is_audited(self):
        url = reverse("visit_record", args=[self.visit.pk])
        self.login(Role.ATTENDER)
        self.client.post(url, self.record_post({"visit-visit_type": "FOLLOW_UP"}))
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.visit_type, m.Visit.Type.NEW)
        self.login(Role.DOCTOR)
        self.client.post(url, self.record_post({"visit-visit_type": "FOLLOW_UP"}))
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.visit_type, m.Visit.Type.FOLLOW_UP)
        self.assertTrue(audited("VISIT_TYPE_CHANGED", object_id=str(self.visit.pk)))

    def test_consultation_status_flow(self):
        url = reverse("visit_record", args=[self.visit.pk])
        self.login(Role.ATTENDER)
        self.client.get(url)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, m.Visit.Status.WAITING)
        self.login(Role.DOCTOR)
        self.client.get(url)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, m.Visit.Status.IN_CONSULTATION)
        self.client.post(url, self.record_post({"complete": "1"}))
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, m.Visit.Status.COMPLETED)

    def test_dashboard_lists_waiting_first_with_badges(self):
        self.visit.status = m.Visit.Status.COMPLETED
        self.visit.save()
        waiting_patient = m.Patient.objects.create(full_name="Waiting Wendy", age=33, gender="F", phone="9222222222")
        m.Visit.objects.create(patient=waiting_patient, visit_type=m.Visit.Type.FOLLOW_UP)
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("home")).content.decode()
        self.assertLess(html.index("Waiting Wendy"), html.index("Test Patient"))
        self.assertIn("Open case sheet", html)
        self.assertIn(">Follow-up</span>", html)

    def test_api_check_in(self):
        response = self.api(Role.ATTENDER).post("/api/visits/check-in/", {"patient": self.patient.pk,
                                                                          "visit_type": "FOLLOW_UP"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["visit_type"], "FOLLOW_UP")
        self.assertEqual(response.json()["previous_visit"], self.visit.pk)
        lab = self.api(Role.LAB).post("/api/visits/check-in/", {"patient": self.patient.pk, "visit_type": "NEW"},
                                      format="json")
        self.assertEqual(lab.status_code, 403)

    def test_api_visit_type_change_requires_doctor(self):
        url = f"/api/visits/{self.visit.pk}/"
        self.assertEqual(self.api(Role.ATTENDER).patch(url, {"visit_type": "FOLLOW_UP"}, format="json").status_code, 400)
        self.assertEqual(self.api(Role.DOCTOR).patch(url, {"visit_type": "FOLLOW_UP"}, format="json").status_code, 200)
        self.assertTrue(audited("VISIT_TYPE_CHANGED", object_id=str(self.visit.pk)))


# ============================================================================ 3. follow-up scheduling
class FollowUpSchedulingTests(HistoryFixtureMixin, ClinicTestCase):
    def schedule(self, role, days=7, notes="", replace=False, patient=None):
        self.login(role)
        data = {"sched-scheduled_date": (timezone.localdate() + timedelta(days=days)).isoformat(),
                "sched-notes": notes, "source_visit": self.visit.pk}
        if replace:
            data["sched-replace"] = "True"
        return self.client.post(reverse("followup_schedule", args=[(patient or self.patient).pk]), data, follow=True)

    def test_doctor_and_reception_can_schedule(self):
        self.schedule(Role.DOCTOR, notes="Check thyroid result")
        followup = m.ScheduledFollowUp.objects.get(patient=self.patient)
        self.assertEqual(followup.notes, "Check thyroid result")
        self.assertEqual(followup.source_visit, self.visit)
        self.assertEqual(followup.scheduled_by, self.users[Role.DOCTOR])
        other = m.Patient.objects.create(full_name="Other", age=50, gender="M", phone="9333333333")
        self.schedule(Role.ATTENDER, notes="ignored for reception", patient=other)
        followup = m.ScheduledFollowUp.objects.get(patient=other)
        self.assertEqual(followup.notes, "")
        self.assertTrue(audited("FOLLOWUP_SCHEDULED", object_id=str(followup.pk)))

    def test_only_one_active_followup_per_patient(self):
        self.schedule(Role.DOCTOR, days=7)
        response = self.schedule(Role.DOCTOR, days=14)
        self.assertContains(response, "Confirm to replace")
        self.assertEqual(m.ScheduledFollowUp.objects.filter(patient=self.patient, status="SCHEDULED").count(), 1)
        self.schedule(Role.DOCTOR, days=14, replace=True)
        statuses = sorted(m.ScheduledFollowUp.objects.filter(patient=self.patient).values_list("status", flat=True))
        self.assertEqual(statuses, ["CANCELLED", "SCHEDULED"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            m.ScheduledFollowUp.objects.create(patient=self.patient, scheduled_date=timezone.localdate())

    def test_reception_never_sees_clinical_notes(self):
        self.schedule(Role.DOCTOR, notes="Secret clinical note")
        self.login(Role.ATTENDER)
        for url in (reverse("patient_detail", args=[self.patient.pk]), reverse("followup_list"),
                    reverse("visit_record", args=[self.visit.pk]), reverse("home")):
            self.assertNotContains(self.client.get(url), "Secret clinical note")
        row = self.api(Role.ATTENDER).get("/api/scheduled-followups/").json()["results"][0]
        self.assertNotIn("notes", row)
        self.assertEqual(self.api(Role.DOCTOR).get("/api/scheduled-followups/").json()["results"][0]["notes"],
                         "Secret clinical note")
        self.login(Role.DOCTOR)
        self.assertContains(self.client.get(reverse("patient_detail", args=[self.patient.pk])), "Secret clinical note")

    def test_dashboard_buckets_and_check_in(self):
        today = timezone.localdate()
        overdue_patient = m.Patient.objects.create(full_name="Overdue Olga", age=60, gender="F", phone="9444444444")
        m.ScheduledFollowUp.objects.create(patient=overdue_patient, scheduled_date=today - timedelta(days=2))
        followup = m.ScheduledFollowUp.objects.create(patient=self.patient, scheduled_date=today)
        self.login(Role.ATTENDER)
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("Overdue Olga", html)
        self.assertIn(reverse("followup_checkin", args=[followup.pk]), html)
        self.client.post(reverse("followup_checkin", args=[followup.pk]), {"next": reverse("home")})
        followup.refresh_from_db()
        self.assertEqual(followup.status, m.ScheduledFollowUp.Status.COMPLETED)
        self.assertEqual(followup.checkin_visit.visit_type, m.Visit.Type.FOLLOW_UP)

    def test_reschedule_and_cancel_are_audited(self):
        followup = m.ScheduledFollowUp.objects.create(patient=self.patient,
                                                      scheduled_date=timezone.localdate() + timedelta(days=3))
        self.login(Role.ATTENDER)
        new_date = timezone.localdate() + timedelta(days=10)
        self.client.post(reverse("followup_reschedule", args=[followup.pk]), {"scheduled_date": new_date.isoformat(),
                                                                              "scheduled_time": "10:30"})
        followup.refresh_from_db()
        self.assertEqual(followup.scheduled_date, new_date)
        self.assertEqual(followup.scheduled_time.strftime("%H:%M"), "10:30")
        self.assertTrue(audited("FOLLOWUP_RESCHEDULED", object_id=str(followup.pk)))
        self.client.post(reverse("followup_cancel", args=[followup.pk]))
        followup.refresh_from_db()
        self.assertEqual(followup.status, m.ScheduledFollowUp.Status.CANCELLED)
        self.assertTrue(audited("FOLLOWUP_CANCELLED", object_id=str(followup.pk)))

    def test_missed_followup_command(self):
        today = timezone.localdate()
        past = m.ScheduledFollowUp.objects.create(patient=self.patient, scheduled_date=today - timedelta(days=1))
        other = m.Patient.objects.create(full_name="Future", age=1, gender="M", phone="9555555555")
        future = m.ScheduledFollowUp.objects.create(patient=other, scheduled_date=today)
        call_command("mark_missed_followups", stdout=open(__import__("os").devnull, "w"))
        past.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(past.status, m.ScheduledFollowUp.Status.MISSED)
        self.assertEqual(future.status, m.ScheduledFollowUp.Status.SCHEDULED)
        self.assertTrue(audited("FOLLOWUP_MISSED", object_id=str(past.pk)))

    def test_api_scheduling_and_lab_exclusion(self):
        date = (timezone.localdate() + timedelta(days=5)).isoformat()
        payload = {"patient": self.patient.pk, "scheduled_date": date}
        self.assertEqual(self.api(Role.ATTENDER).post("/api/scheduled-followups/", payload, format="json").status_code, 201)
        self.assertEqual(self.api(Role.ATTENDER).post("/api/scheduled-followups/", payload, format="json").status_code, 400)
        payload["replace"] = True
        self.assertEqual(self.api(Role.DOCTOR).post("/api/scheduled-followups/", payload, format="json").status_code, 201)
        self.assertEqual(self.api(Role.LAB).get("/api/scheduled-followups/").status_code, 403)
        self.assertNotIn("followups", self.api(Role.LAB).get("/api/dashboard/").json())
        self.login(Role.LAB)
        self.assertEqual(self.client.get(reverse("followup_list")).status_code, 403)


# ============================================================================ 4. lab restrictions
class LabRestrictionTests(HistoryFixtureMixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.investigation = m.Investigation.objects.create(visit=self.visit, request_notes="Thyroid profile")
        self.lab_report = m.InvestigationReport.objects.create(
            investigation=self.investigation, source="LAB", file=SimpleUploadedFile("lab.pdf", PDF))
        self.external_report = m.InvestigationReport.objects.create(
            investigation=self.investigation, source="EXTERNAL", file=SimpleUploadedFile("ext.pdf", PDF))
        m.Diagnosis.objects.create(visit=self.visit, final="Hypothyroidism")
        self.patient.address = "12 Secret Street"
        self.patient.photo = SimpleUploadedFile("face.png", PNG)
        self.patient.save()

    def test_lab_home_is_dashboard_and_search_redirects_to_it(self):
        self.login(Role.LAB)
        self.assertContains(self.client.get(reverse("home")), "Lab dashboard")  # "/" renders the lab dashboard
        self.assertRedirects(self.client.get(reverse("patient_detail", args=[self.patient.pk])),
                             f"{reverse('lab_dashboard')}?q={self.patient.opd_number}")

    def test_worklist_shows_identity_only(self):
        self.login(Role.LAB)
        html = self.client.get(reverse("lab_worklist"), {"q": "Test"}).content.decode()
        for text in (self.patient.opd_number, "Test Patient", "Thyroid profile"):
            self.assertIn(text, html)
        for text in ("9876543210", "Secret Street", "Hypothyroidism"):
            self.assertNotIn(text, html)
        html = self.client.get(reverse("lab_visit", args=[self.visit.pk])).content.decode()
        self.assertIn("lab.pdf"[:3], html)
        self.assertNotIn("Hypothyroidism", html)
        self.assertNotIn("9876543210", html)
        self.assertNotIn(self.external_report.filename, html)

    def test_api_returns_no_clinical_data(self):
        api = self.api(Role.LAB)
        row = api.get("/api/patients/").json()["results"][0]
        self.assertEqual(set(row), {"id", "opd_number", "full_name", "age", "gender", "gender_display"})
        self.assertEqual(api.get("/api/patients/?search=9876543210").json()["count"], 0)
        detail = api.get(f"/api/visits/{self.visit.pk}/").json()
        for key in ("case_history", "diagnosis", "examination", "past_treatment", "prescriptions", "followups",
                    "images", "mental_generals", "physical_generals", "repertory", "followup_note"):
            self.assertNotIn(key, detail)
        self.assertEqual([r["source"] for r in detail["investigations"][0]["reports"]], ["LAB"])
        self.assertEqual([r["id"] for r in api.get("/api/investigation-reports/").json()["results"]],
                         [self.lab_report.pk])
        for endpoint in ("case-histories", "physical-generals", "mental-generals", "followup-notes", "examinations",
                         "diagnoses", "past-treatments", "patient-images", "repertory-analyses", "prescriptions",
                         "followups", "scheduled-followups"):
            self.assertEqual(api.get(f"/api/{endpoint}/").status_code, 403, endpoint)
        self.assertEqual(set(api.get("/api/dashboard/").json()), {"stats", "pending_investigations"})

    def test_me_reflects_lab_access(self):
        body = self.api(Role.LAB).get("/api/auth/me/").json()
        self.assertEqual(body["home"], "lab_dashboard")
        self.assertFalse(body["sections"]["case_record"]["view"])
        self.assertFalse(body["sections"]["patient"]["view"])
        self.assertTrue(body["sections"]["investigation_reports"]["edit"])

    def test_media_blocks_non_lab_files(self):
        self.login(Role.LAB)
        self.assertEqual(self.client.get(self.patient.photo.url).status_code, 403)
        self.assertEqual(self.client.get(self.external_report.file.url).status_code, 403)
        self.assertEqual(self.client.get(self.lab_report.file.url).status_code, 200)
        self.login(Role.DOCTOR)
        self.assertEqual(self.client.get(self.external_report.file.url).status_code, 200)

    def test_lab_blocked_from_clinical_screens(self):
        self.login(Role.LAB)
        for name, args in (("visit_summary", [self.visit.pk]), ("prescription_print", [self.visit.pk]),
                           ("patient_history", [self.patient.pk]), ("reports", []), ("followup_list", []),
                           ("checkin", [])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)

    def test_lab_upload_marks_completed_and_is_lab_sourced(self):
        investigation = m.Investigation.objects.create(visit=self.visit, request_notes="CBC")
        self.login(Role.LAB)
        self.client.post(reverse("lab_visit", args=[self.visit.pk]), {
            "investigation": investigation.pk, "report_files": [SimpleUploadedFile("cbc.pdf", PDF)]})
        investigation.refresh_from_db()
        self.assertEqual(investigation.status, m.Investigation.Status.COMPLETED)
        self.assertEqual(investigation.reports.get().source, "LAB")
