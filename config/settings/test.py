"""
With these settings, tests run faster.
"""

from .base import *  # noqa: F403
from .base import DATABASES
from .base import REST_FRAMEWORK
from .base import TEMPLATES
from .base import env

# DEPLOYMENT TARGET
# ------------------------------------------------------------------------------
# Test environment uses synchronous inline execution (no task queue or HTTP).
DEPLOYMENT_TARGET = "test"

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
# Single storage backend - public media uses public/ prefix (simulated by base_url)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Your stuff...
# ------------------------------------------------------------------------------

# Test environment should mimic public/web surface so login and UI routes exist.
APP_ROLE = "web"
APP_IS_WORKER = False
ACCOUNT_ALLOW_LOGIN = True

# Worker API key is not required in tests (no HTTP calls to worker endpoints).
# Tests that need to verify key behavior use override_settings().
WORKER_API_KEY = ""

# CEL evaluation timeout - increase for tests since heavy test loads can cause
# thread pool overhead that exceeds the default 100ms timeout
CEL_MAX_EVAL_TIMEOUT_MS = 500

# Disable DRF throttling in tests to prevent rate limit failures during test runs
# Tests run many rapid API calls which would trigger throttle limits.
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # type: ignore[name-defined]
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {}  # type: ignore[name-defined]

# CELERY
# ------------------------------------------------------------------------------
# Run Celery tasks synchronously in tests (no broker required).
# This ensures tests execute immediately without Redis dependency.
# For e2e tests that need to test the full dispatch flow, use the TestDispatcher.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True  # Propagate exceptions in eager mode
