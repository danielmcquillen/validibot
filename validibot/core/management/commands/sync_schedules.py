"""
Management command to synchronize scheduled task definitions.

This command reads from the task registry (validibot.core.tasks.registry)
and creates/updates the appropriate schedule entries for the current
deployment backend.

For Docker Compose (Celery Beat):
    Creates PeriodicTask records in django_celery_beat tables.

For GCP (Cloud Scheduler):
    Outputs JSON that can be consumed by deployment scripts,
    or can be used with --dry-run to show what would be created.

Usage:
    # Sync Celery Beat schedules (Docker Compose)
    python manage.py sync_schedules --backend=celery

    # Show what would be synced
    python manage.py sync_schedules --backend=celery --dry-run

    # Output GCP scheduler config as JSON
    python manage.py sync_schedules --backend=gcp --format=json

    # List all registered tasks
    python manage.py sync_schedules --list
"""

import json
import logging

from django.core.management.base import BaseCommand

from validibot.core.tasks.registry import SCHEDULED_TASKS
from validibot.core.tasks.registry import Backend
from validibot.core.tasks.registry import get_tasks_for_backend

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Synchronize scheduled task definitions with the execution backend."""

    help = "Sync scheduled tasks from registry to Celery Beat or Cloud Scheduler"

    def add_arguments(self, parser):
        parser.add_argument(
            "--backend",
            type=str,
            choices=["celery", "gcp", "aws"],
            help="Target backend to sync schedules for",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            dest="list_tasks",
            help="List all registered scheduled tasks",
        )
        parser.add_argument(
            "--format",
            type=str,
            choices=["text", "json"],
            default="text",
            help="Output format (default: text)",
        )

    def handle(self, *args, **options):
        if options["list_tasks"]:
            self._list_tasks(options)
            return

        backend = options.get("backend")
        if not backend:
            self.stderr.write(
                self.style.ERROR(
                    "Please specify --backend (celery, gcp, aws) or use --list"
                )
            )
            return

        if backend == "celery":
            self._sync_celery_beat(options)
        elif backend == "gcp":
            self._output_gcp_config(options)
        elif backend == "aws":
            self.stdout.write(
                self.style.WARNING("AWS EventBridge sync not yet implemented")
            )

    def _list_tasks(self, options):
        """List all registered scheduled tasks."""
        output_format = options["format"]

        if output_format == "json":
            tasks_data = [
                {
                    "id": task.id,
                    "name": task.name,
                    "celery_task": task.celery_task,
                    "api_endpoint": task.api_endpoint,
                    "schedule_cron": task.schedule_cron,
                    "schedule_interval_minutes": task.schedule_interval_minutes,
                    "description": task.description,
                    "enabled": task.enabled,
                    "backends": [b.value for b in task.backends],
                }
                for task in SCHEDULED_TASKS
            ]
            self.stdout.write(json.dumps(tasks_data, indent=2))
            return

        # Text output
        task_count = len(SCHEDULED_TASKS)
        self.stdout.write(
            self.style.SUCCESS(f"\nRegistered Scheduled Tasks ({task_count} total)\n")
        )
        self.stdout.write("=" * 80)

        for task in SCHEDULED_TASKS:
            status = "✓" if task.enabled else "✗"
            backends = ", ".join(b.value for b in task.backends)
            self.stdout.write(f"\n{status} {task.name} ({task.id})")
            self.stdout.write(f"  Schedule:    {task.schedule_cron}")
            self.stdout.write(f"  Celery:      {task.celery_task}")
            self.stdout.write(f"  API:         {task.api_endpoint}")
            self.stdout.write(f"  Backends:    {backends}")
            if task.description:
                self.stdout.write(f"  Description: {task.description}")

        self.stdout.write("\n" + "=" * 80)

    def _sync_celery_beat(self, options):
        """Sync schedules to Celery Beat (django_celery_beat)."""
        dry_run = options["dry_run"]

        try:
            from django_celery_beat.models import CrontabSchedule
            from django_celery_beat.models import IntervalSchedule
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            self.stderr.write(
                self.style.ERROR(
                    "django_celery_beat is not installed. "
                    "Install it with: pip install django-celery-beat"
                )
            )
            return

        tasks = get_tasks_for_backend(Backend.CELERY)
        self.stdout.write(
            self.style.SUCCESS(f"\nSyncing {len(tasks)} tasks to Celery Beat...")
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be made\n"))

        # Track schedules we create for reuse
        interval_cache: dict[int, IntervalSchedule] = {}
        crontab_cache: dict[str, CrontabSchedule] = {}

        created_count = 0
        updated_count = 0

        for task in tasks:
            self.stdout.write(f"\n  Processing: {task.name}")

            # Determine schedule type
            schedule = None
            schedule_type = None

            if task.schedule_interval_minutes:
                # Prefer interval schedule if specified
                minutes = task.schedule_interval_minutes
                if dry_run:
                    self.stdout.write(
                        f"    Would create/use interval: every {minutes} min"
                    )
                else:
                    if minutes not in interval_cache:
                        interval_cache[minutes], _ = (
                            IntervalSchedule.objects.get_or_create(
                                every=minutes,
                                period=IntervalSchedule.MINUTES,
                            )
                        )
                    schedule = interval_cache[minutes]
                    schedule_type = "interval"
            else:
                # Use crontab schedule
                cron_parts = self._parse_cron(task.schedule_cron)
                if dry_run:
                    cron = task.schedule_cron
                    self.stdout.write(f"    Would create/use crontab: {cron}")
                else:
                    cache_key = task.schedule_cron
                    if cache_key not in crontab_cache:
                        crontab_cache[cache_key], _ = (
                            CrontabSchedule.objects.get_or_create(
                                minute=cron_parts["minute"],
                                hour=cron_parts["hour"],
                                day_of_week=cron_parts["day_of_week"],
                                day_of_month=cron_parts["day_of_month"],
                                month_of_year=cron_parts["month_of_year"],
                            )
                        )
                    schedule = crontab_cache[cache_key]
                    schedule_type = "crontab"

            if dry_run:
                self.stdout.write(f"    Would create/update PeriodicTask: {task.name}")
                self.stdout.write(f"      task: {task.celery_task}")
                continue

            # Create or update the PeriodicTask
            # Must include schedule in defaults to pass django-celery-beat validation
            defaults = {
                "task": task.celery_task,
                "enabled": task.enabled,
            }
            if schedule_type == "interval":
                defaults["interval"] = schedule
            else:
                defaults["crontab"] = schedule

            periodic_task, created = PeriodicTask.objects.get_or_create(
                name=task.name,
                defaults=defaults,
            )

            # Update existing task if needed
            if not created:
                # Update schedule reference
                if schedule_type == "interval":
                    periodic_task.interval = schedule
                    periodic_task.crontab = None
                else:
                    periodic_task.crontab = schedule
                    periodic_task.interval = None

                periodic_task.task = task.celery_task
                periodic_task.enabled = task.enabled
                periodic_task.save()

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"    Created: {task.name}"))
            else:
                updated_count += 1
                self.stdout.write(f"    Updated: {task.name}")

        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"Would create/update {len(tasks)} periodic tasks")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done! Created: {created_count}, Updated: {updated_count}"
                )
            )

    def _output_gcp_config(self, options):
        """Output configuration for GCP Cloud Scheduler."""
        output_format = options["format"]
        tasks = get_tasks_for_backend(Backend.GCP)

        if output_format == "json":
            config = {
                "tasks": [
                    {
                        "job_name": task.job_name,
                        "schedule": task.schedule_cron,
                        "endpoint": task.api_endpoint,
                        "description": task.description,
                        "enabled": task.enabled,
                    }
                    for task in tasks
                ]
            }
            self.stdout.write(json.dumps(config, indent=2))
            return

        # Text output
        job_count = len(tasks)
        header = f"\nGCP Cloud Scheduler Configuration ({job_count} jobs)\n"
        self.stdout.write(self.style.SUCCESS(header))
        self.stdout.write("=" * 80)

        for task in tasks:
            self.stdout.write(f"\nJob: {task.job_name}")
            self.stdout.write(f"  Schedule: {task.schedule_cron}")
            self.stdout.write(f"  Endpoint: {task.api_endpoint}")
            self.stdout.write(f"  Description: {task.description}")
            self.stdout.write(f"  Enabled: {task.enabled}")

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(
            "\nTo create these jobs, use: just gcp scheduler-setup <stage>"
        )

    def _parse_cron(self, cron_expr: str) -> dict[str, str]:
        """
        Parse a cron expression into django_celery_beat CrontabSchedule fields.

        Args:
            cron_expr: Standard 5-field cron expression (minute hour dom month dow)

        Returns:
            Dict with minute, hour, day_of_week, day_of_month, month_of_year
        """
        cron_field_count = 5
        parts = cron_expr.split()
        if len(parts) != cron_field_count:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        return {
            "minute": parts[0],
            "hour": parts[1],
            "day_of_month": parts[2],
            "month_of_year": parts[3],
            "day_of_week": parts[4],
        }
