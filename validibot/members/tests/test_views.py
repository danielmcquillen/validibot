from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client):
    org = OrganizationFactory()
    admin = UserFactory(orgs=[org])
    grant_role(admin, org, RoleCode.ADMIN)
    admin.set_current_org(org)
    client.force_login(admin)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    return client, org, admin


@pytest.mark.django_db
def test_member_list_requires_admin(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.get(reverse("members:member_list"))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_member_list_shows_members(admin_client):
    client, org, admin = admin_client
    response = client.get(reverse("members:member_list"))
    assert response.status_code == HTTPStatus.OK
    assert admin.email in response.content.decode()


@pytest.mark.django_db
def test_member_can_be_added(admin_client):
    client, org, admin = admin_client
    invitee = UserFactory()

    response = client.post(
        reverse("members:member_list"),
        data={
            "email": invitee.email,
            "roles": [RoleCode.EXECUTOR],
        },
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    assert Membership.objects.filter(user=invitee, org=org).exists()


@pytest.mark.django_db
def test_member_roles_can_be_updated(admin_client):
    client, org, admin = admin_client
    member = UserFactory()
    membership = Membership.objects.create(user=member, org=org, is_active=True)
    membership.set_roles({RoleCode.WORKFLOW_VIEWER})

    response = client.post(
        reverse("members:member_edit", kwargs={"member_id": membership.pk}),
        data={"roles": [RoleCode.ADMIN, RoleCode.EXECUTOR]},
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    membership.refresh_from_db()
    assert membership.has_role(RoleCode.ADMIN)


@pytest.mark.django_db
def test_member_delete_removes_viewer(admin_client):
    client, org, admin = admin_client
    viewer = UserFactory()
    viewer_membership = Membership.objects.create(user=viewer, org=org, is_active=True)
    viewer_membership.set_roles({RoleCode.WORKFLOW_VIEWER})

    response = client.post(
        reverse("members:member_delete", kwargs={"member_id": viewer_membership.pk}),
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    assert not Membership.objects.filter(pk=viewer_membership.pk).exists()


@pytest.mark.django_db
def test_member_delete_htmx_updates_list(admin_client):
    client, org, admin = admin_client
    viewer = UserFactory()
    viewer_membership = Membership.objects.create(user=viewer, org=org, is_active=True)
    viewer_membership.set_roles({RoleCode.WORKFLOW_VIEWER})

    client.get(reverse("members:member_list"))
    csrf_token = client.cookies["csrftoken"].value

    response = client.delete(
        reverse("members:member_delete", kwargs={"member_id": viewer_membership.pk}),
        HTTP_HX_REQUEST="true",
        HTTP_X_CSRFTOKEN=csrf_token,
    )

    assert response.status_code == HTTPStatus.OK
    assert "member-list-card" in response.content.decode()
    assert "success" in (response.headers.get("HX-Trigger") or "")
    assert not Membership.objects.filter(pk=viewer_membership.pk).exists()


@pytest.mark.django_db
def test_member_delete_prevents_removing_last_admin(admin_client):
    client, org, admin = admin_client
    membership = Membership.objects.get(user=admin, org=org)

    response = client.post(
        reverse("members:member_delete", kwargs={"member_id": membership.pk}),
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    assert Membership.objects.filter(pk=membership.pk).exists()


# Guest invite tests


@pytest.mark.django_db
def test_guest_list_view(admin_client):
    """Test that the guest list page loads."""
    client, org, admin = admin_client
    response = client.get(reverse("members:guest_list"))
    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_guest_invite_form_loads(admin_client):
    """Test that the guest invite modal form loads."""
    client, org, admin = admin_client
    response = client.get(reverse("members:guest_invite_create"))
    assert response.status_code == HTTPStatus.OK
    assert b"Invite Guest" in response.content


@pytest.mark.django_db
def test_guest_invite_create_with_all_workflows(admin_client):
    """Test creating a guest invite with scope=ALL."""
    from validibot.workflows.models import GuestInvite
    from validibot.workflows.tests.factories import WorkflowFactory

    client, org, admin = admin_client

    # Create some workflows
    WorkflowFactory(org=org, user=admin, is_active=True)
    WorkflowFactory(org=org, user=admin, is_active=True)

    response = client.post(
        reverse("members:guest_invite_create"),
        data={
            "email": "guest@example.com",
            "scope": "ALL",
        },
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    assert GuestInvite.objects.filter(
        org=org,
        invitee_email="guest@example.com",
        scope=GuestInvite.Scope.ALL,
    ).exists()


@pytest.mark.django_db
def test_guest_invite_create_with_selected_workflows(admin_client):
    """Test creating a guest invite with selected workflows."""
    from validibot.workflows.models import GuestInvite
    from validibot.workflows.tests.factories import WorkflowFactory

    client, org, admin = admin_client

    # Create some workflows
    wf1 = WorkflowFactory(org=org, user=admin, is_active=True)
    wf2 = WorkflowFactory(org=org, user=admin, is_active=True)

    response = client.post(
        reverse("members:guest_invite_create"),
        data={
            "email": "guest@example.com",
            "scope": "SELECTED",
            "workflows": [wf1.pk, wf2.pk],
        },
        follow=True,
    )

    assert response.status_code == HTTPStatus.OK
    invite = GuestInvite.objects.get(
        org=org,
        invitee_email="guest@example.com",
    )
    assert invite.scope == GuestInvite.Scope.SELECTED
    assert set(invite.workflows.values_list("pk", flat=True)) == {wf1.pk, wf2.pk}


@pytest.mark.django_db
def test_guest_invite_requires_email(admin_client):
    """Test that guest invite requires an email."""
    client, org, admin = admin_client

    response = client.post(
        reverse("members:guest_invite_create"),
        data={
            "email": "",
            "scope": "ALL",
        },
    )

    # Returns the form with error - 200 status
    assert response.status_code == HTTPStatus.OK
    assert b"Email address is required" in response.content


@pytest.mark.django_db
def test_guest_invite_selected_requires_workflows(admin_client):
    """Test that SELECTED scope requires at least one workflow."""
    client, org, admin = admin_client

    response = client.post(
        reverse("members:guest_invite_create"),
        data={
            "email": "guest@example.com",
            "scope": "SELECTED",
            "workflows": [],
        },
    )

    # Returns the form with error - 200 status
    assert response.status_code == HTTPStatus.OK
    assert b"Please select at least one workflow" in response.content
