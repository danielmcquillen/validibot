from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.response import Response

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _force_login_for_workflow(client, workflow):
    user = workflow.user
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return user


def test_launch_page_requires_authentication(client):
    workflow = WorkflowFactory()
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)

    assert response.status_code == 302
    assert "login" in response.url


def test_launch_page_renders_for_org_member(client):
    workflow = WorkflowFactory()
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})
    )

    assert response.status_code == 200
    assert "Start Validation" in response.content.decode()


def test_launch_start_creates_run_and_returns_partial(client, monkeypatch):
    workflow = WorkflowFactory()
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata):  # noqa: ANN001
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return Response(
            {"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=202,
        )

    monkeypatch.setattr(
        "simplevalidations.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch_start", kwargs={"pk": workflow.pk}),
        data={
            "content_type": "application/json",
            "payload": "{}",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 202
    body = response.content.decode()
    assert "Run in progress" in body
    assert ValidationRun.objects.filter(workflow=workflow).count() == 1
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger and "Validation run started" in hx_trigger


def test_launch_start_requires_executor_role(client):
    workflow = WorkflowFactory()
    _force_login_for_workflow(client, workflow)

    response = client.post(
        reverse("workflows:workflow_launch_start", kwargs={"pk": workflow.pk}),
        data={
            "content_type": "application/json",
            "payload": "{}",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 403
    assert (
        "You do not have permission to run this workflow" in response.content.decode()
    )


def test_public_info_view_accessible_when_enabled(client):
    workflow = WorkflowFactory(make_info_public=True)

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert workflow.name in body
    assert "Workflow overview" in body


def test_public_info_view_returns_404_when_disabled(client):
    workflow = WorkflowFactory(make_info_public=False)

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == 404
