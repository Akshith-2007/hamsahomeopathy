"""Phase 3: inferred family links, history pre-fill, family history auto-fill, prescribing through
family, and the lab dashboard / report history."""
import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from clinic import models as m
from clinic import pharmacy, relationships
from clinic.tests import PDF, ClinicTestCase, management
from clinic.tests_family_pharmacy import Phase2Mixin
from core.models import AuditLog, Notification

R = m.Relationship
PR = m.PatientRelationship
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def audited(action, **filters):
    return AuditLog.objects.filter(action=action, **filters).exists()


def link_row(patient, relative):
    return PR.objects.filter(patient=patient, related_patient=relative).first()


def snapshot():
    return sorted(PR.objects.values_list("patient_id", "related_patient_id", "relationship", "source", "status",
                                         "inferred_via_id"))


# ============================================================================ 1. inferred links
class InferenceTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.me = self.patient                                   # female
        self.mother = self.make_patient("Lakshmi", gender="F", age=58)
        self.father = self.make_patient("Ramesh", gender="M", age=62)
        self.staff = self.users[Role.ATTENDER]

    def assert_inferred(self, patient, relative, relationship, via):
        row = link_row(patient, relative)
        self.assertIsNotNone(row, f"{relative} should be linked to {patient}")
        self.assertEqual((row.relationship, row.source, row.status, row.inferred_via_id),
                         (relationship, "INFERRED", "ACTIVE", via.pk))

    def test_father_inferred_through_mother_in_either_order(self):
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, self.father, R.SPOUSE, self.staff)
        self.assert_inferred(self.me, self.father, R.FATHER, self.mother)
        self.assert_inferred(self.father, self.me, R.DAUGHTER, self.mother)
        self.assertTrue(audited("RELATIONSHIP_INFERRED"))

        # Same family created in the opposite order.
        me2 = self.make_patient("Ravi", gender="M")
        mom2, dad2 = self.make_patient("Sita"), self.make_patient("Gopal", gender="M")
        relationships.link(mom2, dad2, R.SPOUSE, self.staff)
        relationships.link(me2, mom2, R.MOTHER, self.staff)
        self.assert_inferred(me2, dad2, R.FATHER, mom2)
        self.assert_inferred(dad2, me2, R.SON, mom2)

    def test_siblings_and_grandparents(self):
        brother = self.make_patient("Kiran", gender="M", age=30)
        grandma = self.make_patient("Ammamma", gender="F", age=80)
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.link(brother, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, grandma, R.MOTHER, self.staff)
        self.assert_inferred(self.me, brother, R.BROTHER, self.mother)
        self.assert_inferred(brother, self.me, R.SISTER, self.mother)
        self.assert_inferred(self.me, grandma, R.GRANDMOTHER, self.mother)
        self.assert_inferred(grandma, brother, R.GRANDSON, self.mother)

    def test_unknown_sex_gets_neutral_label(self):
        child = self.make_patient("Baby", gender="O", age=1)
        relationships.link(child, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, self.father, R.SPOUSE, self.staff)
        self.assertEqual(link_row(self.father, child).relationship, R.CHILD)

    def test_no_inference_beyond_close_family(self):
        aunt = self.make_patient("Aunt Uma", gender="F")
        cousin = self.make_patient("Cousin Vani", gender="F")
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, aunt, R.SISTER, self.staff)
        relationships.link(cousin, aunt, R.MOTHER, self.staff)
        self.assertIsNone(link_row(self.me, aunt))     # aunt: not inferred
        self.assertIsNone(link_row(self.me, cousin))   # cousin: not inferred

    def test_manual_wins_removal_rejection_and_idempotence(self):
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, self.father, R.SPOUSE, self.staff)
        # Rebuilding twice changes nothing.
        before = snapshot()
        with redirect_stdout(io.StringIO()):
            call_command("rebuild_family_links")
            call_command("rebuild_family_links")
        self.assertEqual(snapshot(), before)

        # Manual link overrides the inferred one for the same pair.
        relationships.link(self.me, self.father, R.OTHER, self.staff, label="Stepfather")
        row = link_row(self.me, self.father)
        self.assertEqual((row.source, row.relationship, row.label), ("MANUAL", R.OTHER, "Stepfather"))
        relationships.rebuild_inferred_links(self.me)
        self.assertEqual(link_row(self.me, self.father).source, "MANUAL")
        relationships.unlink(link_row(self.me, self.father), self.staff)

        # Removing the only source link removes the dependent inferred link.
        relationships.rebuild_inferred_links(self.me)
        self.assertEqual(link_row(self.me, self.father).source, "INFERRED")
        relationships.unlink(link_row(self.me, self.mother), self.staff)
        self.assertIsNone(link_row(self.me, self.father))

        # A rejected inference is never re-created, even by the command; admin can undo.
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.reject(link_row(self.me, self.father), self.staff)
        with redirect_stdout(io.StringIO()):
            call_command("rebuild_family_links")
        self.assertEqual(link_row(self.me, self.father).status, "REJECTED")
        self.assertFalse(PR.objects.filter(patient=self.me, related_patient=self.father, status="ACTIVE").exists())
        self.assertTrue(audited("RELATIONSHIP_REJECTED"))
        relationships.unreject(link_row(self.me, self.father), self.users[Role.ADMIN])
        self.assert_inferred(self.me, self.father, R.FATHER, self.mother)

    def test_conflicting_rules_create_nothing_and_warn(self):
        other = self.make_patient("Raj", gender="M")
        brother = self.make_patient("Kiran", gender="M")
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)   # mother's son "Raj" -> my brother …
        relationships.link(self.mother, other, R.SON, self.staff)
        relationships.link(self.me, brother, R.BROTHER, self.staff)      # … but my brother's father is also Raj
        relationships.link(brother, other, R.FATHER, self.staff)
        self.assertFalse(PR.objects.filter(patient=self.me, related_patient=other, status="ACTIVE").exists())
        self.assertTrue(audited("INFERENCE_CONFLICT"))

    def test_patient_page_badges_confirm_and_registration_message(self):
        relationships.link(self.mother, self.father, R.SPOUSE, self.staff)
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("patient_create"), {
            "patient-registration_date": date.today().isoformat(), "patient-full_name": "New Daughter",
            "patient-age": "20", "patient-gender": "F", "patient-phone": "9000000000",
            "visit-visit_date": date.today().isoformat(), "action": "save_only",
            "has_family": "yes", "relative_id": [self.mother.pk], "relative_relationship": [R.MOTHER],
            "relative_label": [""],
        }, follow=True)
        self.assertContains(response, "Also connected automatically: Ramesh (Father)")
        daughter = m.Patient.objects.get(full_name="New Daughter")
        html = self.client.get(reverse("patient_detail", args=[daughter.pk])).content.decode()
        self.assertIn("Auto-linked via Mother", html)
        self.assertIn("Confirm", html)
        self.client.post(reverse("relationship_confirm", args=[link_row(daughter, self.father).pk]))
        self.assertEqual(link_row(daughter, self.father).source, "MANUAL")
        self.assertEqual(link_row(self.father, daughter).source, "MANUAL")
        self.assertTrue(audited("RELATIONSHIP_CONFIRMED"))

    def test_api_relationship_fields_confirm_reject(self):
        relationships.link(self.me, self.mother, R.MOTHER, self.staff)
        relationships.link(self.mother, self.father, R.SPOUSE, self.staff)
        api = self.api(Role.ATTENDER)
        rows = api.get(f"/api/patients/{self.me.pk}/relationships/").json()
        father = next(r for r in rows if r["relative"]["id"] == self.father.pk)
        self.assertEqual((father["source"], father["status"], father["via_label"]), ("INFERRED", "ACTIVE", "Mother"))
        self.assertEqual(father["inferred_via"]["id"], self.mother.pk)
        url = f"/api/patients/{self.me.pk}/relationships/{father['id']}/reject/"
        self.assertEqual(api.post(url).status_code, 204)
        self.assertEqual(link_row(self.me, self.father).status, "REJECTED")
        unreject = f"/api/patients/{self.me.pk}/relationships/{father['id']}/unreject/"
        self.assertEqual(api.post(unreject).status_code, 403)                    # admin only
        self.assertEqual(self.api(Role.ADMIN).post(unreject).status_code, 204)
        new_id = link_row(self.me, self.father).pk
        confirmed = api.post(f"/api/patients/{self.me.pk}/relationships/{new_id}/confirm/").json()
        self.assertEqual(confirmed["source"], "MANUAL")
        self.assertEqual(self.api(Role.LAB).get(f"/api/patients/{self.me.pk}/relationships/").status_code, 403)


