"""
Tests for the step I/O detail feature.

This module tests:
1. ValidatorStepIOListView - full page step I/O list
2. Step I/O detail modals - popup functionality
3. Template rendering - ensuring no template errors or leaking comments
"""

from http import HTTPStatus

import pytest
from django.template import Context
from django.template import Template
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory


@pytest.mark.django_db
class TestValidatorStepIOListView:
    """Tests for the step I/O list page."""

    def _setup_user(self, client, role: str = RoleCode.ADMIN):
        """Create a user with the given role and log them in."""
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

    def test_step_io_list_page_loads_for_system_validator(self, client):
        """The step I/O list should load for a system validator."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Test Validator",
            slug="test-validator",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
            has_processor=True,
        )
        # Create representative input and output definitions.
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="floor_area_m2",
            label="Floor Area (m2)",
            direction="input",
            data_type="number",
            description="Total floor area in square meters",
            native_name="floor_area",
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="energy_output",
            label="Energy Output",
            direction="output",
            data_type="timeseries",
            description="Energy consumption timeseries",
            native_name="results.energy",
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Check page title and structure
        assert "Inputs and outputs for" in content
        assert validator.name in content

        # Check that both definitions are displayed.
        assert input_definition.contract_key in content
        assert output_definition.contract_key in content
        assert input_definition.description in content
        assert output_definition.description in content

        # Check that step I/O details are shown.
        assert "Floor Area (m2)" in content
        assert "Energy Output" in content
        assert "Number" in content
        assert "Timeseries" in content
        assert "Input" in content
        assert "Output" in content

    def test_step_io_list_shows_back_button(self, client):
        """The list should provide a back button to the validator detail."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="My Validator",
            slug="my-validator",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert 'Back to "My Validator"' in content
        assert "bi-arrow-left" in content

    def test_step_io_list_has_correct_breadcrumbs(self, client):
        """The list should expose the expected validator breadcrumbs."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Breadcrumb Test Validator",
            slug="breadcrumb-test",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        min_breadcrumbs = 3
        assert len(breadcrumbs) >= min_breadcrumbs
        assert breadcrumbs[-1]["name"] == "Inputs & Outputs"
        assert validator.name in breadcrumbs[-2]["name"]
        assert "Validator Library" in breadcrumbs[-3]["name"]

    def test_step_io_list_empty_state(self, client):
        """The list should explain when no step I/O definitions exist."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Empty Validator",
            slug="empty-validator",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "No step inputs or outputs have been defined" in content

    def test_step_io_list_requires_library_access(self, client):
        """Test that users without library access are redirected."""
        self._setup_user(client, RoleCode.EXECUTOR)
        validator = ValidatorFactory(
            name="Test Validator",
            slug="test-validator-auth",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]


