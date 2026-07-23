"""Tests for Portfolio Manager authoring and Django-side orchestration.

The isolated backend owns untrusted XLS/XLSX/XML/ZIP parsing, while the
community application owns workflow configuration, the versioned EBL resource,
catalog outputs, and execution routing. These tests pin that boundary so the
convenience form cannot drift from the shared envelope contract.
"""

import hashlib
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from validibot_shared.portfolio_manager import PortfolioManagerInputEnvelope
from validibot_shared.portfolio_manager import PortfolioManagerOutputs
from validibot_shared.portfolio_manager import PortfolioManagerPropertyResult

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import PORTFOLIO_MANAGER_EBL_RESOURCE
from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.models import StepInputBinding
from validibot.validations.models import Validator
from validibot.validations.services.cloud_run.envelope_builder import (
    build_input_envelope,
)
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.portfolio_manager.validator import (
    PortfolioManagerValidator,
)
from validibot.workflows.forms import PortfolioManagerStepConfigForm
from validibot.workflows.forms import get_config_form_class
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory

EBL_JSON = b"""{
  "schema_version": "1.0",
  "id_field": {
    "kind": "standard_id",
    "name": "State of Washington Clean Buildings Standard"
  },
  "euit_unit": "kBtu/ft2/year",
  "buildings": [
    {"id_value": "WA-001", "euit": "40.0"},
    {"id_value": "WA-002"}
  ]
}"""
WASHINGTON_MAXIMUM_REPORT_AGE_MONTHS = 24
SHA256_HEX_LENGTH = 64
CONFIGURED_ARCHIVE_MEMBER_LIMIT = 75
CONFIGURED_MEMBER_BYTES = 15_000_000
MEASURED_WNEUI = 39.5
RESOLVED_EUIT = 40.0
BOUND_EUIT = 42.0


def _base_form_data(**overrides):
    """Return a minimally explicit browser-shaped Portfolio Manager form."""
    data = {
        "name": "Check Portfolio Manager report",
        "submission_structure": "single_report",
        "profile": "generic",
        "near_target_percent": "5",
        "minimum_reporting_period_months": "12",
    }
    data.update(overrides)
    return data


def _sync_system_validators() -> None:
    """Create the exact validator catalog that production startup uses."""
    call_command(
        "sync_validators",
        stdout=StringIO(),
        stderr=StringIO(),
    )


def _file_identity(uri: str, content: bytes) -> FileIdentity:
    """Build a local immutable file identity for an envelope assertion."""
    digest = hashlib.sha256(content).hexdigest()
    return FileIdentity(
        uri=uri,
        size_bytes=len(content),
        sha256=digest,
        storage_version=f"sha256:{digest}",
    )


def test_registry_exposes_a_dedicated_advanced_backend_contract() -> None:
    """The validator must route through isolation rather than TabularValidator."""
    config = get_config(ValidationType.PORTFOLIO_MANAGER)

    assert config is not None
    assert config.slug == "portfolio-manager-validator"
    assert config.image_name == "validibot-validator-backend-portfolio-manager"
    assert ValidationType.PORTFOLIO_MANAGER in ADVANCED_VALIDATION_TYPES
    assert get_config_form_class(ValidationType.PORTFOLIO_MANAGER) is (
        PortfolioManagerStepConfigForm
    )


def test_washington_profile_expands_visible_domain_checks() -> None:
    """The Washington choice applies period, identity, metric, and alert policy."""
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(profile="washington_cbps_tier1_euit"),
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["require_complete_reporting_period"] is True
    assert (
        form.cleaned_data["maximum_reporting_period_age_months"]
        == WASHINGTON_MAXIMUM_REPORT_AGE_MONTHS
    )
    assert form.cleaned_data["require_form_c_ready"] is True
    assert form.cleaned_data["require_washington_standard_id"] is True
    assert form.cleaned_data["meter_gap_policy"] == "error"
    assert form.cleaned_data["estimated_energy_policy"] == "warning"


