from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils import timezone
from django.utils.translation import gettext as _

from simplevalidations.submissions.models import Submission
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.validations.constants import ValidationRunSource
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.workflows.models import Workflow

User = get_user_model()


class Command(BaseCommand):
    help = "Create 300 dummy ValidationRun instances for testing/demo purposes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            help=_(
                "Username or email of the user (defaults to current "
                "user or first available)",
            ),
        )
        parser.add_argument(
            "--count",
            type=int,
            default=300,
            help="Number of validation runs to create (default: 300)",
        )

    def handle(self, *args, **options):
        count = options["count"]
        user_ident = options.get("user")

        # Find the user
        user = None
        if user_ident:
            user = (
                User.objects.filter(username=user_ident).first()
                or User.objects.filter(email=user_ident).first()
            )

        if not user:
            user = User.objects.get(email="daniel@mcquilleninteractive.com")
            if not user:
                raise CommandError(f"User '{user_ident}' not found.")

        # Find the first workflow for this user
        workflow = Workflow.objects.filter(user=user).first()
        if not workflow:
            raise CommandError(f"No workflows found for user '{user.username}'.")

        self.stdout.write(
            f"Creating {count} validation runs for user '{user.username}' "
            f"using workflow '{workflow.name}'...",
        )

        created_runs = []
        tracking_service = TrackingEventService()
        now = timezone.now()

        # Status distribution (realistic mix)
        status_weights = [
            (ValidationRunStatus.SUCCEEDED, 60),  # 60% success
            (ValidationRunStatus.FAILED, 25),  # 25% failed
            (ValidationRunStatus.RUNNING, 8),  # 8% still running
            (ValidationRunStatus.PENDING, 5),  # 5% pending
            (ValidationRunStatus.FAILED, 2),  # 2% more failed (since no ERROR status)
        ]

        for i in range(count):
            # Pick random status based on weights
            status = random.choices(  # noqa: S311
                [s[0] for s in status_weights],
                weights=[s[1] for s in status_weights],
            )[0]

            # Random time in the past (last 30 days)
            days_ago = random.uniform(0, 30)  # noqa: S311
            started_at = now - timedelta(days=days_ago)

            # Duration varies by status
            if status == ValidationRunStatus.RUNNING:
                ended_at = None
                duration_ms = 0
            elif status == ValidationRunStatus.PENDING:
                started_at = None
                ended_at = None
                duration_ms = 0
            else:
                # Completed runs: 1 second to 5 minutes
                duration_seconds = random.uniform(1, 300)  # noqa: S311
                duration_ms = int(duration_seconds * 1000)
                ended_at = (
                    started_at + timedelta(seconds=duration_seconds)
                    if started_at
                    else None
                )

            # Create realistic summary data
            summary = self._create_realistic_summary(status, i)

            # Create dummy submission
            submission = Submission.objects.create(
                org=workflow.org,
                user=user,
                content=self._create_dummy_content(i),
                workflow=workflow,
            )

            # Create the ValidationRun
            run = ValidationRun.objects.create(
                workflow=workflow,
                org=workflow.org,
                user=user,
                submission=submission,
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                summary=summary,
                error=self._create_error_message(status)
                if status == ValidationRunStatus.FAILED
                else None,
                source=ValidationRunSource.LAUNCH_PAGE,
            )

            created_runs.append(run)
            tracking_service.log_validation_run_created(
                run=run,
                recorded_at=started_at or now,
                channel="web",
            )
            tracking_service.log_validation_run_status(
                run=run,
                status=run.status,
                recorded_at=ended_at or started_at or now,
            )

            # Progress indicator
            if (i + 1) % 50 == 0:
                self.stdout.write(f"Created {i + 1}/{count} runs...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully created {len(created_runs)} ValidationRun instances.",
            ),
        )

        # Show status distribution
        status_counts = {}
        for run in created_runs:
            status_counts[run.status.name] = status_counts.get(run.status.name, 0) + 1

        self.stdout.write("\nStatus distribution:")
        for status_name, count in sorted(status_counts.items()):
            self.stdout.write(f"  {status_name}: {count}")

    def _create_dummy_content(self, index: int) -> str:
        """Create realistic dummy JSON content for submissions."""
        products = []
        for i in range(random.randint(1, 3)):  # noqa: S311
            product = {
                "sku": f"PROD-{index:03d}-{i + 1:02d}",
                "name": f"Sample Product {index}-{i + 1}",
                "price": round(random.uniform(9.99, 299.99), 2),  # noqa: S311
                "rating": random.randint(1, 5),  # noqa: S311
            }
            products.append(product)

        return f'{{"products": {products}}}'.replace("'", '"')

    def _create_realistic_summary(
        self,
        status: ValidationRunStatus,
        index: int,
    ) -> dict[str, Any]:
        """Create realistic summary data based on status."""
        steps = []

        if status == ValidationRunStatus.PENDING:
            # No steps for pending runs
            pass
        elif status == ValidationRunStatus.RUNNING:
            # Some completed steps, one in progress
            steps.append(
                {
                    "step_id": 1,
                    "name": "JSON Schema Validation",
                    "status": "COMPLETED",
                    "issues": [],
                    "error": None,
                },
            )
            steps.append(
                {
                    "step_id": 2,
                    "name": "Business Rules Check",
                    "status": "RUNNING",
                    "issues": [],
                    "error": None,
                },
            )
        elif status == ValidationRunStatus.SUCCEEDED:
            # All steps completed successfully
            steps.extend(
                [
                    {
                        "step_id": 1,
                        "name": "JSON Schema Validation",
                        "status": "COMPLETED",
                        "issues": [],
                        "error": None,
                    },
                    {
                        "step_id": 2,
                        "name": "Business Rules Check",
                        "status": "COMPLETED",
                        "issues": [],
                        "error": None,
                    },
                ],
            )
        elif status == ValidationRunStatus.FAILED:
            # Steps with validation issues
            issue_count = random.randint(1, 5)  # noqa: S311
            issues = []
            for j in range(issue_count):
                item = {
                    "path": f"$.products[{j}]",
                    "message": random.choice(  # noqa: S311
                        [
                            "Field 'price' is required",
                            "Value exceeds maximum allowed",
                            "Invalid format for email address",
                            "Date must be in the future",
                            "String length exceeds limit",
                        ],
                    ),
                    "severity": "ERROR",
                    "code": f"VALIDATION_ERROR_{j + 1}",
                }
                issues.append(item)

            steps.extend(
                [
                    {
                        "step_id": 1,
                        "name": "JSON Schema Validation",
                        "status": "FAILED",
                        "issues": issues,
                        "error": None,
                    },
                    {
                        "step_id": 2,
                        "name": "Business Rules Check",
                        "status": "SKIPPED",
                        "issues": [],
                        "error": None,
                    },
                ],
            )
        elif status == ValidationRunStatus.ERROR:
            # System error during processing
            steps.append(
                {
                    "step_id": 1,
                    "name": "JSON Schema Validation",
                    "status": "ERROR",
                    "issues": [],
                    "error": "Failed to parse JSON schema",
                },
            )

        return {
            "total_steps": len(steps),
            "completed_steps": len([s for s in steps if s["status"] == "COMPLETED"]),
            "total_issues": sum(len(s.get("issues", [])) for s in steps),
            "steps": steps,
        }

    def _create_error_message(self, status: ValidationRunStatus) -> str | None:
        """Create realistic error messages for failed runs."""
        if status != ValidationRunStatus.FAILED:
            return None

        errors = [
            "Connection timeout while fetching external schema",
            "Invalid JSON payload: unexpected token at line 15",
            "Schema compilation failed: circular reference detected",
            "Out of memory error during validation",
            "External service unavailable (HTTP 503)",
            "Database connection lost during processing",
        ]
        return random.choice(errors)  # noqa: S311
