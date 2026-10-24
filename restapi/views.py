from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.rbac import can_view, can_view_report, home_view_name, role_of, section_access
from clinic import lab as labsvc
from clinic import models as m
from clinic import pharmacy, relationships, services
from clinic.family import family_history, grouped, summary_text
from clinic.prefill import latest_history, source_label
from core.validators import document_validators, run_validators
from clinic.history import PatientHistory
from core import inbox
from core.models import Notification
from clinic.views import dashboard_context
from core.models import AuditLog

from . import serializers as s
from .permissions import IsAdminRole, SectionPermission, section_permission

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
            "home": home_view_name(request.user),
            "sections": section_access(request.user),
        })


class DashboardView(APIView):
    @extend_schema(responses={200: dict}, tags=["dashboard"])
    def get(self, request):
        ctx = dashboard_context(request.user)
        context = {"request": request}
        data = {
            "stats": ctx["stats"],
            "pending_investigations": s.InvestigationSerializer(
                ctx["pending_investigations"], many=True, context=context).data,
        }
        if "today_visits" in ctx:
            data["today_visits"] = s.VisitSerializer(ctx["today_visits"], many=True, context=context).data
        if "followup_buckets" in ctx:
            data["followups"] = {
                key: s.ScheduledFollowUpSerializer(rows, many=True, context=context).data
                for key, _label, _color, rows in ctx["followup_buckets"]
            }
        if "recent_prescriptions" in ctx:
            data["recent_prescriptions"] = s.PrescriptionSerializer(
                ctx["recent_prescriptions"], many=True, context=context).data
        return Response(data)


# ------------------------------------------------------------------- clinical
class SectionViewSet(viewsets.ModelViewSet):
    """ModelViewSet whose access is governed by accounts.rbac via `section`."""

    section = None
    permission_classes = [permissions.IsAuthenticated, SectionPermission]
    ordering = ["-id"]


def _workflow_error(exc):
    return ValidationError({"detail": str(exc)})


