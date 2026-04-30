"""
Tests for ValidatorResourceFile model and resource file functionality.

These tests cover:
- Model creation and properties
- Scoping (system-wide vs org-specific)
- Storage URI generation
- Integration with step configuration
- Envelope builder resource file resolution
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


class TestValidatorResourceFileModel:
    """Tests for ValidatorResourceFile model basics."""

    def test_create_system_wide_resource(self):
        """System-wide resources have org=NULL."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="San Francisco TMY3",
            filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )

        assert resource.org is None
        assert resource.is_system is True
        assert resource.resource_type == ResourceFileType.ENERGYPLUS_WEATHER

    def test_create_org_specific_resource(self):
        """Org-specific resources are scoped to one organization."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Custom Weather File",
            filename="custom_weather.epw",
        )

        assert resource.org == org
        assert resource.is_system is False

    def test_str_representation_system(self):
        """String representation shows scope for system resources."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test Weather",
            filename="test.epw",
        )

        assert "system" in str(resource)
        assert "Test Weather" in str(resource)

    def test_str_representation_org(self):
        """String representation shows scope for org resources."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Org Weather",
            filename="org.epw",
        )

        assert f"org:{org.id}" in str(resource)

    def test_default_ordering(self):
        """Resources are ordered by is_default (desc), then name."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        r1 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Zebra Weather",
            filename="z.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=False,
        )
        r2 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Alpha Weather",
            filename="a.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=True,
        )
        r3 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Beta Weather",
            filename="b.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=False,
        )

        resources = list(ValidatorResourceFile.objects.filter(validator=validator))
        # Default first, then alphabetical
        assert resources[0] == r2  # Alpha (default)
        assert resources[1] == r3  # Beta
        assert resources[2] == r1  # Zebra

    def test_metadata_field(self):
        """Metadata JSON field can store arbitrary data."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Chicago Weather",
            filename="chicago.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            metadata={
                "location": "Chicago, IL",
                "latitude": 41.8781,
                "longitude": -87.6298,
                "source": "TMY3",
            },
        )

        resource.refresh_from_db()
        assert resource.metadata["location"] == "Chicago, IL"
        assert resource.metadata["latitude"] == 41.8781  # noqa: PLR2004


class TestValidatorResourceFileScoping:
    """Tests for resource file visibility scoping."""

    def test_query_system_and_org_resources(self):
        """Query pattern for showing both system and org resources."""
        from django.db.models import Q

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        # System-wide resource
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="System Weather",
            filename="system.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Org-specific resource
        org_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Org Weather",
            filename="org.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Another org's resource (should not be visible)
        other_org = OrganizationFactory()
        ValidatorResourceFile.objects.create(
            validator=validator,
            org=other_org,
            name="Other Org Weather",
            filename="other.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Query: system-wide OR this org's resources
        visible_resources = ValidatorResourceFile.objects.filter(
            Q(org__isnull=True) | Q(org=org),
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        assert visible_resources.count() == 2  # noqa: PLR2004
        assert system_resource in visible_resources
        assert org_resource in visible_resources

    def test_system_resources_visible_to_all_orgs(self):
        """System resources (org=NULL) are visible to any organization."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="Global Weather",
            filename="global.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Multiple orgs should all see the system resource
        for _ in range(3):
            org = OrganizationFactory()
            from django.db.models import Q

            visible = ValidatorResourceFile.objects.filter(
                Q(org__isnull=True) | Q(org=org),
                validator=validator,
            )
            assert system_resource in visible


