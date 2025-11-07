import pytest
from django.urls import reverse

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import RulesetAssertion
from simplevalidations.validations.models import Validator
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.models import Workflow

pytestmark = pytest.mark.django_db


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
    ProjectFactory(org=org, is_default=True)
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
            "make_info_public": "",
        },
    )
    assert response.status_code == 302
    workflow = Workflow.objects.get(name="Price check")
    assert workflow.project is not None

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
    assert step_response.status_code == 302
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
    assert assertion_response.status_code == 204
    step.refresh_from_db()
    assertion = RulesetAssertion.objects.get(ruleset=step.ruleset)
    assert assertion.target_field == "price"
    assert assertion.operator == AssertionOperator.LT
    assert assertion.rhs.get("value") == 20.0
    assert (
        assertion.message_template == "Price is too expensive! It should be less than $20."
    )
