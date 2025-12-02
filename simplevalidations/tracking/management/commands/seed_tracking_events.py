from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from simplevalidations.projects.models import Project
from simplevalidations.tracking.sample_data import seed_sample_tracking_data
from simplevalidations.users.models import Organization
from simplevalidations.users.models import User
from simplevalidations.workflows.models import Workflow


class Command(BaseCommand):
    help = "Seed sample tracking events to make the dashboard charts meaningful."

    def add_arguments(self, parser):
        parser.add_argument(
            "--org-slug",
            required=True,
            help="Organization slug to seed events for.",
        )
        parser.add_argument(
            "--project-slug",
            help="Optional project slug. Defaults to the first project under the organization or none.",
        )
        parser.add_argument(
            "--workflow-slug",
            help="Optional workflow slug. Defaults to an existing workflow or a generated sample.",
        )
        parser.add_argument(
            "--user-email",
            help="Optional user email to associate with the events.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Number of days of history to generate (default: 7).",
        )
        parser.add_argument(
            "--runs-per-day",
            type=int,
            default=4,
            help="Number of run sequences per seeded day (default: 4).",
        )
        parser.add_argument(
            "--logins-per-day",
            type=int,
            default=2,
            help="Number of login events per seeded day (default: 2).",
        )
        parser.add_argument(
            "--no-failures",
            action="store_true",
            help="Disable synthetic failed runs.",
        )

    def handle(self, *args, **options):
        org_slug = options["org_slug"]
        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization with slug '{org_slug}' not found.") from exc

        project = self._resolve_project(org, options.get("project_slug"))
        user = self._resolve_user(org, options.get("user_email"))
        workflow = self._resolve_workflow(org, user, options.get("workflow_slug"))

        include_failures = not options["no_failures"]

        events = seed_sample_tracking_data(
            org=org,
            project=project,
            workflow=workflow,
            user=user,
            days=options["days"],
            runs_per_day=options["runs_per_day"],
            logins_per_day=options["logins_per_day"],
            include_failures=include_failures,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(events)} tracking events for org '{org.slug}'.",
            ),
        )
        if user is None:
            self.stdout.write(
                self.style.WARNING(
                    "No user was associated with the events; user charts may remain empty.",
                ),
            )

    def _resolve_project(self, org: Organization, slug: str | None) -> Project | None:
        if slug:
            try:
                return Project.objects.get(org=org, slug=slug)
            except Project.DoesNotExist:
                self.stdout.write(
                    self.style.WARNING(f"Project '{slug}' not found; continuing without project."),
                )
                return None
        return org.projects.order_by("name").first()

    def _resolve_user(self, org: Organization, email: str | None) -> User | None:
        if email:
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist as exc:
                raise CommandError(f"User with email '{email}' not found.") from exc
            if org not in user.orgs.all():
                self.stdout.write(
                    self.style.WARNING(
                        f"User '{email}' is not a member of org '{org.slug}'. Events will be recorded without a user.",
                    ),
                )
                return None
            return user
        return org.users.order_by("id").first()

    def _resolve_workflow(
        self,
        org: Organization,
        user: User | None,
        slug: str | None,
    ) -> Workflow | None:
        if slug:
            try:
                return Workflow.objects.get(org=org, slug=slug)
            except Workflow.DoesNotExist:
                self.stdout.write(
                    self.style.WARNING(f"Workflow '{slug}' not found; generating a sample workflow."),
                )

        workflow = org.workflows.order_by("name").first()
        if workflow:
            return workflow
        if not user:
            self.stdout.write(
                self.style.WARNING(
                    "Cannot create a sample workflow without a user. Run seeding will omit workflow context.",
                ),
            )
            return None

        suffix = uuid.uuid4().hex[:8]
        workflow = Workflow.objects.create(
            org=org,
            user=user,
            name=f"Dashboard Sample Workflow {suffix}",
            slug=f"dashboard-sample-{suffix}",
            version="1.0",
        )
        self.stdout.write(f"Created workflow '{workflow.slug}' for seeding.")
        return workflow
