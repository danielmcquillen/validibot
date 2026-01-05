from http import HTTPStatus

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import DataRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def run_validation_tasks_inline(monkeypatch):
    """
    Validation runs now execute inline; fixture retained for compatibility.
    """


def _ensure_custom_validator():
    """Get or create a CUSTOM_VALIDATOR for assertion testing.

    Uses CUSTOM_VALIDATOR because it's in ADVANCED_VALIDATION_TYPES and
    supports workflow step assertions with custom targets.
    """
    validator = Validator.objects.filter(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
    ).first()
    if validator:
        if not validator.allow_custom_assertion_targets:
            validator.allow_custom_assertion_targets = True
            validator.save(update_fields=["allow_custom_assertion_targets"])
        return validator
    return ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        name="Custom Assertions",
        allow_custom_assertion_targets=True,
    )


def _login_user_for_org(client, user, org):
    grant_role(user, org, RoleCode.OWNER)
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()


def test_create_workflow_with_custom_step_and_assertion(client):
    """Verify end-to-end creation of workflow, step, and custom target assertion."""
    user = UserFactory()
    org = OrganizationFactory()
    project = ProjectFactory(org=org, is_default=True)
    _login_user_for_org(client, user, org)

    validator = _ensure_custom_validator()

    # Create workflow
    response = client.post(
        reverse("workflows:workflow_create"),
        data={
            "name": "Price check",
            "slug": "price-check",
            "version": "1.0",
            "is_active": "on",
            "project": str(project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "data_retention": DataRetention.DO_NOT_STORE,
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    workflow = Workflow.objects.get(name="Price check")
    assert workflow.project == project

    # Add CUSTOM_VALIDATOR step
    step_response = client.post(
        reverse(
            "workflows:workflow_step_create",
            kwargs={"pk": workflow.pk, "validator_id": validator.pk},
        ),
        data={
            "name": "Manual price gate",
            "description": "",
            "notes": "",
        },
    )
    assert step_response.status_code == HTTPStatus.FOUND
    step = workflow.steps.get(name="Manual price gate")

    # Add assertion ensuring price < 20
    assertion_response = client.post(
        reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        ),
        data={
            "assertion_type": "basic",
            "target_field": "price",
            "operator": AssertionOperator.LT,
            "comparison_value": "20",
            "severity": "ERROR",
            "message_template": "Price is too expensive! It should be less than $20.",
        },
        HTTP_HX_REQUEST="true",
    )
    assert assertion_response.status_code == HTTPStatus.NO_CONTENT
    step.refresh_from_db()
    assertion = RulesetAssertion.objects.get(ruleset=step.ruleset)
    assert assertion.target_field == "price"
    assert assertion.operator == AssertionOperator.LT
    assert assertion.rhs.get("value") == 20.0  # noqa: PLR2004
    assert (
        assertion.message_template
        == "Price is too expensive! It should be less than $20."
    )


def test_basic_workflow_api_flow_returns_failure_when_price_high(
    api_client,
    run_validation_tasks_inline,
):
    """Test that validation run API correctly reports failures for invalid data.

    Since the workflow API is read-only (ADR-2025-12-22), we create the workflow
    using factories and then test the validation flow via the start endpoint.
    """
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    grant_role(user, org, RoleCode.EXECUTOR)
    api_client.force_authenticate(user=user)

    # Create workflow using factory since API is read-only
    workflow = Workflow.objects.create(
        org=org,
        user=user,
        name="Price check",
        slug="price-check",
        version="1.0",
        is_active=True,
        allowed_file_types=[SubmissionFileType.JSON],
    )

    validator = ValidatorFactory(
        validation_type=ValidationType.BASIC,
        allow_custom_assertion_targets=True,
        org=org,
        is_system=False,
    )
    ruleset = Ruleset.objects.create(
        org=org,
        user=user,
        name="price-check-rules",
        ruleset_type=RulesetType.BASIC,
        version="1.0",
    )
    WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=10,
        name="Manual price gate",
        description="",
        notes="",
        config={},
    )
    message = "The price {{price}} is too expensive! Limit {{ value }}"
    RulesetAssertion.objects.create(
        ruleset=ruleset,
        order=10,
        assertion_type=AssertionType.BASIC,
        operator=AssertionOperator.LT,
        target_field="price",
        severity=Severity.ERROR,
        rhs={"value": 20},
        options={},
        message_template=message,
    )

    start_url = reverse("api:workflow-start", kwargs={"pk": workflow.pk})
    run_resp = api_client.post(start_url, data={"price": 25}, format="json")
    # 201 Created when execution completes, 202 Accepted when still processing
    assert run_resp.status_code == status.HTTP_201_CREATED
    body = run_resp.json()
    assert body["status"] == ValidationRunStatus.FAILED
    assert body["workflow"] == workflow.id
    assert body["workflow_slug"] == workflow.slug
    assert body["error"]
    assert body["steps"], body
    price_step = body["steps"][0]
    assert price_step["status"] == "FAILED"
    issues = price_step["issues"]
    assert issues
    issue = issues[0]
    assert issue["path"] == "price"
    assert issue["message"] == "The price 25 is too expensive! Limit 20"
    assert issue["severity"] == Severity.ERROR

    run = ValidationRun.objects.get(pk=body["id"])
    assert run.status == ValidationRunStatus.FAILED
