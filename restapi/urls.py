from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("patients", views.PatientViewSet)
router.register("visits", views.VisitViewSet)
router.register("case-histories", views.CaseHistoryViewSet)
router.register("followup-notes", views.FollowUpNoteViewSet)
router.register("physical-generals", views.PhysicalGeneralViewSet)
router.register("mental-generals", views.MentalGeneralViewSet)
router.register("examinations", views.ExaminationViewSet)
router.register("diagnoses", views.DiagnosisViewSet)
router.register("investigations", views.InvestigationViewSet)
router.register("investigation-reports", views.InvestigationReportViewSet)
router.register("past-treatments", views.PastTreatmentViewSet)
router.register("past-treatment-attachments", views.PastTreatmentAttachmentViewSet)
router.register("patient-images", views.PatientImageViewSet)
router.register("repertory-analyses", views.RepertoryAnalysisViewSet)
router.register("prescriptions", views.PrescriptionViewSet)
router.register("followups", views.FollowUpViewSet)
router.register("scheduled-followups", views.ScheduledFollowUpViewSet)
router.register("dispense-orders", views.DispenseOrderViewSet)
router.register("users", views.UserViewSet)
router.register("audit-logs", views.AuditLogViewSet)

urlpatterns = [
    path("auth/login/", views.LoginView.as_view(), name="api_login"),
    path("auth/logout/", views.LogoutView.as_view(), name="api_logout"),
    path("auth/me/", views.MeView.as_view(), name="api_me"),
    path("dashboard/", views.DashboardView.as_view(), name="api_dashboard"),
    path("lab/dashboard/", views.LabDashboardView.as_view(), name="api_lab_dashboard"),
    path("notifications/unread/", views.NotificationUnreadView.as_view(), name="api_notifications_unread"),
    path("notifications/read-all/", views.NotificationReadAllView.as_view(), name="api_notifications_read_all"),
    path("notifications/<int:pk>/read/", views.NotificationReadView.as_view(), name="api_notification_read"),
    path("schema/", SpectacularAPIView.as_view(), name="api_schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api_schema"), name="api_docs"),
    path("redoc/", SpectacularRedocView.as_view(url_name="api_schema"), name="api_redoc"),
    path("", include(router.urls)),
]
