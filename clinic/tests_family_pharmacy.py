"""Phase 2: patient relationships, family history, and the prescription → pharmacy flow."""
import json
from datetime import date

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from clinic import models as m
from clinic import pharmacy, relationships
from clinic.services import WorkflowError
from clinic.tests import ClinicTestCase, management
from core.models import AuditLog, Notification

R = m.Relationship


def audited(action, **filters):
    return AuditLog.objects.filter(action=action, **filters).exists()


class Phase2Mixin:
    def api(self, role):
        client = APIClient()
        client.force_authenticate(self.users[role])
        return client

    def make_patient(self, name, gender="F", age=50, phone="9000000000"):
        return m.Patient.objects.create(full_name=name, age=age, gender=gender, phone=phone)

    def add_visit(self, patient, when, diagnosis="", remedy="", past_history=""):
        visit = m.Visit.objects.create(patient=patient, visit_date=when, status=m.Visit.Status.COMPLETED)
        if diagnosis:
            m.Diagnosis.objects.create(visit=visit, final=diagnosis)
        if past_history:
            m.CaseHistory.objects.create(visit=visit, presenting_complaints="x", past_history=past_history)
        if remedy:
            rx = m.Prescription.objects.create(visit=visit)
            m.PrescriptionItem.objects.create(prescription=rx, medicine_name=remedy, potency="30C")
        return visit


