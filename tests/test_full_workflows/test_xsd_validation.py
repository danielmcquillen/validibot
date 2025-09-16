from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
from django.urls import reverse

from roscoe.users.models import Role, RoleCode
from roscoe.users.tests.factories import OrganizationFactory, UserFactory
from roscoe.validations.constants import (
    ValidationRunStatus,
    ValidationType,
    XMLSchemaType,
)
from roscoe.validations.tests.factories import RulesetFactory, ValidatorFactory
from roscoe.workflows.tests.factories import WorkflowFactory, WorkflowStepFactory

if TYPE_CHECKING:
    from roscoe.users.models import Membership

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.django_db


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
    client, url: str, timeout_s: float = 10.0, interval_s: float = 0.25
) -> tuple[dict, int]:
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


@pytest.fixture
def workflow_context(load_xsd_asset, api_client):
    """
    Create a workflow for XML validation. engine ∈ {"XSD","RELAXNG"}.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])

    # Ensure caller has EXECUTOR permissions in this org
    try:
        # Best-effort: set membership role to EXECUTOR if present
        membership: Membership = user.memberships.get(org=org)  # type: ignore[attr-defined]
        executor_role: Role = Role.objects.get(code=RoleCode.EXECUTOR)
        membership.roles.add(executor_role)
    except Exception:
        # Guarantee access in tests even if role wiring differs
        user.is_superuser = True
        user.save()

    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
    )

    schema = load_xsd_asset("example_product_schema.xsd")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.XML_SCHEMA,
        metadata={
            "schema": schema,
            "schema_type": XMLSchemaType.XSD.name,
        },
    )

    workflow = WorkflowFactory(org=org, user=user)
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
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
    }


def _run_and_poll(
    client,
    workflow,
    content: str,
    content_type: str = "application/xml",
) -> dict:
    start_url = start_workflow_url(workflow.pk)
    resp = client.post(start_url, data=content, content_type=content_type)
    assert resp.status_code in (200, 201, 202), resp.content

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
    assert last_status == 200, f"Polling failed: {last_status} {data}"
    return data


def test_xml_xsd_happy_path(load_xml_asset, workflow_context):
    valid_product_xml = load_xml_asset("valid_product.xml")
    client = workflow_context["client"]
    workflow = workflow_context["workflow"]
    data = _run_and_poll(
        client=client,
        workflow=workflow,
        content=valid_product_xml,
    )
    run_status = (data.get("status") or data.get("state") or "").upper()
    assert run_status == ValidationRunStatus.SUCCEEDED.name, (
        f"Unexpected status: {run_status} payload={data}"
    )
    issues = extract_issues(data)
    assert isinstance(issues, list)
    assert len(issues) == 0, f"Expected no issues, got: {issues}"


def test_xml_xsd_one_field_fails(load_xml_asset, workflow_context):
    invalid_product_xml = load_xml_asset("invalid_product.xml")
    client = workflow_context["client"]
    workflow = workflow_context["workflow"]
    data = _run_and_poll(
        client=client,
        workflow=workflow,
        content=invalid_product_xml,
    )
    run_status = (data.get("status") or data.get("state") or "").upper()
    assert run_status == ValidationRunStatus.FAILED.name, (
        f"Unexpected status: {run_status}"
    )
    issues = extract_issues(data)
    assert isinstance(issues, list)
    assert len(issues) >= 1, "Expected at least one issue for invalid payload"
    joined = " | ".join(str(i) for i in issues)
    assert ("rating" in joined) or ("max" in joined.lower()), (
        f"Expected rating/max error in issues, got: {issues}"
    )
