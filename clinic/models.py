import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from core.models import AuditedModel
from core.validators import document_validators, image_validators, sheet_validators

phone_validator = RegexValidator(r"^\+?[0-9 \-]{7,20}$", "Enter a valid phone number.")
bp_validator = RegexValidator(r"^\d{2,3}\s*/\s*\d{2,3}$", "Use the format 120/80.")


def _upload_to(kind):
    """Store files as patients/<OPD>/<kind>/<random>.<ext> so names can't be guessed."""

    def path(instance, filename):
        patient = instance.get_patient()
        ext = Path(filename).suffix.lower()
        return f"patients/{patient.opd_number}/{kind}/{uuid.uuid4().hex}{ext}"

    path.__name__ = f"upload_{kind}"
    path.__qualname__ = path.__name__
    return path


# Django migrations need module-level callables for upload_to.
upload_photo = _upload_to("photo")
upload_investigations = _upload_to("investigations")
upload_past_treatment = _upload_to("past_treatment")
upload_images = _upload_to("images")
upload_repertory = _upload_to("repertory")


class UploadMixin:
    file_field = "file"

    @property
    def filename(self):
        stored = getattr(self, self.file_field)
        return Path(stored.name).name if stored else ""

    @property
    def is_image(self):
        return Path(self.filename).suffix.lower() in {".jpg", ".jpeg", ".png"}

    @property
    def is_pdf(self):
        return Path(self.filename).suffix.lower() == ".pdf"


# --------------------------------------------------------------------------- #
# Patient master
# --------------------------------------------------------------------------- #
class OPDCounter(models.Model):
    year = models.PositiveSmallIntegerField(unique=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "opd_counters"

    @classmethod
    def next_number(cls):
        year = timezone.localdate().year
        for _ in range(3):
            try:
                with transaction.atomic():
                    counter, _ = cls.objects.select_for_update().get_or_create(year=year)
                    counter.last_value += 1
                    counter.save(update_fields=["last_value"])
                    return f"OPD-{year}-{counter.last_value:05d}"
            except IntegrityError:  # two first-of-year registrations raced on get_or_create
                continue
        raise RuntimeError("Could not allocate an OPD number")


class Gender(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "O", "Other"


class Patient(AuditedModel):
    opd_number = models.CharField("OPD No.", max_length=20, unique=True, editable=False)
    registration_date = models.DateField(default=timezone.localdate)
    full_name = models.CharField(max_length=150, db_index=True)
    age = models.PositiveSmallIntegerField(validators=[MaxValueValidator(130)])
    gender = models.CharField("Sex", max_length=1, choices=Gender.choices)
    phone = models.CharField(max_length=20, validators=[phone_validator], db_index=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    occupation = models.CharField(max_length=120, blank=True)
    photo = models.ImageField(upload_to=upload_photo, blank=True, validators=image_validators(5))

    class Meta:
        db_table = "patients"
        ordering = ["-registration_date", "-id"]

    def __str__(self):
        return f"{self.full_name} ({self.opd_number})"

    def save(self, *args, **kwargs):
        if not self.opd_number:
            self.opd_number = OPDCounter.next_number()
        super().save(*args, **kwargs)

    def get_patient(self):
        return self

    def delete(self, using=None, keep_parents=False):
        with transaction.atomic():
            for visit in self.visits.all():
                visit.delete()
            return super().delete(using, keep_parents)

    @property
    def latest_visit(self):
        return self.visits.order_by("-visit_date", "-id").first()


# --------------------------------------------------------------------------- #
# Visits and their one-to-one clinical sections
# --------------------------------------------------------------------------- #
class Visit(AuditedModel):
    class Type(models.TextChoices):
        NEW = "NEW", "New case"
        FOLLOW_UP = "FOLLOW_UP", "Follow-up"

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="visits")
    visit_date = models.DateField(default=timezone.localdate, db_index=True)
    visit_type = models.CharField(max_length=10, choices=Type.choices, default=Type.NEW)
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="visits",
        limit_choices_to={"role": "DOCTOR", "is_active": True},
    )

    class Meta:
        db_table = "visits"
        ordering = ["-visit_date", "-id"]

    def __str__(self):
        return f"{self.patient.full_name} – {self.visit_date:%d-%b-%Y}"

    def get_patient(self):
        return self.patient

    def section(self, name):
        """Return the related one-to-one section, or None if not yet recorded."""
        try:
            return getattr(self, name)
        except models.ObjectDoesNotExist:
            return None

    @property
    def diagnosis_summary(self):
        diagnosis = self.section("diagnosis")
        if diagnosis is None:
            return ""
        return diagnosis.final or diagnosis.provisional or diagnosis.differential

    @property
    def prescription_summary(self):
        items = PrescriptionItem.objects.filter(prescription__visit=self, prescription__is_deleted=False)
        return ", ".join(f"{i.medicine_name} {i.potency}".strip() for i in items)


class VisitSection(AuditedModel):
    """One-to-one section of a visit; subclasses declare `visit` with their related_name."""

    class Meta:
        abstract = True

    def get_patient(self):
        return self.visit.patient

    def __str__(self):
        return f"{self._meta.verbose_name} – {self.visit}"


class CaseHistory(VisitSection):
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="case_history")
    presenting_complaints = models.TextField()
    past_history = models.TextField(blank=True)
    family_history = models.TextField(blank=True)

    class Meta:
        db_table = "case_histories"
        verbose_name_plural = "case histories"


