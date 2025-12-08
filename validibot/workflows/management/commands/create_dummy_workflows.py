from __future__ import annotations

import random
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from faker import Faker

from validibot.projects.models import Project
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowPublicInfo
from validibot.workflows.models import WorkflowStep

User = get_user_model()


class Command(BaseCommand):
    """
    Populate the database with dummy workflows for demos or local development.

    Example:
        python manage.py create_dummy_workflows --count 25
    """

    help = _("Create dummy workflows with random data for demos.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=10,
            help=_("Number of workflows to create (default: 10)."),
        )

    def handle(self, *args, **options):
        count = options["count"]
        if count is None:
            count = 10
        if count <= 0:
            raise CommandError("Count must be a positive integer.")

        self.faker = Faker()
        validator_pool = self._ensure_validator_pool()

        self.stdout.write(
            self.style.NOTICE(f"Creating {count} dummy workflow(s)..."),
        )

        created_workflows: list[Workflow] = []
        for index in range(1, count + 1):
            workflow = self._create_workflow(index=index, validators=validator_pool)
            created_workflows.append(workflow)
            self.stdout.write(f"  [{index}/{count}] {workflow.name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {len(created_workflows)} workflow(s).",
            ),
        )

    def _ensure_validator_pool(self) -> list[Validator]:
        validators = list(Validator.objects.all())
        if validators:
            return validators

        self.stdout.write(
            self.style.WARNING(
                "No validators found; creating a JSON Schema validator."
            ),
        )
        demo_validator = Validator.objects.create(
            name="Demo JSON Schema Validator",
            validation_type=ValidationType.JSON_SCHEMA,
            version="2020-12",
            slug=f"demo-json-{self._random_suffix()}",
            description="Auto-generated validator used for dummy workflows.",
        )
        return [demo_validator]

    def _create_workflow(self, *, index: int, validators: list[Validator]) -> Workflow:
        with transaction.atomic():
            org, user, project = self._create_org_with_user()
            workflow_name = (
                f"{self.faker.catch_phrase()} Workflow {self._random_suffix(4)}"
            )
            workflow_slug = self._build_slug(workflow_name)
            workflow = Workflow.objects.create(
                org=org,
                user=user,
                project=project,
                name=workflow_name,
                slug=workflow_slug,
                version="1",
                is_active=True,
                is_locked=False,
                make_info_public=True,
            )

            self._create_public_info(workflow=workflow)
            self._create_steps(workflow=workflow, validators=validators)

        return workflow

    def _create_org_with_user(self) -> tuple[Organization, User, Project]:
        org_name = self.faker.unique.company()
        org_slug = self._build_slug(f"{org_name}-{self._random_suffix(6)}")
        org = Organization.objects.create(name=org_name, slug=org_slug)

        email = self.faker.unique.email()
        username = slugify(f"{email.split('@')[0]}-{self._random_suffix(4)}")[:150]
        user = User.objects.create_user(
            username=username,
            email=email,
            password="passw0rd!",  # noqa: S106
            name=self.faker.name(),
        )

        membership = Membership.objects.create(user=user, org=org)
        for role_code in (RoleCode.OWNER, RoleCode.ADMIN, RoleCode.EXECUTOR):
            membership.add_role(role_code)

        user.current_org = org
        user.save(update_fields=["current_org"])

        project_name = f"{self.faker.bs().title()} Project"
        project = Project.objects.create(
            org=org,
            name=project_name,
            description=self.faker.sentence(),
            slug=self._build_slug(f"{project_name}-{self._random_suffix(4)}"),
            is_default=False,
        )

        return org, user, project

    def _create_public_info(self, *, workflow: Workflow) -> WorkflowPublicInfo:
        content_sections = [
            self.faker.paragraph(nb_sentences=3),
            self.faker.paragraph(nb_sentences=4),
            "### Highlights",
            "\n".join(f"- {self.faker.sentence(nb_words=10)}" for _ in range(3)),
        ]

        public_info = WorkflowPublicInfo.objects.create(
            workflow=workflow,
            title=f"{workflow.name} Overview",
            content_md="\n\n".join(content_sections),
            show_steps=True,
        )

        return public_info

    def _create_steps(self, *, workflow: Workflow, validators: list[Validator]) -> None:
        step_count = random.randint(2, 4)  # noqa: S311
        for idx in range(step_count):
            validator = random.choice(validators)  # noqa: S311
            WorkflowStep.objects.create(
                workflow=workflow,
                order=(idx + 1) * 10,
                name=self.faker.bs().title(),
                description=self.faker.sentence(nb_words=12),
                validator=validator,
                display_schema=False,
                config={
                    "severity": random.choice(["info", "warning", "error"]),  # noqa: S311
                    "sample_config": self.faker.word(),
                },
            )

    def _random_suffix(self, length: int = 8) -> str:
        return uuid4().hex[:length]

    def _build_slug(self, value: str, *, max_length: int = 50) -> str:
        base = slugify(value)
        if not base:
            base = self._random_suffix(length=min(max_length, 8))
        if len(base) <= max_length:
            return base
        trimmed = base[: max_length - 5].rstrip("-")
        return f"{trimmed}-{self._random_suffix(4)}"
