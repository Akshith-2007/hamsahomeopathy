"""Create one user per role plus realistic sample patients, visits and prescriptions."""
import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Role
from clinic import models as m
from core.middleware import acting_as

User = get_user_model()

DEMO_USERS = [
    # username, role, first, last, extra
    ("admin", Role.ADMIN, "Clinic", "Admin", {"is_staff": True, "is_superuser": True}),
    ("doctor", Role.DOCTOR, "Hamsa", "Rao", {"qualification": "BHMS, MD (Hom)", "registration_number": "TS-HOM-10234"}),
    ("reception", Role.ATTENDER, "Lakshmi", "K", {}),
    ("lab", Role.LAB, "Ravi", "Teja", {}),
]

CASES = [
    {
        "complaint": "Throbbing right-sided headache for 6 months, worse from sun and noise, better by pressure and dark room.",
        "provisional": "Migraine without aura", "final": "Migraine",
        "rubrics": [("Headache", "HEAD - PAIN - sun, from exposure to", "KENT", 3, "Nat-m:3, Glon:3, Bell:2, Lach:2"),
                    ("Headache better pressure", "HEAD - PAIN - pressure - amel.", "KENT", 2, "Bry:3, Nat-m:2, Mag-p:2, Puls:1"),
                    ("Irritable", "MIND - IRRITABILITY - noise, from", "SYNTHESIS", 2, "Nux-v:3, Bell:2, Nat-m:1")],
        "medicines": [("Natrum Muriaticum", "200C", "1 dose", "4 pills on tongue, empty stomach"), ("Placebo", "", "15 days", "4 pills twice daily")],
        "investigation": "CBC, Thyroid profile",
    },
    {
        "complaint": "Itchy eczema on both hands, worse at night and from washing, burning after scratching.",
        "provisional": "Atopic dermatitis", "final": "Chronic eczema",
        "rubrics": [("Itching worse night", "SKIN - ITCHING - night", "KENT", 3, "Sulph:3, Psor:3, Merc:2, Ars:2"),
                    ("Worse washing", "SKIN - ERUPTIONS - washing agg.", "KENT", 2, "Sulph:3, Clem:2, Rhus-t:1"),
                    ("Burning after scratching", "SKIN - ITCHING - scratching - burning after", "KENT", 2, "Sulph:2, Ars:3, Merc:1")],
        "medicines": [("Sulphur", "30C", "7 days", "2 drops in water, morning")],
        "investigation": "Serum IgE, Absolute eosinophil count",
    },
    {
        "complaint": "Irregular menses with weight gain and acne for 1 year. Weepy, desires consolation.",
        "provisional": "PCOD", "final": "Polycystic ovarian disease",
        "rubrics": [("Weeping, consolation", "MIND - CONSOLATION - amel.", "KENT", 3, "Puls:3, Phos:2, Lyc:1"),
                    ("Menses irregular", "FEMALE GENITALIA - MENSES - irregular", "KENT", 2, "Puls:3, Sep:3, Nat-m:2, Lyc:2"),
                    ("Thirstless", "STOMACH - THIRSTLESS", "KENT", 2, "Puls:3, Gels:2, Apis:2")],
        "medicines": [("Pulsatilla", "200C", "1 dose weekly", "4 pills dry on tongue"), ("Placebo", "", "1 month", "4 pills twice daily")],
        "investigation": "USG pelvis, LH/FSH ratio, Fasting insulin",
    },
    {
        "complaint": "Wheezing and breathlessness after midnight, needs to sit up, restless and anxious with chilliness.",
        "provisional": "Bronchial asthma", "final": "Bronchial asthma",
        "rubrics": [("After midnight", "RESPIRATION - ASTHMATIC - midnight - after", "KENT", 3, "Ars:3, Samb:2, Kali-c:2"),
                    ("Restless anxiety", "MIND - ANXIETY - restlessness, with", "KENT", 3, "Ars:3, Acon:3, Rhus-t:2"),
                    ("Chilly", "GENERALS - HEAT - vital, lack of", "KENT", 1, "Ars:3, Kali-c:2, Psor:2")],
        "medicines": [("Arsenicum Album", "30C", "10 days", "2 drops twice daily")],
        "investigation": "Chest X-ray PA view, PFT",
    },
    {
        "complaint": "Burning in stomach and sour belching after spicy food and late nights. Constipated with ineffectual urging.",
        "provisional": "Gastritis", "final": "Acid peptic disease",
        "rubrics": [("Sour eructations", "STOMACH - ERUCTATIONS - sour", "KENT", 2, "Nux-v:3, Lyc:2, Carb-v:2"),
                    ("Ineffectual urging", "RECTUM - CONSTIPATION - ineffectual urging", "KENT", 3, "Nux-v:3, Lyc:2, Sep:1"),
                    ("Irritable, hurried", "MIND - HURRY", "KENT", 2, "Nux-v:2, Lyc:1, Sulph:1")],
        "medicines": [("Nux Vomica", "30C", "7 days", "2 drops at bedtime")],
        "investigation": "",
    },
    {
        "complaint": "Stiffness and pain in knees and lower back, worse on first motion and in damp weather, better continued motion.",
        "provisional": "Osteoarthritis knees", "final": "Osteoarthritis",
        "rubrics": [("Worse first motion", "EXTREMITIES - PAIN - motion - beginning of - agg.", "KENT", 3, "Rhus-t:3, Puls:2, Lyc:1"),
                    ("Worse damp", "GENERALS - WEATHER - wet agg.", "KENT", 2, "Rhus-t:3, Dulc:3, Nat-s:2")],
        "medicines": [("Rhus Toxicodendron", "200C", "3 doses", "Every 15 days"), ("Calcarea Fluorica", "6X", "1 month", "4 tablets thrice daily")],
        "investigation": "X-ray both knees, RA factor, Vitamin D",
    },
]

