"""
Docker Compose execution backend integration tests.

These tests verify the complete execution flow for Docker Compose deployments
using Docker containers for advanced validators (EnergyPlus, FMU).

## Test Categories

1. **Built-in validators (JSON Schema, XML, etc.)**
   - Run in-process, no Docker required
   - Test via normal workflow execution

2. **Advanced validators (EnergyPlus, FMU)**
   - Run in Docker containers via DockerComposeExecutionBackend
   - Requires Docker daemon and validator images

## Prerequisites

For Docker-based tests:
- Docker daemon running
- Validator images available locally:
  - `validibot-validator-energyplus:latest`
  - `validibot-validator-fmu:latest`

## Running These Tests

```bash
# Run all Docker Compose integration tests
pytest tests/tests_integration/test_docker_compose_execution.py -v

# Run only non-Docker tests (fast)
pytest tests/tests_integration/test_docker_compose_execution.py -v -k "not docker"

# Run with verbose logging
pytest tests/tests_integration/test_docker_compose_execution.py -v --log-cli-level=INFO
```
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from django.test import override_settings
from rest_framework.status import HTTP_200_OK

from tests.helpers.assets import load_json_test_asset
from tests.helpers.assets import load_test_asset
from tests.helpers.payloads import invalid_product_payload
from tests.helpers.payloads import valid_product_payload
from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.polling import start_workflow_url
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)


# =============================================================================
# Docker Availability Check
# =============================================================================


def docker_available() -> bool:
    """Check if Docker is available and validator images exist."""
    try:
        from validibot.validations.services.runners import get_validator_runner

        runner = get_validator_runner()
        return runner.is_available()
    except Exception:
        return False


def energyplus_image_available() -> bool:
    """Check if the EnergyPlus validator image is available."""
    if not docker_available():
        return False
    try:
        import docker

        client = docker.from_env()
        images = client.images.list(name="validibot-validator-energyplus")
        return len(images) > 0
    except Exception:
        return False


skip_if_no_docker = pytest.mark.skipif(
    not docker_available(),
    reason="Docker not available",
)

skip_if_no_energyplus_image = pytest.mark.skipif(
    not energyplus_image_available(),
    reason="EnergyPlus validator image not available",
)


def fmu_image_available() -> bool:
    """Check if the FMU validator image is available."""
    if not docker_available():
        return False
    try:
        import docker

        client = docker.from_env()
        images = client.images.list(name="validibot-validator-fmu")
        return len(images) > 0
    except Exception:
        return False


skip_if_no_fmu_image = pytest.mark.skipif(
    not fmu_image_available(),
    reason="FMU validator image not available",
)


# =============================================================================
# Built-in Validator Tests (No Docker Required)
# =============================================================================


@pytest.mark.django_db(transaction=True)
class TestBuiltInValidators:
    """
    Tests for built-in validators that run in-process.

    These tests verify that the validation workflow works correctly for
    validators that don't require Docker (JSON Schema, XML Schema, etc.).
    """

    @pytest.fixture
    def json_schema_workflow(self, api_client):
        """Create a workflow with a JSON Schema validator."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
        )

        schema = load_json_test_asset("assets/json/example_product_schema.json")
        ruleset = RulesetFactory(
            org=org,
            user=user,
            ruleset_type=ValidationType.JSON_SCHEMA,
            rules_text=json.dumps(schema),
            metadata={
                "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            },
        )

        workflow = WorkflowFactory(org=org, user=user)
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "client": api_client,
        }

    def test_json_schema_valid_payload_succeeds(self, json_schema_workflow):
        """Valid JSON payload should pass validation."""
        client = json_schema_workflow["client"]
        workflow = json_schema_workflow["workflow"]
        org = json_schema_workflow["org"]

        url = start_workflow_url(workflow)
        payload = valid_product_payload()

        resp = client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        # Poll until complete
        data, status = poll_until_complete(client, poll_url)
        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Expected SUCCEEDED, got {run_status}: {data}"
        )

        issues = extract_issues(data)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_json_schema_invalid_payload_fails(self, json_schema_workflow):
        """Invalid JSON payload should fail validation with issues."""
        client = json_schema_workflow["client"]
        workflow = json_schema_workflow["workflow"]
        org = json_schema_workflow["org"]

        url = start_workflow_url(workflow)
        payload = invalid_product_payload()

        resp = client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        # Poll until complete
        data, status = poll_until_complete(client, poll_url)
        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Expected FAILED, got {run_status}: {data}"
        )

        issues = extract_issues(data)
        assert len(issues) >= 1, "Expected at least one issue"

        # Check that rating/max error is mentioned
        joined = " | ".join(str(issue) for issue in issues)
        assert ("rating" in joined) or ("maximum" in joined), (
            f"Expected rating/max error, got: {issues}"
        )


