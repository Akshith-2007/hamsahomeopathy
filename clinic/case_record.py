"""Builds, validates and saves the single-page case record for one visit.

Every section is built only if the user may view it, bound to POST data only if
the user may edit it, and saved only if it actually changed. That keeps the
RBAC decision in accounts.rbac and out of the template.
"""
from django.db import transaction

from accounts.models import Role
from accounts.rbac import can_edit, can_view, role_of, section_access
from core.models import AuditLog
from core.validators import document_validators, image_validators, run_validators

from . import forms as f
from . import models as m
from . import pharmacy, services
from .family import family_history, new_information, summary_text
from .prefill import STABLE_SECTIONS, latest_history, source_label

# rbac section -> (form class, model) for one-to-one visit sections
SIMPLE_SECTIONS = {
    "case_history": (f.CaseHistoryForm, m.CaseHistory),
    "followup_assessment": (f.FollowUpNoteForm, m.FollowUpNote),
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
        self.notes = []      # extra success messages for the view
        self.warnings = []   # business-rule refusals (e.g. nothing to send)
        self.prefill = {}    # section -> {field: (value, source visit or None for family records)}
        self.history_values = {}  # section -> {field: (value, source visit)} for "Refresh from history"
        self.family_entries = None
        self.family_autofill_text = ""
        self.family_new_info = ""
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
        self.original_visit_type = visit.visit_type
        self.forms["visit"] = self._form("visit", f.VisitForm, visit)
        if self.forms["visit"] is not None and not can_edit(self.user, "visit_type"):
            self.forms["visit"].fields["visit_type"].disabled = True
        instances = {}
        for section, (form_class, model) in SIMPLE_SECTIONS.items():
            related_name = model._meta.get_field("visit").remote_field.related_name
            instances[section] = visit.section(related_name) or model(visit=visit)
        self._apply_prefill(instances)
        for section, (form_class, model) in SIMPLE_SECTIONS.items():
            self.forms[section] = self._form(section, form_class, instances[section])

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

    # ------------------------------------------------------------ pre-fill
    @property
    def prefill_open(self):
        """Pre-fill only applies to visits still in progress; completed records are never rewritten."""
        return self.visit.status != m.Visit.Status.COMPLETED

    def _apply_prefill(self, instances):
        """Copy stable history (own earlier visits, then linked relatives) into empty fields of unsaved values.

        Only touches in-memory instances: nothing is written unless the user saves.
        """
        editable = [s for s in STABLE_SECTIONS if self.access[s]["edit"]]
        if editable and can_view(self.user, "history_prefill"):
            self.history_values = latest_history(self.patient, self.visit, sections=editable)
            if self.prefill_open:
                for section, values in self.history_values.items():
                    instance = instances[section]
                    for name, (value, source) in values.items():
                        if getattr(instance, name) in (None, ""):
                            setattr(instance, name, value)
                            self.prefill.setdefault(section, {})[name] = (value, source)

        case = instances["case_history"]
        if can_view(self.user, "family_history") and self.access["case_history"]["edit"]:
            self.family_entries = family_history(self.patient)
            text = summary_text(self.family_entries)
            if not case.family_history and text and self.prefill_open:
                case.family_history = text
                self.family_autofill_text = text
                self.prefill.setdefault("case_history", {})["family_history"] = (text, None)
            elif case.family_history and text:
                self.family_new_info = new_information(self.family_entries, case.family_history)

    def prefill_payload(self):
        """JSON for the page: {input name: {value, label, prefilled}} plus the sections that support refresh."""
        fields = {}
        for section, values in self.history_values.items():
            for name, (value, source) in values.items():
                fields[f"{section}-{name}"] = {"value": value, "label": source_label(source), "prefilled": False}
        for section, values in self.prefill.items():
            for name, (value, source) in values.items():
                fields[f"{section}-{name}"] = {"value": value, "label": source_label(source), "prefilled": True}
        return {"fields": fields, "sections": sorted({key.split("-", 1)[0] for key in fields})}

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
            if not self._editable(section, form):
                continue
            # Pre-filled history counts as entered data: it is stored when the user saves.
            if form.has_changed() or section in self.prefill:
                if section == "investigations" and form.instance.pk is None and not form.cleaned_data.get("request_notes"):
                    continue  # don't create an empty investigation request
                self._record_prefill_sources(section, form)
                form.save()

        prescription_changed = False
        for section, parent_attr in (("repertory", "repertory"), ("prescription", "prescription")):
            form, formset = self.forms.get(section), self.formsets.get(section)
            if not self._editable(section, form):
                continue
            if form.has_changed() or formset.has_changed():
                parent = form.save()
                setattr(self, parent_attr, parent)
                formset.instance = parent
                formset.save()
                prescription_changed |= section == "prescription" and formset.has_changed()

        followups = self.formsets.get("followups")
        if self._editable("followups", followups):
            followups.save()

        self._save_uploads()

        if visit.visit_type != self.original_visit_type:
            services.record_visit_type_change(visit, self.original_visit_type, self.user)
        if self.request.POST.get("complete") and can_edit(self.user, "consultation"):
            services.set_visit_status(visit, m.Visit.Status.COMPLETED)
        self._pharmacy(prescription_changed)
        case_form = self.forms.get("case_history")
        if (self.request.POST.get("family_history_inserted") == "1" and can_edit(self.user, "family_history")
                and self._editable("case_history", case_form) and "family_history" in case_form.changed_data):
            AuditLog.record(AuditLog.Action.FAMILY_HISTORY_INSERTED, case_form.instance, user=self.user,
                            changes={"patient": self.patient.pk})
        return visit

    def _record_prefill_sources(self, section, form):
        """Remember which earlier visit each kept pre-filled value came from, and audit it."""
        values = self.prefill.get(section)
        if not values:
            return
        cleaned = getattr(form, "cleaned_data", {}) or {}
        sources, family_kept = {}, False
        for name, (value, source) in values.items():
            current = cleaned.get(name, getattr(form.instance, name))
            if str(current or "") != str(value or ""):
                continue  # edited by the user: no longer a pre-filled value
            if source is None:
                family_kept = True
                sources[name] = "family"
            else:
                sources[name] = source.pk
        if sources:
            form.instance.prefill_sources = {**(form.instance.prefill_sources or {}), **sources}
            visit_sources = {k: v for k, v in sources.items() if v != "family"}
            if visit_sources:
                AuditLog.record(AuditLog.Action.HISTORY_PREFILLED, self.visit, user=self.user,
                                changes={"section": section, "source_visits": visit_sources})
        if family_kept:
            AuditLog.record(AuditLog.Action.FAMILY_HISTORY_AUTOFILLED, self.visit, user=self.user,
                            changes={"relatives": [e.relative.pk for e in self.family_entries or []]})

    def _pharmacy(self, prescription_changed):
        send = self.request.POST.get("send_to_pharmacy")
        if send and can_edit(self.user, "pharmacy_send"):
            if self.prescription.pk is None:
                self.warnings.append("Add at least one medicine before sending the prescription to the pharmacy.")
                return
            try:
                order, created = pharmacy.send_to_pharmacy(self.prescription, self.user, keep_open=send == "keep_open")
            except services.WorkflowError as exc:
                self.warnings.append(str(exc))
            else:
                self.notes.append(f"Prescription sent to the pharmacy (version {order.version})." if created
                                  else "The pharmacy already has this prescription.")
        elif prescription_changed and can_edit(self.user, "pharmacy_send"):
            order = pharmacy.sync_after_edit(self.prescription, self.user)
            if order is not None:
                self.notes.append(f"Updated prescription sent to the pharmacy (version {order.version}).")

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
