from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from accounts.rbac import can_edit, can_view, visible_report_sources
from clinic import models as m
from core.models import AuditLog, Notification

User = get_user_model()
AUDIT_HIDDEN = ("is_deleted", "deleted_at", "deleted_by")


def section_serializer(model, **attrs):
    """ModelSerializer exposing every field except soft-delete bookkeeping."""
    meta = type("Meta", (), {"model": model, "exclude": AUDIT_HIDDEN})
    return type(f"{model.__name__}Serializer", (serializers.ModelSerializer,), {"Meta": meta, **attrs})


CaseHistorySerializer = section_serializer(m.CaseHistory)
PhysicalGeneralSerializer = section_serializer(m.PhysicalGeneral)
MentalGeneralSerializer = section_serializer(m.MentalGeneral)
ExaminationSerializer = section_serializer(m.Examination)
DiagnosisSerializer = section_serializer(m.Diagnosis)
PastTreatmentAttachmentSerializer = section_serializer(m.PastTreatmentAttachment)
FollowUpSerializer = section_serializer(m.FollowUp)
FollowUpNoteSerializer = section_serializer(m.FollowUpNote)


def _request_user(serializer):
    request = serializer.context.get("request")
    return getattr(request, "user", None)


class PastTreatmentSerializer(serializers.ModelSerializer):
    attachments = PastTreatmentAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = m.PastTreatment
        exclude = AUDIT_HIDDEN


PATIENT_IDENTITY_FIELDS = ("id", "opd_number", "full_name", "age", "gender", "gender_display")


class PatientSerializer(serializers.ModelSerializer):
    gender_display = serializers.CharField(source="get_gender_display", read_only=True)

    class Meta:
        model = m.Patient
        exclude = AUDIT_HIDDEN
        read_only_fields = ("opd_number",)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_view(_request_user(self), "patient"):
            # Lab staff get identity only: no contact details, address or photo.
            data = {key: data[key] for key in PATIENT_IDENTITY_FIELDS if key in data}
        return data


class InvestigationReportSerializer(serializers.ModelSerializer):
    """Investigation data only: patient identity, test, dates, uploader and file. Never case-sheet data."""

    patient = serializers.SerializerMethodField()
    test = serializers.CharField(source="investigation.request_notes", read_only=True)
    requested_by = serializers.StringRelatedField(source="investigation.created_by", read_only=True)
    requested_date = serializers.DateField(source="investigation.requested_date", read_only=True)
    completed_at = serializers.DateTimeField(source="investigation.completed_at", read_only=True)
    uploaded_by = serializers.StringRelatedField(source="created_by", read_only=True)
    uploaded_at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = m.InvestigationReport
        fields = ("id", "investigation", "file", "source", "title", "remarks", "version", "status", "supersedes",
                  "patient", "test", "requested_by", "requested_date", "completed_at", "uploaded_by", "uploaded_at")
        read_only_fields = ("source", "version", "status", "supersedes")

    def get_patient(self, obj) -> dict:
        p = obj.investigation.visit.patient
        return {"id": p.pk, "opd_number": p.opd_number, "full_name": p.full_name, "age": p.age, "gender": p.gender}


class InvestigationSerializer(serializers.ModelSerializer):
    reports = InvestigationReportSerializer(many=True, read_only=True)
    requested_by = serializers.StringRelatedField(source="created_by", read_only=True)

    class Meta:
        model = m.Investigation
        exclude = AUDIT_HIDDEN

    def to_representation(self, instance):
        data = super().to_representation(instance)
        sources = visible_report_sources(_request_user(self))
        if sources is not None:
            data["reports"] = [r for r in data["reports"] if r["source"] in sources]
        return data


class PatientImageSerializer(serializers.ModelSerializer):
    uploaded_by = serializers.StringRelatedField(source="created_by", read_only=True)

    class Meta:
        model = m.PatientImage
        exclude = AUDIT_HIDDEN


class _ChildRowsMixin:
    """Writable nested rows: on create/update the given list replaces existing rows."""

    child_field = None  # e.g. "items"
    child_model = None
    parent_fk = None  # e.g. "prescription"

    @transaction.atomic
    def create(self, validated_data):
        rows = validated_data.pop(self.child_field, [])
        parent = super().create(validated_data)
        self._write_rows(parent, rows)
        return parent

    @transaction.atomic
    def update(self, instance, validated_data):
        rows = validated_data.pop(self.child_field, None)
        parent = super().update(instance, validated_data)
        if rows is not None:
            for old in getattr(parent, self.child_field).all():
                old.delete()
            self._write_rows(parent, rows)
        return parent

    def _write_rows(self, parent, rows):
        for position, row in enumerate(rows):
            row.setdefault("order", position)
            self.child_model.objects.create(**{self.parent_fk: parent}, **row)


class PrescriptionItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.PrescriptionItem
        fields = ("id", "medicine_name", "potency", "form", "quantity", "dose", "duration", "instructions", "order")


class PrescriptionSerializer(_ChildRowsMixin, serializers.ModelSerializer):
    child_field, child_model, parent_fk = "items", m.PrescriptionItem, "prescription"
    items = PrescriptionItemSerializer(many=True, required=False)

    class Meta:
        model = m.Prescription
        exclude = AUDIT_HIDDEN


class RepertoryEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = m.RepertoryEntry
        fields = ("id", "symptom", "rubric", "book", "grade", "remedies", "order")


class RepertoryAnalysisSerializer(_ChildRowsMixin, serializers.ModelSerializer):
    child_field, child_model, parent_fk = "entries", m.RepertoryEntry, "analysis"
    entries = RepertoryEntrySerializer(many=True, required=False)

    class Meta:
        model = m.RepertoryAnalysis
        exclude = AUDIT_HIDDEN


class VisitSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    opd_number = serializers.CharField(source="patient.opd_number", read_only=True)
    doctor_name = serializers.StringRelatedField(source="doctor", read_only=True)

    class Meta:
        model = m.Visit
        exclude = AUDIT_HIDDEN
        # Through-family visits are created via POST /api/patients/<id>/visits/ only.
        read_only_fields = ("previous_visit", "consultation_mode", "reported_by")

    def validate_visit_type(self, value):
        user = _request_user(self)
        if self.instance is not None and value != self.instance.visit_type and not can_edit(user, "visit_type"):
            raise serializers.ValidationError("Only a doctor or administrator can change the visit type.")
        return value

    def validate_status(self, value):
        if self.instance is not None and value != self.instance.status and not can_edit(_request_user(self), "consultation"):
            raise serializers.ValidationError("Only a doctor or administrator can change the consultation status.")
        return value


# field on VisitDetailSerializer -> RBAC section that must be viewable
VISIT_SECTION_FIELDS = {
    "case_history": "case_history",
    "followup_note": "followup_assessment",
    "physical_generals": "physical_generals",
    "mental_generals": "mental_generals",
    "examination": "examination",
    "diagnosis": "diagnosis",
    "past_treatment": "past_treatment",
    "investigations": "investigations",
    "repertory": "repertory",
    "prescriptions": "prescription",
    "followups": "followups",
    "images": "patient_images",
}


class VisitDetailSerializer(VisitSerializer):
    """Full visit with nested sections; sections the caller's role can't see are omitted."""

    case_history = CaseHistorySerializer(read_only=True)
    followup_note = FollowUpNoteSerializer(read_only=True)
    physical_generals = PhysicalGeneralSerializer(read_only=True)
    mental_generals = MentalGeneralSerializer(read_only=True)
    examination = ExaminationSerializer(read_only=True)
    diagnosis = DiagnosisSerializer(read_only=True)
    past_treatment = PastTreatmentSerializer(read_only=True)
    investigations = InvestigationSerializer(many=True, read_only=True)
    repertory = RepertoryAnalysisSerializer(read_only=True)
    prescriptions = PrescriptionSerializer(many=True, read_only=True)
    followups = FollowUpSerializer(many=True, read_only=True)
    images = PatientImageSerializer(many=True, read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        user = self.context["request"].user
        for field, section in VISIT_SECTION_FIELDS.items():
            if not can_view(user, section):
                data.pop(field, None)
        return data


class ScheduledFollowUpSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)
    opd_number = serializers.CharField(source="patient.opd_number", read_only=True)
    scheduled_by_name = serializers.StringRelatedField(source="scheduled_by", read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    replace = serializers.BooleanField(write_only=True, required=False, default=False,
                                       help_text="Replace the patient's existing scheduled follow-up.")

    class Meta:
        model = m.ScheduledFollowUp
        exclude = AUDIT_HIDDEN
        read_only_fields = ("status", "scheduled_by", "checkin_visit", "status_changed_at")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_view(_request_user(self), "followup_notes"):
            data.pop("notes", None)  # reception sees date, time and status only
        return data

    def validate(self, attrs):
        if attrs.get("notes") and not can_edit(_request_user(self), "followup_notes"):
            attrs["notes"] = ""
        source = attrs.get("source_visit")
        if source is not None and source.patient_id != attrs["patient"].pk:
            raise serializers.ValidationError({"source_visit": "That visit belongs to another patient."})
        return attrs


class HistoryRangeSerializer(serializers.Serializer):
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)


