"""Emit database-held digest protections for GCP backend image cleanup."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.services.execution.image_retention import (
    build_backend_image_protection_plan,
)


class Command(BaseCommand):
    """Produce the fail-closed database half of the cleanup protection union."""

    help = "List validator backend image digests that cleanup must preserve."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--grace-days", type=int, default=7)
        parser.add_argument("--json", action="store_true")
        parser.add_argument(
            "--json-marker",
            action="store_true",
            help=(
                "Emit one marker-prefixed JSON line for private Cloud Run "
                "operator automation."
            ),
        )

    def handle(self, *args, **options) -> None:
        try:
            plan = build_backend_image_protection_plan(
                grace_days=options["grace_days"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "schema_version": "validibot.validator-backend-image-protection.v1",
            "generated_at": plan.generated_at.isoformat(),
            "grace_days": plan.grace_days,
            "protected": [
                {
                    "digest": item.digest,
                    "reasons": list(item.reasons),
                }
                for item in plan.protected
            ],
            "blockers": list(plan.blockers),
        }
        serialized_payload = json.dumps(payload, sort_keys=True)
        if options["json_marker"]:
            self.stdout.write(
                f"VALIDIBOT_BACKEND_IMAGE_PROTECTION_JSON={serialized_payload}"
            )
            # The private cleanup command consumes blockers from the payload.
            # Returning successfully ensures the marker remains recoverable
            # from Cloud Run logs even when the inventory must fail closed.
            return
        if options["json"]:
            self.stdout.write(serialized_payload)
        else:
            self.stdout.write(
                f"Protected backend digests ({len(plan.protected)}; "
                f"grace={plan.grace_days}d):"
            )
            for item in plan.protected:
                self.stdout.write(f"  {item.digest}: {', '.join(item.reasons)}")
            for blocker in plan.blockers:
                self.stderr.write(self.style.ERROR(f"BLOCKER: {blocker}"))
        if plan.blockers:
            raise CommandError("Backend image protection inventory is incomplete.")