class TestValidatorResourceFileStorage:
    """Tests for storage URI generation."""

    def test_get_storage_uri_local_filesystem(self):
        """Local storage returns file:// URI."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        # Create a simple uploaded file
        file_content = b"LOCATION,San Francisco"
        uploaded_file = SimpleUploadedFile(
            name="test_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Test Weather",
            filename="test_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        uri = resource.get_storage_uri()
        assert uri.startswith("file://")
        assert "test_weather" in uri

    def test_get_storage_uri_gcs(self):
        """GCS storage returns gs:// URI with location prefix."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        # Create resource file with actual file (for DB save)
        file_content = b"LOCATION,Test"
        uploaded_file = SimpleUploadedFile(
            name="gcs_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="GCS Weather",
            filename="gcs_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        # Mock the file field's storage to simulate GCS with location prefix
        with patch.object(resource.file, "storage") as mock_storage:
            mock_storage.__class__.__name__ = "GoogleCloudStorage"
            mock_storage.bucket_name = "validibot-media"
            mock_storage.location = "public"  # Storage location prefix

            with patch.object(resource.file, "name", "resource_files/v123/weather.epw"):
                uri = resource.get_storage_uri()

        # URI should include the location prefix
        assert uri == "gs://validibot-media/public/resource_files/v123/weather.epw"

    def test_get_storage_uri_gcs_no_location(self):
        """GCS storage returns gs:// URI without location when not set."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        file_content = b"LOCATION,Test"
        uploaded_file = SimpleUploadedFile(
            name="gcs_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="GCS Weather",
            filename="gcs_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        # Mock GCS storage without location prefix
        with patch.object(resource.file, "storage") as mock_storage:
            mock_storage.__class__.__name__ = "GoogleCloudStorage"
            mock_storage.bucket_name = "validibot-media"
            mock_storage.location = ""  # No location prefix

            with patch.object(resource.file, "name", "resource_files/v123/weather.epw"):
                uri = resource.get_storage_uri()

        # URI should NOT have double slashes
        assert uri == "gs://validibot-media/resource_files/v123/weather.epw"


class TestResolveStepResources:
    """Tests for ``resolve_step_resources()`` — the envelope builder's
    function that converts a step's relational ``WorkflowStepResource``
    rows into ``ResourceFileItem`` objects ready for the container envelope.

    Authorization is enforced at *write time* (when the form saves the
    step resource), not at resolve time — so these tests focus on correct
    resolution mechanics rather than org/validator scoping.
    """

    def test_resolve_catalog_reference_returns_resource_item(self):
        """A catalog-reference WorkflowStepResource resolves to a
        ResourceFileItem using the underlying ValidatorResourceFile's
        UUID, type, and storage URI.
        """
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        vrf = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Test Weather",
            filename="weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather.epw", b"LOCATION,Test"),
        )
        step = WorkflowStepFactory(validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf,
        )

        items = resolve_step_resources(step)

        assert len(items) == 1
        assert items[0].id == str(vrf.id)
        assert items[0].type == ResourceFileType.ENERGYPLUS_WEATHER
        assert items[0].uri.startswith("file://")

    def test_resolve_step_owned_file_returns_resource_item(self):
        """A step-owned WorkflowStepResource resolves to a ResourceFileItem
        using the record's own PK, resource_type, and file URL.
        """
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        sr = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=SimpleUploadedFile("template.idf", b"! IDF data"),
            filename="template.idf",
            resource_type="ENERGYPLUS_IDF",
        )

        items = resolve_step_resources(step)

        assert len(items) == 1
        assert items[0].id == str(sr.pk)
        assert items[0].type == "ENERGYPLUS_IDF"

    def test_resolve_with_role_filter(self):
        """Passing a role to ``resolve_step_resources()`` returns only
        resources matching that role. This is used by the envelope builder
        to fetch only weather files, for example.
        """
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        vrf = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Weather",
            filename="weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather.epw", b"LOCATION,Test"),
        )
        step = WorkflowStepFactory(validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf,
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=SimpleUploadedFile("template.idf", b"! IDF"),
            filename="template.idf",
        )

        # Filter by WEATHER_FILE only
        weather_items = resolve_step_resources(
            step, role=WorkflowStepResource.WEATHER_FILE
        )
        assert len(weather_items) == 1
        assert weather_items[0].id == str(vrf.id)

        # Filter by MODEL_TEMPLATE only
        template_items = resolve_step_resources(
            step, role=WorkflowStepResource.MODEL_TEMPLATE
        )
        assert len(template_items) == 1

        # No filter returns all
        all_items = resolve_step_resources(step)
        assert len(all_items) == 2  # noqa: PLR2004

    def test_resolve_empty_step_returns_empty_list(self):
        """A step with no WorkflowStepResource rows returns an empty list."""
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()

        result = resolve_step_resources(step)

        assert result == []

    # ── Workspace-aware URI overrides ───────────────────────────────────
    #
    # The local Docker dispatch path materialises resource files into a
    # per-run workspace and needs the envelope to reference the
    # container-visible mount path, not the host MEDIA_ROOT path. The
    # ``resource_uri_overrides`` parameter is the rewriting hook. Cloud
    # Run leaves it unset and gets the original gs:// URIs unchanged.

    def test_resolve_with_uri_override_substitutes_container_path(self):
        """When ``resource_uri_overrides`` contains an entry for a
        resource's id, that URI replaces the one ``get_storage_uri()``
        would have returned. This is what lets the local Docker dispatch
        emit ``file:///validibot/input/resources/<filename>`` instead of
        the host ``MEDIA_ROOT`` path that lives outside the container's
        mount namespace."""
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        vrf = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Weather",
            filename="weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather.epw", b"LOCATION,Test"),
        )
        step = WorkflowStepFactory(validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf,
        )

        container_uri = "file:///validibot/input/resources/weather.epw"
        items = resolve_step_resources(
            step,
            resource_uri_overrides={str(vrf.id): container_uri},
        )

        assert len(items) == 1
        assert items[0].uri == container_uri, (
            "override must replace the model-derived URI"
        )
        # The id and type are unchanged — they identify the resource,
        # while the URI carries the container-visible path.
        assert items[0].id == str(vrf.id)
        assert items[0].type == ResourceFileType.ENERGYPLUS_WEATHER

    def test_resolve_without_overrides_uses_storage_uri_unchanged(self):
        """Cloud Run regression: when ``resource_uri_overrides`` is None
        or empty, the URI must come from ``get_storage_uri()`` exactly
        as before. Cloud Run dispatch never passes overrides — it relies
        on this fall-through path to produce ``gs://`` URIs."""
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        vrf = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Weather",
            filename="weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather.epw", b"LOCATION,Test"),
        )
        step = WorkflowStepFactory(validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf,
        )

        # Both ``None`` and ``{}`` must behave identically: fall through
        # to ``get_storage_uri()`` so the Cloud Run path is unaffected.
        for overrides in (None, {}):
            items = resolve_step_resources(step, resource_uri_overrides=overrides)
            assert len(items) == 1
            # The default URI is what get_storage_uri() returns —
            # file:// for local dev tests, gs:// in production GCS.
            assert items[0].uri == vrf.get_storage_uri()

    def test_resolve_partial_overrides_only_replaces_matching_ids(self):
        """When the override dict has entries for some but not all
        resources, the listed ids get the override and the rest fall
        through to ``get_storage_uri()``. This guards against an override
        accidentally suppressing untouched resources."""
        from validibot.validations.services.cloud_run.envelope_builder import (
            resolve_step_resources,
        )
        from validibot.workflows.models import WorkflowStepResource
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        vrf_overridden = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Weather A",
            filename="weather_a.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather_a.epw", b"a"),
        )
        vrf_passthrough = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Weather B",
            filename="weather_b.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=SimpleUploadedFile("weather_b.epw", b"b"),
        )
        step = WorkflowStepFactory(validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf_overridden,
        )
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf_passthrough,
        )

        items = resolve_step_resources(
            step,
            resource_uri_overrides={
                str(
                    vrf_overridden.id
                ): "file:///validibot/input/resources/weather_a.epw",
            },
        )

        # Sort by URI so the assertion order is independent of QuerySet
        # iteration order, which depends on insertion order and PK order.
        items_by_id = {item.id: item for item in items}
        assert items_by_id[str(vrf_overridden.id)].uri == (
            "file:///validibot/input/resources/weather_a.epw"
        )
        assert items_by_id[str(vrf_passthrough.id)].uri == (
            vrf_passthrough.get_storage_uri()
        ), "passthrough resource must keep its model-derived URI"