# ============================================================================ 2. own history pre-fill
class HistoryPrefillTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.old = self.add_visit(self.patient, today - timedelta(days=90), diagnosis="Migraine", remedy="Natrum Mur")
        m.CaseHistory.objects.create(visit=self.old, presenting_complaints="Headache", past_history="Typhoid 2010")
        m.PhysicalGeneral.objects.create(visit=self.old, constitution="Lean, tall", sleep="Sound")
        self.mid = self.add_visit(self.patient, today - timedelta(days=30), diagnosis="Migraine improved",
                                  remedy="Sulphur")
        m.CaseHistory.objects.create(visit=self.mid, presenting_complaints="Better", past_history="")
        m.PhysicalGeneral.objects.create(visit=self.mid, sleep="Disturbed, wakes 3 am")
        m.Examination.objects.create(visit=self.mid, pulse=88)
        self.visit.visit_type = m.Visit.Type.NEW
        self.visit.save()
        self.url = reverse("visit_record", args=[self.visit.pk])

    def page(self, role=Role.DOCTOR):
        self.login(role)
        return self.client.get(self.url).content.decode()

    def test_stable_fields_prefilled_from_latest_visit_that_had_them(self):
        html = self.page()
        self.assertIn("Typhoid 2010", html)            # past history from the older visit (latest one was blank)
        self.assertIn("Disturbed, wakes 3 am", html)   # sleep from the newest visit
        self.assertIn("Lean, tall", html)              # constitution from the older visit
        payload = json.loads(html.split('id="history-prefill" type="application/json">')[1].split("</script>")[0])
        self.assertTrue(payload["fields"]["case_history-past_history"]["prefilled"])
        self.assertIn("From visit on", payload["fields"]["physical_generals-sleep"]["label"])
        # Visit-specific data is never pre-filled on a new case.
        self.assertNotIn('name="case_history-presenting_complaints">Better', html)
        self.assertNotIn("case_history-presenting_complaints", payload["fields"])
        self.assertNotIn('value="88"', html)
        self.assertNotIn("diagnosis-final", payload["fields"])

    def test_opening_creates_nothing(self):
        counts = [model.objects.count() for model in (m.CaseHistory, m.PhysicalGeneral, m.MentalGeneral)]
        self.page()
        self.page(Role.ATTENDER)
        self.assertEqual([model.objects.count() for model in (m.CaseHistory, m.PhysicalGeneral, m.MentalGeneral)],
                         counts)

    def test_saving_stores_source_visit_and_edits_are_not_attributed(self):
        self.page()
        self.client.post(self.url, self.record_post({
            "case_history-presenting_complaints": "Cough",
            "case_history-past_history": "Typhoid 2010",
            "physical_generals-sleep": "Disturbed, wakes 3 am",
            "physical_generals-constitution": "Lean, tall, edited",
        }))
        case = self.visit.case_history
        generals = self.visit.physical_generals
        self.assertEqual(case.past_history, "Typhoid 2010")
        self.assertEqual(case.prefill_sources["past_history"], self.old.pk)
        self.assertEqual(generals.prefill_sources, {"sleep": self.mid.pk})
        self.assertEqual(generals.constitution, "Lean, tall, edited")
        self.assertTrue(audited("HISTORY_PREFILLED", object_id=str(self.visit.pk)))

    def test_past_history_summary_lists_all_previous_diagnoses_and_remedies(self):
        html = self.page()
        summary = html[html.index('id="past-history-summary"'):]
        summary = summary[:summary.index('for="id_case_history-presenting_complaints"')]
        for text in ("Migraine", "Migraine improved", "Natrum Mur 30C", "Sulphur 30C"):
            self.assertIn(text, summary)
        api = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history-prefill/").json()
        self.assertEqual(api["sections"]["case_history"]["past_history"]["source_visit"], self.old.pk)
        self.assertEqual(len(api["past_summary"]["remedies"]), 2)
        self.assertEqual(self.api(Role.ATTENDER).get(f"/api/patients/{self.patient.pk}/history-prefill/").status_code, 403)

    def test_completed_visit_is_not_rewritten(self):
        self.visit.status = m.Visit.Status.COMPLETED
        self.visit.save()
        html = self.page()
        field = html.split('name="case_history-past_history"')[1].split("</textarea>")[0]
        self.assertNotIn("Typhoid 2010", field)  # still offered via "Refresh from history", but not filled in
        payload = json.loads(html.split('id="history-prefill" type="application/json">')[1].split("</script>")[0])
        self.assertFalse(payload["fields"]["case_history-past_history"]["prefilled"])

    def test_reception_prefill_only_in_editable_sections(self):
        m.PastTreatment.objects.create(visit=self.old, previous_medication="Allopathic painkillers")
        html = self.page(Role.ATTENDER)
        self.assertIn("Allopathic painkillers", html)   # past treatment: reception may edit
        self.assertNotIn("Typhoid 2010", html)           # case history: view-only for reception, not pre-filled
        self.assertNotIn("past-history-summary", html)


