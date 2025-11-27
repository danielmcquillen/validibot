from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory


def test_viewer_cannot_delete_validation_run(client, db):
    org = OrganizationFactory()
    owner = UserFactory()
    grant_role(owner, org, RoleCode.OWNER)
    owner.set_current_org(org)
    workflow = WorkflowFactory(org=org, user=owner)
    run = ValidationRunFactory(
        submission__org=org,
        submission__workflow=workflow,
        submission__project=workflow.project,
        user=owner,
    )

    viewer = UserFactory()
    grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
    viewer.set_current_org(org)
    client.force_login(viewer)

    url = reverse("validations:validation_delete", kwargs={"pk": run.pk})
    resp = client.post(url)

    assert resp.status_code == 404
    assert run.__class__.objects.filter(pk=run.pk).exists()


def test_admin_can_delete_validation_run(client, db):
    org = OrganizationFactory()
    admin = UserFactory()
    grant_role(admin, org, RoleCode.ADMIN)
    admin.set_current_org(org)
    workflow = WorkflowFactory(org=org, user=admin)
    run = ValidationRunFactory(
        submission__org=org,
        submission__workflow=workflow,
        submission__project=workflow.project,
        user=admin,
    )

    client.force_login(admin)
    url = reverse("validations:validation_delete", kwargs={"pk": run.pk})
    resp = client.post(url)

    assert resp.status_code in {302, 204}
    assert not run.__class__.objects.filter(pk=run.pk).exists()


def test_results_viewer_cannot_delete_validation_run(client, db):
    org = OrganizationFactory()
    owner = UserFactory()
    grant_role(owner, org, RoleCode.OWNER)
    owner.set_current_org(org)
    workflow = WorkflowFactory(org=org, user=owner)
    run = ValidationRunFactory(
        submission__org=org,
        submission__workflow=workflow,
        submission__project=workflow.project,
        user=owner,
    )

    reviewer = UserFactory()
    grant_role(reviewer, org, RoleCode.VALIDATION_RESULTS_VIEWER)
    reviewer.set_current_org(org)
    client.force_login(reviewer)

    url = reverse("validations:validation_delete", kwargs={"pk": run.pk})
    resp = client.post(url)

    assert resp.status_code == 403
    assert run.__class__.objects.filter(pk=run.pk).exists()
