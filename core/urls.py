from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("audit/", views.audit_list, name="audit_list"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
]
