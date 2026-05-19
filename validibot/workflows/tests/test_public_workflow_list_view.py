"""Public workflow directory tests.

The public directory is a mixed anonymous/member surface. These tests protect
which workflow rows are exposed there, including the version-family rule that
only the current version of a public workflow should appear in listings.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _force_login_for_workflow(client, workflow):
    """Log in as the workflow owner and make the workflow org active."""
    user = workflow.user
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return user


def test_public_list_shows_public_workflows_only(client):
    """Anonymous visitors should see public info pages, not private rows."""
    public_workflow = WorkflowFactory(
        name="Public Workflow", make_info_page_public=True
    )
    private_workflow = WorkflowFactory(
        name="Private Workflow", make_info_page_public=False
    )

    response = client.get(reverse("public_workflow_list"))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert public_workflow.name in body
    assert private_workflow.name not in body


def test_authenticated_user_sees_private_accessible_workflows(client):
    """Signed-in members should also see private workflows they can access."""
    accessible = WorkflowFactory(name="Member Workflow", make_info_page_public=False)
    _force_login_for_workflow(client, accessible)

    response = client.get(reverse("public_workflow_list"))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Member access" in body
    assert accessible.name in body


def test_search_filters_results(client):
    """Search should narrow the already-authorized public/member queryset."""
    WorkflowFactory(name="Data Quality", make_info_page_public=True)
    WorkflowFactory(name="Image Validation", make_info_page_public=True)

    response = client.get(reverse("public_workflow_list"), {"q": "Image"})

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Image Validation" in body
    assert "Data Quality" not in body


def test_per_page_parameter_limits_results(client):
    """Supported per-page values should change paginator size predictably."""
    WorkflowFactory.create_batch(12, make_info_page_public=True)

    response = client.get(
        reverse("public_workflow_list"),
        {"per_page": "10", "layout": "list"},
    )

    assert response.status_code == HTTPStatus.OK
    page_obj = response.context["page_obj"]
    assert page_obj.paginator.per_page == 10  # noqa: PLR2004
    assert len(response.context["workflows"]) == 10  # noqa: PLR2004


def test_public_list_shows_only_latest_workflow_version(client):
    """Directory listings should not duplicate every version in a family."""
    v1 = WorkflowFactory(
        name="ASHRAE 223P Check v1",
        slug="ashrae-223p-check",
        version="1",
        make_info_page_public=True,
    )
    org = v1.org
    v2 = WorkflowFactory(
        org=org,
        name="ASHRAE 223P Check v2",
        slug=v1.slug,
        version="2",
        make_info_page_public=True,
    )

    response = client.get(reverse("public_workflow_list"))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert v2.name in body
    assert v1.name not in body
