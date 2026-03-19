"""
Provision E2E test workflows for advanced validator testing.

Creates complete, ready-to-run workflows for each advanced validator
scenario.  Each scenario includes the workflow, step configuration,
template/resource files, and output assertions.

This command is designed to be extended: adding a new E2E scenario means
adding a new ``_ensure_*_workflow()`` method and registering it in
``handle()``.

The command reuses the same user, organization, and API token created by
``setup_fullstack_test_data``.  It is idempotent — safe to run repeatedly.

Usage::

    # Human-readable output
    python manage.py setup_e2e_workflows

    # Shell-sourceable output (for just test-e2e-energyplus)
    python manage.py setup_e2e_workflows --export-env
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import RoleCode
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorResourceFile
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource

logger = logging.getLogger(__name__)

# Reuse the same user/org as setup_fullstack_test_data for a shared
# authentication context across all E2E tests.
TEST_USERNAME = "fullstack-test-user"
TEST_EMAIL = "fullstack-test@localhost"
TEST_ORG_NAME = "Fullstack Test Org"
TEST_ORG_SLUG = "fullstack-test-org"

# EnergyPlus template scenario
EP_WORKFLOW_NAME = "E2E EnergyPlus Template Test"
EP_WORKFLOW_SLUG = "e2e-energyplus-template"


class Command(BaseCommand):
    """Provision E2E test workflows for advanced validators."""

    help = (
        "Create complete test workflows for E2E testing of advanced validators. "
        "Includes EnergyPlus parameterized template workflow with output assertions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--export-env",
            action="store_true",
            help="Output shell-sourceable environment variable exports.",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            user, org, token = self._ensure_user_and_org()
            project = ensure_default_project(org)

            # Registry of E2E workflow scenarios.  Each key becomes an
            # environment variable: E2E_{KEY}_WORKFLOW_ID
            workflows = {}

            # --- EnergyPlus parameterized template ---
            ep_workflow = self._ensure_energyplus_template_workflow(
                org,
                user,
                project,
            )
            if ep_workflow:
                workflows["ENERGYPLUS_TEMPLATE"] = ep_workflow

            # Future scenarios:
            # workflows["FMU_BASIC"] = self._ensure_fmu_workflow(org, user, project)

        if options["export_env"]:
            # Also emit the shared auth env vars so callers don't need
            # to run setup_fullstack_test_data separately.
            self.stdout.write(f"FULLSTACK_ORG_SLUG={org.slug}")
            self.stdout.write(f"FULLSTACK_API_TOKEN={token.key}")
            for key, wf in workflows.items():
                self.stdout.write(f"E2E_{key}_WORKFLOW_ID={wf.pk}")
        else:
            self.stdout.write(self.style.SUCCESS("E2E workflow data ready."))
            self.stdout.write(f"  User:  {user.username}")
            self.stdout.write(f"  Org:   {org.slug}")
            self.stdout.write(f"  Token: {token.key[:8]}...")
            for key, wf in workflows.items():
                self.stdout.write(f"  {key}: {wf.pk} ({wf.name})")

    # ------------------------------------------------------------------
    # Shared user/org provisioning
    # ------------------------------------------------------------------

    def _ensure_user_and_org(self) -> tuple[User, Organization, Token]:
        """Create or retrieve the shared E2E test user, org, and token.

        Mirrors the logic in ``setup_fullstack_test_data`` so both
        commands share the same authentication context.
        """
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

        org, _ = Organization.objects.get_or_create(
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

        token, _ = Token.objects.get_or_create(user=user)
        return user, org, token

    # ------------------------------------------------------------------
    # EnergyPlus parameterized template scenario
    # ------------------------------------------------------------------
    # This reproduces the exact workflow from the blog post:
    #   "Validating With EnergyPlus" — Window Glazing Analysis
    #
    # - Template: window_glazing_template.idf (3 variables)
    # - Input bounds: U_FACTOR 0.1-7.0, SHGC 0.01-0.99, VT 0.01-0.99
    # - Output assertions:
    #   1. window_heat_loss_kwh < 800
    #   2. cooling_energy_kwh < heating_energy_kwh
    # ------------------------------------------------------------------

    def _ensure_energyplus_template_workflow(
        self,
        org: Organization,
        user: User,
        project,
    ) -> Workflow | None:
        """Provision the EnergyPlus parameterized template E2E workflow.

        Returns the Workflow, or None if prerequisites are missing.
        """
        # Check for EnergyPlus validator
        validator = Validator.objects.filter(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        ).first()
        if not validator:
            self.stderr.write(
                self.style.WARNING(
                    "EnergyPlus validator not found. "
                    "Run 'manage.py setup_validibot' first. "
                    "Skipping EnergyPlus E2E workflow."
                )
            )
            return None

        # Check for weather file
        weather_resource = ValidatorResourceFile.objects.filter(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            org__isnull=True,
        ).first()
        if not weather_resource:
            self.stderr.write(
                self.style.WARNING(
                    "No weather files found. "
                    "Run 'manage.py seed_weather_files' first. "
                    "Skipping EnergyPlus E2E workflow."
                )
            )
            return None

        # Idempotent workflow creation
        workflow, wf_created = Workflow.objects.get_or_create(
            org=org,
            slug=EP_WORKFLOW_SLUG,
            version="1",
            defaults={
                "name": EP_WORKFLOW_NAME,
                "user": user,
                "project": project,
                "is_active": True,
                "allowed_file_types": [SubmissionFileType.JSON],
            },
        )

        if not wf_created and workflow.steps.exists():
            logger.info(
                "EnergyPlus E2E workflow already exists: %s",
                workflow.pk,
            )
            return workflow

        # Create step with template variable config
        step = self._create_energyplus_step(workflow, validator, org, user)

        # Attach template IDF and weather file
        self._attach_template_idf(step)
        self._attach_weather_file(step, weather_resource)

        # Create output assertions on the step's ruleset
        self._create_output_assertions(step.ruleset, validator)

        self.stdout.write(
            self.style.SUCCESS(f"  Created EnergyPlus template workflow: {workflow.pk}")
        )
        return workflow

    def _create_energyplus_step(
        self,
        workflow: Workflow,
        validator: Validator,
        org: Organization,
        user: User,
    ) -> WorkflowStep:
        """Create the EnergyPlus workflow step with template variable config."""
        ruleset = Ruleset.objects.create(
            org=org,
            user=user,
            name="Window Glazing Output Assertions",
            ruleset_type=RulesetType.ENERGYPLUS,
            rules_text="",
            version="1",
        )

        return WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name="EnergyPlus Window Glazing Simulation",
            config={
                "run_simulation": True,
                "case_sensitive": True,
                "template_variables": [
                    {
                        "name": "U_FACTOR",
                        "description": "Window U-Factor",
                        "variable_type": "number",
                        "units": "W/m2-K",
                        "min_value": 0.1,
                        "max_value": 7.0,
                    },
                    {
                        "name": "SHGC",
                        "description": "Solar Heat Gain Coefficient",
                        "variable_type": "number",
                        "min_value": 0.01,
                        "max_value": 0.99,
                    },
                    {
                        "name": "VISIBLE_TRANSMITTANCE",
                        "description": "Visible Transmittance",
                        "variable_type": "number",
                        "min_value": 0.01,
                        "max_value": 0.99,
                    },
                ],
                "display_signals": [
                    "window_heat_loss_kwh",
                    "window_heat_gain_kwh",
                    "window_transmitted_solar_kwh",
                    "heating_energy_kwh",
                    "cooling_energy_kwh",
                ],
            },
        )

    def _attach_template_idf(self, step: WorkflowStep) -> None:
        """Attach the window glazing template IDF as a step-owned resource."""
        template_path = (
            Path(settings.BASE_DIR)
            / "tests"
            / "assets"
            / "idf"
            / "window_glazing_template.idf"
        )

        if not template_path.exists():
            msg = f"Template IDF not found: {template_path}"
            raise FileNotFoundError(msg)

        content = template_path.read_bytes()
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            validator_resource_file=None,
            step_resource_file=ContentFile(
                content,
                name="window_glazing_template.idf",
            ),
            filename="window_glazing_template.idf",
            resource_type="energyplus_model_template",
        )

    def _attach_weather_file(
        self,
        step: WorkflowStep,
        weather_resource: ValidatorResourceFile,
    ) -> None:
        """Attach a weather file as a catalog-referenced step resource."""
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=weather_resource,
        )

    def _create_output_assertions(
        self,
        ruleset: Ruleset,
        validator: Validator,
    ) -> None:
        """Create the two blog-post output assertions.

        Assertion 1: ``window_heat_loss_kwh < 800``
            Targets the ``window_heat_loss_kwh`` signal definition.
            (target_signal_definition is set, target_data_path is empty)

        Assertion 2: ``cooling_energy_kwh < heating_energy_kwh``
            A cross-signal comparison with no single signal target.
            (target_signal_definition is None, target_data_path is set)
        """
        # Assertion 1: window heat loss threshold
        heat_loss_signal = SignalDefinition.objects.filter(
            validator=validator,
            contract_key="window_heat_loss_kwh",
        ).first()

        if heat_loss_signal:
            RulesetAssertion.objects.create(
                ruleset=ruleset,
                order=10,
                assertion_type=AssertionType.CEL_EXPRESSION,
                operator=AssertionOperator.CEL_EXPR,
                target_signal_definition=heat_loss_signal,
                target_data_path="",
                rhs={"expr": "window_heat_loss_kwh < 800"},
                severity=Severity.ERROR,
                message_template=(
                    "Annual window heat loss must stay under 800 kWh "
                    "to meet our team's standards."
                ),
            )
        else:
            self.stderr.write(
                self.style.WARNING(
                    "Signal definition 'window_heat_loss_kwh' not found. "
                    "Run 'manage.py setup_validibot' to sync signal definitions. "
                    "Skipping heat loss assertion."
                )
            )

        # Assertion 2: cooling < heating (cross-signal comparison)
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=20,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_data_path="cooling_energy_kwh",
            rhs={"expr": "cooling_energy_kwh < heating_energy_kwh"},
            severity=Severity.ERROR,
            message_template=(
                "The glazing must not create a cooling-dominated envelope. "
                "In San Francisco's mild climate, cooling loads should be "
                "below heating loads for a well-chosen window."
            ),
        )
