"""
Tests for the sync_advanced_validators management command.
"""

import json
import sys
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.test import override_settings

from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.management.commands.sync_advanced_validators import (
    METADATA_LABEL,
)
from validibot.validations.management.commands.sync_advanced_validators import (
    ValidatorMetadata,
)
from validibot.validations.models import Validator


def create_mock_docker():
    """Create a mock docker module."""
    mock_docker = MagicMock()
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    return mock_docker, mock_client


class ValidatorMetadataTests(TestCase):
    """Tests for ValidatorMetadata dataclass."""

    def test_from_dict_creates_metadata(self):
        """Test that from_dict creates metadata from a dictionary."""
        data = {
            "id": "energyplus",
            "slug": "energyplus",
            "display_name": "EnergyPlus",
            "version": "24.2.0",
            "description": "EnergyPlus validator",
            "validation_type": "ENERGYPLUS",
        }
        metadata = ValidatorMetadata.from_dict(data, "test-image:latest")

        self.assertEqual(metadata.id, "energyplus")
        self.assertEqual(metadata.slug, "energyplus")
        self.assertEqual(metadata.display_name, "EnergyPlus")
        self.assertEqual(metadata.version, "24.2.0")
        self.assertEqual(metadata.validation_type, "ENERGYPLUS")
        self.assertEqual(metadata.container_image, "test-image:latest")

    def test_validate_returns_empty_for_valid_metadata(self):
        """Test that validate returns no errors for valid metadata."""
        metadata = ValidatorMetadata(
            id="energyplus",
            slug="energyplus",
            display_name="EnergyPlus",
            version="24.2.0",
            description="",
            validation_type="ENERGYPLUS",
        )
        errors = metadata.validate()
        self.assertEqual(errors, [])

    def test_validate_returns_errors_for_missing_fields(self):
        """Test that validate returns errors for missing required fields."""
        metadata = ValidatorMetadata(
            id="",
            slug="",
            display_name="",
            version="",
            description="",
            validation_type="",
        )
        errors = metadata.validate()

        self.assertIn("Missing required field: id", errors)
        self.assertIn("Missing required field: slug", errors)
        self.assertIn("Missing required field: display_name", errors)
        self.assertIn("Missing required field: validation_type", errors)

    def test_validate_returns_error_for_invalid_validation_type(self):
        """Test that validate returns error for invalid validation_type."""
        metadata = ValidatorMetadata(
            id="test",
            slug="test",
            display_name="Test",
            version="1.0",
            description="",
            validation_type="INVALID_TYPE",
        )
        errors = metadata.validate()

        self.assertEqual(len(errors), 1)
        self.assertIn("Invalid validation_type", errors[0])


