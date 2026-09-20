import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("STUDIO_DEBUG") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-only" if DEBUG else "")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is required")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "").split(",")
if not DEBUG:
    ALLOWED_HOSTS.append("healthcheck.railway.app")
CSRF_TRUSTED_ORIGINS = [v for v in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if v]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "studio",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "studio.middleware.PrivacyHeaders",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'local.sqlite3'}" if DEBUG else None,
        conn_max_age=60,
    )
}
if not DATABASES["default"]:
    raise ImproperlyConfigured("DATABASE_URL is required")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 15},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = 12 * 60 * 60
SESSION_COOKIE_NAME = "studio_session" if DEBUG else "__Host-studio_session"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health$"]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "studio.api_errors.handle",
}
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = (
    [BASE_DIR.parent / "frontend" / "dist"]
    if (BASE_DIR.parent / "frontend" / "dist").exists()
    else []
)
STORAGES = {"staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"}}
MEDIA_ROOT = BASE_DIR / ".media"
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
S3_REGION = os.getenv("S3_REGION", "auto")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
GENERATION_PROVIDER = os.getenv("GENERATION_PROVIDER", "mock" if DEBUG else "byteplus")
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "")
ARK_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
PROVIDER_MAX_SECONDS = int(os.getenv("PROVIDER_MAX_SECONDS", "30"))
CREDITS_PER_SECOND = (
    int(os.environ["CREDITS_PER_SECOND"]) if os.getenv("CREDITS_PER_SECOND") else None
)
OUTPUT_HOSTS = [h.strip() for h in os.getenv("PROVIDER_OUTPUT_HOSTS", "").split(",") if h.strip()]
MFA_REQUIRED = not DEBUG
MFA_ENCRYPTION_KEY = os.getenv("MFA_ENCRYPTION_KEY", "")
if not DEBUG and (not S3_BUCKET or not MFA_ENCRYPTION_KEY or GENERATION_PROVIDER == "mock"):
    raise ImproperlyConfigured(
        "Production requires private S3, MFA_ENCRYPTION_KEY and a real provider"
    )
CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 600
CELERY_TASK_TIME_LIMIT = 660
CELERY_TASK_ROUTES = {"studio.tasks.assemble": {"queue": "media"}}
CELERY_BEAT_SCHEDULE = {"recover-and-dispatch": {"task": "studio.tasks.dispatch", "schedule": 15.0}}
# Deliberately no request bodies, prompts, provider responses or access-log URLs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}

BYTEPLUS_ACCESS_KEY_ID = os.getenv("BYTEPLUS_ACCESS_KEY_ID", "")
BYTEPLUS_SECRET_ACCESS_KEY = os.getenv("BYTEPLUS_SECRET_ACCESS_KEY", "")
BYTEPLUS_ASSET_PROJECT = os.getenv("BYTEPLUS_ASSET_PROJECT", "default")

DEEPSEEK_MODEL = (
    os.getenv("DEEPSEEK_ENDPOINT_ID")
    or os.getenv("BYTEPLUS_DEEPSEEK_ENDPOINT_ID")
    or "deepseek-v4-pro-260425"
)
REFERENCE_VISION_MODEL = os.getenv("REFERENCE_VISION_ENDPOINT_ID") or "seed-2-0-lite-260228"
