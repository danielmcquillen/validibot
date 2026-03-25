"""Tests for workflow deletion safeguards."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_delete_blocks_htmx_requests_for_workflows_with_credentials(
    client,
    monkeypatch,
):
    """HTMX delete attempts should be blocked when a workflow has credentials."""

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    owner.set_current_org(workflow.org)
    client.force_login(owner)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()

    monkeypatch.setattr(
        "validibot.workflows.views.management.WorkflowDeleteView._has_issued_credentials",
        lambda _self, _workflow: True,
    )

    response = client.post(
        reverse("workflows:workflow_delete", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["HX-Redirect"].endswith(
        reverse("workflows:workflow_detail", args=[workflow.pk]),
    )
    assert workflow.__class__.objects.filter(pk=workflow.pk).exists()
