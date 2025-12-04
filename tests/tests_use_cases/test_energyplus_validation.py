from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.conf import settings
from django.urls import reverse
from rest_framework.status import HTTP_200_OK
from rest_framework.status import HTTP_201_CREATED
from rest_framework.status import HTTP_202_ACCEPTED
from sv_shared.energyplus.models import EnergyPlusSimulationMetrics
from sv_shared.energyplus.models import EnergyPlusSimulationOutputs

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.models import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.energyplus import EnergyPlusSimulationResult
from simplevalidations.validations.engines.energyplus import configure_modal_runner
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.django_db


class FakeRunner:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def start_workflow_url(workflow_id: int) -> str:
    try:
        return reverse("api:workflow-start", args=[workflow_id])
    except Exception:
        logger.debug("Could not reverse for workflow start")
    return f"/api/v1/workflows/{workflow_id}/start/"


def normalize_poll_url(location: str) -> str:
    if not location:
        return ""
    if location.startswith("http"):
        parsed = urlparse(location)
        return parsed.path
    return location


def poll_until_complete(
    client,
    url: str,
    timeout_s: float = 15.0,
    interval_s: float = 0.25,
) -> tuple[dict, int]:
    deadline = time.time() + timeout_s
    last = None
    last_status = None
    terminal = {"SUCCESS", "FAILED", "COMPLETED", "ERROR"}
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


def load_example_epjson() -> str:
    base = Path(__file__).resolve().parent.parent / "data" / "energyplus"
    path = base / "example_epjson.json"
    return path.read_text(encoding="utf-8")


@pytest.fixture
def energyplus_workflow(api_client):
    """
    Build a minimal workflow configured for the EnergyPlus validation engine.
    """

    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)

    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
        default_ruleset=None,
    )

    weather_file = getattr(
        settings,
        "TEST_ENERGYPLUS_WEATHER_FILE",
        "USA_CA_SF.epw",
    )

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.ENERGYPLUS,
        rules_text="{}",
        metadata={"weather_file": weather_file},
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[
            SubmissionFileType.TEXT,
            SubmissionFileType.JSON,
        ],
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
        config={"run_simulation": True},
    )

    api_client.force_authenticate(user=user)

    live_modal = settings.TEST_ENERGYPLUS_LIVE_MODAL
    fake_runner: FakeRunner | None = None
    if live_modal:
        configure_modal_runner(None)
    else:
        simulation_result = EnergyPlusSimulationResult(
            simulation_id="sim-workflow",
            status="success",
            outputs=EnergyPlusSimulationOutputs(),
            metrics=EnergyPlusSimulationMetrics(
                electricity_kwh=3200.0,
                energy_use_intensity_kwh_m2=22.5,
            ),
            messages=["Simulation completed successfully."],
            errors=[],
            energyplus_returncode=0,
            execution_seconds=33.0,
            invocation_mode="python_api",
        )
        fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
        configure_modal_runner(fake_runner)

    yield {
        "org": org,
        "user": user,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
        "runner": fake_runner,
        "live_modal": live_modal,
        "weather_file": weather_file,
    }

    configure_modal_runner(None)


def _run_and_poll(client, workflow, content: str) -> dict:
    start_url = start_workflow_url(workflow.pk)
    resp = client.post(
        start_url,
        data=content,
        content_type="application/json",
    )
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
        resp.content
    )

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception as e:
            logger.debug("Could not parse JSON response: %s", e)
        run_id = data.get("id")
        if run_id:
            for name in ("validation-run-detail", "api:validation-run-detail"):
                try:
                    poll_url = reverse(name, args=[run_id])
                    break
                except Exception as e:
                    logger.debug("Could not reverse %s for run %s: %s", name, run_id, e)
            if not poll_url:
                poll_url = f"/api/v1/validation-runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestEnergyPlusValidation:
    """
    End-to-end EnergyPlus validation that starts a workflow via the API, polls
    to completion, and verifies the Modal runner interaction and outcomes.
    """

    def test_energyplus_workflow_success(self, energyplus_workflow):
        """
        A valid EPJSON payload should succeed with no issues and, when mocked,
        invoke the Modal runner with the expected weather file and payload shape.
        """
        client = energyplus_workflow["client"]
        workflow = energyplus_workflow["workflow"]
        runner = energyplus_workflow["runner"]
        live_modal = energyplus_workflow["live_modal"]
        weather_file = energyplus_workflow["weather_file"]

        payload = load_example_epjson()

        data = _run_and_poll(client, workflow, payload)

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED.name, (
            f"Unexpected status: {run_status} payload={data}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

        # Ensure Modal runner was invoked with the expected payload shape for faked
        # runs.
        if not live_modal:
            assert runner is not None
            assert runner.calls, "Expected the EnergyPlus Modal runner to be invoked."
            first_call = runner.calls[0]
            assert "energyplus_payload" in first_call
            assert first_call["weather_file"] == weather_file