def test_ebl_upload_rejects_duplicate_json_keys() -> None:
    """Duplicate JSON keys cannot silently replace roster or target evidence."""
    duplicate = EBL_JSON.replace(
        b'"schema_version": "1.0",',
        b'"schema_version": "1.0", "schema_version": "1.0",',
    )
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(submission_structure="zip_collection"),
        files={
            "expected_buildings_list": SimpleUploadedFile(
                "buildings.json",
                duplicate,
                content_type="application/json",
            )
        },
    )

    assert not form.is_valid()
    assert "duplicate JSON key" in str(form.errors["expected_buildings_list"])


@pytest.mark.django_db
def test_saving_zip_configuration_persists_a_hashed_ebl_resource() -> None:
    """A valid roster is stored as a step-owned resource, not config JSON."""
    from validibot.validations.tests.factories import ValidatorFactory
    from validibot.workflows.tests.factories import WorkflowFactory
    from validibot.workflows.views_helpers import save_workflow_step

    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        slug="portfolio-manager-validator",
        validation_type=ValidationType.PORTFOLIO_MANAGER,
        is_system=True,
        supports_assertions=True,
    )
    default_euit_input = StepIODefinitionFactory(
        validator=validator,
        contract_key="default_euit_kbtu_ft2_yr",
        native_name="default_euit_kbtu_ft2_yr",
        direction=StepIODirection.INPUT,
        origin_kind=StepIOOriginKind.CATALOG,
        data_type=CatalogValueType.NUMBER,
        io_medium=StepIOMedium.VALUE,
    )
    form = PortfolioManagerStepConfigForm(
        data=_base_form_data(
            submission_structure="zip_collection",
            default_euit_kbtu_ft2_yr="42",
            max_archive_members="75",
            max_member_size_mb="15",
            max_uncompressed_size_mb="200",
        ),
        files={
            "expected_buildings_list": SimpleUploadedFile(
                "buildings.json",
                EBL_JSON,
                content_type="application/json",
            )
        },
        validator=validator,
        workflow=workflow,
    )
    assert form.is_valid(), form.errors

    step = save_workflow_step(workflow, validator, form)

    resource = step.step_resources.get(
        role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST
    )
    assert resource.resource_type == PORTFOLIO_MANAGER_EBL_RESOURCE
    assert len(resource.content_hash) == SHA256_HEX_LENGTH
    assert "expected_buildings_list" not in step.config
    assert step.config["max_archive_members"] == CONFIGURED_ARCHIVE_MEMBER_LIMIT
    assert step.config["max_member_bytes"] == CONFIGURED_MEMBER_BYTES
    target_binding = StepInputBinding.objects.get(
        workflow_step=step,
        io_definition=default_euit_input,
    )
    assert target_binding.source_scope == BindingSourceScope.CONSTANT
    assert target_binding.default_value == BOUND_EUIT


