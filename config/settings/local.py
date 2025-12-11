from .base import *  # noqa: F403
from .base import INSTALLED_APPS
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = True
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="vYFSWUQZszpWqRqe0s8sdP60HQGXX0t8erh3EZMFOLIxeMCZnDn9zOGnTJGW4n5B",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ["localhost", "0.0.0.0", "127.0.0.1", "7b25b7bffdf5.ngrok-free.app"]  # noqa: S104

# CACHES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#caches
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    },
}

# STORAGE
# -------------------------------------------------------------------------------
# By default, use local filesystem for media. To test GCS locally, set
# GCS_MEDIA_BUCKET and GCS_FILES_BUCKET and run `gcloud auth application-default login`.
GCS_MEDIA_BUCKET = env("GCS_MEDIA_BUCKET", default=None)
GCS_FILES_BUCKET = env("GCS_FILES_BUCKET", default=None)

if GCS_MEDIA_BUCKET and GCS_FILES_BUCKET:
    # Use GCS for media files (matches production)
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": GCS_FILES_BUCKET,
                "file_overwrite": False,
                "querystring_auth": False,
            },
        },
        "public": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": GCS_MEDIA_BUCKET,
                "file_overwrite": False,
                "querystring_auth": False,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://storage.googleapis.com/{GCS_MEDIA_BUCKET}/"
else:
    # Use local filesystem (default for local development)
    # Two separate directories to mirror production's two-bucket strategy
    FILES_ROOT = BASE_DIR / "media" / "files"  # noqa: F405 - Private files
    MEDIA_ROOT = BASE_DIR / "media" / "public"  # noqa: F405 - Public media
    MEDIA_URL = "/media/public/"

    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(FILES_ROOT),
                "base_url": "/media/files/",
            },
        },
        "public": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {
                "location": str(MEDIA_ROOT),
                "base_url": "/media/public/",
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

# EMAIL
# ------------------------------------------------------------------------------
# Console backend prints emails to terminal (no mail server needed)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
# To use Mailpit instead, comment above and uncomment below, then run `mailpit`
# EMAIL_HOST = "localhost"
# EMAIL_PORT = 1025

# WhiteNoise
# ------------------------------------------------------------------------------
# http://whitenoise.evans.io/en/latest/django.html#using-whitenoise-in-development
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]


# django-debug-toolbar
# ------------------------------------------------------------------------------
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#prerequisites
# INSTALLED_APPS += ["debug_toolbar"]
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#middleware
# MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
# https://django-debug-toolbar.readthedocs.io/en/latest/configuration.html#debug-toolbar-config
DEBUG_TOOLBAR_CONFIG = {
    "DISABLE_PANELS": [
        "debug_toolbar.panels.redirects.RedirectsPanel",
        # Disable profiling panel due to an issue with Python 3.13:
        # https://github.com/jazzband/django-debug-toolbar/issues/1875
        "debug_toolbar.panels.profiling.ProfilingPanel",
    ],
    "SHOW_TEMPLATE_CONTEXT": True,
}
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#internal-ips
INTERNAL_IPS = ["127.0.0.1", "10.0.2.2"]


# django-extensions
# ------------------------------------------------------------------------------
# https://django-extensions.readthedocs.io/en/latest/installation_instructions.html#configuration
INSTALLED_APPS += ["django_extensions"]

# Validibot settings for local development
# ------------------------------------------------------------------------------


SUPERUSER_USERNAME = env("SUPERUSER_USERNAME", default="admin")
SUPERUSER_PASSWORD = env("SUPERUSER_PASSWORD", default="someadminpwchangeforrealz")
SUPERUSER_EMAIL = env("SUPERUSER_EMAIL", default="")
SUPERUSER_NAME = env("SUPERUSER_NAME", default="Admin User")


TEST_ENERGYPLUS_LIVE_MODAL = env.bool("TEST_ENERGYPLUS_LIVE_MODAL", default=False)


SIMULATE_LONG_TASKS = env.bool("SIMULATE_LONG_TASKS", default=True)
LONG_TASK_DELAY_SECONDS = env.int("LONG_TASK_DELAY_SECONDS", default=5)


# Logging
# ------------------------------------------------------------------------------
# Make local development chatty so timing diagnostics show up immediately.
LOGGING["root"]["level"] = "DEBUG"  # noqa: F405
LOGGING["loggers"]["validibot"] = {  # noqa: F405
    "handlers": ["console"],
    "level": "DEBUG",
    "propagate": False,
}
