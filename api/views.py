from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role
from accounts.rbac import role_of, section_access
from clinic import models as m
from clinic.views import dashboard_context
from core.models import AuditLog

from . import serializers as s
from .permissions import IsAdminRole, SectionPermission

UPLOAD_PARSERS = [MultiPartParser, FormParser, JSONParser]


# ------------------------------------------------------------------------- auth
class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=s.LoginSerializer, responses={200: dict}, tags=["auth"])
    def post(self, request):
        serializer = s.LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(request, **serializer.validated_data)
        if user is None:
            return Response({"detail": "Invalid credentials or account locked."}, status=status.HTTP_400_BAD_REQUEST)
        token, _ = Token.objects.get_or_create(user=user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        AuditLog.record(AuditLog.Action.LOGIN, user, user=user, changes={"channel": "api"})
        return Response({"token": token.key, "user": s.UserSerializer(user).data})


class LogoutView(APIView):
    @extend_schema(request=None, responses={204: None}, tags=["auth"])
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        AuditLog.record(AuditLog.Action.LOGOUT, request.user, changes={"channel": "api"})
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    @extend_schema(responses={200: dict}, tags=["auth"])
    def get(self, request):
        return Response({
            "user": s.UserSerializer(request.user).data,
            "role": role_of(request.user),
            "sections": section_access(request.user),
        })


class DashboardView(APIView):
    @extend_schema(responses={200: dict}, tags=["dashboard"])
    def get(self, request):
        ctx = dashboard_context(request.user)
        data = {
            "stats": ctx["stats"],
            "today_visits": s.VisitSerializer(ctx["today_visits"], many=True).data,
            "pending_investigations": s.InvestigationSerializer(ctx["pending_investigations"], many=True).data,
        }
        if "upcoming_followups" in ctx:
            data["upcoming_followups"] = s.FollowUpSerializer(ctx["upcoming_followups"], many=True).data
        if "recent_prescriptions" in ctx:
            data["recent_prescriptions"] = s.PrescriptionSerializer(ctx["recent_prescriptions"], many=True).data
        return Response(data)


# ------------------------------------------------------------------- clinical
class SectionViewSet(viewsets.ModelViewSet):
    """ModelViewSet whose access is governed by accounts.rbac via `section`."""

    section = None
    permission_classes = [permissions.IsAuthenticated, SectionPermission]
    ordering = ["-id"]


class PatientViewSet(SectionViewSet):
    section = "patient"
    queryset = m.Patient.objects.all()
    serializer_class = s.PatientSerializer
    parser_classes = UPLOAD_PARSERS
    filterset_fields = {"gender": ["exact"], "registration_date": ["exact", "gte", "lte"], "age": ["gte", "lte"]}
    search_fields = ["opd_number", "full_name", "phone"]
    ordering_fields = ["registration_date", "full_name", "opd_number", "age"]


class VisitViewSet(SectionViewSet):
    section = "visit"
    queryset = m.Visit.objects.select_related("patient", "doctor")
    filterset_fields = {"patient": ["exact"], "doctor": ["exact"], "visit_type": ["exact"],
                        "visit_date": ["exact", "gte", "lte"]}
    search_fields = ["patient__full_name", "patient__opd_number", "patient__phone"]
    ordering_fields = ["visit_date", "id"]
    ordering = ["-visit_date", "-id"]

    def get_serializer_class(self):
        return s.VisitDetailSerializer if self.action == "retrieve" else s.VisitSerializer

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        AuditLog.record(AuditLog.Action.VIEW, self.get_object())
        return response


def section_viewset(model, section, serializer, filters=None, parsers=None, select=("visit__patient",)):
    attrs = {
        "section": section,
        "queryset": model.objects.select_related(*select),
        "serializer_class": serializer,
        "filterset_fields": filters or ["visit"],
        "ordering_fields": ["id", "created_at", "updated_at"],
    }
    if parsers:
        attrs["parser_classes"] = parsers
    return type(f"{model.__name__}ViewSet", (SectionViewSet,), attrs)


CaseHistoryViewSet = section_viewset(m.CaseHistory, "case_history", s.CaseHistorySerializer)
PhysicalGeneralViewSet = section_viewset(m.PhysicalGeneral, "physical_generals", s.PhysicalGeneralSerializer)
MentalGeneralViewSet = section_viewset(m.MentalGeneral, "mental_generals", s.MentalGeneralSerializer)
ExaminationViewSet = section_viewset(m.Examination, "examination", s.ExaminationSerializer)
DiagnosisViewSet = section_viewset(m.Diagnosis, "diagnosis", s.DiagnosisSerializer)
PastTreatmentViewSet = section_viewset(m.PastTreatment, "past_treatment", s.PastTreatmentSerializer)
PastTreatmentAttachmentViewSet = section_viewset(
    m.PastTreatmentAttachment, "past_treatment", s.PastTreatmentAttachmentSerializer,
    filters=["past_treatment"], parsers=UPLOAD_PARSERS, select=("past_treatment__visit__patient",),
)
FollowUpViewSet = section_viewset(
    m.FollowUp, "followups", s.FollowUpSerializer,
    filters={"visit": ["exact"], "visit__patient": ["exact"], "next_followup_date": ["exact", "gte", "lte"]},
)
RepertoryAnalysisViewSet = section_viewset(
    m.RepertoryAnalysis, "repertory", s.RepertoryAnalysisSerializer, parsers=UPLOAD_PARSERS,
)
PatientImageViewSet = section_viewset(
    m.PatientImage, "patient_images", s.PatientImageSerializer,
    filters=["patient", "visit", "stage"], parsers=UPLOAD_PARSERS, select=("patient",),
)


class InvestigationViewSet(SectionViewSet):
    section = "investigations"
    queryset = m.Investigation.objects.select_related("visit__patient").prefetch_related("reports")
    serializer_class = s.InvestigationSerializer
    filterset_fields = {"visit": ["exact"], "visit__patient": ["exact"], "status": ["exact"],
                        "requested_date": ["exact", "gte", "lte"]}
    search_fields = ["visit__patient__full_name", "visit__patient__opd_number", "request_notes"]
    ordering_fields = ["requested_date", "status"]


class InvestigationReportViewSet(SectionViewSet):
    """Upload reports (multipart `file`). Lab uploads mark the investigation completed."""

    section = "investigation_reports"
    queryset = m.InvestigationReport.objects.select_related("investigation__visit__patient")
    serializer_class = s.InvestigationReportSerializer
    parser_classes = UPLOAD_PARSERS
    filterset_fields = ["investigation", "source"]

    def perform_create(self, serializer):
        from_lab = role_of(self.request.user) == Role.LAB
        report = serializer.save(source=m.InvestigationReport.Source.LAB if from_lab else m.InvestigationReport.Source.EXTERNAL)
        if from_lab and report.investigation.status != m.Investigation.Status.COMPLETED:
            report.investigation.status = m.Investigation.Status.COMPLETED
            report.investigation.save(update_fields=["status"])


class PrescriptionViewSet(SectionViewSet):
    section = "prescription"
    queryset = m.Prescription.objects.select_related("visit__patient").prefetch_related("items")
    serializer_class = s.PrescriptionSerializer
    filterset_fields = {"visit": ["exact"], "visit__patient": ["exact"], "prescribed_on": ["exact", "gte", "lte"]}
    search_fields = ["items__medicine_name", "visit__patient__full_name"]
    ordering_fields = ["prescribed_on", "id"]


# --------------------------------------------------------------------- admin
class UserViewSet(viewsets.ModelViewSet):
    queryset = get_user_model().objects.all()
    serializer_class = s.UserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    filterset_fields = ["role", "is_active", "is_locked"]
    search_fields = ["username", "first_name", "last_name", "email"]
    ordering_fields = ["username", "date_joined", "last_login"]
    ordering = ["username"]

    def perform_create(self, serializer):
        user = serializer.save()
        AuditLog.record(AuditLog.Action.CREATE, user, changes={"role": user.role, "channel": "api"})

    def perform_update(self, serializer):
        user = serializer.save()
        AuditLog.record(AuditLog.Action.UPDATE, user, changes={k: str(v) for k, v in serializer.validated_data.items() if k != "password"})

    def perform_destroy(self, instance):
        if instance == self.request.user:
            from rest_framework.exceptions import ValidationError

            raise ValidationError("You cannot delete your own account.")
        AuditLog.record(AuditLog.Action.DELETE, instance)
        instance.is_active = False
        instance.is_locked = True
        instance.save(update_fields=["is_active", "is_locked"])

    def _set_lock(self, request, locked):
        user = self.get_object()
        user.is_locked = locked
        user.locked_at = timezone.now() if locked else None
        user.failed_login_attempts = 0
        user.save(update_fields=["is_locked", "locked_at", "failed_login_attempts"])
        AuditLog.record(AuditLog.Action.LOCK if locked else AuditLog.Action.UNLOCK, user)
        return Response(self.get_serializer(user).data)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def lock(self, request, pk=None):
        return self._set_lock(request, True)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def unlock(self, request, pk=None):
        return self._set_lock(request, False)

    @extend_schema(request=s.PasswordResetSerializer, responses={204: None})
    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()
        serializer = s.PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_password(serializer.validated_data["password"], user)
        user.set_password(serializer.validated_data["password"])
        user.is_locked = False
        user.failed_login_attempts = 0
        user.save()
        Token.objects.filter(user=user).delete()
        AuditLog.record(AuditLog.Action.PASSWORD_RESET, user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related("user")
    serializer_class = s.AuditLogSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    filterset_fields = {"user": ["exact"], "action": ["exact"], "model_name": ["exact"], "timestamp": ["gte", "lte"]}
    search_fields = ["username", "object_repr"]
    ordering_fields = ["timestamp"]