@pytest.mark.django_db
def test_synced_catalog_creates_portfolio_manager_input_bindings() -> None:
    """The report, EBL, and EUIt inputs must receive their intended scopes."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(validator=validator)

    ensure_step_input_bindings(step)

    bindings = {
        binding.io_definition.contract_key: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step,
        ).select_related("io_definition")
    }
    assert bindings["portfolio_manager_report"].source_scope == (
        BindingSourceScope.SUBMISSION_FILE
    )
    assert bindings["expected_buildings_list"].source_scope == (
        BindingSourceScope.WORKFLOW_RESOURCE
    )
    assert bindings["expected_buildings_list"].is_required is False
    assert bindings["default_euit_kbtu_ft2_yr"].source_scope == (
        BindingSourceScope.CONSTANT
    )
    assert bindings["default_euit_kbtu_ft2_yr"].is_required is False


@pytest.mark.django_db
def test_zip_envelope_materializes_report_ebl_and_resolved_inputs() -> None:
    """The app must send one strict, traceable contract to the isolated backend."""
    _sync_system_validators()
    validator = Validator.objects.get(slug="portfolio-manager-validator")
    step = WorkflowStepFactory(
        validator=validator,
        config={
            "submission_structure": "zip_collection",
            "profile": "washington_cbps_tier1_euit",
            "default_euit_kbtu_ft2_yr": 41,
            "compare_to_euit": True,
            "near_target_percent": 5,
        },
    )
    ebl_resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.EXPECTED_BUILDINGS_LIST,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile(
            "expected-buildings.json",
            EBL_JSON,
            content_type="application/json",
        ),
        filename="expected-buildings.json",
        resource_type=PORTFOLIO_MANAGER_EBL_RESOURCE,
    )
    ensure_step_input_bindings(step)

    report_bytes = b"PK synthetic zip identity"
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        project=step.workflow.project,
        user=step.workflow.user,
        content="placeholder",
        file_type=SubmissionFileType.BINARY,
        original_filename="portfolio.zip",
        size_bytes=len(report_bytes),
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    step_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        input_values={"default_euit_kbtu_ft2_yr": BOUND_EUIT},
    )
    ExecutionAttemptFactory(step_run=step_run)
    report_identity = _file_identity(
        "file:///validibot/input/portfolio.zip",
        report_bytes,
    )
    ebl_identity = _file_identity(
        "file:///validibot/input/resources/expected-buildings.json",
        EBL_JSON,
    )

    envelope = build_input_envelope(
        run,
        callback_url="http://localhost/callback/",
        callback_id=None,
        execution_bundle_uri="file:///validibot/output",
        skip_callback=True,
        input_file_uris={"primary_file_uri": report_identity},
        resource_uri_overrides={str(ebl_resource.pk): ebl_identity},
    )

    assert isinstance(envelope, PortfolioManagerInputEnvelope)
    assert envelope.input_files[0].port_key == "portfolio_manager_report"
    assert envelope.input_files[0].name == "portfolio.zip"
    assert envelope.resource_files[0].port_key == "expected_buildings_list"
    assert envelope.resource_files[0].sha256 == ebl_identity.sha256
    assert float(envelope.inputs.default_euit_kbtu_ft2_yr) == BOUND_EUIT
    assert envelope.inputs.max_input_bytes == (PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES)
    assert envelope.inputs.require_form_c_ready is True

    traces = {
        trace.input_contract_key: trace
        for trace in ResolvedInputTrace.objects.filter(step_run=step_run)
    }
    assert traces["portfolio_manager_report"].resolved is True
    assert traces["expected_buildings_list"].resolved is True


def test_preflight_enforces_the_author_selected_submission_shape() -> None:
    """Single mode refuses a ZIP before container compute is allocated."""
    validator = PortfolioManagerValidator()
    step = SimpleNamespace(config={"submission_structure": "single_report"})
    submission = SimpleNamespace(
        original_filename="portfolio.zip",
        size_bytes=100,
    )

    with pytest.raises(ValidationError, match="Single-report mode"):
        validator.preprocess_submission(step=step, submission=submission)


def test_single_property_outputs_are_available_to_cel() -> None:
    """Measured WNEUI and resolved EUIt are projected onto the scalar catalog."""
    outputs = PortfolioManagerOutputs(
        submission_structure="single_report",
        profile="generic",
        file_count=1,
        valid_file_count=1,
        invalid_file_count=0,
        property_count=1,
        reporting_cycle_count=1,
        reporting_cycles_match=True,
        property_results=[
            PortfolioManagerPropertyResult(
                member_name="report.xlsx",
                carrier="xlsx",
                property_id="123",
                weather_normalized_site_eui_kbtu_ft2_yr="39.5",
                resolved_euit_kbtu_ft2_yr="40",
                resolved_euit_source="default",
                meets_euit=True,
            )
        ],
    )

    values = PortfolioManagerValidator().extract_output_values(
        SimpleNamespace(outputs=outputs)
    )

    assert values["weather_normalized_site_eui_kbtu_ft2_yr"] == MEASURED_WNEUI
    assert values["resolved_euit_kbtu_ft2_yr"] == RESOLVED_EUIT
    assert values["meets_euit"] is True