# ============================================================================ 3. family history auto-fill
class FamilyAutofillTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.mother = self.make_patient("Lakshmi", gender="F", age=58)
        self.father = self.make_patient("Ramesh", gender="M", age=62)
        self.add_visit(self.mother, date(2024, 3, 1), diagnosis="Diabetes")
        self.add_visit(self.mother, date(2025, 2, 1), diagnosis="Hypertension")
        self.add_visit(self.father, date(2023, 6, 1), diagnosis="Asthma")
        self.add_visit(self.patient, date(2025, 1, 1), diagnosis="Migraine")
        staff = self.users[Role.ATTENDER]
        relationships.link(self.patient, self.mother, R.MOTHER, staff)
        relationships.link(self.mother, self.father, R.SPOUSE, staff)     # father is inferred for the patient
        self.url = reverse("visit_record", args=[self.visit.pk])

    def test_empty_field_filled_from_all_relatives_including_inferred(self):
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Mother (Lakshmi, 58) – Hypertension (2025), Diabetes (2024)", html)
        self.assertIn("Father (Ramesh, 62) – Asthma (2023)", html)
        self.assertIn("auto-linked via Mother", html)
        self.assertIn("From linked family records", html)

    def test_works_in_both_directions(self):
        father_visit = m.Visit.objects.create(patient=self.father, doctor=self.users[Role.DOCTOR])
        self.login(Role.DOCTOR)
        html = self.client.get(reverse("visit_record", args=[father_visit.pk])).content.decode()
        self.assertIn("Daughter (Test Patient, 30) – Migraine (2025)", html)
        self.assertIn("Spouse (Lakshmi, 58) – Hypertension (2025), Diabetes (2024)", html)

    def test_saving_autofill_audits_and_never_touches_relatives(self):
        relative_rows = [model.objects.filter(visit__patient__in=[self.mother, self.father]).count()
                         for model in (m.CaseHistory, m.Diagnosis, m.PhysicalGeneral)]
        self.login(Role.DOCTOR)
        self.client.get(self.url)
        summary = "Mother (Lakshmi, 58) – Hypertension (2025), Diabetes (2024)\nFather (Ramesh, 62) – Asthma (2023)"
        self.client.post(self.url, self.record_post({
            "case_history-presenting_complaints": "Headache", "case_history-family_history": summary,
        }))
        self.assertEqual(self.visit.case_history.family_history, summary)
        self.assertEqual(self.visit.case_history.prefill_sources.get("family_history"), "family")
        self.assertTrue(audited("FAMILY_HISTORY_AUTOFILLED"))
        self.assertEqual([model.objects.filter(visit__patient__in=[self.mother, self.father]).count()
                          for model in (m.CaseHistory, m.Diagnosis, m.PhysicalGeneral)], relative_rows)

    def test_existing_text_is_kept_and_new_information_offered(self):
        m.CaseHistory.objects.create(visit=self.visit, presenting_complaints="x",
                                     family_history="Mother – Diabetes. Grandfather had TB.")
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Mother – Diabetes. Grandfather had TB.", html)
        notice = html[html.index('id="family-new-info"'):]
        notice = notice[:notice.index("</div>\n    <button")]
        self.assertIn("Hypertension (2025)", notice)
        self.assertIn("Asthma (2023)", notice)
        self.assertNotIn("Diabetes", notice)  # already mentioned
        self.assertIn("Add to family history", html)

    def test_reception_and_lab_do_not_get_family_summary(self):
        self.login(Role.ATTENDER)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Family history from linked relatives", html)
        self.assertNotIn("Hypertension", html)
        self.assertEqual(self.api(Role.ATTENDER).get(f"/api/patients/{self.patient.pk}/family-history/").status_code, 403)
        self.assertEqual(self.api(Role.LAB).get(f"/api/patients/{self.patient.pk}/family-history/").status_code, 403)


