from http import HTTPStatus

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import RulesetAssertion
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import Validator
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def run_validation_tasks_inline(monkeypatch):
    """
    Execute validation runs synchronously during tests so API responses include
    the real ValidationRun payload instead of a pending status.
    """
    from simplevalidations.validations import tasks as validation_tasks
    from simplevalidations.validations.services.validation_run import (
        ValidationRunService,
    )

    def immediate_apply_async(*_, **kwargs):
        task_kwargs = kwargs.get("kwargs") or {}
        service = ValidationRunService()
        execution_result = service.execute(
            validation_run_id=task_kwargs.get("validation_run_id"),
            user_id=task_kwargs.get("user_id"),
            metadata=task_kwargs.get("metadata"),
        )

        class ImmediateResult:
            def get(
                self,
                timeout=None,
                *,
                propagate=False,
            ):
                return execution_result

        return ImmediateResult()

    monkeypatch.setattr(
        validation_tasks.execute_validation_run,
        "apply_async",
        immediate_apply_async,
    )


def _ensure_basic_validator():
    validator = Validator.objects.filter(validation_type=ValidationType.BASIC).first()
    if validator:
        if not validator.allow_custom_assertion_targets:
            validator.allow_custom_assertion_targets = True
            validator.save(update_fields=["allow_custom_assertion_targets"])
        return validator
    return ValidatorFactory(
        validation_type=ValidationType.BASIC,
        name="Manual Assertions",
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


def test_create_workflow_with_basic_step_and_assertion(client):
    user = UserFactory()
    org = OrganizationFactory()
    project = ProjectFactory(org=org, is_default=True)
    _login_user_for_org(client, user, org)

    validator = _ensure_basic_validator()

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
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    workflow = Workflow.objects.get(name="Price check")
    assert workflow.project == project

    # Add BASIC step
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
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    grant_role(user, org, RoleCode.EXECUTOR)
    api_client.force_authenticate(user=user)

    create_resp = api_client.post(
        reverse("api:workflow-list"),
        data={
            "org": org.pk,
            "user": user.pk,
            "name": "Price check",
            "slug": "price-check",
            "version": "1.0",
            "is_active": True,
        },
        format="json",
    )
    assert create_resp.status_code == status.HTTP_201_CREATED
    workflow_id = create_resp.data["id"]
    workflow = Workflow.objects.get(pk=workflow_id)

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