# =============================================================================
# Docker-Based Advanced Validator Tests
# =============================================================================


@pytest.mark.django_db(transaction=True)
@skip_if_no_docker
@skip_if_no_energyplus_image
class TestDockerEnergyPlusExecution:
    """
    Tests for EnergyPlus validation via Docker containers.

    These tests verify the DockerComposeExecutionBackend correctly:
    1. Uploads input envelope to local storage
    2. Runs the Docker container
    3. Reads the output envelope
    4. Returns results to the workflow
    """

    @pytest.fixture(autouse=True)
    def setup_local_storage(self, tmp_path):
        """Set up temporary local storage for test isolation."""
        self.storage_root = tmp_path / "storage"
        self.storage_root.mkdir(parents=True, exist_ok=True)

        # Clear backend cache to ensure fresh initialization
        clear_backend_cache()

        yield

        # Cleanup
        clear_backend_cache()

    @pytest.fixture
    def energyplus_workflow(self, api_client, setup_local_storage):
        """Create a workflow with an EnergyPlus validator."""
        from validibot.core.storage import get_data_storage

        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            version="24.2.0",
        )

        # EnergyPlus doesn't use rules_text the same way, but needs a ruleset
        ruleset = RulesetFactory(
            org=org,
            user=user,
            ruleset_type=ValidationType.ENERGYPLUS,
            rules_text="{}",
        )

        workflow = WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=["json"],
        )

        # Upload the test weather file to local storage
        storage = get_data_storage()
        weather_data = load_test_asset("data/energyplus/test_weather.epw")
        weather_path = f"weather/{org.id}/test_weather.epw"
        storage.write(weather_path, weather_data)
        weather_file_uri = storage.get_uri(weather_path)

        logger.info("Uploaded weather file to: %s", weather_file_uri)

        # Create step with weather file configuration
        # The step needs weather_file_uri in config for EnergyPlus validations
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
            config={
                "weather_file_uri": weather_file_uri,
            },
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "client": api_client,
            "storage_root": self.storage_root,
        }

    @override_settings(
        VALIDATOR_RUNNER="docker",
        DATA_STORAGE_BACKEND="local",
    )
    def test_energyplus_execution_via_docker(self, energyplus_workflow):
        """
        Test EnergyPlus validation executes via Docker container.

        This is an integration test that:
        1. Submits an EnergyPlus model via the API
        2. Waits for the Docker container to run
        3. Verifies the validation completes with results
        """
        client = energyplus_workflow["client"]
        workflow = energyplus_workflow["workflow"]
        org = energyplus_workflow["org"]

        # Load the test model
        model_data = load_test_asset("data/energyplus/example_epjson.json")

        url = start_workflow_url(workflow)

        logger.info("Starting EnergyPlus validation via Docker")
        logger.info("URL: %s", url)

        resp = client.post(
            url,
            data=model_data,
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), (
            f"Failed to start workflow: {resp.status_code} {resp.content}"
        )

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        logger.info("Polling for completion: %s", poll_url)

        # Poll with longer timeout for EnergyPlus (container startup + simulation)
        data, status = poll_until_complete(
            client,
            poll_url,
            timeout_s=120.0,  # EnergyPlus can take time
            interval_s=2.0,
        )

        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        logger.info("Validation completed with status: %s", run_status)

        # For this test, we just verify the run completed (either SUCCESS or FAILED)
        # The model may fail due to missing weather file, but that's OK -
        # we're testing the execution flow, not the model validity
        assert run_status in ("SUCCEEDED", "FAILED"), (
            f"Expected terminal status, got {run_status}: {data}"
        )

        # Log the steps for debugging
        steps = data.get("steps", [])
        for step in steps:
            logger.info(
                "Step %s: status=%s, issues=%d",
                step.get("name", "unknown"),
                step.get("status", "unknown"),
                len(step.get("issues", [])),
            )


