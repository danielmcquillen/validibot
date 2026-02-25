"""
Production settings for Validibot.

This settings file handles all production deployments. The specific infrastructure
is determined by the DEPLOYMENT_TARGET environment variable:

    - "gcp": Google Cloud Platform (Cloud Run, Cloud Tasks, GCS)
    - "docker_compose": Docker Compose (Docker socket, Celery, local/S3/GCS)
    - "aws": Amazon Web Services (future - AWS Batch, SQS, S3)

Required environment variables (all targets):
    DJANGO_SECRET_KEY: Secure secret key for Django
    DATABASE_URL: PostgreSQL connection string
    DJANGO_ALLOWED_HOSTS: Comma-separated list of allowed hosts
    DEPLOYMENT_TARGET: One of "gcp", "docker_compose", "aws"

Target-specific requirements:
    GCP:
        STORAGE_BUCKET: GCS bucket name
        GCP_PROJECT_ID: Google Cloud project ID
        SENTRY_DSN: Sentry error tracking
        DJANGO_ADMIN_URL: Admin URL path

    Docker Compose:
        REDIS_URL: Redis connection string (for Celery)
        (STORAGE_BUCKET optional - uses local filesystem if not set)

    AWS:
        STORAGE_BUCKET: S3 bucket name
        AWS_S3_REGION_NAME: AWS region
"""

import logging

import django.core.exceptions
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import BASE_DIR
from .base import DATABASES
from .base import DEFAULT_FROM_EMAIL
from .base import INSTALLED_APPS
from .base import LOGGING
from .base import SPECTACULAR_SETTINGS
from .base import env

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Determines which infrastructure backend to use for task queue and validators.
DEPLOYMENT_TARGET = env("DEPLOYMENT_TARGET", default="docker_compose")

VALID_DEPLOYMENT_TARGETS = {"gcp", "docker_compose", "aws"}
if DEPLOYMENT_TARGET not in VALID_DEPLOYMENT_TARGETS:
    raise ImproperlyConfigured(
        f"DEPLOYMENT_TARGET must be one of {VALID_DEPLOYMENT_TARGETS}, "
        f"got: {DEPLOYMENT_TARGET}"
    )
# NOTE: AWS is accepted as a deployment target for forward-compatibility but
# is not yet implemented.  Storage (S3), task dispatch, and execution backends
# are all stubs that raise NotImplementedError.  See core/storage/s3.py,
# core/tasks/dispatch/registry.py, and validations/services/execution/registry.py.

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = False
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES
# ------------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default=None)
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        },
    }
else:
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
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-seconds
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=2592000)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-include-subdomains
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-preload
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)

# STORAGE
# ------------------------------------------------------------------------------
# Storage configuration varies by deployment target:
#
# GCP: Uses GCS bucket (required)
# Docker Compose: Uses local filesystem by default, or GCS/S3 if configured
# AWS: Uses S3 bucket (required)
#
# All targets use a single bucket/directory with prefix-based separation:
#   ├── public/      # Publicly accessible (avatars, workflow images)
#   └── private/     # Private files (submissions, validation data)

STORAGE_BUCKET = env("STORAGE_BUCKET", default=None)
DATA_STORAGE_BACKEND = env("DATA_STORAGE_BACKEND", default=None)

if DEPLOYMENT_TARGET == "gcp":
    # GCP requires a storage bucket
    if not STORAGE_BUCKET:
        raise django.core.exceptions.ImproperlyConfigured(
            "STORAGE_BUCKET is required for GCP deployment."
        )

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "public",
                "file_overwrite": False,
                "querystring_auth": False,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://storage.googleapis.com/{STORAGE_BUCKET}/public/"

    # Data storage for validation files
    DATA_STORAGE_BACKEND = "gcs"
    DATA_STORAGE_OPTIONS = {
        "bucket_name": STORAGE_BUCKET,
        "prefix": "private",
    }

