"""
Tests for Validator Resource File CRUD operations, RBAC, and file validation.

Covers:
- ResourceTypeConfig registry and EPW header validation
- ValidatorResourceFileForm file validation chain (extension, size, magic, header)
- Model clean() extension validation
- RBAC: ADMIN/OWNER can CUD, AUTHOR can only view
- Resource file tab view (visible to all with VALIDATOR_VIEW)
- Delete blocker checks (active workflow step references)
- Tabbed validator detail page navigation
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

if TYPE_CHECKING:
    from django.test import Client

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ResourceTypeConfig
from validibot.validations.constants import ValidationType
from validibot.validations.constants import _validate_epw_header
from validibot.validations.constants import get_resource_type_config
from validibot.validations.forms import ValidatorResourceFileForm
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# ResourceTypeConfig registry tests
# ---------------------------------------------------------------------------


class TestResourceTypeConfig:
    """Tests for the declarative resource type configuration registry."""

    def test_epw_config_exists(self):
        """EPW weather file type has a registered config."""
        config = get_resource_type_config(ResourceFileType.ENERGYPLUS_WEATHER)
        assert config is not None
        assert "epw" in config.allowed_extensions
        assert config.max_size_bytes == 15 * 1024 * 1024

    def test_unknown_type_returns_none(self):
        """Unknown resource types return None."""
        config = get_resource_type_config("nonexistent_type")
        assert config is None

    def test_epw_header_validator_valid(self):
        """EPW files starting with 'LOCATION,' pass validation."""
        assert _validate_epw_header(b"LOCATION,San Francisco") is True

    def test_epw_header_validator_invalid(self):
        """Files not starting with 'LOCATION,' fail validation."""
        assert _validate_epw_header(b"NOT_A_WEATHER_FILE") is False
        assert _validate_epw_header(b"") is False

    def test_config_is_frozen(self):
        """ResourceTypeConfig instances are immutable."""
        config = ResourceTypeConfig(
            allowed_extensions=frozenset({"test"}),
            max_size_bytes=1024,
        )
        with pytest.raises(AttributeError):
            config.max_size_bytes = 2048  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model clean() validation tests
# ---------------------------------------------------------------------------


class TestModelCleanValidation:
    """Tests for ValidatorResourceFile.clean() extension validation."""

    def test_clean_valid_epw_extension(self):
        """Valid .epw extension passes model clean()."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test",
            filename="weather.epw",
        )
        # Should not raise
        resource.clean()

    def test_clean_invalid_extension_raises(self):
        """Invalid extension raises ValidationError."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test",
            filename="weather.csv",
        )
        with pytest.raises(ValidationError) as exc_info:
            resource.clean()
        assert "filename" in exc_info.value.message_dict

    def test_clean_no_extension_raises(self):
        """Filename without extension raises ValidationError."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test",
            filename="weather_no_ext",
        )
        with pytest.raises(ValidationError):
            resource.clean()

    def test_clean_unknown_resource_type_skips_validation(self):
        """Unknown resource type skips extension validation."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile(
            validator=validator,
            resource_type="unknown_type",
            name="Test",
            filename="anything.xyz",
        )
        # Should not raise -- no config for this type
        resource.clean()


# ---------------------------------------------------------------------------
# Form validation tests
# ---------------------------------------------------------------------------


class TestValidatorResourceFileForm:
    """Tests for ValidatorResourceFileForm file validation chain."""

    def _make_epw_file(
        self,
        name: str = "weather.epw",
        content: bytes = b"LOCATION,San Francisco",
        size: int | None = None,
    ) -> SimpleUploadedFile:
        """Create a valid EPW file for testing."""
        if size is not None:
            content = b"LOCATION," + b"x" * (size - 9)
        return SimpleUploadedFile(
            name=name,
            content=content,
            content_type="application/octet-stream",
        )

    def test_valid_epw_upload(self):
        """Valid EPW file passes all validation checks."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        uploaded = self._make_epw_file()
        form = ValidatorResourceFileForm(
            data={
                "name": "Test Weather",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "is_default": False,
            },
            files={"file": uploaded},
            validator=validator,
        )
        assert form.is_valid(), form.errors

    def test_wrong_extension_rejected(self):
        """File with wrong extension is rejected."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        uploaded = SimpleUploadedFile(
            name="weather.csv",
            content=b"LOCATION,San Francisco",
            content_type="text/csv",
        )
        form = ValidatorResourceFileForm(
            data={
                "name": "Test",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "is_default": False,
            },
            files={"file": uploaded},
            validator=validator,
        )
        assert not form.is_valid()
        assert "file" in form.errors

    def test_oversized_file_rejected(self):
        """File exceeding max size is rejected."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        max_bytes = 15 * 1024 * 1024  # 15 MB
        uploaded = self._make_epw_file(size=max_bytes + 1)
        form = ValidatorResourceFileForm(
            data={
                "name": "Large File",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "is_default": False,
            },
            files={"file": uploaded},
            validator=validator,
        )
        assert not form.is_valid()
        assert "file" in form.errors
        assert "too large" in str(form.errors["file"])

    def test_suspicious_magic_bytes_rejected(self):
        """Files with suspicious magic bytes (ZIP, PDF, etc.) are rejected."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        # ZIP magic bytes
        uploaded = SimpleUploadedFile(
            name="weather.epw",
            content=b"PK\x03\x04" + b"fake zip content",
            content_type="application/octet-stream",
        )
        form = ValidatorResourceFileForm(
            data={
                "name": "Suspicious File",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "is_default": False,
            },
            files={"file": uploaded},
            validator=validator,
        )
        assert not form.is_valid()
        assert "file" in form.errors
        assert "binary archive" in str(form.errors["file"])

    def test_bad_header_content_rejected(self):
        """EPW file with wrong header content is rejected."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        uploaded = SimpleUploadedFile(
            name="weather.epw",
            content=b"NOT_A_VALID_EPW_FILE_HEADER",
            content_type="application/octet-stream",
        )
        form = ValidatorResourceFileForm(
            data={
                "name": "Bad Header",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "is_default": False,
            },
            files={"file": uploaded},
            validator=validator,
        )
        assert not form.is_valid()
        assert "file" in form.errors
        assert "does not match" in str(form.errors["file"])

    def test_edit_form_excludes_file_and_type_fields(self):
        """Edit form only shows metadata fields (name, description, is_default)."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Existing",
            filename="existing.epw",
        )
        form = ValidatorResourceFileForm(
            instance=resource,
            validator=validator,
            is_edit=True,
        )
        assert "file" not in form.fields
        assert "resource_type" not in form.fields
        assert "name" in form.fields
        assert "description" in form.fields
        assert "is_default" in form.fields


# ---------------------------------------------------------------------------
# RBAC / Permission tests
# ---------------------------------------------------------------------------


def _setup_user(client: Client, org, role: str):
    """Create authenticated user with org context and role."""
    user = UserFactory()
    grant_role(user, org, role)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.id
    session.save()
    return user


class TestResourceFilePermissions:
    """Tests for RBAC on resource file CRUD operations."""

    def test_admin_can_view_resource_files_tab(self, client):
        """ADMIN can view the resource files tab."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        assert "Resource Files" in response.content.decode()

    def test_author_can_view_resource_files_tab(self, client):
        """AUTHOR can view the resource files tab (read-only)."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.AUTHOR)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        # Author should not see the "Add file" button
        assert "Add file" not in response.content.decode()

    def test_admin_sees_add_button(self, client):
        """ADMIN sees the Add file button on resource files tab."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert "Add file" in response.content.decode()

    def test_author_cannot_create_resource_file(self, client):
        """AUTHOR is denied when trying to create a resource file."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.AUTHOR)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:resource_file_create",
            kwargs={"pk": validator.pk},
        )
        response = client.post(url, data={})
        # Should redirect (permission denied)
        assert response.status_code == HTTPStatus.FOUND

    def test_admin_can_create_resource_file(self, client):
        """ADMIN can upload a resource file via POST."""
        org = OrganizationFactory()
        user = _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )

        uploaded = SimpleUploadedFile(
            name="test_weather.epw",
            content=b"LOCATION,Test City",
            content_type="application/octet-stream",
        )
        url = reverse(
            "validations:resource_file_create",
            kwargs={"pk": validator.pk},
        )
        response = client.post(
            url,
            data={
                "name": "Test Weather",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "file": uploaded,
                "is_default": False,
                "description": "",
            },
        )
        # Should redirect on success
        assert response.status_code == HTTPStatus.FOUND

        # Verify resource was created
        rf = ValidatorResourceFile.objects.get(validator=validator)
        assert rf.name == "Test Weather"
        assert rf.uploaded_by == user
        assert rf.org == org

    def test_admin_can_update_resource_file(self, client):
        """ADMIN can update resource file metadata."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Original Name",
            filename="weather.epw",
        )

        url = reverse(
            "validations:resource_file_update",
            kwargs={"pk": validator.pk, "rf_pk": resource.pk},
        )
        response = client.post(
            url,
            data={
                "name": "Updated Name",
                "description": "Updated description",
                "is_default": True,
            },
        )
        assert response.status_code == HTTPStatus.FOUND

        resource.refresh_from_db()
        assert resource.name == "Updated Name"
        assert resource.description == "Updated description"
        assert resource.is_default is True

    def test_cannot_edit_system_wide_resource_via_ui(self, client):
        """System-wide resources (org=NULL) cannot be edited via the UI."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,  # system-wide
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="System Weather",
            filename="system.epw",
        )

        url = reverse(
            "validations:resource_file_update",
            kwargs={"pk": validator.pk, "rf_pk": system_resource.pk},
        )
        response = client.post(
            url,
            data={"name": "Hacked Name", "description": "", "is_default": False},
        )
        # Should 404 because the view filters by org=active_org
        assert response.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Delete blocker tests
# ---------------------------------------------------------------------------


class TestResourceFileDeleteBlockers:
    """Tests for deletion safeguards when resource files are in use."""

    def test_delete_unreferenced_resource_succeeds(self, client):
        """Resource file not referenced by any workflow can be deleted."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Deletable",
            filename="deletable.epw",
        )

        url = reverse(
            "validations:resource_file_delete",
            kwargs={"pk": validator.pk, "rf_pk": resource.pk},
        )
        response = client.post(url, HTTP_HX_REQUEST="true")

        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not ValidatorResourceFile.objects.filter(pk=resource.pk).exists()

    def test_delete_blocked_by_active_workflow(self, client):
        """Resource file referenced by active workflow step cannot be deleted."""
        org = OrganizationFactory()
        user = _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="In Use",
            filename="in_use.epw",
        )

        # Create active workflow with step referencing this resource
        workflow = WorkflowFactory(org=org, user=user, is_active=True)
        WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            name="EP Step",
            order=1,
            config={"resource_file_ids": [str(resource.id)]},
        )

        url = reverse(
            "validations:resource_file_delete",
            kwargs={"pk": validator.pk, "rf_pk": resource.pk},
        )
        response = client.post(url, HTTP_HX_REQUEST="true")

        # Should return 400 with toast error
        assert response.status_code == HTTPStatus.BAD_REQUEST
        trigger = json.loads(response["HX-Trigger"])
        assert trigger["toast"]["level"] == "danger"
        assert "active workflow" in trigger["toast"]["message"].lower()

        # Resource should still exist
        assert ValidatorResourceFile.objects.filter(pk=resource.pk).exists()

    def test_delete_allowed_when_only_inactive_workflows_reference(self, client):
        """Resource file referenced only by inactive workflows can be deleted."""
        org = OrganizationFactory()
        user = _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Old File",
            filename="old.epw",
        )

        # Create INACTIVE workflow with step referencing this resource
        workflow = WorkflowFactory(org=org, user=user, is_active=False)
        WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            name="EP Step",
            order=1,
            config={"resource_file_ids": [str(resource.id)]},
        )

        url = reverse(
            "validations:resource_file_delete",
            kwargs={"pk": validator.pk, "rf_pk": resource.pk},
        )
        response = client.post(url, HTTP_HX_REQUEST="true")

        # Should succeed -- inactive workflow doesn't block
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not ValidatorResourceFile.objects.filter(pk=resource.pk).exists()

    def test_cannot_delete_system_wide_resource_via_ui(self, client):
        """System-wide resources cannot be deleted via the UI."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,  # system-wide
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="System Weather",
            filename="system.epw",
        )

        url = reverse(
            "validations:resource_file_delete",
            kwargs={"pk": validator.pk, "rf_pk": system_resource.pk},
        )
        response = client.post(url, HTTP_HX_REQUEST="true")

        # Should 404 because the view filters by org=active_org
        assert response.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Tab navigation tests
# ---------------------------------------------------------------------------


class TestTabbedValidatorDetail:
    """Tests for the tabbed validator detail page layout."""

    def test_description_tab_is_default(self, client):
        """Visiting validator detail shows the Description tab."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        # Description tab should be active
        assert 'class="nav-link active"' in html
        assert "Description" in html

    def test_signals_tab_loads(self, client):
        """Signals tab URL renders correctly."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_signals_tab",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "Signals" in html

    def test_assertions_tab_loads(self, client):
        """Assertions tab URL renders correctly."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_assertions_tab",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "Default Assertions" in html

    def test_resource_files_tab_loads(self, client):
        """Resource files tab URL renders correctly."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "Resource Files" in html

    def test_all_four_tabs_visible_for_energyplus(self, client):
        """All four tab links are visible for validators that support resource files."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )

        for url_name in [
            "validations:validator_detail",
            "validations:validator_signals_tab",
            "validations:validator_assertions_tab",
            "validations:validator_resource_files",
        ]:
            url = reverse(url_name, kwargs={"slug": validator.slug})
            response = client.get(url)
            html = response.content.decode()
            assert "Description" in html
            assert "Signals" in html
            assert "Default Assertions" in html
            assert "Resource Files" in html

    def test_resource_files_tab_hidden_for_non_supported_validators(self, client):
        """Resource Files tab is hidden for non-resource-file validators."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        html = response.content.decode()
        assert "Description" in html
        assert "Signals" in html
        assert "Default Assertions" in html
        assert "Resource Files" not in html

    def test_resource_files_tab_shows_files(self, client):
        """Resource files tab lists existing resource files."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Chicago TMY3",
            filename="chicago.epw",
        )

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        html = response.content.decode()
        assert "Chicago TMY3" in html

    def test_resource_files_tab_shows_empty_state(self, client):
        """Resource files tab shows empty state when no files exist."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:validator_resource_files",
            kwargs={"slug": validator.slug},
        )
        response = client.get(url)
        html = response.content.decode()
        assert "No resource files" in html


# ---------------------------------------------------------------------------
# HTMX create flow tests
# ---------------------------------------------------------------------------


class TestResourceFileCreateHTMX:
    """Tests for the HTMX resource file upload flow."""

    def test_get_returns_modal_form(self, client):
        """GET request with HX-Request returns the create modal form."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        url = reverse(
            "validations:resource_file_create",
            kwargs={"pk": validator.pk},
        )
        response = client.get(url, HTTP_HX_REQUEST="true")
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "modal-resource-file-create" in html
        assert "multipart/form-data" in html

    def test_post_with_valid_file_redirects(self, client):
        """Valid file upload via HTMX returns HX-Redirect."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )

        uploaded = SimpleUploadedFile(
            name="test.epw",
            content=b"LOCATION,Test Location",
            content_type="application/octet-stream",
        )
        url = reverse(
            "validations:resource_file_create",
            kwargs={"pk": validator.pk},
        )
        response = client.post(
            url,
            data={
                "name": "Test Weather",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "file": uploaded,
                "is_default": False,
                "description": "",
            },
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert "HX-Redirect" in response

    def test_post_with_invalid_file_returns_form_errors(self, client):
        """Invalid file upload returns re-rendered form with errors (200)."""
        org = OrganizationFactory()
        _setup_user(client, org, RoleCode.ADMIN)
        validator = ValidatorFactory(org=org, is_system=False)

        uploaded = SimpleUploadedFile(
            name="bad_file.csv",
            content=b"not an epw file",
            content_type="text/csv",
        )
        url = reverse(
            "validations:resource_file_create",
            kwargs={"pk": validator.pk},
        )
        response = client.post(
            url,
            data={
                "name": "Bad File",
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
                "file": uploaded,
                "is_default": False,
                "description": "",
            },
            HTTP_HX_REQUEST="true",
        )
        # Per HTMX convention: return 200 with form errors, not 400
        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "not allowed" in html


# ---------------------------------------------------------------------------
# Step editor default pre-selection tests
# ---------------------------------------------------------------------------


class TestStepEditorDefaultPreSelection:
    """Tests that EnergyPlusStepConfigForm pre-selects is_default resource files."""

    def test_new_step_preselects_default_resource_file(self):
        """New step form pre-selects the is_default=True resource file."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        org = OrganizationFactory()
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )
        default_rf = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Default Weather",
            filename="default.epw",
            is_default=True,
        )
        ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Other Weather",
            filename="other.epw",
            is_default=False,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)
        assert form.initial.get("weather_file") == str(default_rf.id)

    def test_new_step_no_default_leaves_empty(self):
        """New step form with no default resource file shows empty selection."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        org = OrganizationFactory()
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )
        ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Non-Default Weather",
            filename="nondefault.epw",
            is_default=False,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)
        assert form.initial.get("weather_file") in (None, "")

    def test_existing_step_uses_saved_config_not_default(self):
        """Editing an existing step uses the saved config, not the default."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        org = OrganizationFactory()
        user = UserFactory()
        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.ENERGYPLUS,
        )
        default_rf = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Default Weather",
            filename="default.epw",
            is_default=True,
        )
        chosen_rf = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Chosen Weather",
            filename="chosen.epw",
            is_default=False,
        )

        # Create a step with the non-default file selected
        workflow = WorkflowFactory(org=org, user=user)
        step = WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            name="EP Step",
            order=1,
            config={"resource_file_ids": [str(chosen_rf.id)]},
        )

        form = EnergyPlusStepConfigForm(
            step=step,
            org=org,
            validator=validator,
        )
        # Should use the saved config, not the default
        assert form.initial["weather_file"] == str(chosen_rf.id)
        assert form.initial["weather_file"] != str(default_rf.id)
