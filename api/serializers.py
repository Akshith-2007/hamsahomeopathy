from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from accounts.rbac import can_view
from clinic import models as m
from core.models import AuditLog

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


class PastTreatmentSerializer(serializers.ModelSerializer):
    attachments = PastTreatmentAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = m.PastTreatment
        exclude = AUDIT_HIDDEN


class PatientSerializer(serializers.ModelSerializer):
    gender_display = serializers.CharField(source="get_gender_display", read_only=True)

    class Meta:
        model = m.Patient
        exclude = AUDIT_HIDDEN
        read_only_fields = ("opd_number",)


class InvestigationReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = m.InvestigationReport
        exclude = AUDIT_HIDDEN
        read_only_fields = ("source",)


class InvestigationSerializer(serializers.ModelSerializer):
    reports = InvestigationReportSerializer(many=True, read_only=True)

    class Meta:
        model = m.Investigation
        exclude = AUDIT_HIDDEN


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
        fields = ("id", "medicine_name", "potency", "duration", "instructions", "order")


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


# field on VisitDetailSerializer -> RBAC section that must be viewable
VISIT_SECTION_FIELDS = {
    "case_history": "case_history",
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
