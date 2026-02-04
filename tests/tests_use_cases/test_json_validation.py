from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)


def valid_product_payload() -> dict[str, Any]:
    """
    Produce a sample product payload that satisfies the example JSON Schema.
    """
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
    """
    Produce a payload that intentionally violates the schema (rating > 100).
    """
    bad = valid_product_payload()
    bad["rating"] = 150  # violates max 100
    return bad


@pytest.fixture
def workflow_context(load_json_asset, api_client):
    """
    Build a minimal workflow that validates a product JSON using a JSON Schema
    ruleset, and authenticate the API client with EXECUTOR permissions.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)

    grant_role(user, org, RoleCode.EXECUTOR)

    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
    )

    schema = load_json_asset("example_product_schema.json")
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
    step = WorkflowStepFactory(
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
        "step": step,
        "client": api_client,
    }


def start_workflow_url(workflow) -> str:
    """
    Resolve the workflow start endpoint (org-scoped per ADR-2026-01-06).
    """
    try:
        url = reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
    except Exception:
        logger.debug("Could not reverse for workflow start")
    else:
        return url
    return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


def normalize_poll_url(location: str) -> str:
    """
    Normalize the polling URL returned by a start response.
    """
    if not location:
        return ""
    if location.startswith("http"):
        parsed = urlparse(location)
        return parsed.path
    return location


def poll_until_complete(
    client,
    url: str,
    timeout_s: float = 10.0,
    interval_s: float = 0.25,
) -> tuple[dict, int]:
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
    """
    Collect issues from each validation step in the run payload.
    """
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


@pytest.mark.django_db
class TestJsonValidation:
    """
    End-to-end JSON Schema validation tests that start workflows via the API and
    poll until completion, covering both valid and invalid payloads.
    """

    def test_json_validation_happy_path(self, workflow_context):
        """
        Valid payload should succeed and return no issues from the validation run.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        org = workflow_context["org"]

        start_url = start_workflow_url(workflow)
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
            except Exception as exc:
                logger.debug("Could not parse JSON response: %s", exc)

            run_id = data.get("id")
            if run_id:
                # Use org-scoped route (ADR-2026-01-06)
                try:
                    poll_url = reverse(
                        "api:org-runs-detail",
                        kwargs={"org_slug": org.slug, "pk": run_id},
                    )
                except Exception as exc:
                    logger.debug("Could not reverse org-runs-detail: %s", exc)
                    poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        data, last_status = poll_until_complete(client, poll_url)
        assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_json_validation_one_field_fails(self, workflow_context):
        """
        Invalid payload should fail validation and surface the rating/max error
        in issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        org = workflow_context["org"]

        start_url = start_workflow_url(workflow)
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
            except Exception as exc:
                logger.debug("Could not parse JSON response: %s", exc)
            run_id = data.get("id")
            if run_id:
                # Use org-scoped route (ADR-2026-01-06)
                try:
                    poll_url = reverse(
                        "api:org-runs-detail",
                        kwargs={"org_slug": org.slug, "pk": run_id},
                    )
                except Exception as exc:
                    logger.debug("Could not reverse org-runs-detail: %s", exc)
                    poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        data, last_status = poll_until_complete(client, poll_url)
        assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid payload"

        joined = " | ".join(str(issue) for issue in issues)
        assert ("rating" in joined) or ("maximum" in joined), (
            f"Expected rating/max error in issues, got: {issues}"
        )
