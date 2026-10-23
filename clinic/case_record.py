"""Builds, validates and saves the single-page case record for one visit.

Every section is built only if the user may view it, bound to POST data only if
the user may edit it, and saved only if it actually changed. That keeps the
RBAC decision in accounts.rbac and out of the template.
"""
from django.db import transaction

from accounts.models import Role
from accounts.rbac import role_of, section_access
from core.validators import document_validators, image_validators, run_validators

from . import forms as f
from . import models as m

# section key -> (form class, model) for one-to-one visit sections
SIMPLE_SECTIONS = {
    "case_history": (f.CaseHistoryForm, m.CaseHistory),
    "physical_generals": (f.PhysicalGeneralForm, m.PhysicalGeneral),
    "mental_generals": (f.MentalGeneralForm, m.MentalGeneral),
    "examination": (f.ExaminationForm, m.Examination),
    "diagnosis": (f.DiagnosisForm, m.Diagnosis),
    "past_treatment": (f.PastTreatmentForm, m.PastTreatment),
}

# multi-file <input name=...> -> (section that must be editable, validators)
UPLOAD_INPUTS = {
    "investigation_files": ("investigation_reports", document_validators(20)),
    "past_treatment_files": ("past_treatment", document_validators(20)),
    "before_images": ("patient_images", image_validators(5)),
    "after_images": ("patient_images", image_validators(5)),
}

# Rows shown on a blank record, matching the paper form.
DEFAULT_ROWS = {"repertory": 5, "prescription": 4, "followups": 3}


class CaseRecord:
    def __init__(self, request, visit):
        self.request = request
        self.user = request.user
        self.visit = visit
        self.patient = visit.patient
        self.access = section_access(self.user)
        self.bound = request.method == "POST"
        self.forms = {}
        self.formsets = {}
        self.upload_errors = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _form(self, section, form_class, instance, prefix=None):
        access = self.access[section]
        if not access["view"]:
            return None
        editable = access["edit"] and self.bound
        return form_class(
            self.request.POST if editable else None,
            self.request.FILES if editable else None,
            instance=instance,
            prefix=prefix or section,
            read_only=not access["edit"],
            empty_permitted=instance.pk is None,
        )

    def _formset(self, section, model, parent_model, form_class, parent, prefix, related_name):
        access = self.access[section]
        if not access["view"]:
            return None
        existing = getattr(parent, related_name).count() if parent.pk else 0
        rows = max(1, DEFAULT_ROWS[section] - existing) if access["edit"] else 0
        formset_class = f.formset_for(model, parent_model, form_class, rows)
        editable = access["edit"] and self.bound
        return formset_class(
            self.request.POST if editable else None,
            self.request.FILES if editable else None,
            instance=parent,
            prefix=prefix,
            read_only=not access["edit"],
        )

    def _build(self):
        visit = self.visit
        self.forms["patient"] = self._form("patient", f.PatientForm, self.patient)
        self.forms["visit"] = self._form("visit", f.VisitForm, visit)
        for section, (form_class, model) in SIMPLE_SECTIONS.items():
            instance = visit.section(section) or model(visit=visit)
            self.forms[section] = self._form(section, form_class, instance)

        self.investigation = visit.investigations.first() or m.Investigation(visit=visit)
        self.forms["investigations"] = self._form("investigations", f.InvestigationForm, self.investigation)

        self.repertory = visit.section("repertory") or m.RepertoryAnalysis(visit=visit)
        self.forms["repertory"] = self._form("repertory", f.RepertoryAnalysisForm, self.repertory)
        self.formsets["repertory"] = self._formset(
            "repertory", m.RepertoryEntry, m.RepertoryAnalysis, f.RepertoryEntryForm, self.repertory, "rep", "entries"
        )

        self.prescription = visit.prescriptions.first() or m.Prescription(visit=visit)
        self.forms["prescription"] = self._form("prescription", f.PrescriptionForm, self.prescription)
        self.formsets["prescription"] = self._formset(
            "prescription", m.PrescriptionItem, m.Prescription, f.PrescriptionItemForm, self.prescription, "rx", "items"
        )

        self.formsets["followups"] = self._formset(
            "followups", m.FollowUp, m.Visit, f.FollowUpForm, visit, "fu", "followups"
        )

    # ------------------------------------------------------------- validate
    def _editable(self, section, obj):
        return obj is not None and self.access[section]["edit"]

    def is_valid(self):
        valid = True
        for section, form in self.forms.items():
            if self._editable(section, form) and not form.is_valid():
                valid = False
        for section, formset in self.formsets.items():
            if self._editable(section, formset) and not formset.is_valid():
                valid = False
        for name, (section, validators) in UPLOAD_INPUTS.items():
            if not self.access[section]["edit"]:
                continue
            for upload in self.request.FILES.getlist(name):
                errors = run_validators(upload, validators)
                if errors:
                    self.upload_errors.setdefault(name, []).append(f"{upload.name}: {' '.join(errors)}")
                    valid = False
        return valid

    @property
    def error_sections(self):
        """Sections to force open so the user sees what failed validation."""
        sections = {s for s, form in self.forms.items() if form is not None and form.is_bound and form.errors}
        sections |= {
            s for s, fs in self.formsets.items()
            if fs is not None and fs.is_bound and (any(fs.errors) or fs.non_form_errors())
        }
        for name in self.upload_errors:
            sections.add({"investigation_files": "investigations", "past_treatment_files": "past_treatment"}.get(name, "patient_images"))
        return sections

    # ----------------------------------------------------------------- save
    @transaction.atomic
    def save(self):
        visit = self.visit
        for section in ("patient", "visit", *SIMPLE_SECTIONS, "investigations"):
            form = self.forms.get(section)
            if self._editable(section, form) and form.has_changed():
                if section == "investigations" and form.instance.pk is None and not form.cleaned_data.get("request_notes"):
                    continue  # don't create an empty investigation request
                form.save()

        for section, parent_attr in (("repertory", "repertory"), ("prescription", "prescription")):
            form, formset = self.forms.get(section), self.formsets.get(section)
            if not self._editable(section, form):
                continue
            if form.has_changed() or formset.has_changed():
                parent = form.save()
                setattr(self, parent_attr, parent)
                formset.instance = parent
                formset.save()

        followups = self.formsets.get("followups")
        if self._editable("followups", followups):
            followups.save()

        self._save_uploads()
        return visit

    def _save_uploads(self):
        files = self.request.FILES
        if self.access["investigation_reports"]["edit"] and files.getlist("investigation_files"):
            if self.investigation.pk is None:
                self.investigation.save()
            from_lab = role_of(self.user) == Role.LAB
            for upload in files.getlist("investigation_files"):
                m.InvestigationReport.objects.create(
                    investigation=self.investigation, file=upload,
                    source=m.InvestigationReport.Source.LAB if from_lab else m.InvestigationReport.Source.EXTERNAL,
                )
            if from_lab and self.investigation.status != m.Investigation.Status.COMPLETED:
                self.investigation.status = m.Investigation.Status.COMPLETED
                self.investigation.save(update_fields=["status"])

        if self.access["past_treatment"]["edit"] and files.getlist("past_treatment_files"):
            # The reverse one-to-one cache may hold the unsaved instance built for the form.
            past = self.visit.section("past_treatment") or m.PastTreatment(visit=self.visit)
            if past.pk is None:
                past.save()
            for upload in files.getlist("past_treatment_files"):
                m.PastTreatmentAttachment.objects.create(past_treatment=past, file=upload)

        if self.access["patient_images"]["edit"]:
            for name, stage in (("before_images", m.PatientImage.Stage.BEFORE), ("after_images", m.PatientImage.Stage.AFTER)):
                for upload in files.getlist(name):
                    m.PatientImage.objects.create(patient=self.patient, visit=self.visit, stage=stage, image=upload)


