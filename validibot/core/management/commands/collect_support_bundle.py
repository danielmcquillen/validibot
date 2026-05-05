"""Capture the Django-side portion of a support bundle.

Run by ``just self-hosted collect-support-bundle`` and ``just gcp
collect-support-bundle <stage>`` (the latter as a Cloud Run Job).
The output is a single JSON document conforming to
``validibot.support-bundle.v1`` (see :mod:`validibot.core.support_bundle`),
written to stdout or to a file path the recipe specifies.

The recipe combines this output with host-side artefacts (Compose
state, container logs, disk usage, validator image inventory) into
a final zip. Splitting the work this way keeps the redaction logic
in Python — where it can introspect Django's settings module —
while leaving the substrate-specific bits (gcloud vs docker compose)
to the recipe layer.

Why a single JSON document, not a directory of files
====================================================

The ADR (section 11) describes the bundle as containing several
named files (``doctor.json``, ``versions.txt``, etc.). The
*self-hosted recipe* assembles those filenames at zip time. This
command produces ONE JSON document containing all the app-side
data; the recipe then splits the relevant pieces into the named
files in the zip.

This split-at-recipe-time approach is what makes the
content-redaction unit-testable: a single JSON has a single Pydantic
schema, and we can assert "no sensitive setting value appears
anywhere in the serialized output" with a tight test loop.

Output format
=============

By default, JSON is written to stdout. ``--output <path>`` writes
to a file. ``--indent`` toggles pretty-printing (default: indented
for human readability; the recipe can pass ``--no-indent`` to keep
the file slimmer in the final zip).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC
from datetime import datetime
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from validibot.core.deployment import get_validibot_runtime_version
from validibot.core.support_bundle import SUPPORT_BUNDLE_SCHEMA_VERSION
from validibot.core.support_bundle import MigrationState
from validibot.core.support_bundle import OutboundCallStatus
from validibot.core.support_bundle import RedactedSetting
from validibot.core.support_bundle import SupportBundleAppSnapshot
from validibot.core.support_bundle import ValidatorBackendSummary
from validibot.core.support_bundle import VersionInfo
from validibot.core.support_bundle import is_sensitive_setting
from validibot.core.support_bundle import looks_like_secret_value
from validibot.core.support_bundle import redact_setting_value

# Setting names we explicitly capture in the redacted-settings list. The
# allowlist approach (vs ``dir(settings)``) keeps the bundle output
# stable across Django version bumps, and prevents accidentally
# surfacing settings that future Django adds with secret-shaped names
# we haven't seen.
#
# Names that match a sensitive fragment will be redacted; names that
# don't, surface verbatim. The intent is "settings support typically
# asks about during diagnosis" — DEBUG, ALLOWED_HOSTS, storage paths,
# email backend, validator-trust policy, etc.
CAPTURED_SETTING_NAMES: tuple[str, ...] = (
    # Core deployment shape
    "DEBUG",
    "DEPLOYMENT_TARGET",
    "VALIDIBOT_VERSION",
    "ALLOWED_HOSTS",
    "SITE_URL",
    "TIME_ZONE",
    "LANGUAGE_CODE",
    # Storage
    "DATA_STORAGE_ROOT",
    "DEFAULT_FILE_STORAGE",
    "STORAGES",
    "MEDIA_ROOT",
    "STATIC_ROOT",
    # Database (POSTGRES_* env vars surface here, redacted by name)
    "DATABASES",
    # Cache & queue
    "CACHES",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    # Email
    "EMAIL_BACKEND",
    "EMAIL_HOST",
    "EMAIL_PORT",
    "EMAIL_USE_TLS",
    "DEFAULT_FROM_EMAIL",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",  # name-redacted
    # Security
    "SECRET_KEY",  # name-redacted
    "CSRF_TRUSTED_ORIGINS",
    "SECURE_SSL_REDIRECT",
    "SECURE_PROXY_SSL_HEADER",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    # MFA
    "MFA_SUPPORTED_TYPES",
    "MFA_ENCRYPTION_KEY",  # name-redacted
    # Validators
    "VALIDATOR_BACKEND_IMAGE_POLICY",
    "VALIDATOR_RUNNER",
    "ADVANCED_VALIDATOR_IMAGES",
    # Pro / OIDC / MCP
    "INSTALLED_APPS",
    "IDP_OIDC_MCP_RESOURCE_AUDIENCE",
    "ENABLE_API",
    # Telemetry
    "SENTRY_DSN",  # name-redacted (DSN fragment)
)


class Command(BaseCommand):
    """Capture the app-side support-bundle snapshot.

    Operators normally invoke this via ``just self-hosted
    collect-support-bundle`` or ``just gcp collect-support-bundle
    <stage>``; both shell into this command.
    """

    help = (
        "Capture the Django-side portion of a support bundle as JSON "
        "(``validibot.support-bundle.v1``). The recipe combines this "
        "output with host-side artefacts into a final zip."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="-",
            help=(
                "Where to write the JSON. ``-`` (default) writes to stdout; "
                "a filesystem path writes to disk."
            ),
        )
        parser.add_argument(
            "--no-indent",
            action="store_true",
            help=(
                "Emit minified JSON instead of human-readable indented form. "
                "Use when the bundle's zip size matters."
            ),
        )

    def handle(self, *args, **options):
        snapshot = self._build_snapshot()
        indent = None if options["no_indent"] else 2
        as_json = snapshot.model_dump_json(indent=indent)

        output = options["output"]
        if output == "-":
            self.stdout.write(as_json)
        else:
            Path(output).write_text(as_json + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Snapshot written to {output}"))

    # ── Snapshot assembly ──────────────────────────────────────────────

    def _build_snapshot(self) -> SupportBundleAppSnapshot:
        """Assemble the snapshot from live Django state.

        Each helper builds one section. We capture them in deterministic
        order so two runs against the same deployment produce
        byte-identical bundles (modulo timestamps) — useful for
        diffing across two snapshots when troubleshooting "what
        changed?" between two attempts.
        """
        return SupportBundleAppSnapshot(
            schema_version=SUPPORT_BUNDLE_SCHEMA_VERSION,
            captured_at=datetime.now(UTC).isoformat(),
            versions=self._capture_versions(),
            migrations=self._capture_migrations(),
            settings=self._capture_settings(),
            outbound_calls=self._capture_outbound_calls(),
            validators=self._capture_validators(),
            doctor=self._capture_doctor(),
        )

    def _capture_versions(self) -> VersionInfo:
        """Snapshot Validibot, Python, and Postgres versions."""
        py = (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
        # ``SHOW server_version`` returns the human-readable string;
        # parsing it into semver-shape is left to support tooling.
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            row = cursor.fetchone()
            postgres_version = row[0] if row else "unknown"

        return VersionInfo(
            validibot_version=get_validibot_runtime_version(),
            python_version=py,
            postgres_server_version=postgres_version,
            target=getattr(settings, "DEPLOYMENT_TARGET", None) or "unknown",
            stage=getattr(settings, "VALIDIBOT_STAGE", None),
        )

    def _capture_migrations(self) -> MigrationState:
        """Snapshot the live migration head per app.

        Same logic as ``write_backup_manifest`` — kept inline rather
        than factored to a shared helper because the two commands have
        different lifecycles, and a shared helper tends to grow
        features in the wrong direction.
        """
        recorder = MigrationRecorder(connection)
        applied = recorder.applied_migrations()
        head: dict[str, str] = {}
        for app_label, migration_name in applied:
            current = head.get(app_label, "")
            # Migration names start with a 4-digit prefix, so string
            # compare matches numeric ordering.
            if migration_name > current:
                head[app_label] = migration_name
        return MigrationState(head=head)

    def _capture_settings(self) -> list[RedactedSetting]:
        """Snapshot the captured settings, redacting sensitive values.

        Iterates the static ``CAPTURED_SETTING_NAMES`` allowlist
        rather than ``dir(settings)`` so two factors are stable:
        (1) the bundle's contents don't drift across Django version
        bumps, and (2) we don't surface settings whose existence
        hasn't been audited.
        """
        captured: list[RedactedSetting] = []
        for name in CAPTURED_SETTING_NAMES:
            if not hasattr(settings, name):
                continue
            raw_value = getattr(settings, name)
            redacted_value = redact_setting_value(name, raw_value)
            was_redacted = is_sensitive_setting(name) or looks_like_secret_value(
                raw_value if isinstance(raw_value, str) else "",
            )
            captured.append(
                RedactedSetting(
                    name=name,
                    value=self._jsonable(redacted_value),
                    redacted=was_redacted,
                ),
            )
        return captured

    def _capture_outbound_calls(self) -> OutboundCallStatus:
        """Snapshot which outbound-call channels are enabled.

        Mirrors the ADR section 10 list. We don't try to detect
        every possible outbound; we report the four operators
        commonly ask about.
        """
        return OutboundCallStatus(
            sentry_enabled=bool(getattr(settings, "SENTRY_DSN", None)),
            posthog_enabled=bool(getattr(settings, "POSTHOG_API_KEY", None)),
            email_configured=(
                getattr(settings, "EMAIL_BACKEND", "")
                != "django.core.mail.backends.console.EmailBackend"
            ),
            # Self-hosted Pro never phones home for license verification at
            # runtime per the ADR; the package-index credential is the
            # entitlement gate. We surface the (always False on community)
            # status anyway because operators sometimes ask.
            runtime_license_check_enabled=False,
        )

    def _capture_validators(self) -> list[ValidatorBackendSummary]:
        """Snapshot the ``Validator`` rows known to the deployment.

        The model exists in community for built-ins and Pro extends
        it for advanced validators. We only surface name + slug +
        validation_type — image references are host-side concerns
        (the recipe captures those via ``docker image inspect``).
        """
        try:
            from validibot.validations.models import Validator
        except ImportError:
            return []

        summaries: list[ValidatorBackendSummary] = []
        # ``order_by`` keeps output deterministic across runs.
        for validator in Validator.objects.filter(is_system=True).order_by("slug"):
            summaries.append(
                ValidatorBackendSummary(
                    slug=validator.slug,
                    validation_type=str(validator.validation_type),
                    is_system=validator.is_system,
                    image=getattr(validator, "image", None) or None,
                ),
            )
        return summaries

    def _capture_doctor(self) -> dict | None:
        """Run doctor in --json mode and embed the result.

        Doctor failure is non-fatal for the support bundle — operators
        running collect-support-bundle are usually trying to diagnose
        something that's already broken, so a non-zero doctor exit is
        the very signal we need to capture. We catch SystemExit to
        keep the broken-deployment case surfaced in the bundle.
        """
        out = StringIO()
        try:
            call_command("check_validibot", "--json", stdout=out)
        except SystemExit:
            # Doctor exits non-zero on errors/warnings. The JSON it
            # already wrote captures what failed.
            pass
        except Exception:
            # If doctor itself crashed (the worst case), record the
            # absence rather than crashing this command. Support staff
            # will see the missing field and ask follow-up questions.
            return None

        raw = out.getvalue().strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _jsonable(value: object) -> object:
        """Coerce a Django settings value into a JSON-serializable form.

        Most settings are already JSON-friendly. The exceptions are:

        - ``Path`` objects (from ``BASE_DIR / 'foo'``): convert to str.
        - Dict-of-stuff settings (``DATABASES``, ``STORAGES``,
          ``CACHES``): recursively redact embedded credentials.
        - Tuples (``ALLOWED_HOSTS`` historically): convert to list.

        For anything else, we fall back to ``str()``. The redaction
        sentinel itself is a plain string so it round-trips without
        special handling.
        """
        if isinstance(value, dict):
            return {
                k: redact_setting_value(k, Command._jsonable(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [Command._jsonable(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
