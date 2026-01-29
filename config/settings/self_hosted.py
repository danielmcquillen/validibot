"""
Self-hosted production settings for Validibot.

This settings file is for self-hosted deployments using Docker Compose,
suitable for running on any cloud provider or on-premises infrastructure.

Key differences from GCP production:
- Uses Docker socket for validator containers (not Cloud Run Jobs)
- Uses local storage or S3/GCS depending on configuration
- Simpler infrastructure requirements

Required environment variables:
    DJANGO_SECRET_KEY: Secure secret key for Django
    DATABASE_URL: PostgreSQL connection string
    DJANGO_ALLOWED_HOSTS: Comma-separated list of allowed hosts

Optional environment variables:
    STORAGE_BUCKET: GCS or S3 bucket for file storage (uses local if not set)
    DATA_STORAGE_BACKEND: "local", "gcs", or "s3" (defaults based on STORAGE_BUCKET)
    SENTRY_DSN: Sentry error tracking (optional)
    EMAIL_*: Email configuration (defaults to console backend)
"""

import logging

from .base import *  # noqa: F403
from .base import BASE_DIR
from .base import DATABASES
from .base import DEFAULT_FROM_EMAIL
from .base import LOGGING
from .base import env

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Docker Compose deployment uses Dramatiq + Redis for task queue and Docker for validators.
DEPLOYMENT_TARGET = "docker_compose"

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
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
# Uses local memory cache by default. For multi-instance deployments,
# configure Redis via REDIS_URL.
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
            "LOCATION": "validibot-self-hosted",
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
# https://docs.djangoproject.com/en/dev/ref/middleware/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)

# STORAGE
# ------------------------------------------------------------------------------
# Storage configuration for self-hosted deployments.
#
# For simplest deployment, use local filesystem storage:
#   - Files stored in Docker volume at /app/storage
#   - Public files (avatars, images) served via Django
#   - Private files (validation data) accessible via download endpoint
#
# For cloud storage (S3 or GCS), set STORAGE_BUCKET and DATA_STORAGE_BACKEND.

STORAGE_BUCKET = env("STORAGE_BUCKET", default=None)
DATA_STORAGE_BACKEND = env("DATA_STORAGE_BACKEND", default="local")

if STORAGE_BUCKET:
    # Cloud storage configuration
    if DATA_STORAGE_BACKEND == "gcs":
        # Google Cloud Storage
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
        # Amazon S3 (or S3-compatible storage)
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
        msg = (
            f"Unknown DATA_STORAGE_BACKEND with STORAGE_BUCKET: "
            f"{DATA_STORAGE_BACKEND}"
        )
        raise ValueError(msg)
else:
    # Local filesystem storage (default for simple self-hosted deployments)
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

    # Data storage for validation files
    DATA_STORAGE_BACKEND = "local"
    DATA_STORAGE_ROOT = str(PRIVATE_STORAGE_ROOT)
    DATA_STORAGE_OPTIONS = {"root": DATA_STORAGE_ROOT}

# VALIDATOR RUNNER
# ------------------------------------------------------------------------------
# Self-hosted deployments use Docker socket for running validator containers.
# This requires the Docker socket to be mounted into the Django container.
#
# Available runners:
#   - "docker": Local Docker socket (synchronous, for self-hosted)
#   - "google_cloud_run": Google Cloud Run Jobs (async, for GCP deployments)
#
# The VALIDATOR_RUNNER setting is used by the ExecutionBackend registry to
# select the appropriate backend for running advanced validator containers.

VALIDATOR_RUNNER = env("VALIDATOR_RUNNER", default="docker")
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": env("VALIDATOR_MEMORY_LIMIT", default="4g"),
    "cpu_limit": env("VALIDATOR_CPU_LIMIT", default="2.0"),
    "network": env("VALIDATOR_NETWORK", default=None),
    "timeout_seconds": env.int("VALIDATOR_TIMEOUT_SECONDS", default=3600),
}

# Container image configuration for advanced validators
# Images are expected to follow the naming convention: validibot-validator-{type}
# For example: validibot-validator-energyplus, validibot-validator-fmi
VALIDATOR_IMAGE_TAG = env("VALIDATOR_IMAGE_TAG", default="latest")
VALIDATOR_IMAGE_REGISTRY = env("VALIDATOR_IMAGE_REGISTRY", default="")

# Optional: Explicit image mapping (overrides default naming convention)
# VALIDATOR_IMAGES = {
#     "energyplus": "my-registry/my-energyplus:v1.0",
#     "fmi": "my-registry/my-fmi:v1.0",
# }

# Site URL for callbacks (validators POST results back to Django)
SITE_URL = env("SITE_URL", default="http://localhost:8000")
WORKER_URL = env("WORKER_URL", default=SITE_URL)

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#default-from-email
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", default="[Validibot] ")

# Use SMTP if configured, otherwise console backend
EMAIL_HOST = env("EMAIL_HOST", default=None)
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
else:
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
        environment=env("SENTRY_ENVIRONMENT", default="self-hosted"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
    )

# SUPERUSER BOOTSTRAP
# ------------------------------------------------------------------------------
# Used by setup_all management command to create initial superuser
SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default=None)
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default=None)
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default=None)
SUPERUSER_NAME = env("SUPERUSER_NAME", default=None)
