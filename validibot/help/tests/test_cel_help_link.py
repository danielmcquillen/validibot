"""Regression tests for CEL help links in assertion authoring forms.

The help trigger must remain a real new-tab link and must not be nested inside
the field label. Interactive content inside a label has inconsistent browser
activation behaviour and can send the click to the CEL textarea instead.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse
from lxml import html

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import CustomValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def test_cel_help_link_present_in_assertion_form(client):
    """The workflow assertion dialog exposes an independently clickable link."""
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
    document = html.fromstring(response.content)
    links = document.xpath('//a[@aria-label="CEL expression help (opens in new tab)"]')
    assert len(links) == 1
    link = links[0]
    assert link.get("href") == reverse(
        "help:help_page",
        kwargs={"path": "concepts/cel-expressions"},
    )
    assert link.get("target") == "_blank"
    assert link.get("rel") == "noopener"
    assert link.xpath("ancestor::label") == []


def test_cel_help_link_present_in_default_assertion_modal(client):
    """The validator rule dialog uses the same clickable help-field structure."""
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
        "validations:validator_assertions_tab",
        kwargs={"slug": custom_validator.validator.slug},
    )
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    document = html.fromstring(response.content)
    links = document.xpath('//a[@aria-label="CEL expression help (opens in new tab)"]')
    assert len(links) == 1
    assert links[0].get("target") == "_blank"
    assert links[0].xpath("ancestor::label") == []
