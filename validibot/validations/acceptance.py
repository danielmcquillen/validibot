"""Repeatable live acceptance for managed validator deployments.

This module deliberately keeps the production acceptance surface small.  It
reuses the normal workflow launcher, durable execution-attempt records, the
existing GCS capability probe, and source-controlled fixtures.  The GCP
operator recipe owns the temporary maintenance window; this module owns only
application-level preparation, execution, and a secret-free JSON report.

The acceptance workflows live in a dedicated internal organization and use an
operator account with an unusable password.  They are reused between runs so a
production acceptance does not create a growing collection of fixture
definitions.  Each invocation still creates fresh submissions, runs, attempts,
and immutable evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from google.cloud import storage

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import Submission
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import RoleCode
from validibot.users.models import User
from validibot.users.models import ensure_default_project
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import Ruleset
from validibot.validations.models import StepInputBinding
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.services.cloud_run.gcs_capability_probe import (
    probe_attempt_gcs_runtime_capability,
)
from validibot.validations.services.fmu import build_introspection_metadata
from validibot.validations.services.fmu import introspect_fmu
from validibot.validations.services.fmu_step_io import sync_step_fmu_io_definitions
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.services.template_step_io import (
    sync_step_template_io_definitions,
)
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.validators.base.config import get_config
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource

if TYPE_CHECKING:
    from datetime import datetime

    from validibot.projects.models import Project

ACCEPTANCE_SCHEMA_VERSION = "validibot.validator-acceptance.v1"
ACCEPTANCE_FIXTURE_VERSION = 1
ACCEPTANCE_ORG_SLUG = "validibot-validator-acceptance"
ACCEPTANCE_USERNAME = "validibot-validator-acceptance"
ACCEPTANCE_WEATHER_FILENAME = "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
MAX_ATTEMPTS_PER_BACKEND = 20
REPRESENTATIVE_SAMPLE_SIZE = 20


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Identity and fixture metadata for one managed validator backend."""

    key: str
    validation_type: str
    ruleset_type: str
    provider_start_target_seconds: float


BACKENDS = (
    BackendSpec(
        "energyplus",
        ValidationType.ENERGYPLUS,
        RulesetType.ENERGYPLUS,
        30.0,
    ),
    BackendSpec("fmu", ValidationType.FMU, RulesetType.FMU, 20.0),
    BackendSpec("shacl", ValidationType.SHACL, RulesetType.SHACL, 15.0),
    BackendSpec(
        "schematron",
        ValidationType.SCHEMATRON,
        RulesetType.SCHEMATRON,
        15.0,
    ),
)


