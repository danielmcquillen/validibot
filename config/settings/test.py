"""
With these settings, tests run faster.
"""

import os

# Set test-safe Stripe keys before base settings validates them
# These look like real test keys but are dummy values for testing
os.environ.setdefault("STRIPE_TEST_SECRET_KEY", "sk_test_dummy_test_key_for_testing")
os.environ.setdefault("STRIPE_TEST_PUBLIC_KEY", "pk_test_dummy_test_key_for_testing")

from .base import *  # noqa: F403
from .base import DATABASES
from .base import REST_FRAMEWORK
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
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
# live_server fixture uses localhost with a random port
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

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

# CEL evaluation timeout - increase for tests since heavy test loads can cause
# thread pool overhead that exceeds the default 100ms timeout
CEL_MAX_EVAL_TIMEOUT_MS = 500

# Disable DRF throttling in tests to prevent rate limit failures during test runs
# Tests run many rapid API calls which would trigger throttle limits.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # type: ignore[name-defined]
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {}  # type: ignore[name-defined]
