"""Module for all Form Tests."""

import pytest

from django.utils.translation import gettext_lazy as _

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.forms import OrganizationMemberForm
from simplevalidations.users.forms import OrganizationMemberRolesForm
from simplevalidations.users.forms import UserAdminCreationForm
from simplevalidations.users.models import User
from simplevalidations.users.tests.factories import MembershipFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory


class TestUserAdminCreationForm:
    """
    Test class for all tests related to the UserAdminCreationForm
    """

    def test_username_validation_error_msg(self, user: User):
        """
        Tests UserAdminCreation Form's unique validator functions correctly by testing:
            1) A new user with an existing username cannot be added.
            2) Only 1 error is raised by the UserCreation Form
            3) The desired error message is raised
        """

        # The user already exists,
        # hence cannot be created.
        form = UserAdminCreationForm(
            {
                "username": user.username,
                "password1": user.password,
                "password2": user.password,
            },
        )

        assert not form.is_valid()
        assert len(form.errors) == 1
        assert "username" in form.errors
        assert form.errors["username"][0] == _("This username has already been taken.")


@pytest.mark.django_db
class TestOrganizationMemberForm:
    def test_owner_role_not_assignable(self):
        org = OrganizationFactory()
        user = UserFactory()
        empty_form = OrganizationMemberForm(organization=org)
        owner_option = next(
            option for option in empty_form.role_options if option["value"] == RoleCode.OWNER
        )
        assert owner_option["disabled"] is True

        form = OrganizationMemberForm(
            data={
                "email": user.email,
                "roles": [RoleCode.ADMIN, RoleCode.OWNER],
            },
            organization=org,
        )
        assert not form.is_valid()
        assert "roles" in form.errors


@pytest.mark.django_db
class TestOrganizationMemberRolesForm:
    def test_non_owner_cannot_assign_owner(self):
        membership = MembershipFactory()
        form = OrganizationMemberRolesForm(
            data={"roles": [RoleCode.ADMIN, RoleCode.OWNER]},
            membership=membership,
        )

        assert not form.is_valid()
        assert "Owner role cannot be assigned" in form.errors["roles"][0]

    def test_owner_roles_locked_and_disabled(self):
        membership = MembershipFactory()
        membership.set_roles({RoleCode.OWNER})
        form = OrganizationMemberRolesForm(
            data={"roles": [RoleCode.ADMIN]},
            membership=membership,
        )

        assert not form.is_valid()
        assert "Owner role cannot be removed" in form.errors["roles"][0]
        for option in form.role_options:
            assert option["disabled"] is True

    def test_owner_save_keeps_all_roles(self):
        membership = MembershipFactory()
        membership.set_roles({RoleCode.OWNER})
        form = OrganizationMemberRolesForm(
            data={"roles": [RoleCode.OWNER]},
            membership=membership,
        )

        assert form.is_valid()
        form.save()
        assert set(membership.role_codes) == set(RoleCode.values)
