"""
Provision test data for E2E stress tests.

Creates a test user, organization, workflow (with a JSON Schema step),
and API token. All operations are idempotent - safe to run repeatedly.

Usage::

    # Human-readable output
    python manage.py setup_fullstack_test_data

    # Shell-sourceable output (for just test-e2e)
    python manage.py setup_fullstack_test_data --export-env
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import RoleCode
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

TEST_USERNAME = "fullstack-test-user"
TEST_EMAIL = "fullstack-test@localhost"
TEST_ORG_NAME = "Fullstack Test Org"
TEST_ORG_SLUG = "fullstack-test-org"
TEST_WORKFLOW_NAME = "Fullstack Stress Test Workflow"
TEST_WORKFLOW_SLUG = "fullstack-stress-test"


class Command(BaseCommand):
    help = "Create test user, org, workflow, and API token for full-stack tests."

    def add_arguments(self, parser):
        parser.add_argument(
            "--export-env",
            action="store_true",
            help="Output shell-sourceable environment variable exports.",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            user = self._ensure_user()
            org = self._ensure_org(user)
            project = ensure_default_project(org)
            workflow = self._ensure_workflow(org, user, project)
            token = self._ensure_token(user)

        if options["export_env"]:
            self.stdout.write(
                f"FULLSTACK_ORG_SLUG={org.slug}\n"
                f"FULLSTACK_WORKFLOW_ID={workflow.pk}\n"
                f"FULLSTACK_API_TOKEN={token.key}\n"
            )
        else:
            self.stdout.write(self.style.SUCCESS("Full-stack test data ready."))
            self.stdout.write(f"  User:     {user.username}")
            self.stdout.write(f"  Org:      {org.slug}")
            self.stdout.write(f"  Workflow: {workflow.pk} ({workflow.name})")
            self.stdout.write(f"  Token:    {token.key[:8]}...")

    def _ensure_user(self) -> User:
        user, created = User.objects.get_or_create(
            username=TEST_USERNAME,
            defaults={
                "email": TEST_EMAIL,
                "name": "Fullstack Test User",
                "is_active": True,
            },
        )
        if created:
            user.set_password("test-password-not-for-production")
            user.save()
        return user

    def _ensure_org(self, user: User) -> Organization:
        org, created = Organization.objects.get_or_create(
            slug=TEST_ORG_SLUG,
            defaults={"name": TEST_ORG_NAME},
        )
        membership, mem_created = Membership.objects.get_or_create(
            user=user,
            org=org,
            defaults={"is_active": True},
        )
        if mem_created:
            membership.set_roles(
                {RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR},
            )
        if not user.current_org_id:
            user.set_current_org(org)
        return org

    def _ensure_workflow(self, org, user, project) -> Workflow:
        """Create a workflow with a JSON Schema validation step."""
        workflow, _ = Workflow.objects.get_or_create(
            org=org,
            slug=TEST_WORKFLOW_SLUG,
            version="1",
            defaults={
                "name": TEST_WORKFLOW_NAME,
                "user": user,
                "project": project,
                "is_active": True,
                "allowed_file_types": [SubmissionFileType.JSON],
            },
        )

        # Ensure it has at least one step
        if not workflow.steps.exists():
            validator = Validator.objects.filter(
                validation_type=ValidationType.JSON_SCHEMA,
            ).first()
            if not validator:
                self.stderr.write(
                    self.style.ERROR(
                        "No JSON Schema validator found. "
                        "Run 'manage.py setup_validibot' first.",
                    )
                )
                msg = "No JSON Schema validator available"
                raise SystemExit(msg)

            schema = self._load_test_schema()
            ruleset = Ruleset.objects.create(
                org=org,
                user=user,
                name="Fullstack Test Schema",
                ruleset_type=RulesetType.JSON_SCHEMA,
                rules_text=json.dumps(schema),
                metadata={
                    "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
                },
                version="1",
            )

            WorkflowStep.objects.create(
                workflow=workflow,
                validator=validator,
                ruleset=ruleset,
                order=10,
                name="JSON Schema Validation",
            )

        return workflow

    def _load_test_schema(self) -> dict:
        """Load the example product JSON Schema from test assets."""
        schema_path = (
            Path(settings.BASE_DIR)
            / "tests"
            / "assets"
            / "json"
            / "example_product_schema.json"
        )
        return json.loads(schema_path.read_text())

    def _ensure_token(self, user: User) -> Token:
        token, _ = Token.objects.get_or_create(user=user)
        return token
