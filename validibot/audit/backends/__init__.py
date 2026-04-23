"""Concrete archive backends that can be selected via settings.

Two reference backends ship in ``validibot/audit/archive.py`` itself
(``NullArchiveBackend``, ``FilesystemArchiveBackend``) because they
have no third-party dependencies and belong in every deployment.
This sub-package holds backends that pull in optional client
libraries (e.g. ``google-cloud-storage`` for
:class:`~validibot.audit.backends.gcs.GCSArchiveBackend`) so
operators who don't need them don't have to import them.

Any deployment can point ``AUDIT_ARCHIVE_BACKEND`` at one of these
dotted paths — they're community code, available on every tier.
Cloud deployments default to the GCS backend; self-hosted Pro on
GCP / AWS operators configure it themselves.
"""
