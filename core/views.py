from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.static import serve

from accounts.rbac import can_view, can_view_report, home_view_name, section_required
from clinic.models import InvestigationReport
from clinic.views import dashboard_context

from .models import AuditLog

# patients/<OPD>/<kind>/<file> -> section the viewer must be allowed to see
MEDIA_SECTIONS = {
    "photo": "patient",
    "investigations": "investigation_reports",
    "past_treatment": "past_treatment",
    "images": "patient_images",
    "repertory": "repertory",
}


@login_required
def home(request):
    target = home_view_name(request.user)
    if target == "lab_dashboard":
        from clinic.views import lab_dashboard

        return lab_dashboard(request)  # the lab's home page *is* its dashboard
    if target != "home":
        return redirect(target)
    return render(request, "core/home.html", dashboard_context(request.user))


@login_required
@never_cache
def protected_media(request, path):
    parts = path.split("/")
    if parts[0] == "patients" and len(parts) >= 4:
        section = MEDIA_SECTIONS.get(parts[2])
        if section is None or not can_view(request.user, section):
            raise PermissionDenied
        if section == "investigation_reports":
            report = InvestigationReport.objects.filter(file=path).first()
            if report is None or not can_view_report(request.user, report):
                raise PermissionDenied
    elif parts[0] != "signatures":
        raise PermissionDenied
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Cache-Control"] = "private, no-store"
    return response


@section_required("audit_log")
def audit_list(request):
    logs = AuditLog.objects.select_related("user")
    filters = {
        "user": request.GET.get("user", ""),
        "action": request.GET.get("action", ""),
        "model": request.GET.get("model", ""),
        "start": request.GET.get("start", ""),
        "end": request.GET.get("end", ""),
    }
    if filters["user"]:
        logs = logs.filter(user_id=filters["user"])
    if filters["action"]:
        logs = logs.filter(action=filters["action"])
    if filters["model"]:
        logs = logs.filter(model_name=filters["model"])
    if filters["start"]:
        logs = logs.filter(timestamp__date__gte=filters["start"])
    if filters["end"]:
        logs = logs.filter(timestamp__date__lte=filters["end"])
    page = Paginator(logs, 50).get_page(request.GET.get("page"))
    query = "&".join(f"{k}={v}" for k, v in filters.items() if v)
    return render(request, "core/audit_list.html", {
        "page": page, "filters": filters, "query": query,
        "actions": AuditLog.Action.choices,
        "models": AuditLog.objects.order_by().values_list("model_name", flat=True).distinct(),
        "users": get_user_model().objects.order_by("username"),
    })


def healthz(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - report any DB failure to the probe
        return JsonResponse({"status": "unavailable", "error": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})


def permission_denied(request, exception=None):
    return render(request, "403.html", status=403)


def not_found(request, exception=None):
    return render(request, "404.html", status=404)