# Field layout of the paper form: (field, bootstrap-icon, chip background, chip colour)
PHYSICAL_LAYOUT = [
    [("sleep", "moon-stars-fill"), ("dreams", "cloud-fill"), ("thirst", "droplet-fill"), ("appetite", "egg-fried"),
     ("desires", "heart-fill"), ("aversions", "x-circle"), ("constitution", "person-fill")],
    [("urine", "droplet-half"), ("stool", "circle-half"), ("sweat", "moisture"), ("thermals", "thermometer-half"),
     ("menses", "gender-female"), ("obstetric_history", "person-standing-dress"), ("habits", "slash-circle")],
]

MENTAL_LAYOUT = [
    [("disposition", "person-fill", "#dbeafe", "#1d4ed8"),
     ("emotions", "heart-fill", "#fee2e2", "#dc2626"),
     ("triggers", "lightning-charge-fill", "#fef9c3", "#ca8a04"),
     ("fears", "exclamation-triangle-fill", "#ffedd5", "#ea580c"),
     ("anxiety", "emoji-frown", "#ede9fe", "#7c3aed"),
     ("anger", "emoji-angry", "#fee2e2", "#dc2626"),
     ("grief", "emoji-tear", "#e0f2fe", "#0284c7"),
     ("sensitivity", "shield-fill", "#dcfce7", "#16a34a")],
    [("company_social", "people-fill", "#ede9fe", "#7c3aed"),
     ("relationships", "people", "#fce7f3", "#db2777"),
     ("confidence", "person-fill-check", "#dbeafe", "#1d4ed8"),
     ("responsibility_work", "briefcase-fill", "#dcfce7", "#16a34a"),
     ("concentration", "lightbulb-fill", "#ffedd5", "#ea580c"),
     ("likes", "hand-thumbs-up-fill", "#dbeafe", "#1d4ed8"),
     ("dislikes", "hand-thumbs-down-fill", "#fee2e2", "#dc2626"),
     ("communication", "chat-dots-fill", "#e0e7ff", "#4f46e5")],
]
