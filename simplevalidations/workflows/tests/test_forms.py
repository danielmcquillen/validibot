from __future__ import annotations

import pytest

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.models import ensure_default_project
from simplevalidations.users.tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    UserFactory,
)
from simplevalidations.workflows.forms import WorkflowForm

pytestmark = pytest.mark.django_db


def create_user_in_org():
    org = OrganizationFactory()
    user = UserFactory()
    MembershipFactory(user=user, org=org, is_active=True)
    user.set_current_org(org)
    return user, org


def test_workflow_form_limits_projects_to_current_org():
    user, org = create_user_in_org()
    default_project = ensure_default_project(org)
    extra_project = ProjectFactory(org=org)

    other_org = OrganizationFactory()
    ensure_default_project(other_org)
    ProjectFactory(org=other_org)

    form = WorkflowForm(user=user)
    project_field = form.fields["project"]

    project_ids = set(project_field.queryset.values_list("pk", flat=True))
    assert project_ids == {default_project.pk, extra_project.pk}
    assert project_field.initial == default_project.pk


def test_workflow_form_saves_selected_project():
    user, org = create_user_in_org()
    default_project = ensure_default_project(org)

    form = WorkflowForm(
        data={
            "name": "Compliance checks",
            "slug": "compliance-checks",
            "project": str(default_project.pk),
            "version": "1.0",
        },
        user=user,
    )

    assert form.is_valid(), form.errors

    workflow = form.save(commit=False)
    workflow.org = org
    workflow.user = user
    workflow.save()

    assert workflow.project == default_project
