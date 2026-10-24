"""Private S3-compatible storage for uploaded clinical files (Cloudflare R2, AWS S3, ...).

The bucket stays private. `url()` returns the app's own /media/<path> address so every link
still passes through the authenticated, role-checked `protected_media` view, which then
redirects to a short-lived signed URL from `signed_url()`. This also keeps large files off
the app server (Vercel limits responses to 4.5 MB).
"""
from urllib.parse import quote

from django.conf import settings
from storages.backends.s3 import S3Storage


class ProtectedS3Storage(S3Storage):
    def url(self, name, parameters=None, expire=None, http_method=None):
        return f"{settings.MEDIA_URL}{quote(name)}"

    def signed_url(self, name):
        return super().url(name)
