from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.workflows.tests.factories import WorkflowFactory


@pytest.mark.django_db
def test_can_execute_respects_activation_flag():
    workflow = WorkflowFactory(is_active=False)
    user = workflow.user
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    user.set_current_org(workflow.org)

    assert workflow.can_execute(user=user) is False

    workflow.is_active = True
    workflow.save(update_fields=["is_active"])

    assert workflow.can_execute(user=user) is True


@pytest.mark.django_db
def test_activation_view_toggles_when_authorized(client: Client):
    workflow = WorkflowFactory()
    manager = workflow.user
    manager.set_current_org(workflow.org)
    grant_role(manager, workflow.org, RoleCode.AUTHOR)
    client.force_login(manager)

    response = client.post(
        reverse("workflows:workflow_activation", args=[workflow.pk]),
        {"is_active": "false"},
    )

    assert response.status_code in {302, 204}
    workflow.refresh_from_db()
    assert workflow.is_active is False

    # Executor without author/admin/owner should be rejected
    executor = UserFactory()
    grant_role(executor, workflow.org, RoleCode.EXECUTOR)
    executor.set_current_org(workflow.org)
    client.force_login(executor)

    response = client.post(
        reverse("workflows:workflow_activation", args=[workflow.pk]),
        {"is_active": "true"},
    )

    assert response.status_code == 403
    workflow.refresh_from_db()
    assert workflow.is_active is False
