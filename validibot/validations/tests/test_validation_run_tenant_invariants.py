"""Model-layer tenant-integrity tests for denormalized validation runs.

ValidationRun repeats organization, project, workflow, submission, and user
relationships for historical truth and query performance. That duplication is
also an authorization boundary, so every supported ORM write must reject a
graph whose tenant or parent relationships disagree.
"""

import pytest
from django.core.exceptions import ValidationError

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.models import ValidationRun
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_direct_create_rejects_workflow_from_other_org():
    """A forged denormalized org cannot relabel another tenant's workflow run."""

    run_org = OrganizationFactory()
    workflow = WorkflowFactory()
    user = workflow.user
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=user,
    )

    with pytest.raises(ValidationError, match="organization must match workflow"):
        ValidationRun.objects.create(
            org=run_org,
            workflow=workflow,
            project=workflow.project,
            submission=submission,
            user=user,
        )


def test_direct_create_rejects_submission_from_another_workflow():
    """A run cannot expose a submission through a different workflow parent."""

    org = OrganizationFactory()
    project = ProjectFactory(org=org)
    user = UserFactory(orgs=[org])
    run_workflow = WorkflowFactory(org=org, project=project, user=user)
    submission_workflow = WorkflowFactory(org=org, project=project, user=user)
    submission = SubmissionFactory(
        workflow=submission_workflow,
        org=org,
        project=project,
        user=user,
    )

    with pytest.raises(ValidationError, match="Submission must match run workflow"):
        ValidationRun.objects.create(
            org=org,
            workflow=run_workflow,
            project=project,
            submission=submission,
            user=user,
        )


def test_direct_create_rejects_project_from_other_org():
    """A run cannot attach a project owned by another tenant."""

    org = OrganizationFactory()
    workflow = WorkflowFactory(org=org)
    other_project = ProjectFactory()
    user = workflow.user
    submission = SubmissionFactory(
        workflow=workflow,
        org=org,
        project=None,
        user=user,
    )

    with pytest.raises(ValidationError, match="Project must match run organization"):
        ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            project=other_project,
            submission=submission,
            user=user,
        )


def test_consistent_relationship_graph_saves_normally():
    """The invariant must not obstruct a correctly scoped run creation path."""

    workflow = WorkflowFactory()
    user = workflow.user
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=user,
    )

    run = ValidationRun.objects.create(
        org=workflow.org,
        workflow=workflow,
        project=workflow.project,
        submission=submission,
        user=user,
    )

    assert run.org == workflow.org
    assert run.project == workflow.project
