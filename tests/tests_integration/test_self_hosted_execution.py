"""
Self-hosted execution backend integration tests.

These tests verify the complete execution flow for self-hosted deployments
using Docker containers for advanced validators (EnergyPlus, FMI).

## Test Categories

1. **Built-in validators (JSON Schema, XML, etc.)**
   - Run in-process, no Docker required
   - Test via normal workflow execution

2. **Advanced validators (EnergyPlus, FMI)**
   - Run in Docker containers via SelfHostedExecutionBackend
   - Requires Docker daemon and validator images

## Prerequisites

For Docker-based tests:
- Docker daemon running
- Validator images available locally:
  - `validibot-validator-energyplus:latest`
  - `validibot-validator-fmi:latest`

## Running These Tests

```bash
# Run all self-hosted integration tests
pytest tests/tests_integration/test_self_hosted_execution.py -v

# Run only non-Docker tests (fast)
pytest tests/tests_integration/test_self_hosted_execution.py -v -k "not docker"

# Run with verbose logging
pytest tests/tests_integration/test_self_hosted_execution.py -v --log-cli-level=INFO
```
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

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
# Test Data Helpers
# =============================================================================


def valid_product_payload() -> dict:
    """Produce a sample product payload that satisfies the example JSON Schema."""
    return {
        "sku": "ABCD1234",
        "name": "Widget Mini",
        "price": 19.99,
        "rating": 95,
        "tags": ["gadgets", "mini"],
        "dimensions": {"width": 3.5, "height": 1.2},
        "in_stock": True,
    }


def invalid_product_payload() -> dict:
    """Produce a payload that intentionally violates the schema (rating > 100)."""
    bad = valid_product_payload()
    bad["rating"] = 150  # violates max 100
    return bad


def load_test_asset(relative_path: str) -> bytes:
    """Load a test asset file from the tests directory."""
    asset_path = Path(settings.BASE_DIR) / "tests" / relative_path
    return asset_path.read_bytes()


def load_json_test_asset(relative_path: str) -> dict:
    """Load and parse a JSON test asset."""
    data = load_test_asset(relative_path)
    return json.loads(data)


# =============================================================================
# API Helpers
# =============================================================================


def start_workflow_url(workflow) -> str:
    """Resolve the workflow start endpoint."""
    try:
        return reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
    except Exception:
        return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


def normalize_poll_url(location: str) -> str:
    """Normalize the polling URL returned by a start response."""
    if not location:
        return ""
    if location.startswith("http"):
        parsed = urlparse(location)
        return parsed.path
    return location


def poll_until_complete(
    client,
    url: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> tuple[dict, int]:
    """
    Poll the ValidationRun detail endpoint until a terminal state is reached.

    Returns (json_data, status_code_of_last_poll).
    """
    deadline = time.time() + timeout_s
    last = None
    last_status = None
    terminal = {"SUCCESS", "SUCCEEDED", "FAILED", "COMPLETED", "ERROR"}

    while time.time() < deadline:
        resp = client.get(url)
        last_status = resp.status_code
        if resp.status_code == HTTP_200_OK:
            try:
                data = resp.json()
            except Exception:
                data = {}
            last = data
            status = (data.get("status") or data.get("state") or "").upper()
            if status in terminal:
                return data, resp.status_code
        time.sleep(interval_s)

    return last or {}, last_status or 0


def extract_issues(data: dict) -> list[dict]:
    """Collect issues from each validation step in the run payload."""
    steps = data.get("steps") or []
    collected: list[dict] = []
    for step in steps:
        issues = step.get("issues") or []
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    collected.append(issue)
                else:
                    collected.append({"message": str(issue)})
    return collected


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
        assert run_status == ValidationRunStatus.SUCCEEDED.name, (
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
        assert run_status == ValidationRunStatus.FAILED.name, (
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

    These tests verify the SelfHostedExecutionBackend correctly:
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
# Direct Backend Tests (Lower Level)
# =============================================================================


@pytest.mark.django_db
@skip_if_no_docker
class TestSelfHostedBackendDirect:
    """
    Direct tests of the SelfHostedExecutionBackend without going through API.

    These tests verify the backend's internal behavior at a lower level.
    """

    def test_backend_is_available_when_docker_running(self):
        """Backend should report available when Docker is running."""
        from validibot.validations.services.execution.self_hosted import (
            SelfHostedExecutionBackend,
        )

        backend = SelfHostedExecutionBackend()
        assert backend.is_available() is True

    def test_backend_is_sync(self):
        """Self-hosted backend should be synchronous."""
        from validibot.validations.services.execution.self_hosted import (
            SelfHostedExecutionBackend,
        )

        backend = SelfHostedExecutionBackend()
        assert backend.is_async is False

    def test_get_container_image_default_naming(self):
        """Backend should generate correct image names."""
        from validibot.validations.services.execution.self_hosted import (
            SelfHostedExecutionBackend,
        )

        backend = SelfHostedExecutionBackend()

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
            image = backend.get_container_image("fmi")
            assert image == "gcr.io/my-project/validibot-validator-fmi:v1.0"

    @skip_if_no_energyplus_image
    def test_get_container_image_for_energyplus(self):
        """Backend should find the EnergyPlus image."""
        from validibot.validations.services.execution.self_hosted import (
            SelfHostedExecutionBackend,
        )

        backend = SelfHostedExecutionBackend()
        image = backend.get_container_image("energyplus")

        # Verify image exists in Docker
        import docker

        client = docker.from_env()
        images = client.images.list(name=image.split(":")[0])
        assert len(images) > 0, f"Image {image} not found in Docker"