class PatientViewSet(SectionViewSet):
    # Everyone may list/search patients (identity fields); editing needs the "patient" section.
    section = "patient_identity"
    queryset = m.Patient.objects.all()
    serializer_class = s.PatientSerializer
    parser_classes = UPLOAD_PARSERS
    filterset_fields = {"gender": ["exact"], "registration_date": ["exact", "gte", "lte"], "age": ["gte", "lte"]}
    ordering_fields = ["registration_date", "full_name", "opd_number", "age"]

    @property
    def search_fields(self):
        # Searching by mobile number would reveal contact details to roles that can't see them.
        if can_view(self.request.user, "patient"):
            return ["opd_number", "full_name", "phone"]
        return ["opd_number", "full_name"]

    def get_permissions(self):
        if self.action in ("relationships", "relationship_detail"):
            self.section = "relationships"
        elif self.action not in ("list", "retrieve", "history", "family_history", "history_prefill", "visits",
                                 "relationship_confirm", "relationship_reject", "relationship_unreject"):
            self.section = "patient"
        return super().get_permissions()

    @extend_schema(methods=["GET"], responses={200: s.RelationshipSerializer(many=True)}, tags=["family"])
    @extend_schema(methods=["POST"], request=s.RelationshipCreateSerializer, responses={201: s.RelationshipSerializer},
                   tags=["family"])
    @action(detail=True, methods=["get", "post"], url_path="relationships",
            permission_classes=[permissions.IsAuthenticated, SectionPermission])
    def relationships(self, request, pk=None):
        """Linked relatives (identity + relationship). POST links a relative; the reverse link is automatic."""
        patient = self.get_object()
        if request.method == "POST":
            serializer = s.RelationshipCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            try:
                link = relationships.link(patient, data["related_patient"], data["relationship"], request.user,
                                          label=data.get("label", ""))
            except services.WorkflowError as exc:
                raise _workflow_error(exc) from exc
            return Response(s.RelationshipSerializer(link).data, status=status.HTTP_201_CREATED)
        return Response(s.RelationshipSerializer(relationships.relatives_of(patient), many=True).data)

    @extend_schema(request=None, responses={204: None}, tags=["family"])
    # Unlinking relatives is part of editing the family links (reception may do it), not deleting records.
    @action(detail=True, methods=["delete"], url_path=r"relationships/(?P<link_id>\d+)",
            permission_classes=[permissions.IsAuthenticated, section_permission("relationships", "edit")])
    def relationship_detail(self, request, pk=None, link_id=None):
        """Remove a relative link from both patients."""
        patient = self.get_object()
        link = m.PatientRelationship.objects.filter(pk=link_id, patient=patient).first()
        if link is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        relationships.unlink(link, request.user)  # an inferred link is rejected instead of deleted
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _link_or_404(self, link_id):
        return m.PatientRelationship.objects.filter(pk=link_id, patient=self.get_object()).first()

    @extend_schema(request=None, responses={200: s.RelationshipSerializer}, tags=["family"])
    @action(detail=True, methods=["post"], url_path=r"relationships/(?P<link_id>\d+)/confirm",
            permission_classes=[permissions.IsAuthenticated, section_permission("relationships", "edit")])
    def relationship_confirm(self, request, pk=None, link_id=None):
        """Turn an automatic (inferred) link into a manual one."""
        link = self._link_or_404(link_id)
        if link is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        relationships.confirm(link, request.user)
        link.refresh_from_db()
        return Response(s.RelationshipSerializer(link).data)

    @extend_schema(request=None, responses={204: None}, tags=["family"])
    @action(detail=True, methods=["post"], url_path=r"relationships/(?P<link_id>\d+)/reject",
            permission_classes=[permissions.IsAuthenticated, section_permission("relationships", "edit")])
    def relationship_reject(self, request, pk=None, link_id=None):
        """Reject an inferred link; it will never be inferred again for this pair."""
        link = self._link_or_404(link_id)
        if link is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            relationships.reject(link, request.user)
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={204: None}, tags=["family"])
    @action(detail=True, methods=["post"], url_path=r"relationships/(?P<link_id>\d+)/unreject",
            permission_classes=[permissions.IsAuthenticated, section_permission("relationship_unreject", "edit")])
    def relationship_unreject(self, request, pk=None, link_id=None):
        """Admin: undo a rejection so the pair may be inferred again."""
        link = m.PatientRelationship.objects.filter(pk=link_id, patient=self.get_object(),
                                                    status=m.PatientRelationship.LinkStatus.REJECTED).first()
        if link is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        relationships.unreject(link, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(responses={200: dict}, tags=["history"])
    @action(detail=True, methods=["get"], url_path="history-prefill",
            permission_classes=[permissions.IsAuthenticated, section_permission("history")])
    def history_prefill(self, request, pk=None):
        """Latest value of every stable-history field from the patient's earlier visits, with its source visit."""
        patient = self.get_object()
        values = latest_history(patient)
        history = PatientHistory(patient)
        summary = history.past_summary()
        return Response({
            "patient": patient.pk,
            "sections": {section: {name: {"value": value, "source_visit": visit.pk, "label": source_label(visit)}
                                   for name, (value, visit) in fields.items()}
                         for section, fields in values.items()},
            "past_summary": {
                "diagnoses": summary["diagnoses"], "remedies": summary["remedies"],
                "investigations": [{"visit": v.pk, "test": inv.request_notes, "requested_date": inv.requested_date,
                                    "status": inv.status} for v, inv in summary["investigations"]],
            },
        })

    @extend_schema(request=s.ThroughFamilyVisitSerializer, responses={201: s.VisitSerializer}, tags=["family"])
    @action(detail=True, methods=["post"], url_path="visits",
            permission_classes=[permissions.IsAuthenticated, section_permission("family_consult", "edit")])
    def visits(self, request, pk=None):
        """Doctor/Admin: open a consultation for this patient (the relative) reported by a family member."""
        relative = self.get_object()
        serializer = s.ThroughFamilyVisitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            visit = services.consult_through_family(relative, serializer.validated_data["reported_by"], request.user)
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        body = s.VisitSerializer(visit, context={"request": request}).data
        body["reported_by_text"] = services.reported_by_text(visit)
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: dict}, tags=["family"])
    @action(detail=True, methods=["get"], url_path="family-history",
            permission_classes=[permissions.IsAuthenticated, section_permission("family_history")])
    def family_history(self, request, pk=None):
        """Diagnoses and chronic conditions from linked relatives' own records (Doctor/Admin)."""
        patient = self.get_object()
        entries = family_history(patient)
        return Response({
            "patient": patient.pk,
            "summary_text": summary_text(entries),
            "insert_text": summary_text(entries),
            "groups": [{"group": label, "relatives": [e.relative.pk for e in items]} for label, items in grouped(entries)],
            "relatives": [{
                "relationship": e.link.relationship, "display_label": e.link.display_label,
                "source": e.link.source, "via": e.path,
                "relative": {"id": e.relative.pk, "opd_number": e.relative.opd_number,
                             "full_name": e.relative.full_name, "age": e.relative.age, "gender": e.relative.gender},
                "diagnoses": [{"diagnosis": text, "date": date} for text, date in e.diagnoses],
                "chronic_conditions": e.chronic,
                "last_visit_date": e.last_visit.visit_date if e.last_visit else None,
            } for e in entries],
        })

    @extend_schema(
        parameters=[OpenApiParameter("start", str, description="YYYY-MM-DD"),
                    OpenApiParameter("end", str, description="YYYY-MM-DD")],
        responses={200: dict}, tags=["history"],
    )
    @action(detail=True, methods=["get"], permission_classes=[permissions.IsAuthenticated,
                                                              section_permission("history")])
    def history(self, request, pk=None):
        """Doctor's view of the patient's whole past: summary, every visit, every prescription."""
        patient = self.get_object()
        dates = s.HistoryRangeSerializer(data=request.query_params)
        dates.is_valid(raise_exception=True)
        history = PatientHistory(patient, start=dates.validated_data.get("start"), end=dates.validated_data.get("end"))
        AuditLog.record(AuditLog.Action.VIEW, patient, changes={"view": "history", "channel": "api"})
        return Response(history.as_dict(report_filter=lambda r: can_view_report(request.user, r)))