class PhysicalGeneral(VisitSection):
    class Thermal(models.TextChoices):
        HOT = "HOT", "Hot"
        CHILLY = "CHILLY", "Chilly"
        AMBI = "AMBI", "Ambithermal"

    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="physical_generals")
    sleep = models.CharField(max_length=255, blank=True)
    dreams = models.CharField(max_length=255, blank=True)
    thirst = models.CharField(max_length=255, blank=True)
    appetite = models.CharField(max_length=255, blank=True)
    desires = models.CharField(max_length=255, blank=True)
    aversions = models.CharField(max_length=255, blank=True)
    urine = models.CharField(max_length=255, blank=True)
    stool = models.CharField(max_length=255, blank=True)
    sweat = models.CharField(max_length=255, blank=True)
    thermals = models.CharField(max_length=10, choices=Thermal.choices, blank=True)
    menses = models.CharField(max_length=255, blank=True)
    obstetric_history = models.CharField("Obstetric", max_length=255, blank=True)
    constitution = models.CharField(max_length=255, blank=True)
    habits = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "physical_generals"


class MentalGeneral(VisitSection):
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="mental_generals")
    disposition = models.TextField("Disposition / Temperament", blank=True)
    company_social = models.TextField("Company / Social", blank=True)
    emotions = models.TextField(blank=True)
    relationships = models.TextField(blank=True)
    triggers = models.TextField("Ailments from / Triggers", blank=True)
    confidence = models.TextField("Confidence / Self-esteem", blank=True)
    fears = models.TextField(blank=True)
    responsibility_work = models.TextField("Responsibility / Work", blank=True)
    anxiety = models.TextField("Anxiety / Worries", blank=True)
    concentration = models.TextField("Concentration / Memory", blank=True)
    anger = models.TextField("Anger / Irritability", blank=True)
    likes = models.TextField(blank=True)
    grief = models.TextField("Grief / Disappointment", blank=True)
    dislikes = models.TextField(blank=True)
    sensitivity = models.TextField(blank=True)
    communication = models.TextField("Communication / Expression", blank=True)

    class Meta:
        db_table = "mental_generals"


