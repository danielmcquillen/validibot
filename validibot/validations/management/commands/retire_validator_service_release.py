"""Preflight or record cleanup of one immutable validator Service release.

Provider deletion is deliberately performed by the GCP operator recipe, not
inside Django. This command supplies the durable safety gate before deletion
and records retirement only after the recipe confirms provider removal.
"""

from __future__ import annotations

import json
import re

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    ensure_execution_deployment_can_retire,
)
from validibot.validations.services.execution.deployments import (
    retire_execution_deployment,
)

_RELEASE = re.compile(r"^v?([0-9]+\.[0-9]+\.[0-9]+)$")


class Command(BaseCommand):
    """Gate release cleanup on inactive routing, zero min, and drained work."""

    help = "Preflight or retire all Service deployments for one backend release."

    def add_arguments(self, parser):
        parser.add_argument("backend_release")
        parser.add_argument(
            "--provider-deleted",
            action="store_true",
            help="Record RETIRED after the provider resources were deleted.",
        )
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        match = _RELEASE.fullmatch(str(options["backend_release"]))
        if match is None:
            raise CommandError("backend_release must be X.Y.Z or vX.Y.Z.")
        release = match.group(1)
        deployments = list(
            ValidatorExecutionDeployment.objects.filter(
                deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
                backend_release_identity=release,
            )
            .select_related("validator")
            .order_by("validator__slug", "pk")
        )
        if not deployments:
            raise CommandError(
                f"No Cloud Run Service deployments found for release {release}."
            )
        try:
            for deployment in deployments:
                ensure_execution_deployment_can_retire(deployment)
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc

        if options["provider_deleted"]:
            deployments = [
                retire_execution_deployment(deployment) for deployment in deployments
            ]
        result = {
            "schema_version": "validibot.validator-service-retirement.v1",
            "backend_release": release,
            "deployment_count": len(deployments),
            "provider_deleted": bool(options["provider_deleted"]),
            "retired": bool(options["provider_deleted"]),
            "deployment_ids": [str(deployment.pk) for deployment in deployments],
        }
        if options["json"]:
            self.stdout.write(json.dumps(result, sort_keys=True))
        else:
            action = "Retired" if result["retired"] else "Preflight passed for"
            self.stdout.write(
                f"{action} {len(deployments)} Service deployment(s) from "
                f"release {release}."
            )
