from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK
from rest_framework.status import HTTP_201_CREATED
from rest_framework.status import HTTP_202_ACCEPTED

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


def start_workflow_url(workflow_id: int) -> str:
    """
    Resolve the API start URL for a workflow, falling back to a
    guessed path if reversing fails.
    """
    try:
        return reverse("api:workflow-start", args=[workflow_id])
    except Exception:  # pragma: no cover - fallback for mismatched urls
        logger.debug("Could not reverse for workflow start")
    return f"/api/v1/workflows/{workflow_id}/start/"


def normalize_poll_url(location: str) -> str:
    """
    Normalize the polling URL returned by the workflow start endpoint.
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
    Poll the validation run endpoint until a terminal
    state is reached or timeout occurs.
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
    Collect issues from the step payload of a validation run response.
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


@pytest.fixture
def workflow_context(load_dtd_asset, api_client):
    """
    Build a minimal workflow configured for XML DTD
    validation and authenticate the API client.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)

    validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
    schema = load_dtd_asset("product.dtd")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.XML_SCHEMA,
        rules_text=schema,
        metadata={
            "schema_type": XMLSchemaType.DTD.name,
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

    api_client.force_authenticate(user=user)

    return {
        "workflow": workflow,
        "client": api_client,
        "step": step,
    }


def _run_and_poll(client, workflow, *, content: str) -> dict:
    """
    Start a workflow via the API and poll until the
    validation run completes, returning the payload.
    """
    start_url = start_workflow_url(workflow.pk)
    resp = client.post(start_url, data=content, content_type="application/xml")
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
        resp.content
    )

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception:
            data = {}
        run_id = data.get("id")
        if run_id:
            for name in ("validation-run-detail", "api:validation-run-detail"):
                try:
                    poll_url = reverse(name, args=[run_id])
                    break
                except Exception:
                    logger.info("Could not reverse for %s", name)
                    continue
            if not poll_url:
                poll_url = f"/api/v1/validation-runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestDtdValidation:
    """
    End-to-end DTD validation tests that start a workflow via the API and poll
    until completion, asserting both happy path and failure scenarios.
    """

    def test_xml_dtd_happy_path(self, load_xml_asset, workflow_context):
        """
        Valid XML payload should pass DTD validation,
        produce a succeeded run, and return no issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        payload = load_xml_asset("valid_product.xml")

        data = _run_and_poll(client, workflow, content=payload)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED.name
        assert extract_issues(data) == []

    def test_xml_dtd_missing_required_elements(self, load_xml_asset, workflow_context):
        """
        Invalid XML payload missing required
        elements should fail DTD validation and return issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        payload = load_xml_asset("invalid_product.xml")

        data = _run_and_poll(client, workflow, content=payload)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED.name
        issues = extract_issues(data)
        assert issues, "Expected DTD validation issues"
