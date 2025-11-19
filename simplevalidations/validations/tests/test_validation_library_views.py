import json
from http import HTTPStatus

import pytest
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import MembershipFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.validations.constants import (
    VALIDATION_LIBRARY_LAYOUT_SESSION_KEY,
)
from simplevalidations.validations.models import Validator
from simplevalidations.validations.utils import create_custom_validator
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


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
        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "EnergyPlus Validation" in content
        assert "Modelica Validator" in content

    def test_library_page_honors_tab_query_param(self, client):
        self._setup_user(client, RoleCode.OWNER)
        response = client.get(
            f"{reverse('validations:validation_library')}?tab=system",
        )
        assert response.status_code == HTTPStatus.OK
        assert response.context["active_tab"] == "system"

    def test_library_layout_persists_in_session(self, client):
        self._setup_user(client, RoleCode.OWNER)
        url = reverse("validations:validation_library")

        response = client.get(f"{url}?layout=list")
        assert response.status_code == HTTPStatus.OK
        assert response.context["current_layout"] == "list"
        assert client.session[VALIDATION_LIBRARY_LAYOUT_SESSION_KEY] == "list"

        response = client.get(url)
        assert response.context["current_layout"] == "list"

    def test_library_page_requires_author_admin_owner(self, client):
        self._setup_user(client, RoleCode.WORKFLOW_VIEWER)
        response = client.get(reverse("validations:validation_library"))
        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]

    def test_create_custom_validator(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "KerML Validator",
                "description": "Checks KerML outputs",
                "custom_type": "KERML",
                "notes": "First iteration",
            },
            follow=True,
        )
        assert response.status_code == HTTPStatus.OK
        assert Validator.objects.filter(
            name="KerML Validator",
            org=org,
            is_system=False,
        ).exists()

    def test_create_requires_permission(self, client):
        self._setup_user(client, RoleCode.WORKFLOW_VIEWER)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "Unauthorized",
                "description": "",
                "custom_type": "MODELICA",
            },
        )
        assert response.status_code == HTTPStatus.FOUND
        assert not Validator.objects.filter(name="Unauthorized").exists()

    def test_create_breadcrumb_includes_create_label(self, client):
        self._setup_user(client, RoleCode.AUTHOR)
        response = client.get(reverse("validations:custom_validator_create"))
        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        assert breadcrumbs[-1]["name"] == "Create new validator"

    def test_edit_breadcrumb_uses_validator_name(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="Room Automation",
            description="Validates room schedules.",
            custom_type="MODELICA",
        )
        response = client.get(
            reverse(
                "validations:custom_validator_update",
                kwargs={"slug": custom_validator.validator.slug},
            ),
        )
        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        assert breadcrumbs[-1]["name"] == "Edit Settings"
        assert "Room Automation" in breadcrumbs[-2]["name"]

    def test_system_validator_detail_preserves_tab_query(self, client):
        self._setup_user(client, RoleCode.ADMIN)
        validator = Validator.objects.create(
            name="System Check",
            slug="system-check",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_detail",
                kwargs={"slug": validator.slug},
            )
            + "?tab=system"
        )

        assert response.status_code == HTTPStatus.OK
        assert response.context["return_tab"] == "system"

    def test_validator_detail_requires_author_admin_owner(self, client):
        self._setup_user(client, RoleCode.EXECUTOR)
        validator = Validator.objects.create(
            name="System Check",
            slug="system-check",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
        )
        response = client.get(
            reverse(
                "validations:validator_detail",
                kwargs={"slug": validator.slug},
            )
        )
        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]

    def test_htmx_delete_custom_validator_succeeds(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="Transient Validator",
            description="Delete me",
            custom_type="MODELICA",
        )
        client.get(reverse("validations:validation_library"))
        csrf_token = client.cookies["csrftoken"].value

        response = client.delete(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=f"validator-card-{custom_validator.validator.pk}",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == HTTPStatus.OK
        assert not Validator.objects.filter(pk=custom_validator.validator.pk).exists()
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["toast"]["level"] == "success"

    def test_htmx_delete_custom_validator_blocked_when_in_use(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="In Use Validator",
            description="Still referenced",
            custom_type="MODELICA",
        )
        workflow = WorkflowFactory(org=org, user=user)
        WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)

        client.get(reverse("validations:validation_library"))
        csrf_token = client.cookies["csrftoken"].value

        response = client.delete(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=f"validator-card-{custom_validator.validator.pk}",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == 400
        assert Validator.objects.filter(pk=custom_validator.validator.pk).exists()
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["toast"]["level"] == "danger"
