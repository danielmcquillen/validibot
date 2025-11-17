from __future__ import annotations

import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.workflows.constants import (
    WORKFLOW_LIST_LAYOUT_SESSION_KEY,
    WorkflowListLayout,
)
from simplevalidations.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _switch_workspace(client, org_id: int, *, next_url: str):
    return client.post(
        reverse("users:organization-switch", args=[org_id]),
        data={"next": next_url},
        follow=True,
    )


def test_workflow_list_refreshes_on_workspace_switch(client):
    user = UserFactory()
    org_alpha = OrganizationFactory(name="Alpha Org")
    org_beta = OrganizationFactory(name="Beta Org")
    grant_role(user, org_alpha, RoleCode.OWNER)
    grant_role(user, org_beta, RoleCode.OWNER)

    WorkflowFactory(org=org_alpha, user=user, name="Alpha Workflow")
    WorkflowFactory(org=org_beta, user=user, name="Beta Workflow")

    client.force_login(user)
    list_url = reverse("workflows:workflow_list")

    response = _switch_workspace(client, org_alpha.id, next_url=list_url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Alpha Workflow" in content
    assert "Beta Workflow" not in content

    response = _switch_workspace(client, org_beta.id, next_url=list_url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Alpha Workflow" not in content
    assert "Beta Workflow" in content


def test_workflow_list_layout_persists_in_session(client):
    user = UserFactory()
    org = OrganizationFactory(name="Layout Org")
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(org=org, user=user, name="Layout Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    url = reverse("workflows:workflow_list")
    response = client.get(f"{url}?layout=table")
    assert response.status_code == 200
    assert response.context["current_layout"] == WorkflowListLayout.TABLE
    assert (
        client.session[WORKFLOW_LIST_LAYOUT_SESSION_KEY] == WorkflowListLayout.TABLE
    )

    response = client.get(url)
    assert response.context["current_layout"] == WorkflowListLayout.TABLE