class VisitViewSet(SectionViewSet):
    section = "visit"
    queryset = m.Visit.objects.select_related("patient", "doctor")
    filterset_fields = {"patient": ["exact"], "doctor": ["exact"], "visit_type": ["exact"], "status": ["exact"],
                        "visit_date": ["exact", "gte", "lte"]}
    search_fields = ["patient__full_name", "patient__opd_number"]
    ordering_fields = ["visit_date", "id", "status"]
    ordering = ["-visit_date", "-id"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return s.VisitDetailSerializer
        if self.action == "check_in":
            return s.CheckInSerializer
        if self.action == "repeat_prescription":
            return s.RepeatPrescriptionSerializer
        return s.VisitSerializer

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        AuditLog.record(AuditLog.Action.VIEW, self.get_object())
        return response

    def perform_update(self, serializer):
        old_type = serializer.instance.visit_type
        visit = serializer.save()
        if visit.visit_type != old_type:
            services.record_visit_type_change(visit, old_type, self.request.user)

    @extend_schema(request=s.CheckInSerializer, responses={201: s.VisitSerializer}, tags=["check-in"])
    @action(detail=False, methods=["post"], url_path="check-in",
            permission_classes=[permissions.IsAuthenticated, section_permission("checkin", "edit")])
    def check_in(self, request):
        """Check a patient in as a New case or Follow-up (creates a WAITING visit)."""
        serializer = s.CheckInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.check_in(data["patient"], data["visit_type"], request.user,
                                   doctor=data.get("doctor"), visit_date=data.get("visit_date"))
        body = s.VisitSerializer(result.visit, context={"request": request}).data
        body["completed_followup"] = result.completed_followup.pk if result.completed_followup else None
        body["carried_forward_from"] = result.carried_from.pk if result.carried_from else None
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.RepeatPrescriptionSerializer, responses={200: s.PrescriptionSerializer}, tags=["history"])
    @action(detail=True, methods=["post"], url_path="repeat-prescription",
            permission_classes=[permissions.IsAuthenticated, section_permission("prescription", "edit")])
    def repeat_prescription(self, request, pk=None):
        """Copy an earlier prescription of the same patient into this visit's prescription."""
        visit = self.get_object()
        serializer = s.RepeatPrescriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data["prescription"]
        if source.visit.patient_id != visit.patient_id:
            raise ValidationError({"prescription": "That prescription belongs to another patient."})
        try:
            target = services.repeat_prescription(source, visit, request.user,
                                                  replace=serializer.validated_data["replace"])
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(s.PrescriptionSerializer(target, context={"request": request}).data)


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
FollowUpNoteViewSet = section_viewset(m.FollowUpNote, "followup_assessment", s.FollowUpNoteSerializer)
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
    queryset = m.Investigation.objects.select_related("visit__patient", "created_by").prefetch_related("reports")
    serializer_class = s.InvestigationSerializer
    filterset_fields = {"visit": ["exact"], "visit__patient": ["exact"], "status": ["exact"],
                        "requested_date": ["exact", "gte", "lte"]}
    search_fields = ["visit__patient__full_name", "visit__patient__opd_number", "request_notes"]
    ordering_fields = ["requested_date", "status"]


