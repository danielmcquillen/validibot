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

    def test_admin_implications_apply_on_invite(self):
        org = OrganizationFactory()
        user = UserFactory()
        form = OrganizationMemberForm(
            data={
                "email": user.email,
                "roles": [RoleCode.ADMIN],
            },
            organization=org,
        )

        assert form.is_valid()
        membership = form.save()
        assert membership.has_role(RoleCode.ADMIN)
        assert membership.has_role(RoleCode.AUTHOR)
        assert membership.has_role(RoleCode.EXECUTOR)
        assert membership.has_role(RoleCode.VALIDATION_RESULTS_VIEWER)
        assert membership.has_role(RoleCode.WORKFLOW_VIEWER)


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

    def test_admin_implications_enforced_and_disabled(self):
        membership = MembershipFactory()
        form = OrganizationMemberRolesForm(
            data={"roles": [RoleCode.ADMIN]},
            membership=membership,
        )

        assert form.is_valid()
        roles = set(form.cleaned_data["roles"])
        assert {
            RoleCode.ADMIN,
            RoleCode.AUTHOR,
            RoleCode.EXECUTOR,
            RoleCode.ANALYTICS_VIEWER,
            RoleCode.VALIDATION_RESULTS_VIEWER,
            RoleCode.WORKFLOW_VIEWER,
        }.issubset(roles)
        options = {option["value"]: option for option in form.role_options}
        assert options[RoleCode.AUTHOR]["implied"] is True
        assert options[RoleCode.AUTHOR]["disabled"] is True
        assert options[RoleCode.ANALYTICS_VIEWER]["implied"] is True
        assert options[RoleCode.ANALYTICS_VIEWER]["disabled"] is True
        assert options[RoleCode.VALIDATION_RESULTS_VIEWER]["implied"] is True
        assert options[RoleCode.VALIDATION_RESULTS_VIEWER]["disabled"] is True

    def test_author_implies_executor(self):
        membership = MembershipFactory()
        form = OrganizationMemberRolesForm(
            data={"roles": [RoleCode.AUTHOR]},
            membership=membership,
        )

        assert form.is_valid()
        roles = set(form.cleaned_data["roles"])
        assert RoleCode.AUTHOR in roles
        assert RoleCode.EXECUTOR in roles
        assert RoleCode.ANALYTICS_VIEWER in roles
        assert RoleCode.VALIDATION_RESULTS_VIEWER in roles
        assert RoleCode.WORKFLOW_VIEWER in roles

    def test_prior_checked_viewer_roles_disable_when_author_selected(self):
        membership = MembershipFactory()
        form = OrganizationMemberRolesForm(
            data={
                "roles": [
                    RoleCode.WORKFLOW_VIEWER,
                    RoleCode.VALIDATION_RESULTS_VIEWER,
                    RoleCode.AUTHOR,
                ],
            },
            membership=membership,
        )

        assert form.is_valid()
        options = {option["value"]: option for option in form.role_options}
        assert options[RoleCode.WORKFLOW_VIEWER]["disabled"] is True
        assert options[RoleCode.VALIDATION_RESULTS_VIEWER]["disabled"] is True
        assert options[RoleCode.EXECUTOR]["disabled"] is True
        assert options[RoleCode.ANALYTICS_VIEWER]["disabled"] is True

    def test_member_form_renders_implied_roles_disabled(self):
        membership = MembershipFactory()
        membership.set_roles({RoleCode.ADMIN})
        form = OrganizationMemberRolesForm(membership=membership)
        admin_option = next(option for option in form.role_options if option["value"] == RoleCode.ADMIN)
        author_option = next(option for option in form.role_options if option["value"] == RoleCode.AUTHOR)
        executor_option = next(option for option in form.role_options if option["value"] == RoleCode.EXECUTOR)
        assert admin_option["checked"] is True
        assert author_option["checked"] is True
        assert author_option["disabled"] is True
        assert executor_option["checked"] is True
        assert executor_option["disabled"] is True
