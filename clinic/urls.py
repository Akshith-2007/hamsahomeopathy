from django.urls import path

from . import views

urlpatterns = [
    path("patients/", views.patient_list, name="patient_list"),
    path("patients/new/", views.patient_create, name="patient_create"),
    path("patients/<int:pk>/", views.patient_detail, name="patient_detail"),
    path("patients/<int:pk>/delete/", views.patient_delete, name="patient_delete"),
    path("patients/<int:patient_pk>/visits/new/", views.visit_create, name="visit_create"),
    path("visits/<int:pk>/", views.visit_record, name="visit_record"),
    path("visits/<int:pk>/summary/", views.visit_summary, name="visit_summary"),
    path("visits/<int:pk>/delete/", views.visit_delete, name="visit_delete"),
    path("visits/<int:pk>/prescription/", views.prescription_print, name="prescription_print"),
    path("uploads/<slug:kind>/<int:pk>/delete/", views.upload_delete, name="upload_delete"),
    path("repertory/<int:pk>/sheet/delete/", views.repertory_sheet_delete, name="repertory_sheet_delete"),
    path("reports/", views.reports, name="reports"),
]
