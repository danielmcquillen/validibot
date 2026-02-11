from collections.abc import Sequence
from typing import Any

import factory
from factory import Faker
from factory import post_generation
from factory.django import DjangoModelFactory

from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.models import MembershipRole
from validibot.users.models import Organization
from validibot.users.models import Role
from validibot.users.models import User


class OrganizationFactory(DjangoModelFactory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Test Organization {n}")
    slug = factory.Sequence(lambda n: f"test-org-{n}")


class UserFactory(DjangoModelFactory[User]):
    class Meta:
        model = User
        django_get_or_create = ["username"]

    username = Faker("user_name")
    email = Faker("email")
    name = Faker("name")
    is_active = True

    @post_generation
    def password(self, create: bool, extracted: Sequence[Any], **kwargs):  # noqa: FBT001
        password = (
            extracted
            if extracted
            else Faker(
                "password",
                length=42,
                special_chars=True,
                digits=True,
                upper_case=True,
                lower_case=True,
            ).evaluate(None, None, extra={"locale": None})
        )
        self.set_password(password)

    @post_generation
    def orgs(self, create: bool, extracted: Sequence[Any], **kwargs):  # noqa: FBT001
        if not create:
            # Simple build, do nothing.
            return

        if extracted:
            # A list of organizations were passed in, use them
            for org in extracted:
                self.orgs.add(org)
        else:
            # Create a default organization if none were provided

            org = OrganizationFactory()
            self.orgs.add(org)

    @classmethod
    def _after_postgeneration(cls, instance, create, results=None):
        """Save again the instance if creating and at least one hook ran."""
        if create and results and not cls._meta.skip_postgeneration_save:
            # Some post-generation hooks ran, and may have modified us.
            instance.save()


class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role

    code = RoleCode.EXECUTOR
    name = "Executor"


class MembershipFactory(DjangoModelFactory):
    class Meta:
        model = Membership

    user = factory.SubFactory(UserFactory)
    org = factory.SubFactory(OrganizationFactory)
    is_active = True


class MembershipRoleFactory(DjangoModelFactory):
    class Meta:
        model = MembershipRole

    membership = factory.SubFactory(MembershipFactory)
    role = factory.SubFactory(RoleFactory)


def grant_role(user: User, org: Organization, role_code: RoleCode) -> None:
    """
    Ensure user has an active membership in org with the given role.
    """
    membership, _ = Membership.objects.get_or_create(
        user=user,
        org=org,
        defaults={"is_active": True},
    )
    if not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active"])
    membership.add_role(role_code)
