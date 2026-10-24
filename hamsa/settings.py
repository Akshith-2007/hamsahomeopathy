"""Django settings for the Hamsa Homeopathy case management system.

All deployment-specific values come from environment variables so the same
image runs under docker-compose, Kubernetes, or a local virtualenv.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Read KEY=VALUE lines from .env (if present). Real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


_INSECURE_KEY = "dev-insecure-change-me"
SECRET_KEY = env("DJANGO_SECRET_KEY", _INSECURE_KEY)
DEBUG = env_bool("DJANGO_DEBUG", False)
if not DEBUG and SECRET_KEY == _INSECURE_KEY and not env_bool("DJANGO_ALLOW_INSECURE_KEY"):
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=true for local development).")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
if env_bool("DJANGO_ALLOW_LAN"):
    # Let other devices on the same Wi-Fi reach this PC by its current IP or name.
    import socket

    _hostname = socket.gethostname()
    try:
        _lan_ips = socket.gethostbyname_ex(_hostname)[2]
    except OSError:
        _lan_ips = []
    ALLOWED_HOSTS += [_hostname, f"{_hostname}.local", *_lan_ips]
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
# Render (and similar hosts) tell the app its public hostname.
_render_host = env("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "drf_spectacular",
    "accounts",
    "core",
    "clinic",
    "api",
]

MIDDLEWARE = [
    "core.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.CurrentRequestMiddleware",
]

ROOT_URLCONF = "hamsa.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "hamsa.wsgi.application"

# --- Database ---------------------------------------------------------------
# PostgreSQL is the supported database. DB_ENGINE=sqlite exists only for quick
# local experiments and the test suite.
if env("DB_ENGINE", "postgres") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3")),
        }
    }
elif env("DATABASE_URL"):
    # Hosted PostgreSQL (e.g. Render): postgres://user:password@host:port/dbname
    from urllib.parse import unquote, urlparse

    _db = urlparse(env("DATABASE_URL"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db.path.lstrip("/"),
            "USER": unquote(_db.username or ""),
            "PASSWORD": unquote(_db.password or ""),
            "HOST": _db.hostname or "localhost",
            "PORT": str(_db.port or 5432),
            "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "hamsa"),
            "USER": env("POSTGRES_USER", "hamsa"),
            "PASSWORD": env("POSTGRES_PASSWORD", "hamsa"),
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication & sessions ------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.LockAwareModelBackend"]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
MAX_FAILED_LOGINS = int(env("MAX_FAILED_LOGINS", "5"))

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Browsers share cookies across every port of a host, so the default names
# ("sessionid", "csrftoken") get overwritten by any other Django app on localhost.
SESSION_COOKIE_NAME = "hamsa_sessionid"
CSRF_COOKIE_NAME = "hamsa_csrftoken"
SESSION_COOKIE_AGE =int(env("SESSION_COOKIE_AGE", str(8 * 60 * 60)))  # one clinic shift
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True  # sliding expiry while the user is active
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # the case-record JS reads it for fetch() calls

SECURE_COOKIES = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"  # PDF previews are embedded in iframes
if env_bool("DJANGO_BEHIND_TLS_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Enable once the site is HTTPS-only.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "0"))

# --- I18N ---------------------------------------------------------------------
LANGUAGE_CODE = "en-in"
TIME_ZONE = env("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True
DATE_INPUT_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"]

# --- Static & media -------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = Path(env("STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Uploaded clinical documents live on local disk (a PersistentVolume in k8s) and
# are only served through an authenticated, role-checked view.
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", BASE_DIR / "media"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
MAX_DOCUMENT_UPLOAD_MB = 20
MAX_IMAGE_UPLOAD_MB = 5

# --- REST framework -------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Hamsa Homeopathy – Case Management API",
    "DESCRIPTION": "Role-based REST API for patients, visits, clinical sections, prescriptions, "
    "investigations and uploads. Authenticate with a session or `Authorization: Token <key>`.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "VisitStatusEnum": "clinic.models.Visit.Status",
        "InvestigationStatusEnum": "clinic.models.Investigation.Status",
        "ScheduledFollowUpStatusEnum": "clinic.models.ScheduledFollowUp.Status",
        "VisitConsultationModeEnum": "clinic.models.Visit.Mode",
        "ReportVersionStatusEnum": "clinic.models.InvestigationReport.Version",
        "RelationshipLinkStatusEnum": "clinic.models.PatientRelationship.LinkStatus",
    },
}

# --- Clinic / notifications ---------------------------------------------------
CLINIC_NAME = env("CLINIC_NAME", "Hamsa Homeopathy")
CLINIC_ADDRESS = env("CLINIC_ADDRESS", "")
CLINIC_PHONE = env("CLINIC_PHONE", "")
# Pluggable SMS / WhatsApp delivery. "console" logs messages; real providers can
# be added in core/notifications.py without touching callers.
NOTIFICATION_BACKEND = env("NOTIFICATION_BACKEND", "core.notifications.ConsoleBackend")
SMS_ENABLED = env_bool("SMS_ENABLED", False)
# Lab dashboard: investigations pending longer than this many days are flagged overdue.
LAB_OVERDUE_DAYS = int(env("LAB_OVERDUE_DAYS", "1"))
WHATSAPP_ENABLED = env_bool("WHATSAPP_ENABLED", False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