class RescheduleSerializer(serializers.Serializer):
    scheduled_date = serializers.DateField()
    scheduled_time = serializers.TimeField(required=False, allow_null=True)


class CheckInSerializer(serializers.Serializer):
    patient = serializers.PrimaryKeyRelatedField(queryset=m.Patient.objects.all())
    visit_type = serializers.ChoiceField(choices=m.Visit.Type.choices)
    doctor = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(role="DOCTOR", is_active=True),
                                                required=False, allow_null=True)
    visit_date = serializers.DateField(required=False)


class RepeatPrescriptionSerializer(serializers.Serializer):
    prescription = serializers.PrimaryKeyRelatedField(queryset=m.Prescription.objects.select_related("visit"))
    replace = serializers.BooleanField(required=False, default=False)


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("id", "username", "first_name", "last_name", "email", "phone", "role", "qualification",
                  "registration_number", "is_active", "is_locked", "last_login", "date_joined", "password")
        read_only_fields = ("is_locked", "last_login", "date_joined")

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "A password is required for new users."})
        if attrs.get("password"):
            validate_password(attrs["password"], self.instance)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user


class PasswordResetSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={"input_type": "password"}, write_only=True)


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = "__all__"



# --------------------------------------------------------------------------- family links
class PatientIdentitySerializer(serializers.ModelSerializer):
    """The minimum other roles may see about a patient."""

    class Meta:
        model = m.Patient
        fields = ("id", "opd_number", "full_name", "age", "gender")
        read_only_fields = fields


class RelationshipSerializer(serializers.ModelSerializer):
    """A relative as reception may see it: identity + relationship (+ how it was linked) only."""

    relative = PatientIdentitySerializer(source="related_patient", read_only=True)
    display_label = serializers.CharField(read_only=True)
    inferred_via = PatientIdentitySerializer(read_only=True)
    via_label = serializers.CharField(read_only=True, default="")

    class Meta:
        model = m.PatientRelationship
        fields = ("id", "relationship", "label", "display_label", "source", "status", "inferred_via", "via_label",
                  "relative")


class RelationshipCreateSerializer(serializers.Serializer):
    related_patient = serializers.PrimaryKeyRelatedField(queryset=m.Patient.objects.all())
    relationship = serializers.ChoiceField(choices=m.Relationship.choices)
    label = serializers.CharField(required=False, allow_blank=True, max_length=60)


# --------------------------------------------------------------------------- pharmacy
class DispenseItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.DispenseItem
        fields = ("medicine_name", "potency", "form", "quantity", "dose", "duration", "instructions", "position",
                  "change")


class DispenseOrderSerializer(serializers.ModelSerializer):
    """Pharmacy view of a prescription: patient identity + medicine items only (no clinical data)."""

    patient = PatientIdentitySerializer(read_only=True)
    collect_by = PatientIdentitySerializer(read_only=True)
    doctor = serializers.SerializerMethodField()
    items = DispenseItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = m.DispenseOrder
        fields = ("id", "visit", "patient", "collect_by", "collect_by_label", "doctor", "version", "is_update",
                  "previous_order", "removed_items",
                  "status", "status_display", "sent_at", "prepared_at", "ready_at", "dispensed_at", "notes", "items")

    def get_doctor(self, obj) -> str | None:
        doctor = obj.visit.doctor or obj.sent_by
        return str(doctor) if doctor else None


class SendToPharmacySerializer(serializers.Serializer):
    keep_open = serializers.BooleanField(required=False, default=False,
                                         help_text="Send but keep the consultation open.")


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ("id", "kind", "title", "message", "url", "target_model", "target_id", "created_at", "read_at")



class ThroughFamilyVisitSerializer(serializers.Serializer):
    consultation_mode = serializers.ChoiceField(choices=[m.Visit.Mode.THROUGH_FAMILY])
    reported_by = serializers.PrimaryKeyRelatedField(queryset=m.Patient.objects.all())


class ReportCorrectionSerializer(serializers.Serializer):
    file = serializers.FileField()
    remarks = serializers.CharField(required=False, allow_blank=True)