class SyncAdvancedValidatorsCommandTests(TestCase):
    """Tests for the sync_advanced_validators management command."""

    def call_command(self, *args, **kwargs):
        """Helper to call the command and capture output."""
        out = StringIO()
        err = StringIO()
        call_command(
            "sync_advanced_validators",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_command_warns_when_no_images_configured(self):
        """Test that command warns when ADVANCED_VALIDATOR_IMAGES is empty."""
        with override_settings(ADVANCED_VALIDATOR_IMAGES=[]):
            out, _ = self.call_command()
            self.assertIn("No advanced validator images configured", out)

    def test_command_fails_when_docker_unavailable(self):
        """Test that command fails when Docker is not available."""
        mock_docker = MagicMock()
        mock_docker.from_env.side_effect = Exception("Docker not running")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                with self.assertRaises(CommandError) as ctx:
                    self.call_command()

                self.assertIn("Docker is not available", str(ctx.exception))

    def test_command_creates_validator_from_label_metadata(self):
        """Test that command creates validator from Docker label metadata."""
        mock_docker, mock_client = create_mock_docker()

        # Setup mock image with metadata label
        metadata = {
            "id": "energyplus",
            "slug": "energyplus-test",
            "display_name": "EnergyPlus Test",
            "version": "24.2.0",
            "description": "Test EnergyPlus validator",
            "validation_type": "ENERGYPLUS",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                out, _ = self.call_command("--no-pull")

        # Verify validator was created
        self.assertIn("Created: EnergyPlus Test", out)
        self.assertTrue(
            Validator.objects.filter(slug="energyplus-test").exists()
        )

        validator = Validator.objects.get(slug="energyplus-test")
        self.assertEqual(validator.name, "EnergyPlus Test")
        self.assertEqual(validator.version, "24.2.0")
        self.assertEqual(validator.validation_type, ValidationType.ENERGYPLUS)
        self.assertTrue(validator.is_system)
        self.assertEqual(validator.release_state, ValidatorReleaseState.PUBLISHED)

    def test_command_updates_existing_validator(self):
        """Test that command updates existing validator."""
        # Create existing validator
        Validator.objects.create(
            slug="energyplus-update",
            name="Old Name",
            version="23.0.0",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        mock_docker, mock_client = create_mock_docker()

        # Setup mock image with updated metadata
        metadata = {
            "id": "energyplus",
            "slug": "energyplus-update",
            "display_name": "New Name",
            "version": "24.2.0",
            "description": "Updated description",
            "validation_type": "ENERGYPLUS",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                out, _ = self.call_command("--no-pull")

        # Verify validator was updated
        self.assertIn("Updated: New Name", out)

        validator = Validator.objects.get(slug="energyplus-update")
        self.assertEqual(validator.name, "New Name")
        self.assertEqual(validator.version, "24.2.0")

    def test_command_dry_run_does_not_create(self):
        """Test that --dry-run does not create validators."""
        mock_docker, mock_client = create_mock_docker()

        # Setup mock image
        metadata = {
            "id": "test",
            "slug": "test-dry-run",
            "display_name": "Test Dry Run",
            "version": "1.0",
            "description": "",
            "validation_type": "FMI",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                out, _ = self.call_command("--no-pull", "--dry-run")

        # Verify dry run output
        self.assertIn("DRY RUN", out)
        self.assertIn("Would create", out)

        # Verify validator was NOT created
        self.assertFalse(
            Validator.objects.filter(slug="test-dry-run").exists()
        )

    def test_command_disables_removed_validators(self):
        """Test that validators not in config are disabled (soft delete)."""
        # Create existing validator that won't be in config
        Validator.objects.create(
            slug="old-validator",
            name="Old Validator",
            version="1.0",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
            release_state=ValidatorReleaseState.PUBLISHED,
        )

        mock_docker, mock_client = create_mock_docker()

        # Setup mock image for new validator
        metadata = {
            "id": "new",
            "slug": "new-validator",
            "display_name": "New Validator",
            "version": "1.0",
            "description": "",
            "validation_type": "ENERGYPLUS",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                out, _ = self.call_command("--no-pull")

        # Verify old validator was disabled
        self.assertIn("Disabled 1 validator", out)

        old_validator = Validator.objects.get(slug="old-validator")
        self.assertEqual(old_validator.release_state, ValidatorReleaseState.DRAFT)

    def test_command_reports_invalid_metadata(self):
        """Test that command reports invalid metadata."""
        mock_docker, mock_client = create_mock_docker()

        # Setup mock image with invalid metadata (missing required fields)
        metadata = {
            "id": "",
            "slug": "",
            "display_name": "",
            "version": "",
            "description": "",
            "validation_type": "",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=["test-image:latest"]):
                out, _ = self.call_command("--no-pull")

        # Verify errors were reported
        self.assertIn("Missing required field", out)
        self.assertIn("1 failed", out)

    def test_command_single_image_option(self):
        """Test that --image option syncs a single image."""
        mock_docker, mock_client = create_mock_docker()

        # Setup mock image
        metadata = {
            "id": "single",
            "slug": "single-image-test",
            "display_name": "Single Image Test",
            "version": "1.0",
            "description": "",
            "validation_type": "FMI",
        }
        mock_image = MagicMock()
        mock_image.labels = {METADATA_LABEL: json.dumps(metadata)}
        mock_client.images.get.return_value = mock_image

        # Call with --image (ignores ADVANCED_VALIDATOR_IMAGES)
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with override_settings(ADVANCED_VALIDATOR_IMAGES=[]):
                out, _ = self.call_command(
                    "--no-pull",
                    "--image=custom-image:latest",
                )

        # Verify validator was created
        self.assertIn("Created: Single Image Test", out)
        self.assertTrue(
            Validator.objects.filter(slug="single-image-test").exists()
        )
