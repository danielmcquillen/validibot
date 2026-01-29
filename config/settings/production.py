# ruff: noqa: E501
import logging

import django.core.exceptions
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from .base import *  # noqa: F403
from .base import DATABASES
from .base import DEFAULT_FROM_EMAIL
from .base import INSTALLED_APPS
from .base import SPECTACULAR_SETTINGS
from .base import env

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# GCP production uses Cloud Tasks and Cloud Run Jobs.
DEPLOYMENT_TARGET = "gcp"

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES
# ------------------------------------------------------------------------------
# No Redis in the current stack; use in-memory cache for now. When adding
# Cloud Memcache/Redis later, replace with the appropriate backend and URL.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "validibot-production",
    },
}

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-proxy-ssl-header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-ssl-redirect
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-secure
SESSION_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-name
SESSION_COOKIE_NAME = "__Secure-sessionid"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-secure
CSRF_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-name
CSRF_COOKIE_NAME = "__Secure-csrftoken"
# https://docs.djangoproject.com/en/dev/topics/security/#ssl-https
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-seconds
# TODO: set this to 60 seconds first and then to 518400 once you prove the former works
SECURE_HSTS_SECONDS = 2592000
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-include-subdomains
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-preload
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
# https://docs.djangoproject.com/en/dev/ref/middleware/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)


# STATIC & MEDIA (GCS)
# ------------------------------------------------------------------------------
# File storage uses a SINGLE GCS bucket with prefix-based separation:
#
#   gs://validibot-storage/
#   ├── public/      # Publicly accessible (avatars, workflow images)
#   └── private/     # Private files (submissions, validation data, artifacts)
#
# Security model:
# - The bucket itself is PRIVATE (no public access at bucket level)
# - The `public/` prefix is made publicly readable via IAM Conditions:
#     Principal: allUsers
#     Role: roles/storage.objectViewer
#     Condition: resource.name.startsWith("projects/_/buckets/BUCKET/objects/public/")
# - The `private/` prefix remains private, accessible only to the service account
# - Users download private files via time-limited signed URLs
#
# Authentication: Cloud Run provides credentials via the attached service account.
# No API keys or credential files needed.
#
# See docs/dev_docs/how-to/configure-storage.md for setup instructions.
STORAGE_BUCKET = env("STORAGE_BUCKET")
if not STORAGE_BUCKET:
    raise django.core.exceptions.ImproperlyConfigured(
        "STORAGE_BUCKET is required in production."
    )

STORAGES = {
    # "default" is used by FileFields without explicit storage parameter.
    # Public media files (avatars, workflow images) go under public/ prefix.
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "location": "public",  # Files stored under public/ prefix
            "file_overwrite": False,
            # Direct URLs work because public/ prefix has allUsers read access
            "querystring_auth": False,
        },
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media URL points to the public prefix
MEDIA_URL = f"https://storage.googleapis.com/{STORAGE_BUCKET}/public/"

# GOOGLE CLOUD KMS
# ------------------------------------------------------------------------------
# Configuration for credential signing using Google Cloud KMS.
# See docs/dev_docs/google_cloud/kms.md for details.
#
# The signing key is the primary key used to sign credentials/badges.
# The JWKS keys list includes all keys that should be published in the
# /.well-known/jwks.json endpoint. During key rotation, this should include
# both the new and previous key until old badges expire.

GCP_KMS_SIGNING_KEY = env(
    "GCP_KMS_SIGNING_KEY",
    default="projects/project-a509c806-3e21-4fbc-b19/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing",
)

GCP_KMS_JWKS_KEYS = env.list(
    "GCP_KMS_JWKS_KEYS",
    default=[GCP_KMS_SIGNING_KEY],
)

SV_JWKS_ALG = env("SV_JWKS_ALG", default="ES256")

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#default-from-email

# https://docs.djangoproject.com/en/dev/ref/settings/#server-email
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
# https://docs.djangoproject.com/en/dev/ref/settings/#email-subject-prefix
EMAIL_SUBJECT_PREFIX = env(
    "DJANGO_EMAIL_SUBJECT_PREFIX",
    default="[Validibot] ",
)
ACCOUNT_EMAIL_SUBJECT_PREFIX = EMAIL_SUBJECT_PREFIX

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL regex.
ADMIN_URL = env("DJANGO_ADMIN_URL")