# ============================================================================ 4. prescribing for a family member
class ThroughFamilyTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.mother = self.make_patient("Lakshmi", gender="F", age=58)
        self.add_visit(self.mother, date(2025, 2, 1), diagnosis="Hypertension")
        relationships.link(self.patient, self.mother, R.MOTHER, self.users[Role.ATTENDER])

    def consult(self, role=Role.DOCTOR, relative=None):
        self.login(role)
        return self.client.post(reverse("consult_through_family", args=[(relative or self.mother).pk]),
                                {"reported_by": self.patient.pk, "back_visit": self.visit.pk})

    def test_visit_created_for_relative_with_correct_type_and_reporter(self):
        response = self.consult()
        visit = self.mother.visits.order_by("-id").first()
        self.assertRedirects(response, f"{reverse('visit_record', args=[visit.pk])}?from={self.patient.pk}"
                                       f"&back_visit={self.visit.pk}", fetch_redirect_response=False)
        self.assertEqual((visit.visit_type, visit.consultation_mode, visit.reported_by),
                         (m.Visit.Type.FOLLOW_UP, "THROUGH_FAMILY", self.patient))
        self.assertTrue(audited("VISIT_THROUGH_FAMILY", object_id=str(visit.pk)))
        html = self.client.get(reverse("visit_record", args=[visit.pk])).content.decode()
        self.assertIn("Reported by daughter, Test Patient", html)
        self.assertIn("Patient not present", html)
        newcomer = self.make_patient("Baby Brother", gender="M", age=2)
        relationships.link(self.patient, newcomer, R.BROTHER, self.users[Role.ATTENDER])
        self.consult(relative=newcomer)
        self.assertEqual(newcomer.visits.get().visit_type, m.Visit.Type.NEW)

    def test_prescription_and_order_belong_to_relative_and_group_with_patient(self):
        own_rx = m.Prescription.objects.create(visit=self.visit)
        m.PrescriptionItem.objects.create(prescription=own_rx, medicine_name="Nux Vomica", potency="30C")
        pharmacy.send_to_pharmacy(own_rx, self.users[Role.DOCTOR], keep_open=True)
        self.consult()
        visit = self.mother.visits.order_by("-id").first()
        data = self.record_post({**management("rx", total=1), "rx-0-medicine_name": "Aconite", "rx-0-potency": "200C",
                                 "send_to_pharmacy": "complete"})
        data.update({"patient-full_name": self.mother.full_name, "patient-age": "58", "patient-gender": "F",
                     "patient-phone": self.mother.phone, "visit-visit_type": "FOLLOW_UP"})
        self.client.post(reverse("visit_record", args=[visit.pk]), data)
        order = m.DispenseOrder.objects.get(visit=visit)
        self.assertEqual((order.patient, order.collect_by, order.collect_by_label), (self.mother, self.patient, "daughter"))
        self.assertTrue(audited("COLLECT_BY_ORDER", object_id=str(order.pk)))
        self.login(Role.ATTENDER)
        html = self.client.get(reverse("pharmacy_queue")).content.decode()
        own_card = html.index(f'id="order-{m.DispenseOrder.objects.get(visit=self.visit).pk}"')
        relative_card = html.index(f'id="order-{order.pk}"')
        self.assertLess(own_card, relative_card)
        self.assertIn("Same pickup as Test Patient", html)
        self.assertIn("Collect by:", html)
        label = self.client.get(reverse("pharmacy_label", args=[order.pk])).content.decode()
        self.assertIn("Lakshmi", label)
        self.assertIn("Collect by: <strong>Test Patient</strong>", label)

    def test_visit_appears_only_in_relatives_history(self):
        self.consult()
        visit = self.mother.visits.order_by("-id").first()
        mine = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/").json()
        self.assertNotIn(visit.pk, [v["id"] for v in mine["visits"]])
        theirs = self.api(Role.DOCTOR).get(f"/api/patients/{self.mother.pk}/history/").json()
        entry = next(v for v in theirs["visits"] if v["id"] == visit.pk)
        self.assertEqual(entry["consultation_mode"], "THROUGH_FAMILY")
        self.login(Role.DOCTOR)
        self.assertNotIn(f"/visits/{visit.pk}/", self.client.get(reverse("visit_record", args=[self.visit.pk]))
                         .content.decode().split('id="history-card"')[1].split("</table>")[0])

    def test_requires_relationship_and_doctor(self):
        stranger = self.make_patient("Stranger", gender="M")
        url = f"/api/patients/{stranger.pk}/visits/"
        payload = {"consultation_mode": "THROUGH_FAMILY", "reported_by": self.patient.pk}
        self.assertEqual(self.api(Role.DOCTOR).post(url, payload, format="json").status_code, 400)
        ok = self.api(Role.DOCTOR).post(f"/api/patients/{self.mother.pk}/visits/", payload, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["reported_by_text"], "Reported by daughter, Test Patient")
        self.assertEqual(self.api(Role.ATTENDER).post(f"/api/patients/{self.mother.pk}/visits/", payload,
                                                      format="json").status_code, 403)
        self.assertEqual(self.consult(role=Role.ATTENDER).status_code, 403)
        visits = self.api(Role.DOCTOR).patch(f"/api/visits/{self.visit.pk}/", {"consultation_mode": "THROUGH_FAMILY"},
                                             format="json")
        self.assertEqual(visits.json()["consultation_mode"], "IN_PERSON")  # read-only on the generic endpoint


