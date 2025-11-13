import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import MembershipFactory


def _login_with_membership(client, membership):
    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


@pytest.mark.django_db
def test_viewer_nav_shows_limited_links(client):
    membership = MembershipFactory()
    membership.set_roles({RoleCode.VIEWER})
    _login_with_membership(client, membership)

    response = client.get(reverse("workflows:workflow_list"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Dashboard" not in html
    assert "Validator Library" not in html
    assert 'group-label mt-4">\n        Design' not in html
    assert 'group-label mt-4">\n        Analytics' not in html
    assert 'group-label mt-4">\n        Admin' not in html
    assert "Workflows" in html
    assert "Validation Runs" in html


@pytest.mark.django_db
def test_author_nav_shows_design_sections(client):
    membership = MembershipFactory()
    membership.set_roles({RoleCode.AUTHOR})
    _login_with_membership(client, membership)

    response = client.get(reverse("workflows:workflow_list"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Dashboard" in html
    assert "Validator Library" in html
    assert "group-label" in html