class InvestigationReportViewSet(SectionViewSet):
    """Reports (multipart `file`). Lab uploads mark the investigation completed and notify the doctor.

    Filters: `status` (CURRENT/SUPERSEDED), `test` (quick type, e.g. cbc), `date_from`, `date_to`,
    `uploaded_by=me`, `patient` (id), `investigation`, `source`.
    """

    section = "investigation_reports"
    queryset = m.InvestigationReport.objects.select_related(
        "investigation__visit__patient", "investigation__created_by", "created_by")
    serializer_class = s.InvestigationReportSerializer
    parser_classes = UPLOAD_PARSERS
    filterset_fields = ["investigation", "source", "status"]
    ordering = ["-created_at", "-id"]

    def get_queryset(self):
        queryset = labsvc.visible_reports(self.request.user, super().get_queryset())
        params = self.request.query_params
        if params.get("test"):
            queryset = queryset.filter(labsvc.test_filter(params["test"], prefix="investigation__"))
        if params.get("date_from"):
            queryset = queryset.filter(created_at__date__gte=params["date_from"])
        if params.get("date_to"):
            queryset = queryset.filter(created_at__date__lte=params["date_to"])
        if params.get("uploaded_by") == "me":
            queryset = queryset.filter(created_by=self.request.user)
        if params.get("patient", "").isdigit():
            queryset = queryset.filter(investigation__visit__patient_id=params["patient"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from_lab = can_view(request.user, "lab_worklist") and not can_view(request.user, "external_reports")
        if from_lab:
            report = labsvc.upload_reports(serializer.validated_data["investigation"],
                                           [serializer.validated_data["file"]], request.user,
                                           remarks=serializer.validated_data.get("remarks", ""))[0]
        else:
            report = serializer.save(source=m.InvestigationReport.Source.EXTERNAL)
        return Response(self.get_serializer(report).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.ReportCorrectionSerializer, responses={201: s.InvestigationReportSerializer})
    @action(detail=True, methods=["post"],
            permission_classes=[permissions.IsAuthenticated, section_permission("lab_reports", "edit")])
    def correct(self, request, pk=None):
        """Upload a corrected version; the old one is kept as SUPERSEDED and the doctor is notified."""
        report = self.get_object()
        if report.status != m.InvestigationReport.Version.CURRENT:
            raise ValidationError({"detail": "Only the current version of a report can be corrected."})
        serializer = s.ReportCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        errors = run_validators(serializer.validated_data["file"], document_validators(20))
        if errors:
            raise ValidationError({"file": errors})
        corrected = labsvc.correct_report(report, serializer.validated_data["file"], request.user,
                                          remarks=serializer.validated_data.get("remarks", ""))
        return Response(self.get_serializer(corrected).data, status=status.HTTP_201_CREATED)


class LabDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated, section_permission("lab_worklist")]

    @extend_schema(responses={200: dict}, tags=["lab"])
    def get(self, request):
        """Counts, pending investigations (oldest first, overdue flagged) and recent lab uploads."""
        data = labsvc.dashboard(request.user, request.query_params.get("q", ""), request.query_params.get("test", ""))
        return Response({
            "counts": data["counts"], "overdue_days": data["overdue_days"],
            "pending": [{
                "id": inv.pk, "visit": inv.visit_id, "test": inv.request_notes,
                "patient": s.PatientIdentitySerializer(inv.visit.patient).data,
                "requested_by": str(inv.created_by) if inv.created_by else None,
                "requested_at": inv.created_at, "overdue": inv.is_overdue,
                "pending_seconds": int(inv.pending_for.total_seconds()),
            } for inv in data["pending"]],
            "recent": s.InvestigationReportSerializer(data["recent"], many=True, context={"request": request}).data,
        })


class PrescriptionViewSet(SectionViewSet):
    section = "prescription"
    queryset = m.Prescription.objects.select_related("visit__patient").prefetch_related("items")
    serializer_class = s.PrescriptionSerializer
    filterset_fields = {"visit": ["exact"], "visit__patient": ["exact"], "prescribed_on": ["exact", "gte", "lte"]}
    search_fields = ["items__medicine_name", "visit__patient__full_name"]
    ordering_fields = ["prescribed_on", "id"]

    def perform_update(self, serializer):
        prescription = serializer.save()
        pharmacy.sync_after_edit(prescription, self.request.user)  # re-send if already with the pharmacy

    @extend_schema(request=s.SendToPharmacySerializer, responses={200: s.DispenseOrderSerializer}, tags=["pharmacy"])
    @action(detail=True, methods=["post"], url_path="send-to-pharmacy",
            permission_classes=[permissions.IsAuthenticated, section_permission("pharmacy_send", "edit")])
    def send_to_pharmacy(self, request, pk=None):
        """Freeze the items into a dispense order and notify reception."""
        prescription = self.get_object()
        options = s.SendToPharmacySerializer(data=request.data)
        options.is_valid(raise_exception=True)
        try:
            order, created = pharmacy.send_to_pharmacy(prescription, request.user,
                                                       keep_open=options.validated_data["keep_open"])
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(s.DispenseOrderSerializer(order).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class DispenseOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """Pharmacy queue. Items only, never clinical data. Status actions need the pharmacy_items edit right."""

    queryset = m.DispenseOrder.objects.select_related("patient", "visit__doctor", "sent_by").prefetch_related("items")
    serializer_class = s.DispenseOrderSerializer
    permission_classes = [permissions.IsAuthenticated, section_permission("pharmacy_status")]
    filterset_fields = {"status": ["exact", "in"], "patient": ["exact"], "visit": ["exact"],
                        "sent_at": ["gte", "lte"]}
    search_fields = ["patient__full_name", "patient__opd_number"]
    ordering_fields = ["sent_at", "status"]
    ordering = ["sent_at", "id"]

    def _change(self, request, action_name):
        order = self.get_object()
        try:
            pharmacy.change_status(order, action_name, request.user)
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(self.get_serializer(order).data)

    _edit = [permissions.IsAuthenticated, section_permission("pharmacy_items", "edit")]

    @extend_schema(request=None, responses={200: s.DispenseOrderSerializer})
    @action(detail=True, methods=["post"], url_path="start-preparing", permission_classes=_edit)
    def start_preparing(self, request, pk=None):
        return self._change(request, "start-preparing")

    @extend_schema(request=None, responses={200: s.DispenseOrderSerializer})
    @action(detail=True, methods=["post"], url_path="mark-ready", permission_classes=_edit)
    def mark_ready(self, request, pk=None):
        return self._change(request, "mark-ready")

    @extend_schema(request=None, responses={200: s.DispenseOrderSerializer})
    @action(detail=True, methods=["post"], url_path="mark-dispensed", permission_classes=_edit)
    def mark_dispensed(self, request, pk=None):
        return self._change(request, "mark-dispensed")

    @extend_schema(request=None, responses={200: s.DispenseOrderSerializer})
    @action(detail=True, methods=["post"], permission_classes=_edit)
    def cancel(self, request, pk=None):
        return self._change(request, "cancel")


class NotificationUnreadView(APIView):
    permission_classes = [permissions.IsAuthenticated, section_permission("notifications")]

    @extend_schema(responses={200: dict}, tags=["notifications"])
    def get(self, request):
        """Unread notifications for the current user (poll every 10–15 s)."""
        unread = inbox.unread_for(request.user)
        return Response({"count": unread.count(),
                         "results": s.NotificationSerializer(unread[:20], many=True).data})


class NotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated, section_permission("notifications")]

    @extend_schema(request=None, responses={200: s.NotificationSerializer}, tags=["notifications"])
    def post(self, request, pk):
        notification = Notification.objects.filter(pk=pk, recipient=request.user).first()
        if notification is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(s.NotificationSerializer(inbox.mark_read(notification)).data)


class NotificationReadAllView(APIView):
    permission_classes = [permissions.IsAuthenticated, section_permission("notifications")]

    @extend_schema(request=None, responses={200: dict}, tags=["notifications"])
    def post(self, request):
        return Response({"marked": inbox.mark_all_read(request.user)})


class ScheduledFollowUpViewSet(SectionViewSet):
    """Follow-up appointments. Create = schedule (one active per patient; pass `replace` to swap)."""

    section = "followup_schedule"
    queryset = m.ScheduledFollowUp.objects.select_related("patient", "scheduled_by")
    serializer_class = s.ScheduledFollowUpSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]
    filterset_fields = {"patient": ["exact"], "status": ["exact"], "scheduled_date": ["exact", "gte", "lte"]}
    search_fields = ["patient__full_name", "patient__opd_number"]
    ordering_fields = ["scheduled_date", "scheduled_time", "id"]
    ordering = ["scheduled_date", "scheduled_time", "id"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            followup = services.schedule_followup(
                data["patient"], data["scheduled_date"], request.user, scheduled_time=data.get("scheduled_time"),
                notes=data.get("notes", ""), source_visit=data.get("source_visit"), replace=data.get("replace", False),
            )
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(self.get_serializer(followup).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.RescheduleSerializer, responses={200: s.ScheduledFollowUpSerializer})
    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        followup = self.get_object()
        serializer = s.RescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.reschedule_followup(followup, serializer.validated_data["scheduled_date"], request.user,
                                         scheduled_time=serializer.validated_data.get("scheduled_time"))
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(self.get_serializer(followup).data)

    @extend_schema(request=None, responses={200: s.ScheduledFollowUpSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        followup = self.get_object()
        try:
            services.cancel_followup(followup, request.user)
        except services.WorkflowError as exc:
            raise _workflow_error(exc) from exc
        return Response(self.get_serializer(followup).data)

    @extend_schema(request=None, responses={201: s.VisitSerializer}, tags=["check-in"])
    @action(detail=True, methods=["post"], url_path="check-in",
            permission_classes=[permissions.IsAuthenticated, section_permission("checkin", "edit")])
    def check_in(self, request, pk=None):
        """Start the Follow-up check-in for this appointment (marks it COMPLETED)."""
        followup = self.get_object()
        result = services.check_in(followup.patient, m.Visit.Type.FOLLOW_UP, request.user)
        return Response(s.VisitSerializer(result.visit, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


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
    permission_classes = [permissions.IsAuthenticated, section_permission("audit_log")]
    filterset_fields = {"user": ["exact"], "action": ["exact"], "model_name": ["exact"], "timestamp": ["gte", "lte"]}
    search_fields = ["username", "object_repr"]
    ordering_fields = ["timestamp"]
