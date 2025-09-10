from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import pytest
from django.urls import reverse

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.users.tests.factories import OrganizationFactory
from roscoe.users.tests.factories import UserFactory
from roscoe.validations.constants import ValidationType
from roscoe.validations.tests.factories import RulesetFactory
from roscoe.validations.tests.factories import ValidatorFactory
from roscoe.workflows.tests.factories import WorkflowFactory
from roscoe.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.django_db


def valid_product_payload() -> dict[str, Any]:
    return {
        "sku": "ABCD1234",
        "name": "Widget Mini",
        "price": 19.99,
        "rating": 95,
        "tags": ["gadgets", "mini"],
        "dimensions": {"width": 3.5, "height": 1.2},
        "in_stock": True,
    }


def invalid_product_payload() -> dict[str, Any]:
    bad = valid_product_payload()
    bad["rating"] = 150  # violates max 100
    return bad


@pytest.fixture
def workflow_context(load_json_asset, api_client):
    """
    Build a minimal workflow that validates a product JSON using a JSON Schema ruleset.
    Schema is loaded from tests/assets/json/example_product_schema.json via fixture.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    project = ProjectFactory(org=org)

    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        is_public=True,
    )

    schema = load_json_asset("json/example_product_schema.json")
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.JSON_SCHEMA,
        metadata={"schema": schema},
    )

    workflow = WorkflowFactory(org=org, user=user, project=project)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
    )

    # Authenticate API client
    api_client.force_authenticate(user=user)

    return {
        "org": org,
        "user": user,
        "project": project,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
    }


def start_workflow_url(workflow_id: int) -> str:
    # Prefer reversing if a name is available; fallback to conventional path
    for name in (
        "workflow-start",
        "api:workflow-start",
        "workflows-start",
        "api:workflows-start",
    ):
        try:
            return reverse(name, args=[workflow_id])
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not reverse %s for workflow %s: %s", name, workflow_id, e)
    return f"/api/v1/workflows/{workflow_id}/start/"


def normalize_poll_url(location: str) -> str:
    if not location:
        return ""
    if location.startswith("http"):
        parsed = urlparse(location)
        return parsed.path
    return location


def poll_until_complete(client, url: str, timeout_s: float = 10.0, interval_s: float = 0.25) -> tuple[dict, int]:
    """
    Poll the ValidationRun detail endpoint until a terminal state is reached or timeout.
    Returns (json, status_code_of_last_poll).
    """
    deadline = time.time() + timeout_s
    last = None
    last_status = None
    terminal = {"SUCCESS", "FAILED", "COMPLETED", "ERROR"}
    while time.time() < deadline:
        resp = client.get(url)
        last_status = resp.status_code
        if resp.status_code == 200:
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
    """
    Try to extract a list of issues from the run payload using common shapes.
    """
    issues = data.get("issues")
    if isinstance(issues, list):
        return issues

    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("issues"), list):
        return result["issues"]

    steps = data.get("steps") or data.get("step_runs") or []
    collected: list[dict] = []
    if isinstance(steps, list):
        for s in steps:
            sis = s.get("issues") or (s.get("result") or {}).get("issues")
            if isinstance(sis, list):
                collected.extend(sis)
    return collected


def test_json_validation_happy_path(workflow_context):
    client = workflow_context["client"]
    workflow = workflow_context["workflow"]

    start_url = start_workflow_url(workflow.pk)
    payload = valid_product_payload()

    resp = client.post(
        start_url,
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code in (200, 201, 202), resp.content

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not parse JSON response: %s", e)

        run_id = data.get("id")
        if run_id:
            for name in ("validation-run-detail", "api:validation-run-detail"):
                try:
                    poll_url = reverse(name, args=[run_id])
                    break
                except Exception as e:  # noqa: BLE001
                    logger.debug("Could not reverse %s for run %s: %s", name, run_id, e)
            if not poll_url:
                poll_url = f"/api/v1/validation-runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == 200, f"Polling failed: {last_status} {data}"

    run_status = (data.get("status") or data.get("state") or "").upper()
    assert run_status in {"SUCCESS", "COMPLETED"}, f"Unexpected status: {run_status} payload={data}"
    issues = extract_issues(data)
    assert isinstance(issues, list)
    assert len(issues) == 0, f"Expected no issues, got: {issues}"


def test_json_validation_one_field_fails(workflow_context):
    client = workflow_context["client"]
    workflow = workflow_context["workflow"]

    start_url = start_workflow_url(workflow.pk)
    payload = invalid_product_payload()

    resp = client.post(
        start_url,
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code in (200, 201, 202), resp.content

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not parse JSON response: %s", e)
        run_id = data.get("id")
        if run_id:
            for name in ("validation-run-detail", "api:validation-run-detail"):
                try:
                    poll_url = reverse(name, args=[run_id])
                    break
                except Exception as e:  # noqa: BLE001
                    logger.debug("Could not reverse %s for run %s: %s", name, run_id, e)
            if not poll_url:
                poll_url = f"/api/v1/validation-runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == 200, f"Polling failed: {last_status} {data}"

    run_status = (data.get("status") or data.get("state") or "").upper()
    assert run_status in {"FAILED", "COMPLETED", "SUCCESS"}, f"Unexpected status: {run_status}"

    issues = extract_issues(data)
    assert isinstance(issues, list)
    assert len(issues) >= 1, "Expected at least one issue for invalid payload"

    joined = " | ".join(str(i) for i in issues)
    assert ("rating" in joined) or ("maximum" in joined), f"Expected rating/max error in issues, got: {issues}"
