from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.django_db


def start_workflow_url(workflow) -> str:
    """Resolve the API start URL for a workflow (org-scoped per ADR-2026-01-06)."""
    try:
        return reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
    except Exception:
        logger.debug("Could not reverse for workflow start")
    return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


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
    timeout_s: float = 10.0,
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


@pytest.fixture
def workflow_context(load_xsd_asset, api_client):
    """
    Create a workflow for XML validation. engine ∈ {"XSD","RELAXNG"}.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)

    # Ensure caller has EXECUTOR permissions in this org
    grant_role(user, org, RoleCode.EXECUTOR)

    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
    )

    schema = load_xsd_asset("product.xsd")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.XML_SCHEMA,
        rules_text=schema,
        metadata={
            "schema_type": XMLSchemaType.XSD.name,
        },
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[SubmissionFileType.XML],
    )
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
    start_url = start_workflow_url(workflow)
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
            # Use org-scoped route (ADR-2026-01-06)
            org_slug = workflow.org.slug
            try:
                poll_url = reverse(
                    "api:org-runs-detail",
                    kwargs={"org_slug": org_slug, "pk": run_id},
                )
            except Exception as e:
                logger.debug("Could not reverse org-runs-detail: %s", e)
                poll_url = f"/api/v1/orgs/{org_slug}/runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestXsdValidation:
    """
    End-to-end XSD validation tests that start workflows via the API and poll
    to completion.
    """

    def test_xml_xsd_happy_path(self, load_xml_asset, workflow_context):
        """
        Valid XML should satisfy the XSD schema, succeed the run, and return no issues.
        """
        valid_product_xml = load_xml_asset("valid_product.xml")
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        data = _run_and_poll(
            client=client,
            workflow=workflow,
            content=valid_product_xml,
        )
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_xml_xsd_one_field_fails(self, load_xml_asset, workflow_context):
        """
        Invalid XML should fail XSD validation and report at least one issue,
        highlighting rating/max constraints.
        """
        invalid_product_xml = load_xml_asset("invalid_product.xml")
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        data = _run_and_poll(
            client=client,
            workflow=workflow,
            content=invalid_product_xml,
        )
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid payload"
        joined = " | ".join(str(i) for i in issues)
        assert ("rating" in joined) or ("max" in joined.lower()), (
            f"Expected rating/max error in issues, got: {issues}"
        )
