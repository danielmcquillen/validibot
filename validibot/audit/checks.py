"""Startup validation for archive-backend configuration.

Registered via ``AuditConfig.ready()``. Fires on every ``manage.py``
invocation (``manage.py check``, ``runserver``, ``migrate``, etc.)
so misconfiguration surfaces at deploy-time rather than in the
02:30 scheduled retention run.

Checks are scoped to the backend that's actually selected:
* ``NullArchiveBackend`` / ``FilesystemArchiveBackend`` — no checks
  beyond what the constructor already enforces.
* :class:`validibot.audit.backends.gcs.GCSArchiveBackend` — ensures
  ``AUDIT_ARCHIVE_GCS_BUCKET`` is set.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error
from django.core.checks import Tags
from django.core.checks import register

GCS_BACKEND_DOTTED = "validibot.audit.backends.gcs.GCSArchiveBackend"


@register(Tags.compatibility)
def check_gcs_archive_backend_config(app_configs, **kwargs) -> list[Error]:
    """Flag misconfigured :class:`GCSArchiveBackend` at startup.

    If the operator has selected the GCS backend but not provided a
    bucket name, the scheduled retention run would raise
    ``ValueError`` at 02:30 every night until someone notices.
    Turning that into a startup-time ``Error`` keeps operators
    honest. Fires for both hosted Validibot Cloud deployments and
    self-hosted Pro deployments on GCP that point the setting at
    the GCS backend.
    """

    backend = getattr(settings, "AUDIT_ARCHIVE_BACKEND", "")
    if backend != GCS_BACKEND_DOTTED:
        return []

    errors: list[Error] = []
    bucket = getattr(settings, "AUDIT_ARCHIVE_GCS_BUCKET", "")
    if not bucket:
        errors.append(
            Error(
                (
                    "AUDIT_ARCHIVE_BACKEND points at GCSArchiveBackend but "
                    "AUDIT_ARCHIVE_GCS_BUCKET is empty. Nightly retention "
                    "would fail at 02:30 every day."
                ),
                hint=(
                    "Set AUDIT_ARCHIVE_GCS_BUCKET to the GCS bucket name "
                    "(no ``gs://`` prefix). See "
                    "docs/dev_docs/how-to/audit-archive-gcs.md for bucket "
                    "provisioning."
                ),
                id="validibot.audit.E001",
            ),
        )
    return errors