class TestStepConfigIntegration:
    """Tests for resource file integration with step configuration."""

    def test_build_energyplus_config_excludes_resource_file_ids(self):
        """``build_energyplus_config()`` returns IDF checks and simulation flag
        only — resource files are stored relationally via WorkflowStepResource,
        not in the config dict.
        """
        from validibot.workflows.views_helpers import build_energyplus_config

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "weather_file": "some-uuid",
            "idf_checks": ["check_version"],
            "run_simulation": True,
        }

        config, _template_vars = build_energyplus_config(mock_form)

        assert "resource_file_ids" not in config
        assert config["idf_checks"] == ["check_version"]
        assert config["run_simulation"] is True

    def test_build_energyplus_config_empty_form(self):
        """Empty form data produces default config without resource_file_ids."""
        from validibot.workflows.views_helpers import build_energyplus_config

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "weather_file": "",
            "idf_checks": [],
            "run_simulation": False,
        }

        config, _template_vars = build_energyplus_config(mock_form)

        assert "resource_file_ids" not in config
        assert config["idf_checks"] == []
        assert config["run_simulation"] is False


class TestFormIntegration:
    """Tests for resource file integration with step editor forms."""

    def test_form_populates_weather_choices(self):
        """EnergyPlus form populates weather file choices from database."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        # Create system and org resources
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="System Weather",
            filename="system.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )
        org_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Org Weather",
            filename="org.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        # Get choice values (excluding the empty choice)
        choice_values = [c[0] for c in form.fields["weather_file"].choices if c[0]]

        assert str(system_resource.id) in choice_values
        assert str(org_resource.id) in choice_values

    def test_form_excludes_other_org_resources(self):
        """Form excludes resources from other organizations."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        other_org = OrganizationFactory()

        # Create resource for other org
        other_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=other_org,
            name="Other Org Weather",
            filename="other.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        choice_values = [c[0] for c in form.fields["weather_file"].choices if c[0]]

        assert str(other_resource.id) not in choice_values

    def test_form_labels_org_resources(self):
        """Form adds '(org)' suffix to org-specific resource labels."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Custom Weather",
            filename="custom.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        # Find the choice label for org resource
        labels = [c[1] for c in form.fields["weather_file"].choices]
        org_labels = [label for label in labels if "(org)" in label]

        assert len(org_labels) == 1
        assert "Custom Weather" in org_labels[0]
