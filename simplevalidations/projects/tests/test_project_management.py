from datetime import timedelta
from http import HTTPStatus

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from simplevalidations.integrations.models import OutboundEvent
from simplevalidations.projects.models import Project
from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.models import Submission
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.tracking.tests.factories import TrackingEventFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.tests.factories import ValidationRunFactory


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
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    return client, user, org


@pytest.mark.django_db
def test_project_list_requires_admin(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.get(reverse("projects:project-list"))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_project_create(client_admin):
    client, user, org = client_admin

    response = client.post(
        reverse("projects:project-create"),
        data={
            "name": "Analytics",
            "description": "Dashboards",
            "color": "#00AA88",
        },
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    project = Project.objects.get(name="Analytics")
    assert project.org == org
    assert project.color == "#00AA88"


@pytest.mark.django_db
def test_project_update(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)

    response = client.post(
        reverse("projects:project-update", args=[project.pk]),
        data={
            "name": "Updated Project",
            "description": "Updated",
            "color": "#1185FF",
        },
        follow=True,
    )
    assert response.status_code == HTTPStatus.OK
    project.refresh_from_db()
    assert project.name == "Updated Project"
    assert project.color == "#1185FF"


@pytest.mark.django_db
def test_project_delete(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)

    response = client.post(reverse("projects:project-delete", args=[project.pk]))
    assert response.status_code == HTTPStatus.FOUND
    assert not Project.objects.filter(pk=project.pk).exists()
    archived = Project.all_objects.filter(pk=project.pk).first()
    assert archived is not None
    assert archived.is_active is False
    assert archived.deleted_at is not None


@pytest.mark.django_db
def test_default_project_cannot_be_deleted(client_admin):
    client, user, org = client_admin
    default = ProjectFactory(org=org, name="Default", is_default=True)

    response = client.post(reverse("projects:project-delete", args=[default.pk]))
    assert response.status_code == HTTPStatus.FOUND
    refreshed = Project.all_objects.get(pk=default.pk)
    assert refreshed.is_active is True


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

    submission_refreshed = Submission.objects.get(pk=submission.pk)
    run_refreshed = ValidationRun.objects.get(pk=run.pk)
    assert submission_refreshed.project is None
    assert run_refreshed.project is None
    assert not Project.objects.filter(pk=project.pk).exists()
    archived = Project.all_objects.filter(pk=project.pk).first()
    assert archived is not None
    assert archived.is_active is False
    assert not TrackingEvent.objects.filter(project=project).exists()
    assert OutboundEvent.objects.filter(project=project).count() == 0


@pytest.mark.django_db
def test_purge_projects_command_removes_old_soft_deleted_projects(client_admin):
    client, user, org = client_admin
    project = ProjectFactory(org=org)
    client.post(reverse("projects:project-delete", args=[project.pk]))

    Project.all_objects.filter(pk=project.pk).update(
        deleted_at=timezone.now() - timedelta(days=10),
    )

    call_command("purge_projects", days=7)

    assert not Project.all_objects.filter(pk=project.pk).exists()


@pytest.mark.django_db
def test_project_list_recovers_from_stale_session_scope(client):
    valid_org = OrganizationFactory(name="Alpha Org")
    rogue_org = OrganizationFactory(name="Ghost Org")
    user = UserFactory(orgs=[valid_org])
    grant_role(user, valid_org, RoleCode.ADMIN)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = rogue_org.pk
    session.save()

    response = client.get(reverse("projects:project-list"))

    assert response.status_code == HTTPStatus.OK
    assert client.session["active_org_id"] != rogue_org.pk
    user.refresh_from_db()
    valid_org_ids = set(
        user.memberships.filter(is_active=True).values_list(
            "org_id",
            flat=True,
        ),
    )
    assert client.session["active_org_id"] in valid_org_ids
    assert user.current_org_id == client.session["active_org_id"]
