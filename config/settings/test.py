"""
With these settings, tests run faster.
"""

from .base import *  # noqa: F403
from .base import TEMPLATES
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="INgnwuvH37jf6eck2HmmKz8ISsZbDCj8v5YbhI9PXxzOCuBTS7Ns4Y4gZGGFTfDQ",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#test-runner
TEST_RUNNER = "django.test.runner.DiscoverRunner"
# For threaded live server tests, force new DB connections per request/test.
DATABASES["default"]["CONN_MAX_AGE"] = 0  # type: ignore[name-defined]

# PASSWORDS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#password-hashers
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# DEBUGGING FOR TEMPLATES
# ------------------------------------------------------------------------------
TEMPLATES[0]["OPTIONS"]["debug"] = True  # type: ignore[index]

# MEDIA
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#media-url
MEDIA_URL = "http://media.testserver/"

# STORAGES
# ------------------------------------------------------------------------------
# Configure storage backends for tests
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "public": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# JWKS
# ------------------------------------------------------------------------------
SV_JWKS_ALG = env("SV_JWKS_ALG", default="ES256")

# Your stuff...
# ------------------------------------------------------------------------------

# Test environment should mimic public/web surface so login and UI routes exist.
APP_ROLE = "web"
APP_IS_WORKER = False
ACCOUNT_ALLOW_LOGIN = True
