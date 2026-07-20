"""List safe validator deployment routing and readiness projections."""

import json

from django.core.management.base import BaseCommand

from validibot.validations.models import ValidatorExecutionDeployment


class Command(BaseCommand):
    """Expose the provider route inventory without credential material."""

    help = "List registered validator execution deployments and routing state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit stable JSON rather than a human-readable table.",
        )

    def handle(self, *args, **options):
        """Render every route in deterministic validator/revision order."""
        rows = []
        queryset = ValidatorExecutionDeployment.objects.select_related(
            "validator"
        ).order_by("validator__slug", "deployment_kind", "deployment_revision")
        for deployment in queryset:
            rows.append(
                {
                    "deployment_id": str(deployment.pk),
                    "validator": deployment.validator.slug,
                    "kind": deployment.deployment_kind,
                    "revision": deployment.deployment_revision,
                    "backend_release": deployment.backend_release_identity,
                    "resource": deployment.provider_resource_name,
                    "image_digest": deployment.backend_image_digest,
                    "readiness": deployment.readiness_state,
                    "routing_role": deployment.routing_role,
                    "blocked": deployment.emergency_blocked,
                    "last_verified_at": (
                        deployment.last_verified_at.isoformat()
                        if deployment.last_verified_at
                        else None
                    ),
                    "minimum_instances": deployment.minimum_instances,
                    "maximum_instances": deployment.maximum_instances,
                    "concurrency": deployment.concurrency,
                    "maximum_execution_seconds": (deployment.maximum_execution_seconds),
                    "request_timeout_seconds": deployment.request_timeout_seconds,
                    "dispatch_timeout_seconds": (deployment.dispatch_timeout_seconds),
                }
            )
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2, sort_keys=True))
            return
        if not rows:
            self.stdout.write("No validator execution deployments registered.")
            return
        for row in rows:
            block = " BLOCKED" if row["blocked"] else ""
            self.stdout.write(
                f"{row['validator']}: {row['kind']} {row['revision']} "
                f"[{row['readiness']}/{row['routing_role']}{block}] "
                f"{row['image_digest']}"
            )
