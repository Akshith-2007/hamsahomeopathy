import zipfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.utils.deconstruct import deconstructible

DOCUMENT_EXTENSIONS = ["pdf", "doc", "docx", "jpg", "jpeg", "png"]
IMAGE_EXTENSIONS = ["jpg", "jpeg", "png"]
SHEET_EXTENSIONS = ["pdf", "jpg", "jpeg", "png"]

# First bytes of each accepted format; stops renamed executables slipping through.
_SIGNATURES = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "docx": [b"PK\x03\x04"],
    "doc": [b"\xd0\xcf\x11\xe0"],
}


@deconstructible
class MaxFileSizeValidator:
    def __init__(self, max_mb):
        self.max_mb = max_mb

    def __call__(self, file):
        if getattr(file, "_committed", False):
            return
        if file.size > self.max_mb * 1024 * 1024:
            raise ValidationError(f"File too large ({file.size / 1048576:.1f} MB). Maximum is {self.max_mb} MB.")

    def __eq__(self, other):
        return isinstance(other, MaxFileSizeValidator) and other.max_mb == self.max_mb


@deconstructible
class FileSignatureValidator:
    """Check the file content matches its extension (only for freshly uploaded files)."""

    def __call__(self, file):
        if getattr(file, "_committed", False):
            return  # existing stored file; don't reopen it from disk
        uploaded = getattr(file, "file", None)
        if uploaded is None or not hasattr(uploaded, "content_type"):
            return  # existing stored file, already validated on upload
        ext = Path(file.name).suffix.lower().lstrip(".")
        signatures = _SIGNATURES.get(ext)
        if not signatures:
            return
        position = uploaded.tell()
        uploaded.seek(0)
        head = uploaded.read(8)
        uploaded.seek(position)
        if not any(head.startswith(sig) for sig in signatures):
            raise ValidationError(f"The file content does not look like a .{ext} file.")

    def __eq__(self, other):
        return isinstance(other, FileSignatureValidator)


ATTACHMENT_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "doc", "docx"]
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def sniff_content_type(uploaded):
    """MIME type worked out from the file's bytes (never from its name or the browser), or None."""
    position = uploaded.tell()
    try:
        uploaded.seek(0)
        head = uploaded.read(8)
        if head.startswith(b"%PDF"):
            return CONTENT_TYPES["pdf"]
        if head.startswith(b"\x89PNG"):
            return CONTENT_TYPES["png"]
        if head.startswith(b"\xff\xd8\xff"):
            return CONTENT_TYPES["jpg"]
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return CONTENT_TYPES["doc"]
        if head.startswith(b"PK\x03\x04"):
            # Any ZIP starts like this; a Word document must contain word/document.xml.
            uploaded.seek(0)
            try:
                with zipfile.ZipFile(uploaded) as archive:
                    if "word/document.xml" in archive.namelist():
                        return CONTENT_TYPES["docx"]
            except zipfile.BadZipFile:
                return None
        return None
    finally:
        uploaded.seek(position)


@deconstructible
class SettingMaxFileSizeValidator:
    """Like MaxFileSizeValidator, but reads the limit from a setting so it can change without a migration."""

    def __init__(self, setting_name):
        self.setting_name = setting_name

    def __call__(self, file):
        MaxFileSizeValidator(getattr(settings, self.setting_name))(file)

    def __eq__(self, other):
        return isinstance(other, SettingMaxFileSizeValidator) and other.setting_name == self.setting_name


def attachment_validators():
    return [FileExtensionValidator(ATTACHMENT_EXTENSIONS), SettingMaxFileSizeValidator("ATTACHMENT_MAX_UPLOAD_MB"),
            FileSignatureValidator()]


def document_validators(max_mb=20):
    return [FileExtensionValidator(DOCUMENT_EXTENSIONS), MaxFileSizeValidator(max_mb), FileSignatureValidator()]


def image_validators(max_mb=5):
    return [FileExtensionValidator(IMAGE_EXTENSIONS), MaxFileSizeValidator(max_mb), FileSignatureValidator()]


def sheet_validators(max_mb=10):
    return [FileExtensionValidator(SHEET_EXTENSIONS), MaxFileSizeValidator(max_mb), FileSignatureValidator()]


def run_validators(file, validators):
    """Validate a raw UploadedFile (multi-file inputs bypass model forms)."""
    errors = []
    for validator in validators:
        try:
            validator(_Wrapped(file))
        except ValidationError as exc:
            errors.extend(exc.messages)
    return errors


class _Wrapped:
    """Make a bare UploadedFile look like a FieldFile for the validators above."""

    def __init__(self, uploaded):
        self.file = uploaded
        self.name = uploaded.name
        self.size = uploaded.size
