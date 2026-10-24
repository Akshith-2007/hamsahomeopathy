from django import forms
from django.contrib.auth import get_user_model
from django.forms import inlineformset_factory

from accounts.models import Role
from core.forms import BootstrapMixin

from . import models as m

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class PatientForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Patient
        fields = ["registration_date", "full_name", "age", "gender", "phone", "email", "occupation", "address", "photo"]
        widgets = {
            "registration_date": DATE,
            "address": forms.Textarea(attrs={"rows": 2}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png"}),
        }


class VisitForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Visit
        fields = ["visit_date", "visit_type", "doctor"]
        widgets = {"visit_date": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctor"].queryset = get_user_model().objects.filter(role=Role.DOCTOR, is_active=True)


class RegistrationVisitForm(BootstrapMixin, forms.ModelForm):
    """Visit details captured while registering a new patient (always a New case)."""

    class Meta:
        model = m.Visit
        fields = ["visit_date", "doctor"]
        widgets = {"visit_date": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctor"].queryset = get_user_model().objects.filter(role=Role.DOCTOR, is_active=True)


class FollowUpNoteForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.FollowUpNote
        exclude = ["visit"]
        widgets = {
            "changes_since_last": forms.Textarea(attrs={"rows": 3, "placeholder": "What has changed since the last visit?"}),
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Doctor's notes for this follow-up"}),
        }


TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


class ScheduleFollowUpForm(BootstrapMixin, forms.Form):
    scheduled_date = forms.DateField(label="Date", widget=DATE)
    scheduled_time = forms.TimeField(label="Time", required=False, widget=TIME)
    notes = forms.CharField(label="Notes for the doctor", required=False,
                            widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Optional clinical notes"}))
    replace = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, show_notes=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not show_notes:
            del self.fields["notes"]


class RescheduleFollowUpForm(BootstrapMixin, forms.Form):
    scheduled_date = forms.DateField(label="New date", widget=DATE)
    scheduled_time = forms.TimeField(label="Time", required=False, widget=TIME)


class CaseHistoryForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.CaseHistory
        fields = ["presenting_complaints", "past_history", "family_history"]
        widgets = {
            "presenting_complaints": forms.Textarea(attrs={"rows": 3, "placeholder": "Enter presenting complaints"}),
            "past_history": forms.Textarea(attrs={"placeholder": "Enter past history"}),
            "family_history": forms.Textarea(attrs={"placeholder": "Enter family history"}),
        }


class PhysicalGeneralForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.PhysicalGeneral
        exclude = ["visit"]


class MentalGeneralForm(BootstrapMixin, forms.ModelForm):
    textarea_rows = 1

    class Meta:
        model = m.MentalGeneral
        exclude = ["visit"]


class ExaminationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Examination
        exclude = ["visit"]
        widgets = {
            "pallor": forms.RadioSelect,
            "blood_pressure": forms.TextInput(attrs={"placeholder": "120/80"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pallor"].choices = [c for c in self.fields["pallor"].choices if c[0]]


class DiagnosisForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Diagnosis
        exclude = ["visit"]


class InvestigationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Investigation
        fields = ["request_notes", "requested_date", "status"]
        widgets = {
            "request_notes": forms.Textarea(attrs={"rows": 2, "placeholder": "Enter investigation details (e.g. blood tests, X-ray, USG, etc.)"}),
            "requested_date": DATE,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requested_date"].required = False
        self.fields["status"].required = False

    def clean_requested_date(self):
        return self.cleaned_data.get("requested_date") or self.instance.requested_date

    def clean_status(self):
        return self.cleaned_data.get("status") or self.instance.status


class PastTreatmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.PastTreatment
        exclude = ["visit"]
        widgets = {
            "previous_medication": forms.Textarea(attrs={"rows": 2, "placeholder": "Allopathic / homeopathic medicines taken"}),
        }


class RepertoryAnalysisForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.RepertoryAnalysis
        fields = ["notes", "sheet"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3, "placeholder": "Enter repertory analysis, justification for remedy selection, etc."}),
            "sheet": forms.ClearableFileInput(attrs={"accept": ".pdf,image/jpeg,image/png"}),
        }


class RepertoryEntryForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.RepertoryEntry
        fields = ["symptom", "rubric", "book", "grade", "remedies"]
        widgets = {"remedies": forms.TextInput(attrs={"placeholder": "Nux-v:3, Sulph:2"})}


class PrescriptionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.Prescription
        fields = ["dietary_advice", "followup_advice"]
        widgets = {
            "dietary_advice": forms.Textarea(attrs={"placeholder": "Enter dietary advice"}),
            "followup_advice": forms.Textarea(attrs={"placeholder": "Enter follow up advice"}),
        }


class PrescriptionItemForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.PrescriptionItem
        fields = ["medicine_name", "potency", "form", "quantity", "dose", "duration", "instructions"]
        widgets = {
            "medicine_name": forms.TextInput(attrs={"placeholder": "Enter medicine name", "list": "medicine-list"}),
            "quantity": forms.TextInput(attrs={"placeholder": "2 dram"}),
            "dose": forms.TextInput(attrs={"placeholder": "4 pills, 3 times daily"}),
            "duration": forms.TextInput(attrs={"placeholder": "7 days"}),
            "instructions": forms.TextInput(attrs={"placeholder": "e.g. empty stomach, avoid coffee"}),
        }


class FollowUpForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = m.FollowUp
        fields = ["visit_date", "progress_notes", "medicines_given", "next_followup_date"]
        widgets = {
            "visit_date": DATE,
            "next_followup_date": DATE,
            "progress_notes": forms.TextInput(attrs={"placeholder": "Enter follow up description / progress"}),
            "medicines_given": forms.TextInput(attrs={"placeholder": "Enter medicine given"}),
        }


class OrderedInlineFormSet(forms.BaseInlineFormSet):
    """Persist row order and pass read_only through to every row form."""

    def __init__(self, *args, read_only=False, **kwargs):
        self.read_only = read_only
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["read_only"] = self.read_only
        return kwargs

    def save(self, commit=True):
        objects = super().save(commit=False)
        for obj in self.deleted_objects:
            obj.delete()
        position = {id(form.instance): i for i, form in enumerate(self.forms)}
        for obj in objects:
            if hasattr(obj, "order"):
                obj.order = position.get(id(obj), obj.order)
            obj.save()
        return objects


def formset_for(model, parent, form, rows):
    """Build an inline formset showing at least `rows` rows (like the paper form)."""
    return inlineformset_factory(
        parent, model, form=form, formset=OrderedInlineFormSet, extra=rows, can_delete=True,
    )


class RelationshipForm(BootstrapMixin, forms.Form):
    relationship = forms.ChoiceField(label="This relative is the patient's")
    label = forms.CharField(label="Describe (for Other)", required=False, max_length=60)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .relationships import CHOICES

        self.fields["relationship"].choices = CHOICES

    def clean(self):
        data = super().clean()
        if data.get("relationship") == m.Relationship.OTHER and not data.get("label"):
            self.add_error("label", "Describe the relationship (e.g. Cousin).")
        return data


class PatientSearchForm(forms.Form):
    q = forms.CharField(required=False, widget=forms.TextInput(attrs={
        "class": "form-control", "placeholder": "Search by OPD No., name or mobile number", "autofocus": True,
    }))
