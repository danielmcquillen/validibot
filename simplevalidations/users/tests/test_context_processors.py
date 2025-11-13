import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.context_processors import organization_context
from simplevalidations.users.tests.factories import MembershipFactory
from simplevalidations.users.tests.factories import MembershipFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory


def _attach_session(request):
    middleware = SessionMiddleware(lambda req: None)
    middleware.process_request(request)
    request.session.save()


@pytest.mark.django_db
def test_organization_context_clears_stale_session_scope():
    factory = RequestFactory()
    request = factory.get("/")
    _attach_session(request)

    rogue_org = OrganizationFactory(name="Ghost Org")
    user = UserFactory()
    request.user = user

    request.session["active_org_id"] = rogue_org.pk
    request.session.save()

    user.current_org = rogue_org
    user.save(update_fields=["current_org"])

    context = organization_context(request)

    assert context["active_org"] is not None
    assert context["active_org"].id != rogue_org.id
    assert request.session["active_org_id"] == context["active_org"].id

    user.refresh_from_db()
    assert user.current_org_id == context["active_org"].id
    valid_ids = set(
        user.memberships.filter(is_active=True).values_list("org_id", flat=True)
    )
    assert request.session["active_org_id"] in valid_ids


@pytest.mark.django_db
def test_organization_context_marks_owner_as_admin():
    factory = RequestFactory()
    request = factory.get("/")
    _attach_session(request)

    user = UserFactory()
    org = OrganizationFactory()
    membership = MembershipFactory(user=user, org=org)
    membership.set_roles({RoleCode.OWNER})
    user.set_current_org(org)
    request.user = user

    context = organization_context(request)
    assert context["active_membership"].has_role(RoleCode.OWNER)
    assert context["is_org_admin"] is True
    assert context["has_author_admin_owner"] is True


@pytest.mark.django_db
def test_viewer_lacks_author_admin_owner_flag():
    factory = RequestFactory()
    request = factory.get("/")
    _attach_session(request)

    membership = MembershipFactory()
    membership.set_roles({RoleCode.VIEWER})
    user = membership.user
    user.set_current_org(membership.org)
    request.user = user
    request.session["active_org_id"] = membership.org.id

    context = organization_context(request)
    assert context["has_author_admin_owner"] is False
    assert context["can_manage_validators"] is False
