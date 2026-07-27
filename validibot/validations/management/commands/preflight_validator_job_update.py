"""Block fixed-name validator Job updates until application attempts drain."""

from __future__ import annotations

import json
import re

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.services.execution.image_retention import (
    validator_job_update_blockers,
)

_JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class Command(BaseCommand):
    """Enforce the database drain half of a fixed Job image replacement."""

    help = "Preflight one fixed-name validator Cloud Run Job image update."

    def add_arguments(self, parser) -> None:
        parser.add_argument("job_name")
        parser.add_argument("current_digest")
        parser.add_argument("replacement_digest")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options) -> None:
        job_name = str(options["job_name"])
        current_digest = str(options["current_digest"])
        replacement_digest = str(options["replacement_digest"])
        if _JOB_NAME_RE.fullmatch(job_name) is None:
            raise CommandError("job_name is not a valid Cloud Run Job name.")
        for label, digest in (
            ("current_digest", current_digest),
            ("replacement_digest", replacement_digest),
        ):
            if _DIGEST_RE.fullmatch(digest) is None:
                raise CommandError(f"{label} must be a lowercase sha256 digest.")

        blockers = validator_job_update_blockers(job_name=job_name)
        payload = {
            "schema_version": "validibot.validator-job-update-preflight.v1",
            "job_name": job_name,
            "current_digest": current_digest,
            "replacement_digest": replacement_digest,
            "would_change": current_digest != replacement_digest,
            "drained": not blockers,
            "blockers": list(blockers),
        }
        if options["json"]:
            self.stdout.write(json.dumps(payload, sort_keys=True))
        if blockers:
            raise CommandError("; ".join(blockers))
        if not options["json"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Fixed Job {job_name} has no nonterminal application attempts."
                )
            )
