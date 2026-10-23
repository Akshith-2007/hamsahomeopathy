from django.contrib import admin
from django.urls import include, path, re_path

from core import views as core_views

urlpatterns = [
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("", include("clinic.urls")),
    path("api/", include("api.urls")),
    path("django-admin/", admin.site.urls),
    re_path(r"^media/(?P<path>.+)$", core_views.protected_media, name="protected_media"),
]

handler403 = "core.views.permission_denied"
handler404 = "core.views.not_found"
