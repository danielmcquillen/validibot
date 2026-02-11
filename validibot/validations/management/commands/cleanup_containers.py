"""
Management command to clean up orphaned Docker containers.

This command removes Validibot-managed containers that have exceeded their
timeout. It's useful for:
- Cleaning up after worker crashes
- Periodic maintenance
- Manual cleanup when needed

Usage:
    python manage.py cleanup_containers [--all] [--grace-period SECONDS]

Options:
    --all: Remove ALL managed containers, not just orphaned ones
    --grace-period: Extra seconds beyond timeout before cleanup (default: 300)
    --dry-run: Show what would be cleaned up without removing anything
"""

from datetime import UTC

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Clean up orphaned Docker containers spawned by Validibot."""

    help = (
        "Clean up Validibot-managed Docker containers. "
        "By default, only removes orphaned containers that have exceeded "
        "their timeout plus grace period."
    )

    def add_arguments(self, parser):
        """Add command-line arguments."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="Remove ALL managed containers, not just orphaned ones",
        )
        parser.add_argument(
            "--grace-period",
            type=int,
            default=300,
            help="Extra seconds beyond timeout before cleanup (default: 300)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be cleaned up without removing anything",
        )

    def handle(self, *args, **options):
        """Execute the cleanup."""
        try:
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )
        except ImportError:
            self.stderr.write(
                self.style.ERROR(
                    "Docker package not installed. Install with: pip install docker"
                )
            )
            return

        runner = DockerValidatorRunner()

        # Check Docker availability
        if not runner.is_available():
            self.stderr.write(
                self.style.ERROR(
                    "Docker is not available. Ensure Docker is running and accessible."
                )
            )
            return

        if options["dry_run"]:
            self._dry_run(runner, options)
        elif options["all"]:
            self._cleanup_all(runner)
        else:
            self._cleanup_orphaned(runner, options["grace_period"])

    def _dry_run(self, runner, options):
        """Show what would be cleaned up without removing anything."""
        from datetime import datetime

        from validibot.validations.services.runners.docker import LABEL_RUN_ID
        from validibot.validations.services.runners.docker import LABEL_STARTED_AT
        from validibot.validations.services.runners.docker import LABEL_TIMEOUT_SECONDS
        from validibot.validations.services.runners.docker import LABEL_VALIDATOR

        containers = runner.list_managed_containers()

        if not containers:
            self.stdout.write("No Validibot-managed containers found.")
            return

        now = datetime.now(UTC)
        grace_period = options["grace_period"]
        cleanup_all = options["all"]

        self.stdout.write(f"Found {len(containers)} Validibot-managed container(s):\n")

        for container in containers:
            labels = container.labels
            run_id = labels.get(LABEL_RUN_ID, "unknown")
            validator = labels.get(LABEL_VALIDATOR, "unknown")
            started_at_str = labels.get(LABEL_STARTED_AT, "unknown")
            timeout_str = labels.get(LABEL_TIMEOUT_SECONDS, "3600")

            # Calculate age
            try:
                started_at = datetime.fromisoformat(started_at_str)
                age_seconds = (now - started_at).total_seconds()
                timeout = int(timeout_str)
                max_age = timeout + grace_period
                is_orphaned = age_seconds > max_age
            except (ValueError, TypeError):
                age_seconds = 0
                is_orphaned = False

            status = container.status
            would_remove = cleanup_all or is_orphaned

            self.stdout.write(
                f"  - {container.short_id}: "
                f"run_id={run_id}, "
                f"validator={validator}, "
                f"status={status}, "
                f"age={age_seconds:.0f}s"
            )
            if would_remove:
                self.stdout.write(self.style.WARNING("    ^ Would be removed"))

    def _cleanup_all(self, runner):
        """Remove all managed containers."""
        self.stdout.write("Removing ALL Validibot-managed containers...")
        removed, failed = runner.cleanup_all_managed_containers()

        if removed > 0:
            self.stdout.write(self.style.SUCCESS(f"Removed {removed} container(s)."))
        if failed > 0:
            self.stdout.write(
                self.style.ERROR(f"Failed to remove {failed} container(s).")
            )
        if removed == 0 and failed == 0:
            self.stdout.write("No containers to remove.")

    def _cleanup_orphaned(self, runner, grace_period: int):
        """Remove only orphaned containers."""
        self.stdout.write(
            f"Cleaning up orphaned containers (grace period: {grace_period}s)..."
        )
        removed, failed = runner.cleanup_orphaned_containers(grace_period)

        if removed > 0:
            self.stdout.write(
                self.style.SUCCESS(f"Removed {removed} orphaned container(s).")
            )
        if failed > 0:
            self.stdout.write(
                self.style.ERROR(f"Failed to remove {failed} container(s).")
            )
        if removed == 0 and failed == 0:
            self.stdout.write("No orphaned containers found.")