class Examination(VisitSection):
    class Pallor(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        ABSENT = "ABSENT", "Absent"

    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="examination")
    pulse = models.PositiveSmallIntegerField("Pulse (bpm)", null=True, blank=True,
                                             validators=[MinValueValidator(20), MaxValueValidator(250)])
    blood_pressure = models.CharField("BP (mmHg)", max_length=10, blank=True, validators=[bp_validator])
    weight = models.DecimalField("Wt. (kg)", max_digits=5, decimal_places=1, null=True, blank=True,
                                 validators=[MinValueValidator(Decimal("0"))])
    temperature = models.DecimalField("Temp. (°F)", max_digits=4, decimal_places=1, null=True, blank=True,
                                      validators=[MinValueValidator(Decimal("90")), MaxValueValidator(Decimal("110"))])
    respiratory_rate = models.PositiveSmallIntegerField("RR (/min)", null=True, blank=True,
                                                        validators=[MaxValueValidator(80)])
    pallor = models.CharField(max_length=10, choices=Pallor.choices, blank=True)

    class Meta:
        db_table = "examinations"


class Diagnosis(VisitSection):
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="diagnosis")
    provisional = models.CharField("Provisional diagnosis", max_length=500, blank=True)
    differential = models.CharField("Differential diagnosis", max_length=500, blank=True)
    final = models.CharField("Final diagnosis", max_length=500, blank=True)

    class Meta:
        db_table = "diagnoses"
        verbose_name_plural = "diagnoses"


# --------------------------------------------------------------------------- #
# Investigations
# --------------------------------------------------------------------------- #
class Investigation(AuditedModel):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        COMPLETED = "COMPLETED", "Completed"

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="investigations")
    request_notes = models.TextField("Investigations", blank=True)
    requested_date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED, db_index=True)

    class Meta:
        db_table = "investigations"
        ordering = ["-requested_date", "-id"]

    def __str__(self):
        return f"Investigation – {self.visit}"

    def get_patient(self):
        return self.visit.patient


class InvestigationReport(UploadMixin, AuditedModel):
    class Source(models.TextChoices):
        EXTERNAL = "EXTERNAL", "External (from patient)"
        LAB = "LAB", "Clinic laboratory"

    investigation = models.ForeignKey(Investigation, on_delete=models.CASCADE, related_name="reports")
    file = models.FileField(upload_to=upload_investigations, validators=document_validators(20))
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.EXTERNAL)
    title = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "investigation_reports"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or self.filename

    def get_patient(self):
        return self.investigation.visit.patient


# --------------------------------------------------------------------------- #
# Past treatment
# --------------------------------------------------------------------------- #
class PastTreatment(VisitSection):
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="past_treatment")
    previous_medication = models.TextField(blank=True, help_text="Allopathic / homeopathic medicines taken")
    duration = models.CharField(max_length=120, blank=True)
    outcome = models.TextField("Outcome / response", blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "past_treatments"


class PastTreatmentAttachment(UploadMixin, AuditedModel):
    past_treatment = models.ForeignKey(PastTreatment, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=upload_past_treatment, validators=document_validators(20))

    class Meta:
        db_table = "past_treatment_attachments"
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename

    def get_patient(self):
        return self.past_treatment.visit.patient


# --------------------------------------------------------------------------- #
# Before / after images
# --------------------------------------------------------------------------- #
class PatientImage(UploadMixin, AuditedModel):
    class Stage(models.TextChoices):
        BEFORE = "BEFORE", "Before treatment"
        AFTER = "AFTER", "After treatment"

    file_field = "image"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="images")
    visit = models.ForeignKey(Visit, null=True, blank=True, on_delete=models.SET_NULL, related_name="images")
    stage = models.CharField(max_length=10, choices=Stage.choices, db_index=True)
    image = models.ImageField(upload_to=upload_images, validators=image_validators(5))
    caption = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "patient_images"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_stage_display()} – {self.patient.full_name}"

    def get_patient(self):
        return self.patient

    @property
    def upload_date(self):
        return self.created_at

    @property
    def uploaded_by(self):
        return self.created_by


# --------------------------------------------------------------------------- #
# Repertory
# --------------------------------------------------------------------------- #
class RepertoryAnalysis(UploadMixin, VisitSection):
    file_field = "sheet"

    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="repertory")
    notes = models.TextField("Repertory notes / analysis", blank=True)
    sheet = models.FileField("Repertorization sheet", upload_to=upload_repertory, blank=True,
                             validators=sheet_validators(10))

    class Meta:
        db_table = "repertory_analysis"
        verbose_name_plural = "repertory analyses"


