"""Documents attached to a patient's record: old case sheets migrated from paper, outside reports, scans.

Shared by the HTML views and the REST API. Who may see or upload which category is decided by
accounts.rbac ("attachments" section, `attachment_categories`); files are only ever served through
`file_response`, never through a public /media/ link.
"""
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone

from accounts.rbac import attachment_categories, can_delete, can_edit
from core.models import AuditLog
from core.validators import ATTACHMENT_EXTENSIONS, CONTENT_TYPES, sniff_content_type

from . import models as m

Category = m.PatientAttachment.Category
ACCEPT = ",".join(f".{ext}" for ext in ATTACHMENT_EXTENSIONS)
TYPE_LABELS = {"pdf": "PDF", "jpg": "JPG", "jpeg": "JPEG", "png": "PNG", "doc": "Word", "docx": "Word"}


def visible_attachments(user, queryset=None):
    """Live attachments the user may see (lab staff: lab reports only)."""
    queryset = m.PatientAttachment.objects.all() if queryset is None else queryset
    categories = attachment_categories(user)
    if categories is None:
        return queryset
    return queryset.filter(category__in=categories) if categories else queryset.none()


def category_choices(user):
    categories = attachment_categories(user)
    return [(value, label) for value, label in Category.choices if categories is None or value in categories]


def default_category(user):
    choices = category_choices(user)
    values = [value for value, _ in choices]
    return Category.OLD_CASE_SHEET if Category.OLD_CASE_SHEET in values else (values[0] if values else "")


def size_hint():
    return f"PDF, JPG, PNG, DOC, DOCX (Max size: {settings.ATTACHMENT_MAX_UPLOAD_MB:g} MB each)"


def access(user):
    """Template context: what the user may do with attachments."""
    return {
        "upload": can_edit(user, "attachments"),
        "delete": can_delete(user, "attachments"),
        "categories": category_choices(user),
        "default_category": default_category(user),
        "accept": ACCEPT,
        "hint": size_hint(),
    }


def validate_upload(upload):
    """Polite, specific problems with one uploaded file (empty list = fine)."""
    name = Path(upload.name).name
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in ATTACHMENT_EXTENSIONS:
        return [f"“{name}” is not a supported file type. Please attach a PDF, JPG, PNG or Word (DOC/DOCX) file."]
    if not upload.size:
        return [f"“{name}” is empty. Please check the file and try again."]
    max_mb = settings.ATTACHMENT_MAX_UPLOAD_MB
    if upload.size > max_mb * 1024 * 1024:
        return [f"“{name}” is {upload.size / 1048576:.1f} MB, which is over the {max_mb:g} MB limit. "
                "Please scan it at a lower resolution or compress it, then try again."]
    if sniff_content_type(upload) != CONTENT_TYPES[ext]:
        return [f"“{name}” does not look like a genuine {TYPE_LABELS[ext]} file. It may be damaged or renamed. "
                "Please check the file and try again."]
    return []


def validate_uploads(files, user, category, document_date=None):
    errors = []
    if not files:
        errors.append("Please choose at least one file to attach.")
    elif len(files) > settings.ATTACHMENT_MAX_FILES:
        errors.append(f"Please attach up to {settings.ATTACHMENT_MAX_FILES} files at a time.")
    if category not in dict(category_choices(user)):
        errors.append("Please choose a category you are allowed to upload."
                      if category in Category.values else "Please choose a category for the document.")
    if document_date and document_date > timezone.localdate():
        errors.append("The document date cannot be in the future.")
    for upload in files[: settings.ATTACHMENT_MAX_FILES]:
        errors.extend(validate_upload(upload))
    return errors


def attach(patient, files, *, category, description="", document_date=None, visit=None):
    """Store already validated files on the patient's record. Uploader is recorded by AuditedModel."""
    created = []
    with transaction.atomic():
        for upload in files:
            name = Path(upload.name).name
            attachment = m.PatientAttachment(
                patient=patient, visit=visit, file=upload, original_filename=name[:255], file_size=upload.size,
                content_type=CONTENT_TYPES[Path(name).suffix.lower().lstrip(".")], category=category,
                description=description, document_date=document_date,
            )
            attachment.save()
            created.append(attachment)
    return created


def _disposition(kind, filename):
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "document"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def file_response(attachment, *, download=False):
    """Serve the file to an already authorised user (view inline, or download)."""
    stored = attachment.file
    if not stored or not stored.storage.exists(stored.name):
        raise Http404("The file is no longer available.")
    AuditLog.record(AuditLog.Action.EXPORT if download else AuditLog.Action.VIEW, attachment,
                    changes={"file": attachment.original_filename, "action": "download" if download else "view"})
    disposition = _disposition("attachment" if download else "inline", attachment.original_filename)
    storage = stored.storage
    if hasattr(storage, "signed_url"):
        # Private bucket: a short-lived signed link that carries the right name and type.
        response = redirect(storage.signed_url(stored.name, parameters={
            "ResponseContentDisposition": disposition, "ResponseContentType": attachment.content_type}))
    elif settings.MEDIA_ACCEL_REDIRECT_PREFIX:
        # nginx serves the bytes from its `internal` location; Python only made the access decision.
        response = HttpResponse(content_type=attachment.content_type)
        response["X-Accel-Redirect"] = f"{settings.MEDIA_ACCEL_REDIRECT_PREFIX.rstrip('/')}/{quote(stored.name)}"
        response["Content-Disposition"] = disposition
    else:
        response = FileResponse(stored.open("rb"), content_type=attachment.content_type)
        response["Content-Disposition"] = disposition
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
