"""Use-case tests for the SysMLv2 radiator workflow example.

These tests exercise the production-style workflow shape used by the SysMLv2
radiator example:

1. JSON Schema validation for the submitted model artifact
2. BASIC/CEL domain assertions over mapped SysML input values
3. FMU simulation as the final advanced-validator step

The current regression target is step 2. The radiator model stores key values
such as ``emissivity`` and ``mass`` at nested paths, so the CEL context must
honor ``StepSignalBinding`` mappings instead of assuming those names are
top-level keys in the submission.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

ASSET_DIR = (
    Path(__file__).resolve().parents[1] / "assets" / "sysml_v2" / "radiator_example"
)

STEP_TWO_BINDINGS = {
    "panelArea": "ownedMember[0].ownedAttribute[0].defaultValue",
    "emissivity": "ownedMember[0].ownedAttribute[1].defaultValue",
    "absorptivity": "ownedMember[0].ownedAttribute[2].defaultValue",
    "mass": "ownedMember[0].ownedAttribute[3].defaultValue",
    "solarIrradiance": "ownedMember[1].ownedAttribute[0].defaultValue",
    "end": "ownedMember[2].end",
    # This mirrors the production-style CEL expression, which only checks
    # for a non-null reference rather than validating referential integrity.
    "satisfiedRequirement": "ownedMember[4].satisfiedRequirement",
}

STEP_TWO_ASSERTIONS = (
    ("emissivity", "emissivity > 0.0 && emissivity <= 1.0"),
    ("absorptivity", "absorptivity >= 0.0 && absorptivity <= 1.0"),
    ("panelArea", "panelArea > 0.0"),
    ("mass", "mass > 0.0"),
    ("solarIrradiance", "solarIrradiance >= 0.0"),
    ("end", "size(end) == 2"),
    ("satisfiedRequirement", "satisfiedRequirement != null"),
)


class SysmlV2RadiatorWorkflowTests(TestCase):
    """Use-case coverage for the SysMLv2 radiator workflow."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        grant_role(cls.user, cls.org, RoleCode.EXECUTOR)
        cls.user.set_current_org(cls.org)
        cls.project = ProjectFactory(org=cls.org)
        cls.workflow = cls._build_workflow()

    @classmethod
    def _asset_text(cls, name: str) -> str:
        """Load a test asset from the radiator example directory."""
        return (ASSET_DIR / name).read_text(encoding="utf-8")

    @classmethod
    def _build_workflow(cls):
        """Create the three-step radiator workflow used in the regression tests."""
        workflow = WorkflowFactory(
            org=cls.org,
            user=cls.user,
            project=cls.project,
            is_active=True,
            allowed_file_types=[SubmissionFileType.JSON],
            name="SysMLv2 Radiator Workflow",
            slug="sysmlv2-radiator-workflow",
        )

        cls._add_schema_step(workflow)
        cls._add_cel_step(workflow)
        cls._add_fmu_step(workflow)

        return workflow

    @classmethod
    def _add_schema_step(cls, workflow):
        """Add step 1: JSON Schema validation for the radiator model."""
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            is_system=False,
            org=cls.org,
            supports_assertions=False,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator schema",
            ruleset_type=RulesetType.JSON_SCHEMA,
            version="1.0",
            rules_text=cls._asset_text("thermal_radiator_schema.json"),
            metadata={"schema_type": JSONSchemaVersion.DRAFT_2020_12.value},
        )
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name="Check SysMLv2 file schema",
            config={},
        )

    @classmethod
    def _add_cel_step(cls, workflow):
        """Add step 2: CEL assertions over bound SysML values."""
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            org=cls.org,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator CEL rules",
            ruleset_type=RulesetType.BASIC,
            version="1.0",
        )
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=20,
            name="Check domain constraints",
            config={},
        )

        signals: dict[str, object] = {}
        for contract_key, source_data_path in STEP_TWO_BINDINGS.items():
            signal = SignalDefinitionFactory(
                workflow_step=step,
                validator=None,
                contract_key=contract_key,
                native_name=contract_key,
                direction=SignalDirection.INPUT,
                origin_kind=SignalOriginKind.CATALOG,
            )
            StepSignalBindingFactory(
                workflow_step=step,
                signal_definition=signal,
                source_data_path=source_data_path,
                is_required=False,
            )
            signals[contract_key] = signal

        for order, (target_key, expression) in enumerate(STEP_TWO_ASSERTIONS, start=1):
            RulesetAssertion.objects.create(
                ruleset=ruleset,
                order=order * 10,
                assertion_type=AssertionType.CEL_EXPRESSION,
                operator=AssertionOperator.CEL_EXPR,
                target_signal_definition=signals[target_key],
                target_data_path="",
                severity=Severity.ERROR,
                rhs={"expr": expression},
            )

    @classmethod
    def _add_fmu_step(cls, workflow):
        """Add step 3: placeholder FMU step to mirror the production flow.

        The regression tests below fail before this step executes, but we still
        create a real FMU validator from the radiator example asset so the
        workflow shape matches production more closely.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile

        from validibot.validations.services.fmu import create_fmu_validator

        upload = SimpleUploadedFile(
            "ThermalRadiator.fmu",
            (ASSET_DIR / "ThermalRadiator.fmu").read_bytes(),
            content_type="application/octet-stream",
        )
        validator = create_fmu_validator(
            org=cls.org,
            project=workflow.project,
            name="Thermal Radiator FMU",
            upload=upload,
        )
        ruleset = Ruleset.objects.create(
            org=cls.org,
            user=cls.user,
            name="SysMLv2 radiator FMU step",
            ruleset_type=RulesetType.FMU,
            version="1.0",
        )
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=30,
            name="Run thermal radiation simulation",
            config={},
        )

    def _run_submission(self, asset_name: str) -> ValidationRun:
        """Create a submission from an asset and execute the workflow."""
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            workflow=self.workflow,
            file_type=SubmissionFileType.JSON,
            content=self._asset_text(asset_name),
        )
        validation_run = ValidationRun.objects.create(
            org=self.org,
            workflow=self.workflow,
            submission=submission,
            project=self.project,
            user=self.user,
            status=ValidationRunStatus.PENDING,
        )
        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run.id,
            user_id=self.user.id,
        )
        validation_run.refresh_from_db()
        return validation_run

    def test_schema_failure_stops_before_domain_checks(self):
        """A schema-invalid SysML submission should fail on step 1 only."""
        validation_run = self._run_submission(
            "invalid_thermal_radiator_model_schema_fail.json",
        )

        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 1)
        self.assertEqual(step_runs[0].workflow_step.name, "Check SysMLv2 file schema")
        self.assertEqual(step_runs[0].status, "FAILED")
        self.assertGreater(step_runs[0].findings.count(), 0)

    def test_cel_failures_use_bound_sysml_inputs_instead_of_undefined_names(self):
        """Nested SysML values should resolve through step bindings for CEL.

        The invalid CEL asset should fail on the assertions that actually
        evaluate to false, without emitting spurious "undefined name" or
        timeout errors for values that exist in the nested submission.
        """
        validation_run = self._run_submission(
            "invalid_thermal_radiator_model_cel_fail.json",
        )

        self.assertEqual(validation_run.status, ValidationRunStatus.FAILED)

        step_runs = list(validation_run.step_runs.order_by("step_order"))
        self.assertEqual(len(step_runs), 2)
        self.assertEqual(step_runs[0].status, "PASSED")
        self.assertEqual(step_runs[1].workflow_step.name, "Check domain constraints")
        self.assertEqual(step_runs[1].status, "FAILED")

        findings = list(step_runs[1].findings.order_by("id"))
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(finding.severity == Severity.ERROR for finding in findings))

        messages = [finding.message for finding in findings]
        self.assertTrue(
            all("undefined name" not in message for message in messages),
            messages,
        )
        self.assertTrue(
            all("timed out" not in message.lower() for message in messages),
            messages,
        )
        self.assertEqual(
            messages.count("CEL assertion evaluated to false."),
            3,
        )
