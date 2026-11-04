"""Patient history: complete newest-first timeline, imported old records, search and query counts."""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from clinic import models as m
from clinic.tests_attachments import AttachmentTestCase
from clinic.tests_workflow import HistoryFixtureMixin


class HistoryTimelineTests(HistoryFixtureMixin, AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.previous = self.make_previous_visits()
        today = timezone.localdate()
        first = self.previous[0]
        rx = first.prescriptions.get()
        rx.followup_advice = "Review after two weeks"
        rx.save()
        m.ScheduledFollowUp.objects.create(patient=self.patient, source_visit=first, status="COMPLETED",
                                           scheduled_date=first.visit_date + timedelta(days=14))
        self.old_sheet = self.make_attachment(name="paper-2012.pdf", document_date=today - timedelta(days=4000),
                                              description="Previous clinic notes")

    def history_html(self, role=Role.DOCTOR):
        self.login(role)
        return self.client.get(reverse("patient_history", args=[self.patient.pk])).content.decode()

    def test_timeline_is_newest_first_and_includes_imported_records_last_by_date(self):
        page = self.history_html()
        html = page[page.index('<div class="timeline">'):]  # the summary bar above also shows the first visit date
        dates = [v.visit_date.strftime("%d-%b-%Y") for v in sorted(self.previous, key=lambda v: v.visit_date,
                                                                    reverse=True)]
        positions = [html.index(d) for d in dates]
        self.assertEqual(positions, sorted(positions), "visits must be newest first")
        self.assertIn("Imported / Old record", html)
        self.assertGreater(html.index("paper-2012.pdf"), positions[-1], "a 2012 paper sheet sorts after recent visits")
        self.assertIn(reverse("attachment_file", args=[self.old_sheet.pk]), html)

    def test_timeline_shows_complaint_diagnosis_remedy_dose_advice_and_next_followup(self):
        html = self.history_html()
        for text in ("Complaint1", "Diagnosis1", "Remedy1", "30C", "4 pills twice daily", "7 days",
                     "Follow-up advice: Review after two weeks", "Next follow-up"):
            self.assertIn(text, html)

    def test_consultation_timeline_also_shows_imported_records(self):
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[self.visit.pk])).content.decode()
        timeline = html[html.index('id="tab-timeline"'):html.index('id="tab-attachments"')]
        self.assertIn("Imported / Old record", timeline)

    def test_lab_staff_never_see_old_case_sheets_in_history_api(self):
        self.login(Role.LAB)
        self.assertEqual(self.client.get(f"/api/patients/{self.patient.pk}/history/").status_code, 403)
        data = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/").json()
        self.assertEqual([r["original_filename"] for r in data["imported_records"]], ["paper-2012.pdf"])

    def test_api_history_returns_the_real_dose_and_next_followups(self):
        data = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/").json()
        oldest = data["visits"][-1]
        item = oldest["prescriptions"][0]["items"][0]
        self.assertEqual(item["instructions"], "4 pills twice daily")
        self.assertIn("dose", item)
        self.assertEqual(oldest["next_followups"][0]["status"], "COMPLETED")
        self.assertEqual(oldest["prescriptions"][0]["followup_advice"], "Review after two weeks")

    def test_past_visit_opens_full_details(self):
        self.login(Role.DOCTOR)
        response = self.client.get(reverse("visit_summary", args=[self.previous[0].pk]))
        self.assertContains(response, "Complaint1")
        self.assertContains(response, "Remedy1")

    def test_empty_history_shows_a_friendly_message(self):
        newcomer = m.Patient.objects.create(full_name="New Person", age=22, gender="M", phone="9000000009")
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("patient_history", args=[newcomer.pk])).content.decode()
        self.assertIn("No visits or old records yet.", html)
        detail = self.client.get(reverse("patient_detail", args=[newcomer.pk])).content.decode()
        self.assertIn("No visits yet.", detail)
        self.assertIn("No old case sheets or documents attached yet.", detail)


class FollowUpCarryForwardTests(HistoryFixtureMixin, AttachmentTestCase):
    def test_returning_patient_follow_up_links_to_original_case_and_carries_history(self):
        first = m.Visit.objects.create(patient=self.patient, doctor=self.users[Role.DOCTOR], status="COMPLETED",
                                       visit_date=timezone.localdate() - timedelta(days=30))
        m.CaseHistory.objects.create(visit=first, presenting_complaints="Migraine", past_history="Typhoid 2010",
                                     family_history="Father: diabetes")
        self.visit.delete()
        self.login(Role.ATTENDER)
        self.client.post(reverse("checkin_start", args=[self.patient.pk]), {"visit_type": "FOLLOW_UP", "open": "0"})
        follow_up = self.patient.visits.order_by("-id").first()
        self.assertEqual(follow_up.visit_type, m.Visit.Type.FOLLOW_UP)
        self.assertEqual(follow_up.previous_visit, first)
        self.assertEqual(follow_up.case_history.past_history, "Typhoid 2010")
        self.assertEqual(follow_up.case_history.family_history, "Father: diabetes")
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[follow_up.pk])).content.decode()
        self.assertIn("Migraine", html)


class PatientSearchTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.target = m.Patient.objects.create(full_name="Lakshmi Narayana", age=60, gender="F", phone="9848012345")

    def search(self, query):
        self.login(Role.ATTENDER)
        return self.client.get(reverse("patient_list"), {"q": query}).content.decode()

    def test_search_by_name_phone_and_opd_number(self):
        for query in ("lakshmi", "Narayana", "98480", self.target.opd_number, self.target.opd_number.lower()):
            self.assertIn("Lakshmi Narayana", self.search(query), query)
        self.assertNotIn("Lakshmi Narayana", self.search("Ramesh"))


class HistoryQueryCountTests(HistoryFixtureMixin, AttachmentTestCase):
    """The history pages must not run one query per visit (N+1)."""

    def count(self, url, role=Role.DOCTOR):
        self.login(role)
        self.client.get(url)  # warm up session / content types
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx)

    def test_query_count_does_not_grow_with_visits(self):
        self.make_previous_visits(count=2)
        self.make_attachment()
        urls = [reverse("patient_detail", args=[self.patient.pk]), reverse("patient_history", args=[self.patient.pk]),
                reverse("visit_record", args=[self.visit.pk])]
        before = [self.count(url) for url in urls]
        self.make_previous_visits(count=6, start_days_ago=300)
        for _ in range(4):
            self.make_attachment()
        after = [self.count(url) for url in urls]
        for url, small, large in zip(urls, before, after):
            self.assertLessEqual(large - small, 2, f"{url}: {small} -> {large} queries")
