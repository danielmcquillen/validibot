from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.workflows.constants import WORKFLOW_LIST_LAYOUT_SESSION_KEY
from simplevalidations.workflows.constants import (
    WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY,
)
from simplevalidations.workflows.constants import WorkflowListLayout
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
    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert "Alpha Workflow" in content
    assert "Beta Workflow" not in content

    response = _switch_workspace(client, org_beta.id, next_url=list_url)
    assert response.status_code == HTTPStatus.OK
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
    assert response.status_code == HTTPStatus.OK
    assert response.context["current_layout"] == WorkflowListLayout.TABLE
    assert client.session[WORKFLOW_LIST_LAYOUT_SESSION_KEY] == WorkflowListLayout.TABLE

    response = client.get(url)
    assert response.context["current_layout"] == WorkflowListLayout.TABLE


def test_workflow_delete_button_has_target_id(client):
    user = UserFactory()
    org = OrganizationFactory(name="Delete Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Delete Me")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    expected_id = f"workflow-item-wrapper-{workflow.pk}"
    assert f'hx-target="#{expected_id}"' in html


def test_archive_button_visible_for_owner_with_runs(client):
    user = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Archive Me")
    submission = SubmissionFactory(org=org, user=user, workflow=workflow)
    ValidationRunFactory(submission=submission)

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    html = response.content.decode()
    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    assert archive_url in html


def test_archive_button_hidden_for_non_owner_author_other_workflow(client):
    owner = UserFactory()
    other = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(owner, org, RoleCode.OWNER)
    grant_role(other, org, RoleCode.AUTHOR)
    WorkflowFactory(org=org, user=owner, name="Owner Workflow")

    client.force_login(other)
    other.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    html = response.content.decode()
    assert "workflow_archive" not in html


def test_archived_badge_priority(client):
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(
        org=org,
        user=user,
        name="Archived State",
        is_active=False,
        is_archived=True,
    )
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list") + "?archived=1"
    response = client.get(list_url)
    html = response.content.decode()
    assert "Archived" in html


def test_unarchive_hx_updates_state(client):
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="To Unarchive",
        is_active=False,
        is_archived=True,
    )
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    url = reverse("workflows:workflow_archive", args=[workflow.pk])
    response = client.post(
        url,
        HTTP_HX_REQUEST="true",
        data={
            "unarchive": "1",
            "show_archived": "1",
            "layout": "grid",
        },
    )
    assert response.status_code == HTTPStatus.OK
    workflow.refresh_from_db()
    assert workflow.is_archived is False
    assert workflow.is_active is True
    html = response.content.decode()
    assert "Active" in html


def test_workflow_archive_button_rendered_when_runs_exist(client):
    user = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Archive Me")
    submission = SubmissionFactory(org=org, user=user, workflow=workflow)
    ValidationRunFactory(submission=submission)

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    assert archive_url in html


def test_archived_toggle_urls_are_absolute(client):
    user = UserFactory()
    org = OrganizationFactory(name="Toggle Org")
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(org=org, user=user, name="Toggle Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    toggle_urls = response.context["archived_toggle_urls"]
    assert toggle_urls["show"].startswith(list_url)
    assert "archived=1" in toggle_urls["show"]
    assert toggle_urls["hide"].startswith(list_url)
    assert "archived=0" in toggle_urls["hide"]


def test_archive_view_updates_show_archived_preference(client):
    user = UserFactory()
    org = OrganizationFactory(name="Preference Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Preference Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    response = client.post(archive_url, data={"show_archived": "1"})
    assert response.status_code in {200, 302}
    assert client.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] is True

    response = client.post(
        archive_url,
        data={
            "show_archived": "0",
            "unarchive": "1",
        },
    )
    assert response.status_code in {200, 302}
    assert client.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] is False


def test_viewer_cannot_toggle_archived(client):
    user = UserFactory()
    org = OrganizationFactory(name="Viewer Org")
    grant_role(user, org, RoleCode.WORKFLOW_VIEWER)
    WorkflowFactory(org=org, user=user, name="Visible Workflow")
    WorkflowFactory(
        org=org,
        user=user,
        name="Archived Hidden Workflow",
        is_archived=True,
        is_active=False,
    )

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(f"{list_url}?archived=1")
    assert response.status_code == HTTPStatus.OK
    assert response.context["show_archived"] is False
    html = response.content.decode()
    assert "Archived Hidden Workflow" not in html
    assert "Archived toggle" not in html
