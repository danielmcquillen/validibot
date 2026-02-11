from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import CustomValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def test_cel_help_link_present_in_assertion_form(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.OWNER)
    user.set_current_org(org)
    client.force_login(user)
    custom_validator = CustomValidatorFactory(org=org, validator__org=org)
    workflow = WorkflowFactory(org=org, user=user)
    step = WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)

    url = reverse(
        "workflows:workflow_step_assertion_create",
        kwargs={"pk": workflow.pk, "step_id": step.pk},
    )
    response = client.get(url, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.OK
    assert "cel-expressions" in response.content.decode()


def test_cel_help_link_present_in_default_assertion_modal(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.OWNER)
    user.set_current_org(org)
    client.force_login(user)
    custom_validator = CustomValidatorFactory(
        org=org,
        validator__org=org,
        created_by=user,
    )
    url = reverse(
        "validations:validator_detail",
        kwargs={"slug": custom_validator.validator.slug},
    )
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    assert "cel-expressions" in response.content.decode()