class RepertoryEntry(AuditedModel):
    class Book(models.TextChoices):
        KENT = "KENT", "Kent"
        SYNTHESIS = "SYNTHESIS", "Synthesis"
        COMPLETE = "COMPLETE", "Complete Repertory"
        MURPHY = "MURPHY", "Murphy"
        BOERICKE = "BOERICKE", "Boericke"
        BBCR = "BBCR", "Boenninghausen (BBCR)"
        PHATAK = "PHATAK", "Phatak"
        OTHER = "OTHER", "Other"

    analysis = models.ForeignKey(RepertoryAnalysis, on_delete=models.CASCADE, related_name="entries")
    symptom = models.CharField(max_length=255, blank=True)
    rubric = models.CharField(max_length=255)
    book = models.CharField(max_length=12, choices=Book.choices, default=Book.KENT)
    grade = models.PositiveSmallIntegerField(default=1, choices=[(i, str(i)) for i in range(1, 5)])
    remedies = models.CharField(
        max_length=1000, blank=True,
        help_text="Remedies in this rubric with their grade, e.g. 'Nux-v:3, Sulph:2, Puls:1'",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "repertory_entries"
        ordering = ["order", "id"]
        verbose_name_plural = "repertory entries"

    def __str__(self):
        return self.rubric

    def remedy_grades(self):
        """Parse 'Nux-v:3, Sulph' into {'Nux-v': 3, 'Sulph': 1}."""
        result = {}
        for chunk in self.remedies.split(","):
            name, _, grade = chunk.strip().partition(":")
            if name.strip():
                result[name.strip()] = int(grade) if grade.strip().isdigit() else 1
        return result


# --------------------------------------------------------------------------- #
# Prescriptions
# --------------------------------------------------------------------------- #
class Potency(models.TextChoices):
    Q = "Q", "Q (Mother tincture)"
    X3 = "3X", "3X"
    X6 = "6X", "6X"
    C6 = "6C", "6C"
    C30 = "30C", "30C"
    C200 = "200C", "200C"
    M1 = "1M", "1M"
    M10 = "10M", "10M"
    M50 = "50M", "50M"
    CM = "CM", "CM"
    LM1 = "0/1", "0/1 (LM1)"
    LM2 = "0/2", "0/2 (LM2)"
    LM3 = "0/3", "0/3 (LM3)"


class Prescription(AuditedModel):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="prescriptions")
    prescribed_on = models.DateField(default=timezone.localdate)
    dietary_advice = models.TextField(blank=True)
    followup_advice = models.TextField("Follow-up advice", blank=True)

    class Meta:
        db_table = "prescriptions"
        ordering = ["-prescribed_on", "-id"]

    def __str__(self):
        return f"Prescription – {self.visit}"

    def get_patient(self):
        return self.visit.patient


class PrescriptionItem(AuditedModel):
    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="items")
    medicine_name = models.CharField("Medicine", max_length=150)
    potency = models.CharField(max_length=10, choices=Potency.choices, blank=True)
    duration = models.CharField(max_length=60, blank=True)
    instructions = models.CharField(max_length=255, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "prescription_items"
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.medicine_name} {self.potency}".strip()


# --------------------------------------------------------------------------- #
# Follow-up history
# --------------------------------------------------------------------------- #
class FollowUp(AuditedModel):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="followups")
    visit_date = models.DateField("Date", default=timezone.localdate)
    progress_notes = models.TextField("Description / progress", blank=True)
    medicines_given = models.CharField("Medicine", max_length=255, blank=True)
    next_followup_date = models.DateField("Next follow-up", null=True, blank=True, db_index=True)

    class Meta:
        db_table = "followups"
        ordering = ["visit_date", "id"]

    def __str__(self):
        return f"Follow-up {self.visit_date} – {self.visit.patient.full_name}"

    def get_patient(self):
        return self.visit.patient
