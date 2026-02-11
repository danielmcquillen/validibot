"""
Validate that GCP scheduler configuration matches the task registry.

This command checks that the scheduler-setup recipe in just/gcp/mod.just
has all the tasks defined in the registry and that schedules match.

Usage:
    python manage.py validate_scheduler_config

This helps catch configuration drift when tasks are added to the registry
but not to the GCP justfile.
"""

import re
from pathlib import Path

from django.core.management.base import BaseCommand

from validibot.core.tasks.registry import Backend
from validibot.core.tasks.registry import get_tasks_for_backend


class Command(BaseCommand):
    """Validate GCP scheduler config matches the task registry."""

    help = "Validate GCP scheduler jobs match the task registry"

    def handle(self, *args, **options):
        # Get GCP tasks from registry
        registry_tasks = get_tasks_for_backend(Backend.GCP)

        # Find and read the justfile
        # Path from: validibot/core/management/commands/ -> project root
        justfile_path = Path(__file__).parents[4] / "just" / "gcp" / "mod.just"

        if not justfile_path.exists():
            self.stderr.write(
                self.style.ERROR(f"Justfile not found at {justfile_path}")
            )
            return

        justfile_content = justfile_path.read_text()

        # Extract job definitions from justfile
        # Pattern matches: create_or_update_job \
        #     "job-name" \
        #     "schedule" \
        #     "endpoint" \
        job_pattern = re.compile(
            r"create_or_update_job\s+\\\s+"
            r'"([^"]+)\$\{JOB_SUFFIX\}"\s+\\\s+'
            r'"([^"]+)"\s+\\\s+'
            r'"([^"]+)"',
            re.MULTILINE,
        )

        justfile_jobs = {}
        for match in job_pattern.finditer(justfile_content):
            job_base = match.group(1)  # e.g., "validibot-clear-sessions"
            schedule = match.group(2)  # e.g., "0 2 * * *"
            endpoint = match.group(3)  # e.g., "/api/v1/scheduled/clear-sessions/"
            justfile_jobs[job_base] = {
                "schedule": schedule,
                "endpoint": endpoint,
            }

        # Compare
        errors = []
        warnings = []

        for task in registry_tasks:
            job_name = task.job_name
            if job_name not in justfile_jobs:
                errors.append(
                    f"Missing in justfile: {job_name} "
                    f"(schedule: {task.schedule_cron}, endpoint: {task.api_endpoint})"
                )
                continue

            jf_job = justfile_jobs[job_name]
            if jf_job["schedule"] != task.schedule_cron:
                errors.append(
                    f"Schedule mismatch for {job_name}: "
                    f"registry={task.schedule_cron}, justfile={jf_job['schedule']}"
                )
            if jf_job["endpoint"] != task.api_endpoint:
                errors.append(
                    f"Endpoint mismatch for {job_name}: "
                    f"registry={task.api_endpoint}, justfile={jf_job['endpoint']}"
                )

        # Check for jobs in justfile not in registry
        registry_job_names = {t.job_name for t in registry_tasks}
        for job_name in justfile_jobs:
            if job_name not in registry_job_names:
                warnings.append(f"Extra job in justfile not in registry: {job_name}")

        # Report results
        if errors:
            self.stdout.write(self.style.ERROR("\nConfiguration errors found:\n"))
            for error in errors:
                self.stdout.write(self.style.ERROR(f"  ✗ {error}"))
            self.stdout.write("")

        if warnings:
            self.stdout.write(self.style.WARNING("\nWarnings:\n"))
            for warning in warnings:
                self.stdout.write(self.style.WARNING(f"  ⚠ {warning}"))
            self.stdout.write("")

        if not errors and not warnings:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✓ All {len(registry_tasks)} GCP scheduler jobs "
                    "match the task registry\n"
                )
            )
        elif errors:
            self.stdout.write(
                self.style.ERROR(
                    "\nTo fix: Update just/gcp/mod.just scheduler-setup recipe "
                    "to match registry"
                )
            )
            raise SystemExit(1)