# ============================================================================ relationships
class RelationshipTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.lakshmi = self.make_patient("Lakshmi", gender="F", age=62)
        self.reception = self.users[Role.ATTENDER]

    def reverse_of(self, patient, relative):
        return m.PatientRelationship.objects.get(patient=relative, related_patient=patient)

    def test_reverse_link_uses_patient_sex(self):
        relationships.link(self.patient, self.lakshmi, R.MOTHER, self.reception)  # patient is female
        self.assertEqual(self.reverse_of(self.patient, self.lakshmi).relationship, R.DAUGHTER)
        ravi = self.make_patient("Ravi", gender="M")
        relationships.link(ravi, self.lakshmi, R.MOTHER, self.reception)
        self.assertEqual(self.reverse_of(ravi, self.lakshmi).relationship, R.SON)
        alex = self.make_patient("Alex", gender="O")
        relationships.link(alex, self.lakshmi, R.MOTHER, self.reception)
        self.assertEqual(self.reverse_of(alex, self.lakshmi).relationship, R.CHILD)
        uncle = self.make_patient("Uncle Raj", gender="M")
        relationships.link(alex, uncle, R.UNCLE, self.reception)
        reverse = self.reverse_of(alex, uncle)
        self.assertEqual((reverse.relationship, reverse.display_label), (R.OTHER, "Nephew / Niece"))
        self.assertTrue(audited("RELATIONSHIP_LINKED"))

    def test_duplicates_self_links_and_conflicts_rejected(self):
        relationships.link(self.patient, self.lakshmi, R.MOTHER, self.reception)
        with self.assertRaises(WorkflowError):
            relationships.link(self.patient, self.lakshmi, R.MOTHER, self.reception)   # duplicate
        with self.assertRaises(WorkflowError):
            relationships.link(self.patient, self.lakshmi, R.AUNT, self.reception)     # conflicting
        with self.assertRaises(WorkflowError):
            relationships.link(self.lakshmi, self.patient, R.SISTER, self.reception)   # conflicts with reverse
        with self.assertRaises(WorkflowError):
            relationships.link(self.patient, self.patient, R.SISTER, self.reception)   # self
        self.assertEqual(m.PatientRelationship.objects.count(), 2)

    def test_edit_and_remove_update_both_sides(self):
        link = relationships.link(self.patient, self.lakshmi, R.MOTHER, self.reception)
        relationships.update(link, R.GRANDMOTHER, self.reception)
        self.assertEqual(self.reverse_of(self.patient, self.lakshmi).relationship, R.GRANDDAUGHTER)
        relationships.unlink(self.reverse_of(self.patient, self.lakshmi), self.reception)  # remove from the other side
        self.assertEqual(m.PatientRelationship.objects.count(), 0)
        self.assertTrue(audited("RELATIONSHIP_EDITED") and audited("RELATIONSHIP_REMOVED"))
        relationships.link(self.patient, self.lakshmi, R.MOTHER, self.reception)  # can re-link after removal
        self.assertEqual(m.PatientRelationship.objects.count(), 2)

    def test_shared_mobile_and_registration_suggestions(self):
        shared = self.patient.phone
        sibling = self.make_patient("Sister Same Phone", phone=shared)
        self.assertEqual(m.Patient.objects.filter(phone=shared).count(), 2)
        self.login(Role.ATTENDER)
        results = self.client.get(reverse("patient_lookup"), {"q": shared}).json()["results"]
        self.assertEqual({r["id"] for r in results}, {self.patient.pk, sibling.pk})
        self.assertNotIn("diagnosis", json.dumps(results))

    def test_registration_links_selected_relatives(self):
        self.login(Role.ATTENDER)
        response = self.client.post(reverse("patient_create"), {
            "patient-registration_date": date.today().isoformat(), "patient-full_name": "Baby Girl",
            "patient-age": "3", "patient-gender": "F", "patient-phone": self.patient.phone,
            "visit-visit_date": date.today().isoformat(), "action": "save_only",
            "has_family": "yes", "relative_id": [self.patient.pk, self.lakshmi.pk],
            "relative_relationship": [R.MOTHER, R.GRANDMOTHER], "relative_label": ["", ""],
        })
        baby = m.Patient.objects.get(full_name="Baby Girl")
        self.assertRedirects(response, reverse("patient_detail", args=[baby.pk]))
        self.assertEqual(dict(baby.relationships.values_list("related_patient", "relationship")),
                         {self.patient.pk: R.MOTHER, self.lakshmi.pk: R.GRANDMOTHER})
        self.assertEqual(self.reverse_of(baby, self.patient).relationship, R.DAUGHTER)

    def test_patient_page_links_and_reception_sees_identity_only(self):
        self.add_visit(self.lakshmi, date(2025, 3, 1), diagnosis="Diabetes mellitus")
        self.login(Role.ATTENDER)
        self.client.post(reverse("relationship_add", args=[self.patient.pk]),
                         {"relative_id": self.lakshmi.pk, "relationship": R.MOTHER})
        html = self.client.get(reverse("patient_detail", args=[self.patient.pk])).content.decode()
        self.assertIn("Lakshmi", html)
        self.assertIn("Mother", html)
        self.assertNotIn("Diabetes", html)
        rows = self.api(Role.ATTENDER).get(f"/api/patients/{self.patient.pk}/relationships/").json()
        self.assertEqual(rows[0]["relative"]["full_name"], "Lakshmi")
        self.assertEqual(self.api(Role.ATTENDER).get(f"/api/patients/{self.patient.pk}/family-history/").status_code, 403)
        for url in (f"/api/patients/{self.patient.pk}/relationships/", f"/api/patients/{self.patient.pk}/family-history/"):
            self.assertEqual(self.api(Role.LAB).get(url).status_code, 403)
        self.login(Role.LAB)
        self.assertEqual(self.client.get(reverse("patient_lookup"), {"q": "Lakshmi"}).status_code, 403)

    def test_api_link_and_delete(self):
        api = self.api(Role.ATTENDER)
        response = api.post(f"/api/patients/{self.patient.pk}/relationships/",
                            {"related_patient": self.lakshmi.pk, "relationship": "MOTHER"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        duplicate = api.post(f"/api/patients/{self.patient.pk}/relationships/",
                             {"related_patient": self.lakshmi.pk, "relationship": "AUNT"}, format="json")
        self.assertEqual(duplicate.status_code, 400)
        link_id = response.json()["id"]
        self.assertEqual(api.delete(f"/api/patients/{self.patient.pk}/relationships/{link_id}/").status_code, 204)
        self.assertEqual(m.PatientRelationship.objects.count(), 0)


# ============================================================================ family history
class FamilyHistoryTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        self.mother = self.make_patient("Lakshmi", gender="F", age=62)
        self.add_visit(self.mother, date(2023, 5, 1), diagnosis="Diabetes", remedy="RelativeRemedy")
        self.add_visit(self.mother, date(2024, 6, 1), diagnosis="Diabetes", past_history="Thyroid since 2010")
        self.add_visit(self.mother, date(2025, 2, 1), diagnosis="Hypertension")
        relationships.link(self.patient, self.mother, R.MOTHER, self.users[Role.ATTENDER])
        self.url = reverse("visit_record", args=[self.visit.pk])

    def test_doctor_panel_shows_relatives_diagnoses_with_dates(self):
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        panel = html[html.index('id="family-history-panel"'):html.index('id="sec-physical_generals"')]
        self.assertIn("Lakshmi", panel)
        self.assertEqual(panel.count('<span class="dx-chip">Diabetes '), 1, "duplicate diagnoses must be merged")
        self.assertIn("Hypertension", panel)
        self.assertIn("Thyroid since 2010", panel)
        self.assertIn("Mother (Lakshmi, 62) – Hypertension (2025), Diabetes (2024)", html)
        self.assertIn(f"?from={self.patient.pk}", html)

    def test_insert_fills_only_current_patient(self):
        self.login(Role.DOCTOR)
        text = "Mother – Hypertension (2025), Diabetes (2024)"
        self.client.post(self.url, self.record_post({
            "case_history-presenting_complaints": "Headache", "case_history-family_history": text,
            "family_history_inserted": "1",
        }))
        self.assertEqual(self.visit.case_history.family_history, text)
        self.assertFalse(m.CaseHistory.objects.filter(visit__patient=self.mother, family_history__contains="Mother").exists())
        self.assertTrue(audited("FAMILY_HISTORY_INSERTED"))

    def test_reception_and_lab_never_see_family_history(self):
        self.login(Role.ATTENDER)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("family-history-panel", html)
        self.assertNotIn("Hypertension", html)
        body = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/family-history/").json()
        self.assertEqual([d["diagnosis"] for d in body["relatives"][0]["diagnoses"]], ["Hypertension", "Diabetes"])

    def test_relatives_records_never_mix_into_patient_history(self):
        self.login(Role.DOCTOR)
        html = self.client.get(self.url).content.decode()
        # The medicine-name autocomplete is a clinic-wide vocabulary, not patient data; check everything else.
        html = html[:html.find('<datalist id="medicine-list">')] + html[html.find("</datalist>"):]
        self.assertNotIn("RelativeRemedy", html)
        history = self.api(Role.DOCTOR).get(f"/api/patients/{self.patient.pk}/history/").json()
        self.assertEqual(history["summary"]["total_visits"], 1)
        self.assertNotIn("RelativeRemedy", json.dumps(history))
        self.assertNotIn("Hypertension", json.dumps(history))

    def test_family_panel_queries_do_not_grow_with_relatives(self):
        self.login(Role.DOCTOR)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for n in range(5):
            relative = self.make_patient(f"Relative {n}", gender="M")
            self.add_visit(relative, date(2024, 1, n + 1), diagnosis=f"Asthma {n}", past_history="Allergy")
            relationships.link(self.patient, relative, R.BROTHER, self.users[Role.ATTENDER])
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertLessEqual(len(large) - len(small), 2)


# ============================================================================ pharmacy
class PharmacyTests(Phase2Mixin, ClinicTestCase):
    def setUp(self):
        super().setUp()
        m.Diagnosis.objects.create(visit=self.visit, final="SecretDiagnosis")
        m.MentalGeneral.objects.create(visit=self.visit, fears="SecretFear")
        m.CaseHistory.objects.create(visit=self.visit, presenting_complaints="SecretComplaint")
        self.url = reverse("visit_record", args=[self.visit.pk])

    def rx_post(self, rows, send=None, initial_ids=()):
        data = self.record_post({**management("rx", initial=len(initial_ids), total=len(rows)),
                                 "case_history-presenting_complaints": "SecretComplaint"})
        for i, row in enumerate(rows):
            for key, value in row.items():
                data[f"rx-{i}-{key}"] = value
            if i < len(initial_ids):
                data[f"rx-{i}-id"] = initial_ids[i]
        if send:
            data["send_to_pharmacy"] = send
        return data

    def send_first_version(self):
        self.login(Role.DOCTOR)
        self.client.post(self.url, self.rx_post([
            {"medicine_name": "Nux Vomica", "potency": "30C", "form": "PILLS", "quantity": "2 dram",
             "dose": "4 pills TDS", "duration": "7 days", "instructions": "Avoid coffee"},
        ], send="complete"))
        return m.DispenseOrder.objects.get()

    def test_send_creates_snapshot_order_and_notifies_reception(self):
        order = self.send_first_version()
        self.assertEqual((order.version, order.status), (1, "SENT"))
        item = order.items.get()
        self.assertEqual((item.medicine_name, item.form, item.quantity, item.dose), ("Nux Vomica", "PILLS", "2 dram",
                                                                                      "4 pills TDS"))
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, m.Visit.Status.COMPLETED)
        self.assertTrue(Notification.objects.filter(recipient=self.users[Role.ATTENDER]).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.users[Role.ADMIN]).exists())
        self.assertFalse(Notification.objects.filter(recipient__in=[self.users[Role.DOCTOR], self.users[Role.LAB]]).exists())
        unread = self.api(Role.ATTENDER).get("/api/notifications/unread/").json()
        self.assertEqual(unread["count"], 1)
        self.assertEqual(unread["results"][0]["kind"], "PRESCRIPTION_SENT")
        self.assertTrue(audited("PRESCRIPTION_SENT"))
        # snapshot does not follow later edits made outside the pharmacy flow
        m.PrescriptionItem.objects.update(medicine_name="Changed silently")
        self.assertEqual(order.items.get().medicine_name, "Nux Vomica")

    def test_keep_consultation_open_and_empty_prescription(self):
        self.login(Role.DOCTOR)
        response = self.client.post(self.url, self.rx_post([], send="complete"), follow=True)
        self.assertContains(response, "Add at least one medicine")
        self.assertFalse(m.DispenseOrder.objects.exists())
        self.client.post(self.url, self.rx_post([{"medicine_name": "Sulphur", "potency": "200C"}], send="keep_open"))
        self.visit.refresh_from_db()
        self.assertNotEqual(self.visit.status, m.Visit.Status.COMPLETED)
        self.assertEqual(m.DispenseOrder.objects.count(), 1)

    def test_queue_shows_items_but_no_clinical_data(self):
        order = self.send_first_version()
        self.login(Role.ATTENDER)
        html = self.client.get(reverse("pharmacy_queue")).content.decode()
        self.assertIn("Nux Vomica", html)
        self.assertIn("4 pills TDS", html)
        for secret in ("SecretDiagnosis", "SecretFear", "SecretComplaint"):
            self.assertNotIn(secret, html)
        body = json.dumps(self.api(Role.ATTENDER).get("/api/dispense-orders/").json())
        self.assertIn("Nux Vomica", body)
        for secret in ("SecretDiagnosis", "SecretFear", "SecretComplaint", "9876543210"):
            self.assertNotIn(secret, body)
        # reception still cannot open the full prescription section
        self.assertNotIn('id="sec-prescription"', self.client.get(self.url).content.decode())
        label = self.client.get(reverse("pharmacy_label", args=[order.pk]) + "?size=label")
        self.assertContains(label, "Nux Vomica")
        self.assertTrue(audited("LABEL_PRINTED", object_id=str(order.pk)))

    def test_status_flow_and_doctor_sees_status(self):
        order = self.send_first_version()
        self.login(Role.DOCTOR)
        self.assertEqual(self.client.post(reverse("pharmacy_action", args=[order.pk, "start-preparing"])).status_code, 403)
        self.assertEqual(self.api(Role.DOCTOR).post(f"/api/dispense-orders/{order.pk}/start-preparing/").status_code, 403)
        self.login(Role.ATTENDER)
        bad = self.client.post(reverse("pharmacy_action", args=[order.pk, "mark-dispensed"]), follow=True)
        self.assertContains(bad, "cannot mark dispensed")
        for action, expected in (("start-preparing", "PREPARING"), ("mark-ready", "READY")):
            self.client.post(reverse("pharmacy_action", args=[order.pk, action]))
            order.refresh_from_db()
            self.assertEqual(order.status, expected)
        self.login(Role.DOCTOR)
        self.assertContains(self.client.get(reverse("home")), "Pharmacy: Ready")
        response = self.api(Role.ATTENDER).post(f"/api/dispense-orders/{order.pk}/mark-dispensed/")
        self.assertEqual(response.json()["status"], "DISPENSED")
        order.refresh_from_db()
        self.assertEqual(order.dispensed_by, self.users[Role.ATTENDER])
        self.assertContains(self.client.get(self.url), "pharmacy-dispensed")
        self.assertEqual(AuditLog.objects.filter(action="DISPENSE_STATUS_CHANGED", object_id=str(order.pk)).count(), 3)

    def test_edit_after_send_creates_new_version_and_supersedes(self):
        first = self.send_first_version()
        item = m.PrescriptionItem.objects.get()
        self.client.post(self.url, self.rx_post([
            {"medicine_name": "Nux Vomica", "potency": "200C", "form": "PILLS", "quantity": "2 dram",
             "dose": "4 pills TDS", "duration": "7 days", "instructions": "Avoid coffee"},
            {"medicine_name": "Sac Lac", "potency": "", "dose": "4 pills BD"},
        ], initial_ids=[item.pk]))
        first.refresh_from_db()
        self.assertEqual(first.status, "SUPERSEDED")
        second = m.DispenseOrder.objects.get(version=2)
        self.assertTrue(second.is_update)
        self.assertEqual(second.status, "SENT")
        # Items are matched by remedy + potency, so a new potency reads as "old line removed, new line added".
        self.assertEqual(dict(second.items.values_list("medicine_name", "change")),
                         {"Nux Vomica": "ADDED", "Sac Lac": "ADDED"})
        self.assertIn("Nux Vomica 30C", second.removed_items)
        notification = Notification.objects.filter(recipient=self.users[Role.ATTENDER]).order_by("-id").first()
        self.assertEqual(notification.kind, "PRESCRIPTION_UPDATED")
        self.assertTrue(audited("PRESCRIPTION_UPDATED"))
        self.login(Role.ATTENDER)
        self.assertContains(self.client.get(reverse("pharmacy_queue")), "Updated prescription")

    def test_changed_item_is_flagged(self):
        first = self.send_first_version()
        item = m.PrescriptionItem.objects.get()
        self.client.post(self.url, self.rx_post([
            {"medicine_name": "Nux Vomica", "potency": "30C", "form": "PILLS", "quantity": "2 dram",
             "dose": "6 pills TDS", "duration": "14 days", "instructions": "Avoid coffee"},
        ], initial_ids=[item.pk]))
        second = m.DispenseOrder.objects.get(previous_order=first)
        self.assertEqual(second.items.get().change, "CHANGED")

    def test_dispensed_order_kept_when_prescription_changes(self):
        order = self.send_first_version()
        for action in ("start-preparing", "mark-ready", "mark-dispensed"):
            pharmacy.change_status(order, action, self.users[Role.ATTENDER])
        rx = m.Prescription.objects.get()
        m.PrescriptionItem.objects.create(prescription=rx, medicine_name="Arnica", potency="30C")
        pharmacy.sync_after_edit(rx, self.users[Role.DOCTOR])
        order.refresh_from_db()
        self.assertEqual(order.status, "DISPENSED")
        self.assertEqual(m.DispenseOrder.objects.filter(version=2, is_update=True).count(), 1)

    def test_notifications_read_and_read_all(self):
        self.send_first_version()
        api = self.api(Role.ATTENDER)
        note = Notification.objects.get(recipient=self.users[Role.ATTENDER])
        self.assertEqual(self.api(Role.ADMIN).post(f"/api/notifications/{note.pk}/read/").status_code, 404)
        self.assertIsNotNone(api.post(f"/api/notifications/{note.pk}/read/").json()["read_at"])
        self.assertEqual(api.get("/api/notifications/unread/").json()["count"], 0)
        self.login(Role.ADMIN)
        admin_note = Notification.objects.get(recipient=self.users[Role.ADMIN])
        response = self.client.get(reverse("notification_open", args=[admin_note.pk]))
        self.assertTrue(response.url.startswith(reverse("pharmacy_queue")))
        admin_note.refresh_from_db()
        self.assertIsNotNone(admin_note.read_at)
        Notification.objects.create(recipient=self.users[Role.ATTENDER], title="x")
        self.assertEqual(api.post("/api/notifications/read-all/").json()["marked"], 1)

    def test_api_send_to_pharmacy_permissions(self):
        rx = m.Prescription.objects.create(visit=self.visit)
        m.PrescriptionItem.objects.create(prescription=rx, medicine_name="Bryonia", potency="30C")
        url = f"/api/prescriptions/{rx.pk}/send-to-pharmacy/"
        self.assertEqual(self.api(Role.ATTENDER).post(url, {}, format="json").status_code, 403)
        response = self.api(Role.DOCTOR).post(url, {"keep_open": True}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["items"][0]["medicine_name"], "Bryonia")
        self.assertEqual(self.api(Role.DOCTOR).post(url, {}, format="json").status_code, 200)  # unchanged -> no new version

    def test_lab_blocked_from_pharmacy_and_notifications(self):
        order = self.send_first_version()
        api = self.api(Role.LAB)
        for method, url in (("get", "/api/dispense-orders/"), ("get", f"/api/dispense-orders/{order.pk}/"),
                            ("post", f"/api/dispense-orders/{order.pk}/mark-ready/"),
                            ("get", "/api/notifications/unread/"), ("post", "/api/notifications/read-all/"),
                            ("post", "/api/notifications/1/read/"),
                            ("post", f"/api/prescriptions/{order.prescription_id}/send-to-pharmacy/")):
            self.assertEqual(getattr(api, method)(url).status_code, 403, url)
        self.login(Role.LAB)
        for url in (reverse("pharmacy_queue"), reverse("pharmacy_label", args=[order.pk])):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse("pharmacy_action", args=[order.pk, "mark-ready"])).status_code, 403)
