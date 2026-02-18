"""
Tests for the signal detail feature.

This module tests:
1. ValidatorSignalsListView - full page signal list
2. Signal detail modals - popup functionality
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
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory


@pytest.mark.django_db
class TestValidatorSignalsListView:
    """Tests for the signals list page."""

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

    def test_signals_list_page_loads_for_system_validator(self, client):
        """Test that the signals list page loads for a system validator."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Test Validator",
            slug="test-validator",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
            has_processor=True,
        )
        # Create some signals
        input_signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="floor_area_m2",
            label="Floor Area (m2)",
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.NUMBER,
            description="Total floor area in square meters",
            target_field="floor_area",
        )
        output_signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="energy_output",
            label="Energy Output",
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.TIMESERIES,
            description="Energy consumption timeseries",
            target_field="results.energy",
        )

        response = client.get(
            reverse(
                "validations:validator_signals_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Check page title and structure
        assert "Signals for" in content
        assert validator.name in content

        # Check that signals are displayed
        assert input_signal.slug in content
        assert output_signal.slug in content
        assert input_signal.description in content
        assert output_signal.description in content

        # Check that signal details are shown
        assert "Floor Area (m2)" in content
        assert "Energy Output" in content
        assert "Number" in content
        assert "Timeseries" in content
        assert "Input" in content
        assert "Output" in content

    def test_signals_list_shows_back_button(self, client):
        """Test that the signals list page has a back button to validator detail."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="My Validator",
            slug="my-validator",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert 'Back to "My Validator"' in content
        assert "bi-arrow-left" in content

    def test_signals_list_has_correct_breadcrumbs(self, client):
        """Test that breadcrumbs are correct on signals list page."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Breadcrumb Test Validator",
            slug="breadcrumb-test",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        min_breadcrumbs = 3
        assert len(breadcrumbs) >= min_breadcrumbs
        assert breadcrumbs[-1]["name"] == "Signals"
        assert validator.name in breadcrumbs[-2]["name"]
        assert "Validator Library" in breadcrumbs[-3]["name"]

    def test_signals_list_empty_state(self, client):
        """Test that empty state message shows when no signals exist."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Empty Validator",
            slug="empty-validator",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "No signals have been defined" in content

    def test_signals_list_requires_library_access(self, client):
        """Test that users without library access are redirected."""
        self._setup_user(client, RoleCode.EXECUTOR)
        validator = ValidatorFactory(
            name="Test Validator",
            slug="test-validator-auth",
            is_system=True,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_list",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]


@pytest.mark.django_db
class TestValidatorDetailSignalModals:
    """Tests for signal detail modals on the validator detail page."""

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

    def test_validator_detail_includes_signal_detail_modals(self, client):
        """Test that signals tab includes modals for each signal."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Modal Test Validator",
            slug="modal-test-validator",
            is_system=True,
            has_processor=True,
        )
        signal1 = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="signal_one",
            run_stage=CatalogRunStage.INPUT,
        )
        signal2 = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="signal_two",
            run_stage=CatalogRunStage.OUTPUT,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Check that modal IDs are present for both signals
        assert f'id="modal-signal-detail-{signal1.id}"' in content
        assert f'id="modal-signal-detail-{signal2.id}"' in content

        # Check that modal triggers (info buttons) are present
        assert f'data-bs-target="#modal-signal-detail-{signal1.id}"' in content
        assert f'data-bs-target="#modal-signal-detail-{signal2.id}"' in content

    def test_validator_detail_no_template_comments_leak(self, client):
        """Test that template comments are not rendered in the HTML output."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="Comment Test Validator",
            slug="comment-test-validator",
            is_system=True,
            has_processor=True,
        )
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="test_signal",
            run_stage=CatalogRunStage.INPUT,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_tab",
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
        assert "ValidatorCatalogEntry instance" not in content

    def test_signal_info_buttons_visible_for_author_role(self, client):
        """Test that signal info buttons are visible for AUTHOR role users."""
        # Test with AUTHOR role (can view library but may not edit system validators)
        self._setup_user(client, RoleCode.AUTHOR)
        validator = ValidatorFactory(
            name="Info Button Test",
            slug="info-button-test",
            is_system=True,
        )
        signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="visible_signal",
            run_stage=CatalogRunStage.INPUT,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()

        # Info button should be visible
        assert "bi-info-circle" in content
        assert f'data-bs-target="#modal-signal-detail-{signal.id}"' in content

    def test_view_all_signals_link_present(self, client):
        """Test that 'View all' link is present when signals exist."""
        self._setup_user(client, RoleCode.ADMIN)
        validator = ValidatorFactory(
            name="View All Test",
            slug="view-all-test",
            is_system=True,
        )
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="some_signal",
            run_stage=CatalogRunStage.INPUT,
        )

        response = client.get(
            reverse(
                "validations:validator_signals_tab",
                kwargs={"slug": validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "View all" in content
        assert "bi-list-ul" in content
        assert f"/library/custom/{validator.slug}/signals/" in content


@pytest.mark.django_db
class TestSignalDetailContentTemplate:
    """Tests for the signal detail content partial template."""

    def test_signal_detail_content_renders_all_fields(self):
        """Test that signal detail content shows all signal fields."""
        validator = ValidatorFactory(is_system=True)
        signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="test_signal_slug",
            label="Test Signal Label",
            run_stage=CatalogRunStage.INPUT,
            data_type=CatalogValueType.NUMBER,
            description="A description of the test signal",
            target_field="path.to.field",
            input_binding_path="binding.path",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/signal_detail_content.html" '
            "with signal=signal %}"
        )
        context = Context({"signal": signal})
        rendered = template.render(context)

        # Check all fields are rendered
        assert "test_signal_slug" in rendered
        assert "Test Signal Label" in rendered
        assert "Input" in rendered
        assert "Number" in rendered
        assert "A description of the test signal" in rendered
        assert "path.to.field" in rendered
        assert "binding.path" in rendered

    def test_signal_detail_content_handles_empty_optional_fields(self):
        """Test that signal detail gracefully handles empty optional fields."""
        validator = ValidatorFactory(is_system=True)
        signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="minimal_signal",
            label="",  # Empty label
            description="",  # Empty description
            target_field="",  # Empty target
            input_binding_path="",  # Empty binding path
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/signal_detail_content.html" '
            "with signal=signal %}"
        )
        context = Context({"signal": signal})
        rendered = template.render(context)

        # Should render without errors
        assert "minimal_signal" in rendered
        # Empty fields should show dash or be hidden
        assert rendered.count("—") >= 1 or "Label" not in rendered


@pytest.mark.django_db
class TestSignalDetailModalTemplate:
    """Tests for the signal detail modal template."""

    def test_modal_has_correct_structure(self):
        """Test that the modal has the correct Bootstrap modal structure."""
        validator = ValidatorFactory(is_system=True)
        signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="modal_test_signal",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/signal_detail_modal.html" '
            "with signal=signal entry_id=signal.id %}"
        )
        context = Context({"signal": signal})
        rendered = template.render(context)

        # Check modal structure
        assert 'class="modal fade"' in rendered
        assert f'id="modal-signal-detail-{signal.id}"' in rendered
        assert 'class="modal-dialog' in rendered
        assert 'class="modal-content"' in rendered
        assert 'class="modal-header"' in rendered
        assert 'class="modal-body"' in rendered
        assert 'class="modal-footer"' in rendered

        # Check modal has close button
        assert 'data-bs-dismiss="modal"' in rendered
        assert "btn-close" in rendered

        # Check modal title
        assert "Signal Details" in rendered

    def test_modal_no_comment_leakage(self):
        """Test that template comments don't leak into rendered output."""
        validator = ValidatorFactory(is_system=True)
        signal = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="no_comment_signal",
        )

        template = Template(
            "{% load i18n %}"
            '{% include "validations/library/partials/signal_detail_modal.html" '
            "with signal=signal entry_id=signal.id %}"
        )
        context = Context({"signal": signal})
        rendered = template.render(context)

        # Ensure no template comment syntax
        assert "{#" not in rendered
        assert "#}" not in rendered
        assert "Context required" not in rendered