# Anymail
# ------------------------------------------------------------------------------
# https://anymail.readthedocs.io/en/stable/installation/#installing-anymail
INSTALLED_APPS += ["anymail"]
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
# https://anymail.readthedocs.io/en/stable/installation/#anymail-settings-reference
# https://anymail.readthedocs.io/en/stable/esps/postmark/


POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN", default=None)

if POSTMARK_SERVER_TOKEN:
    EMAIL_BACKEND = "anymail.backends.postmark.EmailBackend"
    ANYMAIL = {
        "POSTMARK_SERVER_TOKEN": POSTMARK_SERVER_TOKEN,
    }
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
# See https://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
#
# We use structured JSON logging in production so that Cloud Logging can parse
# and index log fields, making them searchable and filterable. Cloud Run
# automatically captures stdout and sends it to Cloud Logging.
#
# See docs/dev_docs/google_cloud/logging.md for usage details.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        # JSON formatter for Cloud Logging integration
        # Fields like severity, message, module, etc. become queryable
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(message)s",
            # Cloud Logging expects "severity" instead of "levelname"
            "rename_fields": {"levelname": "severity"},
        },
        # Keep verbose formatter for local debugging if needed
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "json",  # Use JSON for Cloud Logging
        },
    },
    "root": {
        "level": "INFO",  # INFO in production (DEBUG is too verbose)
        "handlers": ["console"],
    },
    "loggers": {
        "django.db.backends": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
        "django.request": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
        # Errors logged by the SDK itself
        "sentry_sdk": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
        "validibot.users": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "validibot.validations": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}

# Sentry
# ------------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN")
SENTRY_LOG_LEVEL = env.int("DJANGO_SENTRY_LOG_LEVEL", logging.INFO)

sentry_logging = LoggingIntegration(
    level=SENTRY_LOG_LEVEL,  # Capture info and above as breadcrumbs
    event_level=logging.ERROR,  # Send errors as events
)
integrations = [
    sentry_logging,
    DjangoIntegration(),
]
sentry_sdk.init(
    dsn=SENTRY_DSN,
    integrations=integrations,
    environment=env("SENTRY_ENVIRONMENT", default="production"),
    traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
    ignore_errors=[django.core.exceptions.DisallowedHost],
)

# django-rest-framework
# -------------------------------------------------------------------------------
# Tools that generate code samples can use SERVERS to point to the correct domain
SPECTACULAR_SETTINGS["SERVERS"] = [
    {
        "url": "https://validibotvalidator.com",
        "description": "Production server",
    },
]
# Your stuff...
# ------------------------------------------------------------------------------

# Superuser configuration for setup_all command
# These are used to bootstrap a superuser in production
SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default=None)
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default=None)
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default=None)
SUPERUSER_NAME = env("SUPERUSER_NAME", default=None)

# DATA STORAGE (Validation Pipeline Files)
# ------------------------------------------------------------------------------
# In production, use GCS for validation data (submissions, envelopes, outputs).
# Uses the same bucket as Django storage, but with private/ prefix.
DATA_STORAGE_BACKEND = "gcs"
DATA_STORAGE_BUCKET = STORAGE_BUCKET
DATA_STORAGE_PREFIX = "private"
DATA_STORAGE_OPTIONS = {
    "bucket_name": DATA_STORAGE_BUCKET,
    "prefix": DATA_STORAGE_PREFIX,
}

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# GCP production uses Google Cloud Run Jobs for container-based validators.
VALIDATOR_RUNNER = "google_cloud_run"
VALIDATOR_RUNNER_OPTIONS = {
    "project_id": env("GCP_PROJECT_ID"),
    "region": env("GCP_REGION", default="australia-southeast1"),
}

# Cloud Run Job Validator Settings
# ------------------------------------------------------------------------------
# Production settings for Cloud Run Jobs infrastructure
GCS_VALIDATION_BUCKET = STORAGE_BUCKET  # Use same bucket for validation files
GCS_TASK_QUEUE_NAME = env("GCS_TASK_QUEUE_NAME", default="validibot-tasks")
GCS_ENERGYPLUS_JOB_NAME = env(
    "GCS_ENERGYPLUS_JOB_NAME",
    default="validibot-validator-energyplus",
)
GCS_FMI_JOB_NAME = env(
    "GCS_FMI_JOB_NAME",
    default="validibot-validator-fmi",
)
SITE_URL = env("SITE_URL", default="https://validi.com")
WORKER_URL = env("WORKER_URL", default="")