elif DEPLOYMENT_TARGET == "aws":
    # AWS requires a storage bucket
    if not STORAGE_BUCKET:
        raise django.core.exceptions.ImproperlyConfigured(
            "STORAGE_BUCKET is required for AWS deployment."
        )

    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default=None)
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "bucket_name": STORAGE_BUCKET,
                "location": "public",
                "file_overwrite": False,
                "querystring_auth": False,
                "region_name": AWS_S3_REGION_NAME,
                "endpoint_url": AWS_S3_ENDPOINT_URL,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://{STORAGE_BUCKET}.s3.amazonaws.com/public/"

    # Data storage for validation files
    DATA_STORAGE_BACKEND = "s3"
    DATA_STORAGE_OPTIONS = {
        "bucket_name": STORAGE_BUCKET,
        "prefix": "private",
        "region_name": AWS_S3_REGION_NAME,
        "endpoint_url": AWS_S3_ENDPOINT_URL,
    }

# Docker Compose can use local storage or cloud storage
elif STORAGE_BUCKET:
    # Cloud storage configured
    if DATA_STORAGE_BACKEND == "gcs":
        STORAGES = {
            "default": {
                "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "public",
                    "file_overwrite": False,
                    "querystring_auth": False,
                },
            },
            "staticfiles": {
                "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
            },
        }
        MEDIA_URL = f"https://storage.googleapis.com/{STORAGE_BUCKET}/public/"
        DATA_STORAGE_OPTIONS = {
            "bucket_name": STORAGE_BUCKET,
            "prefix": "private",
        }
    elif DATA_STORAGE_BACKEND == "s3":
        AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default=None)
        AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)

        STORAGES = {
            "default": {
                "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
                "OPTIONS": {
                    "bucket_name": STORAGE_BUCKET,
                    "location": "public",
                    "file_overwrite": False,
                    "querystring_auth": False,
                    "region_name": AWS_S3_REGION_NAME,
                    "endpoint_url": AWS_S3_ENDPOINT_URL,
                },
            },
            "staticfiles": {
                "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
            },
        }
        MEDIA_URL = f"https://{STORAGE_BUCKET}.s3.amazonaws.com/public/"
        DATA_STORAGE_OPTIONS = {
            "bucket_name": STORAGE_BUCKET,
            "prefix": "private",
            "region_name": AWS_S3_REGION_NAME,
            "endpoint_url": AWS_S3_ENDPOINT_URL,
        }
    else:
        raise django.core.exceptions.ImproperlyConfigured(
            f"DATA_STORAGE_BACKEND must be 'gcs' or 's3' when STORAGE_BUCKET is set, "
            f"got: {DATA_STORAGE_BACKEND}"
        )
else:
    # Local filesystem storage (default for simple Docker Compose deployments)
    STORAGE_ROOT = BASE_DIR / "storage"
    PUBLIC_STORAGE_ROOT = STORAGE_ROOT / "public"
    PRIVATE_STORAGE_ROOT = STORAGE_ROOT / "private"
    MEDIA_ROOT = PUBLIC_STORAGE_ROOT
    MEDIA_URL = "/media/"

    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(PUBLIC_STORAGE_ROOT),
                "base_url": "/media/",
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

    DATA_STORAGE_BACKEND = "local"
    DATA_STORAGE_ROOT = str(PRIVATE_STORAGE_ROOT)
    DATA_STORAGE_OPTIONS = {"root": DATA_STORAGE_ROOT}

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# The validator runner executes advanced validator containers.
#
# GCP: Google Cloud Run Jobs (async with callbacks)
# Docker Compose: Local Docker socket (synchronous)
# AWS: AWS Batch (future)