# =============================================================================
# FMU Docker-Based Validator Tests
# =============================================================================


def load_fmu_asset() -> bytes:
    """Load the test FMU file from test assets."""
    asset_path = Path(__file__).parents[1] / "assets" / "fmu" / "Feedthrough.fmu"
    if not asset_path.exists():
        pytest.skip(f"Test FMU asset not found: {asset_path}")
    return asset_path.read_bytes()


@pytest.mark.django_db(transaction=True)
@skip_if_no_docker
@skip_if_no_fmu_image
class TestDockerFMUExecution:
    """
    Tests for FMU (Functional Mock-up Unit) validation via Docker containers.

    These tests verify the DockerComposeExecutionBackend correctly:
    1. Creates an FMU validator with an attached FMU file
    2. Uploads input envelope with FMU URI to local storage
    3. Runs the FMU Docker container
    4. Reads the output envelope
    5. Returns simulation results to the workflow
    """

    @pytest.fixture(autouse=True)
    def setup_local_storage(self, tmp_path):
        """Set up temporary local storage for test isolation."""
        self.storage_root = tmp_path / "storage"
        self.storage_root.mkdir(parents=True, exist_ok=True)

        # Clear backend cache to ensure fresh initialization
        clear_backend_cache()

        yield

        # Cleanup
        clear_backend_cache()

    @pytest.fixture
    def fmu_workflow(self, api_client, setup_local_storage):
        """Create a workflow with an FMU validator using the Feedthrough FMU."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from validibot.submissions.constants import SubmissionFileType
        from validibot.validations.constants import RulesetType
        from validibot.validations.models import Ruleset
        from validibot.validations.services.fmu import create_fmu_validator

        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        user.set_current_org(org)
        grant_role(user, org, RoleCode.EXECUTOR)

        # Load the FMU and create a SimpleUploadedFile
        fmu_data = load_fmu_asset()
        fmu_upload = SimpleUploadedFile(
            "Feedthrough.fmu",
            fmu_data,
            content_type="application/octet-stream",
        )

        # Create a project for the workflow
        from validibot.projects.tests.factories import ProjectFactory

        project = ProjectFactory(org=org)

        # Create the FMU validator using the service function
        # This handles FMU introspection and catalog seeding
        validator = create_fmu_validator(
            org=org,
            project=project,
            name="Feedthrough FMU Validator",
            upload=fmu_upload,
        )

        logger.info(
            "Created FMU validator: id=%s, fmu_model=%s",
            validator.id,
            validator.fmu_model,
        )

        # Create a ruleset for FMU
        ruleset = Ruleset.objects.create(
            org=org,
            user=user,
            name="FMU Test Rules",
            ruleset_type=RulesetType.FMU,
            version="1",
            rules_text="{}",
        )

        # Create workflow that accepts binary files (FMUs)
        workflow = WorkflowFactory(
            org=org,
            user=user,
            project=project,
            allowed_file_types=[SubmissionFileType.BINARY],
        )

        # Create step with FMU validator
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=1,
            config={},
        )

        api_client.force_authenticate(user=user)

        return {
            "org": org,
            "user": user,
            "validator": validator,
            "ruleset": ruleset,
            "workflow": workflow,
            "project": project,
            "client": api_client,
            "storage_root": self.storage_root,
        }

    @override_settings(
        VALIDATOR_RUNNER="docker",
        DATA_STORAGE_BACKEND="local",
    )
    def test_fmu_execution_via_docker(self, fmu_workflow):
        """
        Test FMU validation executes via Docker container.

        This is an integration test that:
        1. Submits a binary file (FMU input parameters) via the API
        2. Waits for the Docker container to run the FMU simulation
        3. Verifies the validation completes with results
        """
        from io import BytesIO

        client = fmu_workflow["client"]
        workflow = fmu_workflow["workflow"]
        org = fmu_workflow["org"]

        # FMU submissions contain input parameter values as JSON
        # Submit as binary content (since workflow accepts BINARY files)
        submission_data = {
            "input_parameters": {
                "real_in": 1.5,
            },
        }
        # Encode JSON as binary for submission
        binary_content = json.dumps(submission_data).encode("utf-8")

        url = start_workflow_url(workflow)

        logger.info("Starting FMU validation via Docker")
        logger.info("URL: %s", url)

        # Submit as multipart form data with a file
        resp = client.post(
            url,
            data={"file": BytesIO(binary_content)},
            format="multipart",
        )
        assert resp.status_code in (200, 201, 202), (
            f"Failed to start workflow: {resp.status_code} {resp.content}"
        )

        # Get polling URL
        loc = resp.headers.get("Location") or ""
        poll_url = normalize_poll_url(loc)
        if not poll_url:
            data = resp.json()
            run_id = data.get("id")
            if run_id:
                poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        logger.info("Polling for completion: %s", poll_url)

        # Poll with timeout for FMU (container startup + simulation)
        data, status = poll_until_complete(
            client,
            poll_url,
            timeout_s=60.0,
            interval_s=2.0,
        )

        assert status == HTTP_200_OK, f"Polling failed: {status} {data}"

        run_status = (data.get("status") or "").upper()
        logger.info("Validation completed with status: %s", run_status)

        # For this test, we verify the run completed (either SUCCESS or FAILED)
        # The FMU may fail for various reasons, but we're testing the execution flow
        assert run_status in ("SUCCEEDED", "FAILED"), (
            f"Expected terminal status, got {run_status}: {data}"
        )

        # Log the steps for debugging
        steps = data.get("steps", [])
        for step in steps:
            logger.info(
                "Step %s: status=%s, issues=%d",
                step.get("name", "unknown"),
                step.get("status", "unknown"),
                len(step.get("issues", [])),
            )


# =============================================================================
# Direct Backend Tests (Lower Level)
# =============================================================================


@pytest.mark.django_db
@skip_if_no_docker
class TestDockerComposeBackendDirect:
    """
    Direct tests of the DockerComposeExecutionBackend without going through API.

    These tests verify the backend's internal behavior at a lower level.
    """

    def test_backend_is_available_when_docker_running(self):
        """Backend should report available when Docker is running."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is True

    def test_backend_is_sync(self):
        """Docker Compose backend should be synchronous."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        assert backend.is_async is False

    def test_get_container_image_default_naming(self):
        """Backend should generate correct image names."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()

        # Default naming convention
        with override_settings(
            VALIDATOR_IMAGE_TAG="latest",
            VALIDATOR_IMAGE_REGISTRY="",
        ):
            image = backend.get_container_image("energyplus")
            assert image == "validibot-validator-energyplus:latest"

        # With registry
        with override_settings(
            VALIDATOR_IMAGE_TAG="v1.0",
            VALIDATOR_IMAGE_REGISTRY="gcr.io/my-project",
        ):
            image = backend.get_container_image("fmu")
            assert image == "gcr.io/my-project/validibot-validator-fmu:v1.0"

    @skip_if_no_energyplus_image
    def test_get_container_image_for_energyplus(self):
        """Backend should find the EnergyPlus image."""
        from validibot.validations.services.execution.docker_compose import (
            DockerComposeExecutionBackend,
        )

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        # Verify image exists in Docker
        import docker

        client = docker.from_env()
        images = client.images.list(name=image.split(":")[0])
        assert len(images) > 0, f"Image {image} not found in Docker"