NAMES = [
    ("Ananya Reddy", 29, "F", "Software engineer"), ("Suresh Kumar", 46, "M", "Bank officer"),
    ("Priya Sharma", 24, "F", "Student"), ("Venkat Rao", 58, "M", "Retired teacher"),
    ("Kavya Iyer", 33, "F", "Homemaker"), ("Mohammed Irfan", 39, "M", "Business"),
    ("Sneha Patil", 27, "F", "Nurse"), ("Rajesh Gupta", 51, "M", "Accountant"),
    ("Divya Nair", 36, "F", "Teacher"), ("Arjun Varma", 19, "M", "Student"),
    ("Lalitha Devi", 63, "F", "Homemaker"), ("Kiran Babu", 42, "M", "Driver"),
]


class Command(BaseCommand):
    help = "Seed demo users (one per role) and sample patient records."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="Hamsa@2026", help="Password for all demo users")
        parser.add_argument("--patients", type=int, default=len(NAMES))

    @transaction.atomic
    def handle(self, *args, password, patients, **options):
        users = {}
        for username, role, first, last, extra in DEMO_USERS:
            user, created = User.objects.get_or_create(
                username=username, defaults={"role": role, "first_name": first, "last_name": last, **extra}
            )
            if created:
                user.set_password(password)
                user.save()
            users[role] = user
            self.stdout.write(f"{'created' if created else 'exists '} {username:10} ({role})")

        if m.Patient.objects.exists():
            self.stdout.write(self.style.WARNING("Patients already exist; skipping sample records."))
            return

        rng = random.Random(42)
        doctor, attender, lab = users[Role.DOCTOR], users[Role.ATTENDER], users[Role.LAB]
        today = timezone.localdate()
        for index, (name, age, gender, occupation) in enumerate(NAMES[:patients]):
            case = CASES[index % len(CASES)]
            with acting_as(attender):
                patient = m.Patient.objects.create(
                    full_name=name, age=age, gender=gender, occupation=occupation,
                    phone=f"9{rng.randint(100000000, 999999999)}", address="Hyderabad, Telangana",
                    registration_date=today - timedelta(days=120 - index * 7),
                )
            visit_count = 1 + index % 3
            previous = None
            for n in range(visit_count):
                # The last visit of the first few patients is today, for the dashboard.
                days_ago = 0 if (n == visit_count - 1 and index < 4) else (visit_count - n) * 30 + index
                previous = self._make_visit(patient, case, today - timedelta(days=days_ago), n, doctor, attender, lab,
                                            rng, previous)
            # Next appointment: a mix of overdue, due today and upcoming follow-ups.
            if index >= 4:
                offset = [-2, 0, 3, 5, 10, 21, -1, 7][index % 8]
                with acting_as(doctor):
                    m.ScheduledFollowUp.objects.create(
                        patient=patient, source_visit=previous, scheduled_by=doctor,
                        scheduled_date=today + timedelta(days=offset),
                        notes="Review response to " + case["medicines"][0][0] + ".",
                    )
        self._link_family(attender)
        self._send_prescriptions(doctor, today)
        self.stdout.write(self.style.SUCCESS(f"Created {patients} patients. Demo password: {password}"))

    def _link_family(self, attender):
        """A family sharing one mobile number. Manual links: Suresh's mother Lalitha, Suresh's daughter Priya,
        Lalitha's husband Venkat. Inference then adds Venkat as Suresh's father and Priya's grandfather, etc."""
        from clinic.relationships import link

        people = {p.full_name: p for p in m.Patient.objects.filter(
            full_name__in=["Lalitha Devi", "Suresh Kumar", "Priya Sharma", "Venkat Rao"])}
        if len(people) < 4:
            return
        suresh = people["Suresh Kumar"]
        for name in ("Lalitha Devi", "Priya Sharma", "Venkat Rao"):
            people[name].phone = suresh.phone
            people[name].save(update_fields=["phone"])
        with acting_as(attender):
            link(suresh, people["Lalitha Devi"], m.Relationship.MOTHER, attender)
            link(suresh, people["Priya Sharma"], m.Relationship.DAUGHTER, attender)
            link(people["Lalitha Devi"], people["Venkat Rao"], m.Relationship.SPOUSE, attender)

    def _send_prescriptions(self, doctor, today):
        """Put two of today's prescriptions into the pharmacy queue."""
        from clinic.pharmacy import change_status, send_to_pharmacy

        todays = m.Prescription.objects.filter(visit__visit_date=today).select_related("visit")[:2]
        with acting_as(doctor):
            for index, rx in enumerate(todays):
                order, _ = send_to_pharmacy(rx, doctor, keep_open=index > 0)
                if index == 0:
                    change_status(order, "start-preparing", doctor)

    def _make_visit(self, patient, case, visit_date, n, doctor, attender, lab, rng, previous=None):
        today = timezone.localdate()
        with acting_as(attender):
            visit = m.Visit.objects.create(
                patient=patient, visit_date=visit_date, doctor=doctor,
                visit_type=m.Visit.Type.NEW if n == 0 else m.Visit.Type.FOLLOW_UP,
                previous_visit=previous if n else None,
                status=m.Visit.Status.WAITING if visit_date == today else m.Visit.Status.COMPLETED,
            )
            m.Examination.objects.create(
                visit=visit, pulse=rng.randint(68, 92), blood_pressure=f"{rng.randint(110, 140)}/{rng.randint(70, 90)}",
                weight=rng.randint(48, 85), temperature=98.4, respiratory_rate=rng.randint(14, 20), pallor="ABSENT",
            )
            investigation = None
            if case["investigation"] and n == 0:
                investigation = m.Investigation.objects.create(visit=visit, request_notes=case["investigation"],
                                                               requested_date=visit_date)
        with acting_as(doctor):
            m.CaseHistory.objects.create(
                visit=visit, presenting_complaints=case["complaint"] if n == 0 else "Follow-up: " + rng.choice(
                    ["better by 40%", "frequency reduced", "mild aggravation first week, now better", "much improved"]),
                past_history="Typhoid in childhood.", family_history="Father – diabetes; mother – hypertension.",
            )
            m.PhysicalGeneral.objects.create(
                visit=visit, sleep="Disturbed, wakes 3 am", thirst="Large quantities", appetite="Good",
                desires="Spicy, sweets", aversions="Milk", thermals=rng.choice(["HOT", "CHILLY", "AMBI"]),
                stool="Regular", urine="Normal", sweat="Profuse on head",
            )
            m.MentalGeneral.objects.create(
                visit=visit, disposition="Reserved, sensitive", emotions="Weeps easily when alone",
                fears="Of dark and failure", anxiety="Anticipatory anxiety before exams/work",
                anger="Irritable when hungry", likes="Music", dislikes="Crowds", sensitivity="To reprimand",
            )
            if n:
                m.FollowUpNote.objects.create(
                    visit=visit, remedy_response=rng.choice(["BETTER", "BETTER", "SAME", "MIXED"]),
                    changes_since_last="Complaints less frequent; sleep improved.",
                    notes="Continue same remedy; review in 2 weeks.",
                )
            m.Diagnosis.objects.create(
                visit=visit, provisional=case["provisional"], final=case["final"] + (" – improving" if n else ""),
            )
            analysis = m.RepertoryAnalysis.objects.create(visit=visit, notes="Totality based on mentals and modalities.")
            for order, (symptom, rubric, book, grade, remedies) in enumerate(case["rubrics"]):
                m.RepertoryEntry.objects.create(analysis=analysis, symptom=symptom, rubric=rubric, book=book,
                                                grade=grade, remedies=remedies, order=order)
            rx = m.Prescription.objects.create(visit=visit, prescribed_on=visit_date,
                                               dietary_advice="Avoid coffee, mint and raw onion around doses.",
                                               followup_advice="Review after 15 days.")
            for order, (name, potency, duration, instructions) in enumerate(case["medicines"]):
                m.PrescriptionItem.objects.create(prescription=rx, medicine_name=name, potency=potency,
                                                  form=m.MedicineForm.PILLS, quantity="2 dram", dose=instructions,
                                                  duration=duration, instructions="Avoid coffee and mint",
                                                  order=order)
            m.FollowUp.objects.create(
                visit=visit, visit_date=visit_date, progress_notes="Medicine dispensed; patient counselled.",
                medicines_given=", ".join(f"{i[0]} {i[1]}" for i in case["medicines"]).strip(),
                next_followup_date=visit_date + timedelta(days=rng.choice([3, 5, 7, 15])),
            )
        # Half of the requested investigations already have a lab report uploaded.
        if investigation is not None and rng.random() < 0.5:
            with acting_as(lab):
                investigation.status = m.Investigation.Status.COMPLETED
                investigation.completed_at = timezone.now()
                investigation.save()
        return visit