@pytest.mark.django_db
class TestValidatorDetailStepIOModals:
    """Tests for step I/O detail modals on the validator detail page."""

    def _setup_user(self, client, role: str = RoleCode.ADMIN):
        """Create a user with the given role and log them in."""
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

    def test_validator_detail_includes_step_io_detail_modals(self, client):
        """The step I/O tab should include a modal for each definition."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Modal Test Validator",
            slug="modal-test-validator",
            is_system=True,
            has_processor=True,
        )
        input_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="input_one",
            direction="input",
        )
        output_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="output_one",
            direction="output",
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Check that modal IDs are present for both definitions.
        assert f'id="modal-step-io-detail-{input_definition.id}"' in content
        assert f'id="modal-step-io-detail-{output_definition.id}"' in content

        # Check that modal triggers (info buttons) are present
        assert (
            f'data-bs-target="#modal-step-io-detail-{input_definition.id}"' in content
        )
        assert (
            f'data-bs-target="#modal-step-io-detail-{output_definition.id}"' in content
        )

    def test_validator_detail_no_template_comments_leak(self, client):
        """Test that template comments are not rendered in the HTML output."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Comment Test Validator",
            slug="comment-test-validator",
            is_system=True,
            has_processor=True,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="test_value",
            direction="input",
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Ensure no Django template comment syntax appears in output
        assert "{#" not in content
        assert "#}" not in content
        # Check that docstring-like content isn't rendered
        assert "Context required:" not in content
        assert "StepIODefinition instance" not in content

    def test_step_io_info_buttons_visible_for_author_role(self, client):
        """Step I/O info buttons should be visible to author-role users."""
        # Test with AUTHOR role (can view library but may not edit system validators)
        self._setup_user(client, RoleCode.AUTHOR)
        validator = ValidatorFactory(
            name="Info Button Test",
            slug="info-button-test",
            is_system=True,
        )
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="visible_input",
            direction="input",
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Info button should be visible
        assert "bi-info-circle" in content
        assert f'data-bs-target="#modal-step-io-detail-{io_definition.id}"' in content

    def test_view_all_step_outputs_link_present(self, client):
        """The step I/O tab should link to the complete definition list."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="View All Test",
            slug="view-all-test",
            is_system=True,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="some_input",
            direction="input",
        )

        response = client.get(
            reverse(
                "validations:validator_step_io_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "View all" in content
        assert "bi-list-ul" in content
        assert f"/library/custom/{validator.slug}/step-io/" in content


@pytest.mark.django_db
class TestStepIODetailContentTemplate:
    """Tests for the step I/O detail content partial template."""

    def test_step_io_detail_content_renders_all_fields(self):
        """The detail content should render every step I/O field."""
        validator = ValidatorFactory(is_system=True)
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="test_output_key",
            label="Test Output Label",
            direction="input",
            data_type="number",
            description="A description of the test output",
            native_name="path.to.field",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/step_io_detail_content.html" '
            "with io_definition=io_definition %}"
        )
        context = Context({"io_definition": io_definition})
        rendered = template.render(context)

        # Check all fields are rendered
        assert "test_output_key" in rendered
        assert "Test Output Label" in rendered
        assert "Input" in rendered
        assert "Number" in rendered
        assert "A description of the test output" in rendered
        assert "path.to.field" in rendered

    def test_step_io_detail_content_handles_empty_optional_fields(self):
        """The detail content should handle empty optional fields."""
        validator = ValidatorFactory(is_system=True)
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="minimal_input",
            label="",  # Empty label
            description="",  # Empty description
            native_name="",  # Empty native name
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/step_io_detail_content.html" '
            "with io_definition=io_definition %}"
        )
        context = Context({"io_definition": io_definition})
        rendered = template.render(context)

        # Should render without errors
        assert "minimal_input" in rendered
        # Empty fields should show dash or be hidden
        assert rendered.count("—") >= 1 or "Label" not in rendered


@pytest.mark.django_db
class TestStepIODetailModalTemplate:
    """Tests for the step I/O detail modal template."""

    def test_modal_has_correct_structure(self):
        """Test that the modal has the correct Bootstrap modal structure."""
        validator = ValidatorFactory(is_system=True)
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="modal_test_output",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/step_io_detail_modal.html" '
            "with io_definition=io_definition entry_id=io_definition.id %}"
        )
        context = Context({"io_definition": io_definition})
        rendered = template.render(context)

        # Check modal structure
        assert 'class="modal fade"' in rendered
        assert f'id="modal-step-io-detail-{io_definition.id}"' in rendered
        assert 'class="modal-dialog' in rendered
        assert 'class="modal-content"' in rendered
        assert 'class="modal-header"' in rendered
        assert 'class="modal-body"' in rendered
        assert 'class="modal-footer"' in rendered

        # Check modal has close button
        assert 'data-bs-dismiss="modal"' in rendered
        assert "btn-close" in rendered

        # Check modal title
        assert "Input/Output Details" in rendered

    def test_modal_no_comment_leakage(self):
        """Test that template comments don't leak into rendered output."""
        validator = ValidatorFactory(is_system=True)
        io_definition = StepIODefinitionFactory(
            validator=validator,
            contract_key="no_comment_output",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/step_io_detail_modal.html" '
            "with io_definition=io_definition entry_id=io_definition.id %}"
        )
        context = Context({"io_definition": io_definition})
        rendered = template.render(context)

        # Ensure no template comment syntax
        assert "{#" not in rendered
        assert "#}" not in rendered
        assert "Context required" not in rendered
