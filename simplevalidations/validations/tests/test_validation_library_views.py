import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import MembershipFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.validations.utils import create_custom_validator
from simplevalidations.validations.models import Validator


@pytest.mark.django_db
class TestValidationLibraryViews:
    def _setup_user(self, client, role: str = RoleCode.ADMIN):
        org = OrganizationFactory()
        user = UserFactory()
        membership = MembershipFactory(user=user, org=org)
        membership.add_role(role)
        user.set_current_org(org)
        client.force_login(user)
        session = client.session
        session["active_org_id"] = org.id
        session.save()
        return user, org

    def test_library_page_lists_validators(self, client):
        user, org = self._setup_user(client, RoleCode.OWNER)
        Validator.objects.create(
            name="EnergyPlus Validation",
            slug="energyplus-validation",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
        )
        create_custom_validator(
            org=org,
            user=user,
            name="Modelica Validator",
            description="Custom validator description",
            custom_type="MODELICA",
        )

        response = client.get(reverse("validations:validation_library"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "EnergyPlus Validation" in content
        assert "Modelica Validator" in content

    def test_create_custom_validator(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "PyWinCalc Validator",
                "description": "Checks PyWinCalc outputs",
                "custom_type": "PYWINCALC",
                "notes": "First iteration",
            },
            follow=True,
        )
        assert response.status_code == 200
        assert Validator.objects.filter(
            name="PyWinCalc Validator",
            org=org,
            is_system=False,
        ).exists()

    def test_create_requires_permission(self, client):
        self._setup_user(client, RoleCode.VIEWER)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "Unauthorized",
                "description": "",
                "custom_type": "MODELICA",
            },
        )
        assert response.status_code == 302
        assert not Validator.objects.filter(name="Unauthorized").exists()