@dataclass(frozen=True, slots=True)
class AcceptanceScenario:
    """A reusable workflow plus the exact submission used to exercise it."""

    backend: BackendSpec
    workflow: Workflow
    inline_text: str
    filename: str
    file_type: str
    fixture_sha256: str


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    """One stable, secret-free acceptance verdict."""

    check_id: str
    status: str
    summary: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Project the check to the versioned report format."""
        return {
            "id": self.check_id,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


class AcceptanceReport:
    """Accumulate verdicts and produce the single operator-facing report."""

    def __init__(self, *, release_tag: str, attempts_per_backend: int) -> None:
        timestamp = timezone.now()
        suffix = uuid.uuid4().hex[:8]
        self.acceptance_id = (
            f"va-{timestamp:%Y%m%dT%H%M%SZ}-{release_tag.removeprefix('v')}-{suffix}"
        )
        self.release_tag = release_tag
        self.attempts_per_backend = attempts_per_backend
        self.started_at = timestamp
        self.finished_at: datetime | None = None
        self.checks: list[AcceptanceCheck] = []

    def add(
        self,
        check_id: str,
        status: str,
        summary: str,
        **details: Any,
    ) -> None:
        """Append one check while keeping status vocabulary constrained."""
        if status not in {"passed", "failed", "skipped"}:
            raise ValueError(f"Unknown acceptance status: {status}")
        self.checks.append(
            AcceptanceCheck(
                check_id=check_id,
                status=status,
                summary=summary,
                details=details,
            )
        )

    @property
    def passed(self) -> bool:
        """Return true only when the report has checks and none failed."""
        return bool(self.checks) and all(
            check.status != "failed" for check in self.checks
        )

    def finish(self) -> None:
        """Freeze the completion time used by the report projection."""
        self.finished_at = timezone.now()

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON-safe acceptance document."""
        finished_at = self.finished_at or timezone.now()
        return {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            "acceptance_id": self.acceptance_id,
            "stage": str(getattr(settings, "VALIDIBOT_STAGE", "") or "unknown"),
            "backend_release": self.release_tag,
            "attempts_per_backend": self.attempts_per_backend,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


class AcceptanceFixtureBuilder:
    """Create or reuse the four private acceptance workflows."""

    def __init__(self) -> None:
        self.user, self.org, self.project = self._ensure_actor()

    def build_all(self) -> dict[str, AcceptanceScenario]:
        """Return deterministic scenarios for every managed backend."""
        with transaction.atomic():
            return {spec.key: self._build_scenario(spec) for spec in BACKENDS}

    def _ensure_actor(self) -> tuple[User, Organization, Project]:
        """Create a non-login operator identity that bypasses tenant quotas."""
        user, created = User.objects.get_or_create(
            username=ACCEPTANCE_USERNAME,
            defaults={
                "email": "validator-acceptance@localhost.invalid",
                "name": "Validator Acceptance Operator",
                "is_active": True,
                "is_superuser": True,
                "is_staff": False,
            },
        )
        required_updates: list[str] = []
        if not user.is_active:
            user.is_active = True
            required_updates.append("is_active")
        if not user.is_superuser:
            user.is_superuser = True
            required_updates.append("is_superuser")
        if user.is_staff:
            user.is_staff = False
            required_updates.append("is_staff")
        if created or user.has_usable_password():
            user.set_unusable_password()
            required_updates.append("password")
        if required_updates:
            user.save(update_fields=sorted(set(required_updates)))

        org, _ = Organization.objects.get_or_create(
            slug=ACCEPTANCE_ORG_SLUG,
            defaults={"name": "Validibot Validator Acceptance"},
        )
        membership, membership_created = Membership.objects.get_or_create(
            user=user,
            org=org,
            defaults={"is_active": True},
        )
        if not membership.is_active:
            membership.is_active = True
            membership.save(update_fields=["is_active"])
        if membership_created or not membership.has_role(RoleCode.EXECUTOR):
            membership.set_roles({RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR})
        if user.current_org_id != org.pk:
            user.set_current_org(org)
        return user, org, ensure_default_project(org)

    def _build_scenario(self, spec: BackendSpec) -> AcceptanceScenario:
        """Create one backend-specific workflow and submission definition."""
        validator = self._current_validator(spec)
        existing = self._existing_workflow(spec, validator)
        if existing is not None:
            return self._scenario_for_existing(spec, existing)
        if spec.validation_type == ValidationType.ENERGYPLUS:
            return self._create_energyplus(spec, validator)
        if spec.validation_type == ValidationType.FMU:
            return self._create_fmu(spec, validator)
        if spec.validation_type == ValidationType.SHACL:
            return self._create_shacl(spec, validator)
        if spec.validation_type == ValidationType.SCHEMATRON:
            return self._create_schematron(spec, validator)
        raise ValueError(f"Unsupported acceptance backend: {spec.key}")

    def _current_validator(self, spec: BackendSpec) -> Validator:
        """Resolve the exact current system-validator contract."""
        config = get_config(spec.validation_type)
        if config is None:
            raise ValueError(f"No registered validator config for {spec.key}")
        validator = Validator.objects.filter(
            slug=config.slug,
            version=config.version,
            validation_type=spec.validation_type,
            is_system=True,
            is_enabled=True,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).first()
        if validator is None:
            raise ValueError(
                f"Current {spec.key} system validator is missing; run sync_validators"
            )
        return validator

    def _workflow_slug(self, spec: BackendSpec, validator: Validator) -> str:
        """Version fixture identity without mutating workflows already in use."""
        return (
            f"validator-acceptance-{spec.key}-f{ACCEPTANCE_FIXTURE_VERSION}"
            f"-v{validator.version}"
        )

    def _existing_workflow(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> Workflow | None:
        """Reuse an immutable fixture workflow only when its contract matches."""
        workflow = Workflow.objects.filter(
            org=self.org,
            slug=self._workflow_slug(spec, validator),
            version="1",
        ).first()
        if workflow is None:
            return None
        step = workflow.steps.order_by("order", "pk").first()
        if step is None or step.validator_id != validator.pk:
            raise ValueError(f"Existing {spec.key} acceptance workflow has drifted")
        return workflow

    def _scenario_for_existing(
        self,
        spec: BackendSpec,
        workflow: Workflow,
    ) -> AcceptanceScenario:
        """Rebuild source-controlled submission metadata for a reused workflow."""
        text, filename, file_type = self._submission_fixture(spec)
        return AcceptanceScenario(
            backend=spec,
            workflow=workflow,
            inline_text=text,
            filename=filename,
            file_type=file_type,
            fixture_sha256=_sha256_text(text),
        )

    def _create_workflow(
        self,
        spec: BackendSpec,
        validator: Validator,
        *,
        allowed_file_types: list[str],
        rules_text: str = "",
        rules_metadata: dict[str, Any] | None = None,
        step_config: dict[str, Any] | None = None,
    ) -> tuple[Workflow, WorkflowStep]:
        """Create the common one-step workflow structure."""
        workflow = Workflow.objects.create(
            org=self.org,
            slug=self._workflow_slug(spec, validator),
            version="1",
            name=f"Validator acceptance: {spec.key}",
            user=self.user,
            project=self.project,
            is_active=True,
            allowed_file_types=allowed_file_types,
        )
        ruleset = Ruleset.objects.create(
            org=self.org,
            user=self.user,
            name=f"{workflow.slug}-rules",
            ruleset_type=spec.ruleset_type,
            rules_text=rules_text,
            metadata=rules_metadata or {},
            version="1",
        )
        step = WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name=f"{spec.key.title()} acceptance canary",
            config=step_config or {},
        )
        return workflow, step

    def _create_energyplus(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the small parameterised EnergyPlus canary."""
        template = self._asset_text("idf/window_glazing_template.idf")
        weather = ValidatorResourceFile.objects.filter(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            org__isnull=True,
            filename=ACCEPTANCE_WEATHER_FILENAME,
        ).first()
        if weather is None:
            raise ValueError(
                f"Acceptance weather file {ACCEPTANCE_WEATHER_FILENAME} is missing"
            )
        variables = [
            {"name": "U_FACTOR", "variable_type": "number"},
            {"name": "SHGC", "variable_type": "number"},
            {"name": "VISIBLE_TRANSMITTANCE", "variable_type": "number"},
        ]
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.JSON],
            step_config={"run_simulation": True, "case_sensitive": True},
        )
        sync_step_template_io_definitions(step, variables)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(
                template.encode("utf-8"),
                name="window_glazing_template.idf",
            ),
            filename="window_glazing_template.idf",
            resource_type="energyplus_model_template",
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=weather,
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow)

    def _create_fmu(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create a system-FMU workflow using the tiny Feedthrough fixture."""
        fmu_payload = self._asset_bytes("fmu/Feedthrough.fmu")
        result = introspect_fmu(fmu_payload, "Feedthrough.fmu")
        sim = result.simulation_defaults
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.JSON],
            step_config={
                "fmu_simulation": {
                    "start_time": sim.start_time,
                    "stop_time": sim.stop_time,
                    "step_size": sim.step_size,
                    "tolerance": sim.tolerance,
                },
                "fmu_introspection": build_introspection_metadata(result),
            },
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.FMU_MODEL,
            step_resource_file=ContentFile(fmu_payload, name="Feedthrough.fmu"),
            filename="Feedthrough.fmu",
            resource_type="fmu",
        )
        variables = [
            {
                "name": variable.name,
                "causality": variable.causality,
                "variability": variable.variability,
                "value_reference": variable.value_reference,
                "value_type": variable.value_type,
                "unit": variable.unit,
                "description": variable.description,
                "label": "",
            }
            for variable in result.variables
        ]
        sync_step_fmu_io_definitions(step, variables)
        ensure_step_input_bindings(step)
        # The sync creates payload bindings for FMU inputs.  Keep their blank
        # paths: the resolver intentionally falls back to each contract key.
        StepInputBinding.objects.filter(
            workflow_step=step,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
        ).update(is_required=True)
        return self._scenario_for_existing(spec, workflow)

    def _create_shacl(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the minimal conforming RDF/SHACL canary."""
        shapes = self._asset_text("shacl/example_person_shapes.ttl")
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.TEXT],
            rules_text=shapes,
            rules_metadata={"submission_format": "turtle"},
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow)

    def _create_schematron(
        self,
        spec: BackendSpec,
        validator: Validator,
    ) -> AcceptanceScenario:
        """Create the valid calibration-certificate Schematron canary."""
        rules = self._asset_text("schematron/calibration/calibration-rules-demo.sch")
        workflow, step = self._create_workflow(
            spec,
            validator,
            allowed_file_types=[SubmissionFileType.XML],
            rules_text=rules,
        )
        ensure_step_input_bindings(step)
        return self._scenario_for_existing(spec, workflow)

    def _submission_fixture(self, spec: BackendSpec) -> tuple[str, str, str]:
        """Return exact source-controlled input bytes for one backend."""
        if spec.validation_type == ValidationType.ENERGYPLUS:
            return (
                json.dumps(
                    {
                        "U_FACTOR": 2.0,
                        "SHGC": 0.4,
                        "VISIBLE_TRANSMITTANCE": 0.6,
                    },
                    sort_keys=True,
                ),
                "energyplus-acceptance.json",
                SubmissionFileType.JSON,
            )
        if spec.validation_type == ValidationType.FMU:
            return (
                json.dumps(
                    {
                        "real_continuous_in": 42.0,
                        "real_discrete_in": 7.0,
                        "int_in": 7,
                        "bool_in": True,
                    },
                    sort_keys=True,
                ),
                "fmu-acceptance.json",
                SubmissionFileType.JSON,
            )
        if spec.validation_type == ValidationType.SHACL:
            return (
                self._asset_text("shacl/valid_person.ttl"),
                "valid-person.ttl",
                SubmissionFileType.TEXT,
            )
        if spec.validation_type == ValidationType.SCHEMATRON:
            return (
                self._asset_text(
                    "schematron/calibration/calibration-certificate-valid.xml"
                ),
                "calibration-certificate-valid.xml",
                SubmissionFileType.XML,
            )
        raise ValueError(f"Unsupported acceptance backend: {spec.key}")

    def _asset_bytes(self, relative_path: str) -> bytes:
        """Read a shipped fixture and fail clearly when images omit tests."""
        path = Path(settings.BASE_DIR) / "tests" / "assets" / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"Validator acceptance fixture missing: {path}")
        return path.read_bytes()

    def _asset_text(self, relative_path: str) -> str:
        """Read one UTF-8 fixture without silently replacing invalid bytes."""
        return self._asset_bytes(relative_path).decode("utf-8")


class ValidatorAcceptanceRunner:
    """Run preflight, storage, and end-to-end canaries for one release."""

    def __init__(
        self,
        *,
        release_tag: str,
        attempts_per_backend: int = 1,
        timeout_seconds: int = 1200,
        poll_interval_seconds: float = 2.0,
        run_storage_probe: bool = True,
        ambient_isolation_verified: bool = False,
    ) -> None:
        if not RELEASE_TAG_PATTERN.fullmatch(release_tag):
            raise ValueError("release_tag must be vX.Y.Z")
        if not 1 <= attempts_per_backend <= MAX_ATTEMPTS_PER_BACKEND:
            raise ValueError(
                f"attempts_per_backend must be between 1 and {MAX_ATTEMPTS_PER_BACKEND}"
            )
        self.release_tag = release_tag
        self.attempts_per_backend = attempts_per_backend
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.run_storage_probe = run_storage_probe
        self.ambient_isolation_verified = ambient_isolation_verified

    def run(self) -> AcceptanceReport:
        """Execute the complete non-destructive application acceptance suite."""
        report = AcceptanceReport(
            release_tag=self.release_tag,
            attempts_per_backend=self.attempts_per_backend,
        )
        deployments_ok = self._check_deployments(report)
        self._check_storage(report)
        if not deployments_ok:
            report.add(
                "VA-SMOKE-ABORTED",
                "failed",
                "Canaries were not started because deployment preflight failed.",
            )
            report.finish()
            return report

        try:
            scenarios = AcceptanceFixtureBuilder().build_all()
        except Exception as exc:
            report.add(
                "VA-FIXTURES",
                "failed",
                "Acceptance fixtures could not be prepared.",
                error=_safe_error(exc),
            )
            report.finish()
            return report

        report.add(
            "VA-FIXTURES",
            "passed",
            "All four immutable acceptance workflows are ready.",
            fixture_version=ACCEPTANCE_FIXTURE_VERSION,
            fixture_hashes={
                key: scenario.fixture_sha256 for key, scenario in scenarios.items()
            },
        )
        launched_by_backend = {}
        for spec in BACKENDS:
            scenario = scenarios[spec.key]
            launched_by_backend[spec.key] = self._launch_backend(report, scenario)
        self._wait_for_runs(launched_by_backend)
        for spec in BACKENDS:
            scenario = scenarios[spec.key]
            launched = launched_by_backend[spec.key]
            for sequence, run in launched:
                run.refresh_from_db()
                self._record_run(report, scenario, sequence, run)
            self._record_latency(report, scenario, launched)
        report.finish()
        return report

    def _check_deployments(self, report: AcceptanceReport) -> bool:
        """Require current primary Services and retained long-running Jobs."""
        expected_release = self.release_tag.removeprefix("v")
        all_passed = True
        for spec in BACKENDS:
            try:
                validator, primary, compatibility = self._accepted_routes(
                    spec,
                    expected_release,
                )
                report.add(
                    f"VA-{spec.key.upper()}-ROUTE",
                    "passed",
                    "Candidate Service is primary and the Job rollback route is ready.",
                    validator_id=str(validator.pk),
                    service_deployment_id=str(primary.pk),
                    service_revision=primary.deployment_revision,
                    service_image_digest=primary.backend_image_digest,
                    service_minimum_instances=primary.minimum_instances,
                    service_maximum_instances=primary.maximum_instances,
                    job_deployment_id=str(compatibility.pk),
                    job_revision=compatibility.deployment_revision,
                )
            except Exception as exc:
                all_passed = False
                report.add(
                    f"VA-{spec.key.upper()}-ROUTE",
                    "failed",
                    "Managed deployment route failed acceptance preflight.",
                    error=_safe_error(exc),
                )
        return all_passed

    def _accepted_routes(
        self,
        spec: BackendSpec,
        expected_release: str,
    ) -> tuple[Validator, ValidatorExecutionDeployment, ValidatorExecutionDeployment]:
        """Resolve and validate the two routes required for a safe canary."""
        config = get_config(spec.validation_type)
        if config is None:
            raise ValueError("validator config is not registered")
        validator = Validator.objects.get(
            slug=config.slug,
            version=config.version,
            is_system=True,
        )
        routes = {
            route.routing_role: route
            for route in ValidatorExecutionDeployment.objects.filter(
                validator=validator,
                routing_role__in=(
                    ExecutionDeploymentRoutingRole.PRIMARY,
                    ExecutionDeploymentRoutingRole.LONG_RUNNING,
                ),
            )
        }
        primary = routes.get(ExecutionDeploymentRoutingRole.PRIMARY)
        compatibility = routes.get(ExecutionDeploymentRoutingRole.LONG_RUNNING)
        if primary is None:
            raise ValueError("primary Service route is missing")
        if primary.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
            raise ValueError("primary route is not a Cloud Run Service")
        if primary.readiness_state != ExecutionDeploymentReadiness.READY:
            raise ValueError("primary Service route is not ready")
        if primary.emergency_blocked:
            raise ValueError("primary Service route is emergency blocked")
        if not primary.last_verification_succeeded:
            raise ValueError("primary Service verification has not passed")
        if primary.backend_release_identity != expected_release:
            raise ValueError(
                "primary Service release is "
                f"{primary.backend_release_identity or '<missing>'}"
            )
        if not primary.backend_image_digest.startswith("sha256:"):
            raise ValueError("primary Service image is not digest-pinned")
        if compatibility is None:
            raise ValueError("long-running Job route is missing")
        if compatibility.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_JOB:
            raise ValueError("long-running route is not a Cloud Run Job")
        if compatibility.readiness_state != ExecutionDeploymentReadiness.READY:
            raise ValueError("long-running Job route is not ready")
        return validator, primary, compatibility

    def _check_storage(self, report: AcceptanceReport) -> None:
        """Require IAM denial proof and exercise the real downscoped token."""
        if not self.run_storage_probe:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "skipped",
                "Live GCS capability probe was disabled for this invocation.",
            )
            return
        if not self.ambient_isolation_verified:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "failed",
                "Ambient validator storage isolation was not verified by the "
                "operator recipe.",
            )
            return
        try:
            result = probe_attempt_gcs_runtime_capability(
                bucket_name=str(getattr(settings, "GCS_VALIDATION_BUCKET", "")),
                project_id=str(getattr(settings, "GCP_PROJECT_ID", "")),
            )
            report.add(
                "VA-STORAGE-CAPABILITY",
                "passed" if result.passed else "failed",
                (
                    "Attempt-scoped GCS operations matched the accepted boundary."
                    if result.passed
                    else "One or more attempt-scoped GCS operations were unsafe."
                ),
                checks=[check.as_dict() for check in result.checks],
                ambient_storage_access_verified=True,
            )
        except Exception as exc:
            report.add(
                "VA-STORAGE-CAPABILITY",
                "failed",
                "The live attempt-scoped GCS probe could not complete.",
                error=_safe_error(exc),
            )

    def _launch_backend(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
    ):
        """Launch one backend burst without serialising other backends behind it."""
        launched = []
        for sequence in range(1, self.attempts_per_backend + 1):
            try:
                run = self._launch(scenario, report.acceptance_id)
                launched.append((sequence, run))
            except Exception as exc:
                report.add(
                    f"VA-{scenario.backend.key.upper()}-SMOKE-{sequence:02d}",
                    "failed",
                    "The acceptance run could not be launched.",
                    error=_safe_error(exc),
                )
        return launched

    def _wait_for_runs(self, launched_by_backend) -> None:
        """Wait once for all four concurrently launched backend bursts."""
        deadline = time.monotonic() + self.timeout_seconds
        pending = {
            str(run.pk): run
            for launched in launched_by_backend.values()
            for _sequence, run in launched
        }
        while pending and time.monotonic() < deadline:
            for run_id, run in list(pending.items()):
                run.refresh_from_db()
                if run.status in VALIDATION_RUN_TERMINAL_STATUSES:
                    pending.pop(run_id)
            if pending:
                time.sleep(self.poll_interval_seconds)

    def _record_latency(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
        launched,
    ) -> None:
        """Measure this exact burst without blending revisions or old runs."""
        provider_start_samples = []
        provider_total_samples = []
        revisions = set()
        minimum_instances = set()
        for _sequence, run in launched:
            attempt = (
                ExecutionAttempt.objects.filter(step_run__validation_run=run)
                .select_related("deployment")
                .order_by("attempt_number")
                .last()
            )
            if attempt is None or attempt.deployment is None:
                continue
            revisions.add(attempt.deployment.deployment_revision)
            minimum_instances.add(attempt.deployment.minimum_instances)
            if (
                attempt.provider_accepted_at
                and attempt.provider_started_at
                and attempt.provider_started_at >= attempt.provider_accepted_at
            ):
                provider_start_samples.append(
                    (
                        attempt.provider_started_at - attempt.provider_accepted_at
                    ).total_seconds()
                )
            if (
                attempt.provider_accepted_at
                and attempt.callback_received_at
                and attempt.callback_received_at >= attempt.provider_accepted_at
            ):
                provider_total_samples.append(
                    (
                        attempt.callback_received_at - attempt.provider_accepted_at
                    ).total_seconds()
                )

        details = {
            "samples": len(provider_start_samples),
            "required_samples": self.attempts_per_backend,
            "representative_sample": (
                self.attempts_per_backend >= REPRESENTATIVE_SAMPLE_SIZE
            ),
            "provider_start_target_seconds": (
                scenario.backend.provider_start_target_seconds
            ),
            "deployment_revisions": sorted(revisions),
            "service_minimum_instances": sorted(minimum_instances),
        }
        failure = ""
        if len(provider_start_samples) != self.attempts_per_backend:
            failure = "one or more provider-start samples is missing"
        elif len(provider_total_samples) != self.attempts_per_backend:
            failure = "one or more provider-total samples is missing"
        elif len(revisions) != 1:
            failure = "the burst did not use exactly one immutable revision"
        else:
            start_p50 = _percentile(provider_start_samples, 50)
            start_p95 = _percentile(provider_start_samples, 95)
            total_p50 = _percentile(provider_total_samples, 50)
            total_p95 = _percentile(provider_total_samples, 95)
            details.update(
                {
                    "provider_start_p50_seconds": start_p50,
                    "provider_start_p95_seconds": start_p95,
                    "provider_total_p50_seconds": total_p50,
                    "provider_total_p95_seconds": total_p95,
                }
            )
            if start_p95 > scenario.backend.provider_start_target_seconds:
                failure = "provider-start p95 exceeded its recorded target"

        check_id = f"VA-{scenario.backend.key.upper()}-LATENCY"
        if failure:
            report.add(
                check_id,
                "failed",
                "The exact acceptance burst missed its latency evidence gate.",
                error=failure,
                **details,
            )
        else:
            report.add(
                check_id,
                "passed",
                "The exact acceptance burst met its provider-start target.",
                **details,
            )

    def _launch(self, scenario: AcceptanceScenario, acceptance_id: str):
        """Create a fresh submission and use the normal application launcher."""
        submission = Submission(
            name=f"{acceptance_id}: {scenario.backend.key}",
            org=scenario.workflow.org,
            project=scenario.workflow.project,
            user=scenario.workflow.user,
            workflow=scenario.workflow,
            metadata={
                "validator_acceptance_id": acceptance_id,
                "fixture_sha256": scenario.fixture_sha256,
            },
        )
        submission.set_content(
            inline_text=scenario.inline_text,
            filename=scenario.filename,
            file_type=scenario.file_type,
        )
        submission.save()
        request = HttpRequest()
        request.method = "POST"
        request.user = scenario.workflow.user
        response = ValidationRunService().launch(
            request=request,
            org=scenario.workflow.org,
            workflow=scenario.workflow,
            submission=submission,
            user_id=scenario.workflow.user_id,
            metadata={"validator_acceptance_id": acceptance_id},
            source=ValidationRunSource.SCHEDULE,
        )
        return response.validation_run

    def _record_run(
        self,
        report: AcceptanceReport,
        scenario: AcceptanceScenario,
        sequence: int,
        run,
    ) -> None:
        """Verify the terminal run used the exact accepted Service release."""
        check_id = f"VA-{scenario.backend.key.upper()}-SMOKE-{sequence:02d}"
        attempt = (
            ExecutionAttempt.objects.filter(step_run__validation_run=run)
            .select_related("deployment")
            .order_by("attempt_number")
            .last()
        )
        details: dict[str, Any] = {
            "run_id": str(run.pk),
            "run_status": run.status,
            "fixture_sha256": scenario.fixture_sha256,
        }
        failure = ""
        if run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
            failure = f"run did not finish within {self.timeout_seconds} seconds"
        elif run.status != ValidationRunStatus.SUCCEEDED:
            failure = f"run finished as {run.status}"
        elif attempt is None:
            failure = "run has no durable execution attempt"
        else:
            deployment = attempt.deployment
            deployment_snapshot = (
                attempt.deployment_snapshot
                if isinstance(attempt.deployment_snapshot, dict)
                else {}
            )
            details.update(
                {
                    "attempt_id": str(attempt.pk),
                    "attempt_state": attempt.state,
                    "deployment_id": str(attempt.deployment_id or ""),
                    "deployment_kind": (
                        deployment.deployment_kind if deployment is not None else ""
                    ),
                    "deployment_revision": (
                        deployment.deployment_revision if deployment is not None else ""
                    ),
                    "backend_image_digest": attempt.backend_image_digest,
                    "deployment_snapshot_revision": deployment_snapshot.get(
                        "deployment_revision",
                        "",
                    ),
                    "input_envelope_sha256": attempt.input_envelope_sha256,
                    "input_evidence_item_count": len(
                        attempt.input_evidence_snapshot.get("files", [])
                        if isinstance(attempt.input_evidence_snapshot, dict)
                        else []
                    ),
                    "output_envelope_sha256": attempt.output_envelope_sha256,
                    "provider_accepted_at": _iso(attempt.provider_accepted_at),
                    "provider_started_at": _iso(attempt.provider_started_at),
                    "provider_finished_at": _iso(attempt.provider_finished_at),
                    "callback_received_at": _iso(attempt.callback_received_at),
                }
            )
            if deployment is None:
                failure = "attempt has no pinned managed deployment"
            elif attempt.state != ExecutionAttemptState.COMPLETED:
                failure = f"attempt finished in unexpected state {attempt.state}"
            elif (
                deployment.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE
            ):
                failure = "attempt did not use the candidate Cloud Run Service"
            elif deployment.backend_release_identity != self.release_tag.removeprefix(
                "v"
            ):
                failure = "attempt used a different backend release"
            elif deployment_snapshot.get("deployment_id") != str(deployment.pk):
                failure = "attempt snapshot names a different deployment"
            elif (
                deployment_snapshot.get("deployment_revision")
                != deployment.deployment_revision
            ):
                failure = "attempt snapshot names a different Service revision"
            elif (
                deployment_snapshot.get("backend_image_digest")
                != deployment.backend_image_digest
            ):
                failure = "attempt snapshot names a different backend image"
            elif attempt.backend_image_digest != deployment.backend_image_digest:
                failure = "attempt observed a different backend image"
            elif not attempt.input_envelope_sha256:
                failure = "attempt did not retain an input-envelope digest"
            elif not attempt.input_evidence_snapshot:
                failure = "attempt did not retain immutable input evidence"
            elif not attempt.output_envelope_sha256:
                failure = "attempt did not retain an output-envelope digest"
            elif attempt.provider_accepted_at is None:
                failure = "provider acceptance time was not recorded"
            elif attempt.provider_started_at is None:
                failure = "provider start time was not recorded"
            elif attempt.provider_finished_at is None:
                failure = "provider finish time was not recorded"
            elif attempt.callback_received_at is None:
                failure = "authenticated callback time was not recorded"

        if failure:
            details["error"] = failure
            if run.error:
                details["run_error"] = str(run.error)[:500]
            report.add(
                check_id,
                "failed",
                "End-to-end validator canary failed.",
                **details,
            )
        else:
            report.add(
                check_id,
                "passed",
                "End-to-end validator canary completed on the candidate Service.",
                **details,
            )


def persist_acceptance_report(report: dict[str, Any]) -> dict[str, str] | None:
    """Create one immutable private GCS report and return its identity."""
    bucket_name = str(getattr(settings, "GCS_VALIDATION_BUCKET", "") or "")
    project_id = str(getattr(settings, "GCP_PROJECT_ID", "") or "")
    if not bucket_name or not project_id:
        return None
    acceptance_id = str(report["acceptance_id"])
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    date = timezone.now().date().isoformat()
    object_name = f"operations/validator-acceptance/{date}/{acceptance_id}.json"
    client = storage.Client(project=project_id)
    blob = client.bucket(bucket_name).blob(object_name)
    blob.upload_from_string(
        canonical,
        content_type="application/json",
        if_generation_match=0,
    )
    return {
        "uri": f"gs://{bucket_name}/{object_name}",
        "sha256": digest,
    }


def _sha256_text(value: str) -> str:
    """Return the fixture identity used in reports and submission metadata."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _percentile(values: list[float], percentile: int) -> float:
    """Return a deterministic nearest-rank percentile rounded for evidence."""
    ordered = sorted(values)
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return round(ordered[rank - 1], 3)


def _iso(value) -> str | None:
    """Render optional datetimes consistently in JSON details."""
    return value.isoformat() if value is not None else None


def _safe_error(exc: Exception) -> str:
    """Bound diagnostic text so reports cannot become log or secret dumps."""
    return f"{type(exc).__name__}: {str(exc)[:400]}"
