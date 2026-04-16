import pytest

from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.models import User
from validibot.users.tests.factories import MembershipFactory


def test_user_get_absolute_url(user: User):
    assert user.get_absolute_url() == f"/app/users/{user.username}/"


def test_personal_org_assigns_executor_role(db):
    user = User.objects.create(username="solo-user", email="solo@example.com")

    org = user.get_current_org()
    membership = Membership.objects.get(user=user, org=org)

    assert org.is_personal is True
    assert membership.has_role(RoleCode.OWNER)
    assert membership.has_role(RoleCode.EXECUTOR)
    assert membership.has_role(RoleCode.ADMIN)
    default_project = org.projects.first()
    assert default_project is not None
    assert default_project.is_default


def test_personal_org_slug_uses_username_not_display_name(db):
    """Personal-org slugs are derived from the username, not the display name.

    Without this, every personal-org URL would contain "-workspace" (e.g.
    ``/orgs/alice-workspace/``) because the display name is ``"Alice's
    Workspace"``. The slug base should be the raw username so URLs look like
    ``/orgs/alice/`` — matching the GitHub/GitLab convention where a user's
    personal namespace is just their handle.

    The display name is still ``"Alice's Workspace"`` for UI clarity; only
    the slug diverges.
    """
    user = User.objects.create(
        username="alice",
        email="alice@example.com",
        name="Alice Example",
    )

    org = user.get_current_org()

    assert org.is_personal is True
    assert org.slug == "alice", f"expected slug derived from username, got {org.slug!r}"
    # Display name keeps the 'Workspace' suffix — only the slug is cleaned up.
    assert "Workspace" in org.name


def test_personal_org_slug_collision_appends_counter(db):
    """Two users whose usernames slugify to the same base get distinct slugs.

    ``_generate_unique_slug`` appends ``-2``, ``-3``, etc. on collision.
    This test pins that behaviour for the personal-org path specifically —
    if the slug source ever changes back to a form that can't collide (or
    the collision helper is swapped out), we want the failure to be loud.
    """
    user_a = User.objects.create(username="bob", email="bob1@example.com")
    user_b = User.objects.create(username="Bob", email="bob2@example.com")

    org_a = user_a.get_current_org()
    org_b = user_b.get_current_org()

    assert org_a.slug == "bob"
    # Both usernames slugify to "bob"; the second must get a suffix.
    assert org_b.slug == "bob-2"


@pytest.mark.django_db
def test_owner_role_assigns_all_permissions():
    membership = MembershipFactory()
    membership.set_roles({RoleCode.OWNER})

    assert set(membership.role_codes) == set(RoleCode.values)
    assert membership.is_admin
    assert membership.has_author_admin_owner_privileges
