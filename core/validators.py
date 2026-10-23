from pathlib import Path

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
