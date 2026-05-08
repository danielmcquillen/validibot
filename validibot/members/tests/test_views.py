from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _enable_member_features():
    """Activate a Pro license with team + guest features for member tests.

    The root conftest autouse fixture snapshots and restores the
    license around every test, so no explicit reset is needed here.
    """
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset(
                {
                    CommercialFeature.TEAM_MANAGEMENT.value,
                    CommercialFeature.GUEST_MANAGEMENT.value,
                },
            ),
        ),
    )


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


# =============================================================================
# Guest invite role gating (PermissionCode.GUEST_INVITE)
# =============================================================================
#
# The guest-invite create view is gated on ``GUEST_INVITE``, which is
# bound to ``{ADMIN, AUTHOR, OWNER}`` in ``PERMISSION_DEFINITIONS`` so
# authors can send guest invites without admin-level authority. These
# tests pin the role binding and the view's mixin wiring — break either
# and the failure here tells reviewers which side has drifted.


def _client_as(client, role: RoleCode):
    """Build a logged-in client whose user holds ``role`` in a fresh org."""

    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, role)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    return client, org, user


@pytest.mark.django_db
def test_guest_invite_create_allows_author(client):
    """AUTHOR can hit the guest-invite create view.

    AUTHOR is in ``GUEST_INVITE``'s role set, so the view's permission
    gate lets them through. The role binding lives in
    ``PERMISSION_DEFINITIONS`` and is pinned by
    ``test_guest_invite_permission_definition_role_set`` below.
    """

    client, _, _ = _client_as(client, RoleCode.AUTHOR)
    response = client.get(reverse("members:guest_invite_create"))
    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_guest_invite_create_allows_owner(client):
    """OWNER retains access — ``OrgPermissionBackend`` short-circuits owners."""

    client, _, _ = _client_as(client, RoleCode.OWNER)
    response = client.get(reverse("members:guest_invite_create"))
    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_guest_invite_create_blocks_executor(client):
    """EXECUTOR cannot send guest invites — not in GUEST_INVITE's role set.

    The role binding is ``{ADMIN, AUTHOR, OWNER}``. EXECUTOR's job is
    running workflows, not managing access — confirms the gate denies.
    """

    client, _, _ = _client_as(client, RoleCode.EXECUTOR)
    response = client.get(reverse("members:guest_invite_create"))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_guest_invite_create_blocks_workflow_viewer(client):
    """WORKFLOW_VIEWER (read-only) cannot send guest invites."""

    client, _, _ = _client_as(client, RoleCode.WORKFLOW_VIEWER)
    response = client.get(reverse("members:guest_invite_create"))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_guest_invite_permission_definition_role_set():
    """Sanity-check the centralised role binding for GUEST_INVITE.

    Pinning the role set here means a future PR that quietly drops
    AUTHOR (or quietly adds EXECUTOR) shows up as a test failure instead
    of as silent behaviour change in production.
    """

    from validibot.users.constants import PermissionCode
    from validibot.users.permissions import roles_for_permission

    roles = roles_for_permission(PermissionCode.GUEST_INVITE)
    assert roles == frozenset(
        {
            RoleCode.AUTHOR,
            RoleCode.ADMIN,
            RoleCode.OWNER,
        },
    )


# Feature gating tests


@pytest.mark.django_db
def test_member_list_returns_404_without_feature(client):
    """Member views return 404 when team management feature is not enabled."""
    # Override the autouse Pro license with a Community one for this
    # single test. The root conftest autouse fixture restores the
    # baseline afterward.
    set_license(License(edition=Edition.COMMUNITY))

    org = OrganizationFactory()
    admin = UserFactory(orgs=[org])
    grant_role(admin, org, RoleCode.ADMIN)
    admin.set_current_org(org)
    client.force_login(admin)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.get(reverse("members:member_list"))
    assert response.status_code == HTTPStatus.NOT_FOUND