# ============================================================================ 5. lab dashboard and reports
class LabDashboardTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        m.Diagnosis.objects.create(visit=self.visit, final="SecretDiagnosis")
        m.CaseHistory.objects.create(visit=self.visit, presenting_complaints="SecretComplaint")
        self.cbc = m.Investigation.objects.create(visit=self.visit, request_notes="CBC",
                                                  created_by=self.users[Role.DOCTOR])
        self.tft = m.Investigation.objects.create(visit=self.visit, request_notes="Thyroid profile")
        m.Investigation.objects.filter(pk=self.cbc.pk).update(created_at=timezone.now() - timedelta(days=3))

    def test_lab_lands_on_dashboard_with_counts_and_overdue_flags(self):
        self.login(Role.LAB)
        response = self.client.get(reverse("home"))
        html = response.content.decode()
        self.assertIn("Lab dashboard", html)
        self.assertIn('data-count="pending">2<', html)
        self.assertIn('data-count="overdue">1<', html)
        row = html[html.index(f'data-investigation="{self.cbc.pk}"') - 80:html.index(f'data-investigation="{self.cbc.pk}"')]
        self.assertIn("lab-overdue", row)
        self.assertLess(html.index("CBC"), html.index("Thyroid profile"))  # oldest first
        for secret in ("SecretDiagnosis", "SecretComplaint", "9876543210"):
            self.assertNotIn(secret, html)
        body = self.api(Role.LAB).get("/api/lab/dashboard/").json()
        self.assertEqual(body["counts"], {"pending": 2, "overdue": 1, "completed_today": 0})
        self.assertTrue(body["pending"][0]["overdue"])
        self.assertNotIn("Secret", json.dumps(body))
        self.assertEqual(self.client.get(reverse("lab_dashboard"), {"test": "thyroid"}).content.decode().count(
            "data-upload-url"), 1)

    def test_dashboard_upload_completes_and_notifies_doctor(self):
        self.login(Role.LAB)
        response = self.client.post(reverse("lab_upload", args=[self.cbc.pk]),
                                    {"report_files": [SimpleUploadedFile("cbc.pdf", PDF)], "remarks": "Hb low"},
                                    headers={"x-requested-with": "fetch"})
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["counts"]["completed_today"], 1)
        self.cbc.refresh_from_db()
        self.assertEqual(self.cbc.status, "COMPLETED")
        self.assertIsNotNone(self.cbc.completed_at)
        report = self.cbc.reports.get()
        self.assertEqual((report.source, report.remarks), ("LAB", "Hb low"))
        note = Notification.objects.get(recipient=self.users[Role.DOCTOR])
        self.assertEqual(note.kind, "REPORT_READY")
        self.assertIn("Report ready: Test Patient – CBC", note.title)
        self.assertTrue(audited("REPORT_UPLOADED"))
        bad = self.client.post(reverse("lab_upload", args=[self.tft.pk]),
                               {"report_files": [SimpleUploadedFile("x.pdf", b"MZ fake")]}, headers={"x-requested-with": "fetch"})
        self.assertEqual(bad.status_code, 400)

    def test_report_filters_and_patient_history(self):
        from clinic import lab

        lab.upload_reports(self.cbc, [SimpleUploadedFile("cbc.pdf", PDF)], self.users[Role.LAB])
        lab.upload_reports(self.tft, [SimpleUploadedFile("tft.pdf", PDF)], self.users[Role.ADMIN])
        m.InvestigationReport.objects.create(investigation=self.tft, source="EXTERNAL", file=SimpleUploadedFile("e.pdf", PDF))
        self.login(Role.LAB)
        page = self.client.get(reverse("lab_reports"), {"test": "cbc"}).content.decode()
        self.assertIn("CBC", page)
        self.assertNotIn("Thyroid profile", page)
        api = self.api(Role.LAB)
        mine = api.get("/api/investigation-reports/", {"uploaded_by": "me"}).json()["results"]
        self.assertEqual([r["test"] for r in mine], ["CBC"])
        everything = api.get("/api/investigation-reports/", {"patient": self.patient.pk}).json()["results"]
        self.assertEqual(sorted(r["source"] for r in everything), ["LAB", "LAB"])  # no external reports for lab
        today = timezone.localdate().isoformat()
        self.assertEqual(api.get("/api/investigation-reports/", {"date_from": today, "date_to": today}).json()["count"], 2)
        history = self.client.get(reverse("lab_patient_reports", args=[self.patient.pk])).content.decode()
        self.assertIn("Thyroid profile", history)
        for secret in ("SecretDiagnosis", "SecretComplaint", "9876543210"):
            self.assertNotIn(secret, history)
        self.assertNotIn("Secret", json.dumps(everything))

    def test_lab_gets_403_for_non_investigation_files(self):
        self.patient.photo = SimpleUploadedFile("face.png", PNG)
        self.patient.save()
        past = m.PastTreatment.objects.create(visit=self.visit)
        attachment = m.PastTreatmentAttachment.objects.create(past_treatment=past, file=SimpleUploadedFile("p.pdf", PDF))
        image = m.PatientImage.objects.create(patient=self.patient, stage="BEFORE", image=SimpleUploadedFile("b.png", PNG))
        sheet = m.RepertoryAnalysis.objects.create(visit=self.visit, sheet=SimpleUploadedFile("r.pdf", PDF))
        from clinic import lab

        report = lab.upload_reports(self.cbc, [SimpleUploadedFile("cbc.pdf", PDF)], self.users[Role.LAB])[0]
        self.login(Role.LAB)
        for url in (self.patient.photo.url, attachment.file.url, image.image.url, sheet.sheet.url):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.get(report.file.url).status_code, 200)

    def test_corrected_upload_keeps_old_version(self):
        from clinic import lab

        original = lab.upload_reports(self.cbc, [SimpleUploadedFile("cbc.pdf", PDF)], self.users[Role.LAB])[0]
        self.login(Role.LAB)
        self.client.post(reverse("report_correct", args=[original.pk]),
                         {"report_files": [SimpleUploadedFile("cbc-v2.pdf", PDF)], "remarks": "Wrong Hb value"})
        original.refresh_from_db()
        corrected = m.InvestigationReport.objects.get(supersedes=original)
        self.assertEqual((original.status, corrected.status, corrected.version), ("SUPERSEDED", "CURRENT", 2))
        self.assertTrue(audited("REPORT_CORRECTED"))
        self.assertTrue(Notification.objects.filter(recipient=self.users[Role.DOCTOR], kind="REPORT_CORRECTED").exists())
        lab_ids = [r["id"] for r in self.api(Role.LAB).get("/api/investigation-reports/").json()["results"]]
        self.assertEqual(lab_ids, [corrected.pk])                                   # lab sees the current version
        self.assertEqual(self.client.get(original.file.url).status_code, 403)
        doctor_ids = {r["id"] for r in self.api(Role.DOCTOR).get("/api/investigation-reports/").json()["results"]}
        self.assertEqual(doctor_ids, {original.pk, corrected.pk})                   # doctor sees both
        # API correction and no deletes for the lab.
        api = self.api(Role.LAB)
        response = api.post(f"/api/investigation-reports/{corrected.pk}/correct/",
                            {"file": SimpleUploadedFile("v3.pdf", PDF), "remarks": "again"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["version"], 3)
        self.assertEqual(api.delete(f"/api/investigation-reports/{response.json()['id']}/").status_code, 403)
        self.assertEqual(self.client.post(reverse("upload_delete", args=["report", corrected.pk])).status_code, 403)
        self.login(Role.DOCTOR)
        self.assertEqual(self.client.post(reverse("upload_delete", args=["report", corrected.pk])).status_code, 403)