if DEPLOYMENT_TARGET == "gcp":
    VALIDATOR_RUNNER = "google_cloud_run"
    VALIDATOR_RUNNER_OPTIONS = {
        "project_id": env("GCP_PROJECT_ID"),
        "region": env("GCP_REGION", default="us-west1"),
    }

    # Cloud Run Job names
    GCS_VALIDATION_BUCKET = STORAGE_BUCKET
    GCS_TASK_QUEUE_NAME = env("GCS_TASK_QUEUE_NAME", default="validibot-tasks")
    GCS_ENERGYPLUS_JOB_NAME = env(
        "GCS_ENERGYPLUS_JOB_NAME",
        default="validibot-validator-energyplus",
    )
    GCS_FMU_JOB_NAME = env(
        "GCS_FMU_JOB_NAME",
        default="validibot-validator-fmu",
    )

elif DEPLOYMENT_TARGET == "aws":
    # AWS Batch runner (future implementation)
    VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="aws_batch")
    VALIDATOR_RUNNER_OPTIONS = {
        "region": env("AWS_REGION", default="us-east-1"),
        # Additional AWS Batch configuration will go here
    }

else:  # docker_compose
    VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="docker")
    VALIDATOR_RUNNER_OPTIONS = {
        "memory_limit": env("VALIDATOR_MEMORY_LIMIT", default="4g"),
        "cpu_limit": env("VALIDATOR_CPU_LIMIT", default="2.0"),
        "network": env("VALIDATOR_NETWORK", default=None),
        "timeout_seconds": env.int("VALIDATOR_TIMEOUT_SECONDS", default=3600),
    }

    # Container image configuration for advanced validators
    VALIDATOR_IMAGE_TAG = env("VALIDATOR_IMAGE_TAG", default="latest")
    VALIDATOR_IMAGE_REGISTRY = env("VALIDATOR_IMAGE_REGISTRY", default="")

    # Advanced validator images to enable (for sync_validators command)
    ADVANCED_VALIDATOR_IMAGES = env.list("ADVANCED_VALIDATOR_IMAGES", default=[])

# Site URL for callbacks
SITE_URL = env("SITE_URL", default="http://localhost:8000")
WORKER_URL = env("WORKER_URL", default=SITE_URL)

# EMAIL
# ------------------------------------------------------------------------------
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", default="[Validibot] ")
ACCOUNT_EMAIL_SUBJECT_PREFIX = EMAIL_SUBJECT_PREFIX

# Check for various email backends
POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN", default=None)
EMAIL_HOST = env("EMAIL_HOST", default=None)

if POSTMARK_SERVER_TOKEN:
    # Postmark (via Anymail)
    INSTALLED_APPS += ["anymail"]
    EMAIL_BACKEND = "anymail.backends.postmark.EmailBackend"
    ANYMAIL = {
        "POSTMARK_SERVER_TOKEN": POSTMARK_SERVER_TOKEN,
    }
elif EMAIL_HOST:
    # Generic SMTP
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
else:
    # Console backend (for development/testing)
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = env("DJANGO_ADMIN_URL", default="admin/")

# LOGGING
# ------------------------------------------------------------------------------
# Use JSON logging for production (compatible with most log aggregators)
LOGGING["formatters"]["json"] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(message)s",
    "rename_fields": {"levelname": "severity"},
}
LOGGING["handlers"]["console"]["formatter"] = "json"
LOGGING["root"]["level"] = "INFO"

# Add production-specific logger configuration
LOGGING["loggers"] = {
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
}

# SENTRY (Optional)
# ------------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default=None)
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_logging = LoggingIntegration(
        level=logging.INFO,
        event_level=logging.ERROR,
    )
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[sentry_logging, DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default=DEPLOYMENT_TARGET),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        ignore_errors=[django.core.exceptions.DisallowedHost],
    )

# SUPERUSER BOOTSTRAP
# ------------------------------------------------------------------------------
# Used by setup_validibot management command to create initial superuser
SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default=None)
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default=None)
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default=None)
SUPERUSER_NAME = env("SUPERUSER_NAME", default=None)

# django-rest-framework
# -------------------------------------------------------------------------------
if SITE_URL and SITE_URL != "http://localhost:8000":
    SPECTACULAR_SETTINGS["SERVERS"] = [
        {
            "url": SITE_URL,
            "description": "Production server",
        },
    ]
