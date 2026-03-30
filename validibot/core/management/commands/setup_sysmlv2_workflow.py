"""
Provision a local SysML v2 Thermal Radiator workflow.

Recreates the structure of the live workflow at app.validibot.com
(workflow 3) for local development and testing.  The workflow has
three steps:

1. **JSON Schema** -- validates structural conformance of the SysML v2
   JSON model (correct types, required fields, FMU reference).
2. **CEL Assertions** -- checks domain constraints (emissivity range,
   absorptivity range) using the Basic Validator with
   ``allow_custom_assertion_targets=True``.
3. **FMU Simulation** -- runs the ThermalRadiator.fmu with input
   signals bound from the submission payload and asserts the
   equilibrium temperature is within range.

The command is idempotent -- safe to run repeatedly.

Usage::

    source set-env.sh && uv run python manage.py setup_sysmlv2_workflow
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

ASSETS_DIR = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "assets"
    / "sysml_v2"
    / "radiator_example"
)


class Command(BaseCommand):
    help = "Create the SysML v2 Thermal Radiator workflow for local testing."

    @transaction.atomic
    def handle(self, *args, **options):
        org, user = self._ensure_org_and_user()

        # System validators (reuse existing or create)
        json_schema_validator = self._ensure_validator(
            slug="json-schema-validator",
            name="JSON Schema Validator",
            validation_type=ValidationType.JSON_SCHEMA,
            allow_custom_assertion_targets=False,
        )
        basic_validator = self._ensure_validator(
            slug="basic-validator",
            name="Basic Validator",
            validation_type=ValidationType.BASIC,
            allow_custom_assertion_targets=True,
        )
        fmu_validator = self._ensure_validator(
            slug="fmi-validator",
            name="FMU Validator",
            validation_type=ValidationType.FMU,
            allow_custom_assertion_targets=True,
        )

        workflow = self._ensure_workflow(org, user)

        # Step 1: JSON Schema validation
        schema_ruleset = self._ensure_schema_ruleset(org, user)
        self._ensure_step(
            workflow=workflow,
            order=10,
            name="Check SysMLv2 file schema",
            step_key="check_sysmlv2_file_schema",
            validator=json_schema_validator,
            ruleset=schema_ruleset,
            config={
                "schema_type": "2020-12",
                "schema_source": "text",
                "schema_type_label": "Draft 2020-12",
            },
        )

        # Step 2: CEL domain constraint assertions
        cel_ruleset = self._ensure_cel_ruleset(org, user)
        self._ensure_step(
            workflow=workflow,
            order=20,
            name="Check domain constraints",
            step_key="check_domain_constraints",
            validator=basic_validator,
            ruleset=cel_ruleset,
            config={},
        )

        # Step 3: FMU simulation
        fmu_ruleset = self._ensure_fmu_ruleset(org, user)
        fmu_step = self._ensure_step(
            workflow=workflow,
            order=30,
            name="Run thermal radiation simulation",
            step_key="run_thermal_radiation_simulation",
            validator=fmu_validator,
            ruleset=fmu_ruleset,
            config={
                "fmu_variables": [
                    {
                        "name": "solar_irradiance",
                        "causality": "input",
                        "value_type": "Real",
                        "value_reference": 0,
                    },
                    {
                        "name": "panel_area",
                        "causality": "input",
                        "value_type": "Real",
                        "value_reference": 1,
                    },
                    {
                        "name": "emissivity",
                        "causality": "input",
                        "value_type": "Real",
                        "value_reference": 2,
                    },
                    {
                        "name": "absorptivity",
                        "causality": "input",
                        "value_type": "Real",
                        "value_reference": 3,
                    },
                    {
                        "name": "equilibrium_temp",
                        "causality": "output",
                        "value_type": "Real",
                        "value_reference": 4,
                    },
                    {
                        "name": "heat_rejected",
                        "causality": "output",
                        "value_type": "Real",
                        "value_reference": 5,
                    },
                ],
            },
        )
        self._ensure_fmu_signal_bindings(fmu_validator, fmu_step)

        self.stdout.write(
            self.style.SUCCESS(
                f"SysML v2 workflow ready: id={workflow.id}, slug={workflow.slug}"
            ),
        )

    def _ensure_org_and_user(self):
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.first()
        if not user:
            self.stderr.write(
                "No users exist. Run setup_validibot first.",
            )
            raise SystemExit(1)

        org = Organization.objects.filter(
            membership__user=user,
        ).first()
        if not org:
            self.stderr.write(
                "No organization found for user. Run setup_validibot first.",
            )
            raise SystemExit(1)

        return org, user

    def _ensure_validator(self, *, slug, name, validation_type, **kwargs):
        validator, created = Validator.objects.get_or_create(
            slug=slug,
            defaults={
                "name": name,
                "validation_type": validation_type,
                "is_system": True,
                **kwargs,
            },
        )
        if not created:
            # Ensure properties match production
            for key, val in kwargs.items():
                setattr(validator, key, val)
            validator.save(update_fields=list(kwargs.keys()))
        action = "Created" if created else "Reusing"
        self.stdout.write(f"  {action} validator: {validator.name}")
        return validator

    def _ensure_workflow(self, org, user):
        workflow, created = Workflow.objects.get_or_create(
            slug="sysmlv2-thermal-radiator",
            org=org,
            defaults={
                "name": "SysML v2 Thermal Radiator Validation",
                "description": (
                    "Validates thermal radiator SysMLv2 files: "
                    "schema, domain constraints, and FMU simulation."
                ),
                "user": user,
                "is_active": True,
                "allowed_file_types": [SubmissionFileType.JSON],
            },
        )
        action = "Created" if created else "Reusing"
        self.stdout.write(f"  {action} workflow: {workflow.name}")
        return workflow

    def _ensure_step(
        self, *, workflow, order, name, step_key, validator, ruleset, config
    ):
        step, created = WorkflowStep.objects.get_or_create(
            workflow=workflow,
            step_key=step_key,
            defaults={
                "order": order,
                "name": name,
                "validator": validator,
                "ruleset": ruleset,
                "config": config,
            },
        )
        if not created:
            step.order = order
            step.name = name
            step.validator = validator
            step.ruleset = ruleset
            step.config = config
            step.save()
        action = "Created" if created else "Updated"
        self.stdout.write(f"  {action} step: {name}")
        return step

    def _ensure_schema_ruleset(self, org, user):
        schema_text = (ASSETS_DIR / "thermal_radiator_schema.json").read_text()
        ruleset, created = Ruleset.objects.get_or_create(
            name="sysmlv2-thermal-radiator-schema",
            org=org,
            defaults={
                "user": user,
                "ruleset_type": RulesetType.JSON_SCHEMA,
                "rules_text": schema_text,
                "metadata": {"schema_type": "2020-12"},
            },
        )
        if not created:
            ruleset.rules_text = schema_text
            ruleset.metadata = {"schema_type": "2020-12"}
            ruleset.save(update_fields=["rules_text", "metadata"])
        action = "Created" if created else "Updated"
        self.stdout.write(f"  {action} schema ruleset")
        return ruleset

    def _ensure_cel_ruleset(self, org, user):
        ruleset, created = Ruleset.objects.get_or_create(
            name="sysmlv2-domain-constraints",
            org=org,
            defaults={
                "user": user,
                "ruleset_type": RulesetType.BASIC,
                "rules_text": "",
                "metadata": {},
            },
        )
        action = "Created" if created else "Reusing"
        self.stdout.write(f"  {action} CEL assertion ruleset")

        # Ensure assertions exist
        assertions = [
            {
                "order": 10,
                "rhs": {"expr": "emissivity > 0.0 && emissivity <= 1.0"},
                "cel_cache": "emissivity > 0.0 && emissivity <= 1.0",
                "target_data_path": ("emissivity > 0.0 && emissivity <= 1.0"),
            },
            {
                "order": 20,
                "rhs": {
                    "expr": ("absorptivity >= 0.0 && absorptivity <= 1.0"),
                },
                "cel_cache": ("absorptivity >= 0.0 && absorptivity <= 1.0"),
                "target_data_path": ("absorptivity >= 0.0 && absorptivity <= 1.0"),
            },
        ]
        for assertion_data in assertions:
            RulesetAssertion.objects.get_or_create(
                ruleset=ruleset,
                order=assertion_data["order"],
                defaults={
                    "assertion_type": AssertionType.CEL_EXPRESSION,
                    "severity": Severity.ERROR,
                    "target_data_path": assertion_data["target_data_path"],
                    "cel_cache": assertion_data["cel_cache"],
                    "rhs": assertion_data["rhs"],
                    "options": {},
                },
            )
        self.stdout.write(
            f"  Ensured {len(assertions)} CEL assertions on domain ruleset",
        )
        return ruleset

    def _ensure_fmu_ruleset(self, org, user):
        ruleset, created = Ruleset.objects.get_or_create(
            name="sysmlv2-fmu-assertions",
            org=org,
            defaults={
                "user": user,
                "ruleset_type": RulesetType.FMU,
                "rules_text": "",
                "metadata": {},
            },
        )

        RulesetAssertion.objects.get_or_create(
            ruleset=ruleset,
            order=20,
            defaults={
                "assertion_type": AssertionType.CEL_EXPRESSION,
                "severity": Severity.ERROR,
                "target_data_path": (
                    "equilibrium_temp >= 150.0 && equilibrium_temp <= 400.0"
                ),
                "cel_cache": ("equilibrium_temp >= 150.0 && equilibrium_temp <= 400.0"),
                "rhs": {
                    "expr": ("equilibrium_temp >= 150.0 && equilibrium_temp <= 400.0"),
                },
                "options": {},
            },
        )
        action = "Created" if created else "Reusing"
        self.stdout.write(f"  {action} FMU assertion ruleset")
        return ruleset

    def _ensure_fmu_signal_bindings(self, fmu_validator, fmu_step):
        """Create signal definitions and step bindings for FMU inputs.

        The FMU expects flat input variable names (solar_irradiance,
        panel_area, etc.) but the SysML v2 submission stores these as
        named elements in nested arrays.  The signal bindings use
        filter expressions to resolve the right values.
        """
        input_signals = [
            {
                "contract_key": "solar_irradiance",
                "native_name": "solar_irradiance",
                "source_data_path": (
                    "ownedMember[?@.name=='ThermalEnvironment']"
                    ".ownedAttribute[?@.name=='solarIrradiance']"
                    ".defaultValue"
                ),
            },
            {
                "contract_key": "panel_area",
                "native_name": "panel_area",
                "source_data_path": (
                    "ownedMember[?@.name=='RadiatorPanel']"
                    ".ownedAttribute[?@.name=='panelArea']"
                    ".defaultValue"
                ),
            },
            {
                "contract_key": "emissivity",
                "native_name": "emissivity",
                "source_data_path": (
                    "ownedMember[?@.name=='RadiatorPanel']"
                    ".ownedAttribute[?@.name=='emissivity']"
                    ".defaultValue"
                ),
            },
            {
                "contract_key": "absorptivity",
                "native_name": "absorptivity",
                "source_data_path": (
                    "ownedMember[?@.name=='RadiatorPanel']"
                    ".ownedAttribute[?@.name=='absorptivity']"
                    ".defaultValue"
                ),
            },
        ]

        for sig_data in input_signals:
            sig, _ = SignalDefinition.objects.get_or_create(
                validator=fmu_validator,
                contract_key=sig_data["contract_key"],
                defaults={
                    "native_name": sig_data["native_name"],
                    "direction": SignalDirection.INPUT,
                    "order": input_signals.index(sig_data) * 10,
                },
            )
            StepSignalBinding.objects.get_or_create(
                workflow_step=fmu_step,
                signal_definition=sig,
                defaults={
                    "source_scope": BindingSourceScope.SUBMISSION_PAYLOAD,
                    "source_data_path": sig_data["source_data_path"],
                    "is_required": True,
                },
            )

        self.stdout.write(
            f"  Ensured {len(input_signals)} FMU signal bindings",
        )
