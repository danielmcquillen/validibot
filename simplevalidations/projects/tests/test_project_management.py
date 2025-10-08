import pytest
from django.urls import reverse

from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from simplevalidations.projects.models import Project
from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.tracking.tests.factories import TrackingEventFactory
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.integrations.models import OutboundEvent
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory, UserFactory, grant_role


@pytest.fixture
def admin_user(db):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.ADMIN)
    user.set_current_org(org)
    return user, org


@pytest.fixture
def client_admin(client, admin_user):
    user, org = admin_user
    client.force_login(user)
    return client, user, org


@pytest.mark.django_db
def test_project_list_requires_admin(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    client.force_login(user)

    response = client.get(reverse("projects:project-list"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_project_create(client_admin):
    client, user, org = client_admin

    response = client.post(
        reverse("projects:project-create"),
        data={"name": "Analytics", "description": "Dashboards"},
        follow=True,
    )

    assert response.status_code == 200
    project = Project.objects.get(name="Analytics")
    assert project.org == org


@pytest.mark.django_db
def test_project_update(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)

    response = client.post(
        reverse("projects:project-update", args=[project.pk]),
        data={"name": "Updated Project", "description": "Updated"},
        follow=True,
    )
    assert response.status_code == 200
    project.refresh_from_db()
    assert project.name == "Updated Project"


@pytest.mark.django_db
def test_project_delete(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)

    response = client.post(reverse("projects:project-delete", args=[project.pk]))
    assert response.status_code == 302
    assert not Project.objects.filter(pk=project.pk).exists()
    archived = Project.all_objects.get(pk=project.pk)
    assert archived.is_active is False
    assert archived.deleted_at is not None


@pytest.mark.django_db
def test_default_project_cannot_be_deleted(client_admin):
    client, user, org = client_admin
    default = ProjectFactory(org=org, name="Default", is_default=True)

    response = client.post(reverse("projects:project-delete", args=[default.pk]))
    assert response.status_code == 302
    default.refresh_from_db()
    assert default.is_active is True


@pytest.mark.django_db
def test_soft_delete_detaches_related_records(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)
    submission = SubmissionFactory(org=org, project=project, user=user)
    run = ValidationRunFactory(submission=submission, project=project)
    TrackingEventFactory(project=project, user=user)
    OutboundEvent.objects.create(
        org=org,
        project=project,
        event_type="validation",
        resource_type="validation_run",
        resource_id=str(run.id),
        payload={"id": str(run.id)},
    )

    client.post(reverse("projects:project-delete", args=[project.pk]))

    submission.refresh_from_db()
    run.refresh_from_db()
    assert submission.project is None
    assert run.project is None
    assert not Project.objects.filter(pk=project.pk).exists()
    assert Project.all_objects.get(pk=project.pk).is_active is False
    assert not TrackingEventFactory._meta.model.objects.filter(project=project).exists()
    assert OutboundEvent.objects.filter(project=project).count() == 0


@pytest.mark.django_db
def test_purge_projects_command_removes_old_soft_deleted_projects(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)
    client.post(reverse("projects:project-delete", args=[project.pk]))

    Project.all_objects.filter(pk=project.pk).update(
        deleted_at=timezone.now() - timedelta(days=10)
    )

    call_command("purge_projects", days=7)

    assert not Project.all_objects.filter(pk=project.pk).exists()
