"""Privacy-scoping tests for ``WorkflowValidationListView``.

The per-workflow validation list must only show a viewer the runs they are
permitted to see. A member who can VIEW a workflow but only holds
``VALIDATION_RESULTS_VIEW_OWN`` (e.g. an Executor) must see *their own* runs
only — not co-workers' submissions and results. A member holding
``VALIDATION_RESULTS_VIEW_ALL`` (e.g. a Validation Results Viewer) sees every
run for the workflow.

Regression for ADR 04-23 review-ep-#10, where ``get_queryset`` returned all
runs for the workflow to any viewer — a privacy leak. The view now mirrors
``WorkflowLaunchContextMixin.get_displayable_run_queryset``'s permission gate.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _login_with_org(client, user, workflow):
    """Authenticate *user* against the workflow's organization context."""
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def _run_for(workflow, user):
    """A ValidationRun on *workflow* launched by *user*."""
    return ValidationRunFactory(
        workflow=workflow,
        org=workflow.org,
        user=user,
    )


def test_view_own_member_sees_only_their_runs(client):
    """An Executor (VIEW_OWN, not VIEW_ALL) sees only the runs they launched.

    This is the core privacy regression: before the fix, any member who could
    view the workflow saw every run, including other members' submissions.
    """
    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)

    executor = UserFactory()
    grant_role(executor, workflow.org, RoleCode.EXECUTOR)

    own_run = _run_for(workflow, executor)
    other_run = _run_for(workflow, owner)

    _login_with_org(client, executor, workflow)
    response = client.get(
        reverse("workflows:workflow_validation_list", args=[workflow.pk]),
    )

    assert response.status_code == HTTPStatus.OK
    visible = list(response.context["validations"])
    assert own_run in visible
    assert other_run not in visible


def test_view_all_member_sees_every_run(client):
    """A Validation Results Viewer (VIEW_ALL) sees all runs for the workflow.

    Uses VALIDATION_RESULTS_VIEWER rather than OWNER so the test exercises the
    real VIEW_ALL permission branch — OWNER would short-circuit every
    permission check in the backend and mask a scoping bug.
    """
    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)

    results_viewer = UserFactory()
    grant_role(results_viewer, workflow.org, RoleCode.VALIDATION_RESULTS_VIEWER)

    run_a = _run_for(workflow, owner)
    run_b = _run_for(workflow, results_viewer)

    _login_with_org(client, results_viewer, workflow)
    response = client.get(
        reverse("workflows:workflow_validation_list", args=[workflow.pk]),
    )

    assert response.status_code == HTTPStatus.OK
    visible = list(response.context["validations"])
    assert run_a in visible
    assert run_b in visible
