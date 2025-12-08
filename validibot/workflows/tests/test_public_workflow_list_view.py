from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _force_login_for_workflow(client, workflow):
    user = workflow.user
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return user


def test_public_list_shows_public_workflows_only(client):
    public_workflow = WorkflowFactory(name="Public Workflow", make_info_public=True)
    private_workflow = WorkflowFactory(name="Private Workflow", make_info_public=False)

    response = client.get(reverse("public_workflow_list"))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert public_workflow.name in body
    assert private_workflow.name not in body


def test_authenticated_user_sees_private_accessible_workflows(client):
    accessible = WorkflowFactory(name="Member Workflow", make_info_public=False)
    _force_login_for_workflow(client, accessible)

    response = client.get(reverse("public_workflow_list"))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Member access" in body
    assert accessible.name in body


def test_search_filters_results(client):
    WorkflowFactory(name="Data Quality", make_info_public=True)
    WorkflowFactory(name="Image Validation", make_info_public=True)

    response = client.get(reverse("public_workflow_list"), {"q": "Image"})

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Image Validation" in body
    assert "Data Quality" not in body


def test_per_page_parameter_limits_results(client):
    WorkflowFactory.create_batch(12, make_info_public=True)

    response = client.get(
        reverse("public_workflow_list"),
        {"per_page": "10", "layout": "list"},
    )

    assert response.status_code == HTTPStatus.OK
    page_obj = response.context["page_obj"]
    assert page_obj.paginator.per_page == 10  # noqa: PLR2004
    assert len(response.context["workflows"]) == 10  # noqa: PLR2004
