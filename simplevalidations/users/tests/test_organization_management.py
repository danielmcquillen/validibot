import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Membership, Organization
from simplevalidations.users.tests.factories import OrganizationFactory, UserFactory, grant_role


@pytest.fixture
def admin_user(db):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.ADMIN)
    user.set_current_org(org)
    return user, org


@pytest.fixture
def client_logged_in(client, admin_user):
    user, org = admin_user
    client.force_login(user)
    return client, user, org


@pytest.mark.django_db
def test_organization_list_requires_admin(client):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.VIEWER)
    client.force_login(user)

    response = client.get(reverse("users:organization-list"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_organization_list_shows_admin_orgs(client_logged_in):
    client, user, org = client_logged_in

    response = client.get(reverse("users:organization-list"))
    assert response.status_code == 200
    assert org.name in response.content.decode()


@pytest.mark.django_db
def test_organization_create_assigns_admin(client_logged_in):
    client, user, _ = client_logged_in
    response = client.post(
        reverse("users:organization-create"),
        data={"name": "Research Lab"},
        follow=True,
    )
    assert response.status_code == 200
    org = Organization.objects.get(name="Research Lab")
    membership = Membership.objects.get(user=user, org=org)
    assert RoleCode.ADMIN in membership.role_codes
    assert RoleCode.OWNER in membership.role_codes
    assert client.session["active_org_id"] == org.id


@pytest.mark.django_db
def test_organization_update_changes_name(client_logged_in):
    client, user, org = client_logged_in
    response = client.post(
        reverse("users:organization-update", args=[org.pk]),
        data={"name": "Updated Org"},
        follow=True,
    )
    assert response.status_code == 200
    org.refresh_from_db()
    assert org.name == "Updated Org"


@pytest.mark.django_db
def test_organization_delete_requires_another_admin(client_logged_in):
    client, user, org = client_logged_in
    membership = Membership.objects.get(user=user, org=org)
    assert not Membership.objects.filter(
        org=org,
        is_active=True,
        membership_roles__role__code=RoleCode.ADMIN,
    ).exclude(pk=membership.pk).distinct().exists()
    response = client.post(reverse("users:organization-delete", args=[org.pk]))
    assert response.status_code == 302
    assert Organization.objects.filter(pk=org.pk).exists()


@pytest.mark.django_db
def test_add_member_to_organization(client_logged_in):
    client, user, org = client_logged_in
    invitee = UserFactory()
    response = client.post(
        reverse("users:organization-detail", args=[org.pk]),
        data={"email": invitee.email, "roles": [RoleCode.EXECUTOR]},
        follow=True,
    )
    assert response.status_code == 200
    membership = Membership.objects.filter(user=invitee, org=org).first()
    assert membership is not None
    assert RoleCode.EXECUTOR in membership.role_codes


@pytest.mark.django_db
def test_update_member_roles_requires_remaining_admin(client_logged_in):
    client, user, org = client_logged_in
    other = UserFactory(orgs=[org])
    grant_role(other, org, RoleCode.ADMIN)

    membership = Membership.objects.get(user=other, org=org)
    response = client.post(
        reverse("users:organization-member-update", args=[org.pk, membership.pk]),
        data={"roles": [RoleCode.EXECUTOR]},
        follow=True,
    )
    assert response.status_code == 200
    membership.refresh_from_db()
    assert RoleCode.ADMIN not in membership.role_codes


@pytest.mark.django_db
def test_remove_member_prevents_last_admin(client_logged_in):
    client, user, org = client_logged_in
    response = client.post(
        reverse("users:organization-member-delete", args=[org.pk, Membership.objects.get(user=user, org=org).pk]),
    )
    assert response.status_code == 302
    assert Membership.objects.filter(user=user, org=org).exists()


@pytest.mark.django_db
def test_switch_current_org_updates_session(client, db):
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.ADMIN)
    client.force_login(user)

    response = client.post(
        reverse("users:organization-switch", args=[org.pk]),
        follow=True,
    )
    assert response.status_code == 200
    assert client.session["active_org_id"] == org.id
