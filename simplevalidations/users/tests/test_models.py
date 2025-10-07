from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Membership
from simplevalidations.users.models import User


def test_user_get_absolute_url(user: User):
    assert user.get_absolute_url() == f"/app/users/{user.username}/"


def test_personal_org_assigns_executor_role(db):
    user = User.objects.create(username="solo-user", email="solo@example.com")

    assert user.memberships.count() == 0

    org = user.get_current_org()
    membership = Membership.objects.get(user=user, org=org)

    assert membership.has_role(RoleCode.OWNER)
    assert membership.has_role(RoleCode.EXECUTOR)
