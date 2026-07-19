"""Probe the deployed attempt-scoped GCS capability against the real provider.

Operators run this command through ``just gcp validator-storage-capability-probe
<stage>`` after deploying the capability-aware Django and validator images. It
uses the configured stage bucket and trusted Django identity, emits no bearer
material, cleans its unique probe prefix, and exits non-zero unless every
allowed and forbidden operation matches the accepted storage contract.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.services.cloud_run.gcs_capability_probe import (
    probe_attempt_gcs_runtime_capability,
)


class Command(BaseCommand):
    """Run live GCS token-boundary checks using the deployed stage settings."""

    help = "Probe attempt-scoped GCS read/create and denial boundaries."

    def add_arguments(self, parser):
        """Add a stable JSON mode for automation and rollout recipes."""
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the versioned machine-readable probe report.",
        )

    def handle(self, *args, **options):
        """Validate configuration, run the provider probe, and fail closed."""
        if not getattr(
            settings,
            "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED",
            False,
        ):
            raise CommandError(
                "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED must be true"
            )

        bucket_name = str(getattr(settings, "GCS_VALIDATION_BUCKET", "") or "")
        project_id = str(getattr(settings, "GCP_PROJECT_ID", "") or "")
        if not bucket_name or not project_id:
            raise CommandError(
                "GCS_VALIDATION_BUCKET and GCP_PROJECT_ID must be configured"
            )

        report = probe_attempt_gcs_runtime_capability(
            bucket_name=bucket_name,
            project_id=project_id,
        )
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2))
        else:
            for check in report.checks:
                marker = "PASS" if check.passed else "FAIL"
                self.stdout.write(
                    f"[{marker}] {check.name}: expected={check.expected} "
                    f"observed={check.observed}"
                )

        if not report.passed:
            raise CommandError("Attempt-scoped GCS capability probe failed")
        if not options["json"]:
            self.stdout.write(
                self.style.SUCCESS("Attempt-scoped GCS capability probe passed.")
            )
