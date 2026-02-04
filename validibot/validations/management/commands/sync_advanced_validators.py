"""
Management command to sync advanced validators from container images.

This command reads ADVANCED_VALIDATOR_IMAGES from settings, inspects each image
for embedded metadata, and creates/updates validator records in the database.

Usage:
    python manage.py sync_advanced_validators

    # Dry run (preview changes without applying)
    python manage.py sync_advanced_validators --dry-run

    # Skip pulling images (use local only)
    python manage.py sync_advanced_validators --no-pull

Per ADR 2026-01-29, validator metadata is embedded in container images via:
1. Docker label: org.validibot.validator.metadata (JSON)
2. Fallback: /validibot-metadata.json file inside the container

Example metadata:
{
    "id": "energyplus",
    "slug": "energyplus",
    "display_name": "EnergyPlus",
    "version": "24.2.0",
    "description": "EnergyPlus building energy simulation validator",
    "validation_type": "ENERGYPLUS",
    "supported_data_formats": ["ENERGYPLUS_IDF", "ENERGYPLUS_EPJSON"],
    "default_timeout_seconds": 600
}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import Validator

logger = logging.getLogger(__name__)

# Advanced validator types that run in containers
ADVANCED_VALIDATOR_TYPES = {
    ValidationType.ENERGYPLUS,
    ValidationType.FMI,
}

# Docker label containing validator metadata
METADATA_LABEL = "org.validibot.validator.metadata"

# Fallback path for metadata file inside container
METADATA_FILE_PATH = "/validibot-metadata.json"


@dataclass
class ValidatorMetadata:
    """Parsed validator metadata from container image."""

    id: str
    slug: str
    display_name: str
    version: str
    description: str
    validation_type: str
    supported_data_formats: list[str] | None = None
    supported_file_types: list[str] | None = None
    default_timeout_seconds: int = 600
    container_image: str = ""

    @classmethod
    def from_dict(cls, data: dict, image: str) -> ValidatorMetadata:
        """Create metadata from a dictionary (label or file content)."""
        return cls(
            id=data.get("id", ""),
            slug=data.get("slug", ""),
            display_name=data.get("display_name", ""),
            version=data.get("version", ""),
            description=data.get("description", ""),
            validation_type=data.get("validation_type", ""),
            supported_data_formats=data.get("supported_data_formats"),
            supported_file_types=data.get("supported_file_types"),
            default_timeout_seconds=data.get("default_timeout_seconds", 600),
            container_image=image,
        )

    def validate(self) -> list[str]:
        """Validate metadata and return list of errors."""
        errors = []
        if not self.id:
            errors.append("Missing required field: id")
        if not self.slug:
            errors.append("Missing required field: slug")
        if not self.display_name:
            errors.append("Missing required field: display_name")
        if not self.validation_type:
            errors.append("Missing required field: validation_type")
        elif self.validation_type not in ValidationType.values:
            errors.append(
                f"Invalid validation_type: {self.validation_type}. "
                f"Must be one of: {', '.join(ValidationType.values)}"
            )
        return errors


class Command(BaseCommand):
    help = "Sync advanced validators from container image metadata."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without applying them.",
        )
        parser.add_argument(
            "--no-pull",
            action="store_true",
            help="Skip pulling images (use local images only).",
        )
        parser.add_argument(
            "--image",
            type=str,
            help="Sync a single image instead of all configured images.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        no_pull = options["no_pull"]
        single_image = options.get("image")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be made"))

        # Get configured images
        if single_image:
            images = [single_image]
        else:
            images = getattr(settings, "ADVANCED_VALIDATOR_IMAGES", [])

        if not images:
            self.stdout.write(
                self.style.WARNING(
                    "No advanced validator images configured. "
                    "Set ADVANCED_VALIDATOR_IMAGES in settings."
                )
            )
            return

        self.stdout.write(f"Found {len(images)} image(s) to sync")

        # Check Docker availability
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception as e:
            raise CommandError(f"Docker is not available: {e}") from e

        # Track results
        created = 0
        updated = 0
        failed = 0
        configured_slugs = set()

        for image in images:
            self.stdout.write(f"\nProcessing: {image}")

            try:
                # Pull image if needed
                if not no_pull:
                    self._pull_image(client, image)

                # Extract metadata
                metadata = self._extract_metadata(client, image)
                if not metadata:
                    self.stdout.write(
                        self.style.ERROR("  No metadata found in image")
                    )
                    failed += 1
                    continue

                # Validate metadata
                errors = metadata.validate()
                if errors:
                    for error in errors:
                        self.stdout.write(self.style.ERROR(f"  {error}"))
                    failed += 1
                    continue

                configured_slugs.add(metadata.slug)

                # Create or update validator
                if dry_run:
                    exists = Validator.objects.filter(
                        slug=metadata.slug,
                        is_system=True,
                    ).exists()
                    action = "Would update" if exists else "Would create"
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  {action}: {metadata.display_name} "
                            f"({metadata.slug} v{metadata.version})"
                        )
                    )
                else:
                    was_created = self._sync_validator(metadata)
                    if was_created:
                        created += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  Created: {metadata.display_name} "
                                f"({metadata.slug} v{metadata.version})"
                            )
                        )
                    else:
                        updated += 1
                        self.stdout.write(
                            f"  Updated: {metadata.display_name} "
                            f"({metadata.slug} v{metadata.version})"
                        )

            except Exception as e:
                logger.exception("Failed to process image: %s", image)
                self.stdout.write(self.style.ERROR(f"  Error: {e}"))
                failed += 1

        # Mark removed validators as unavailable (soft delete)
        if not dry_run and not single_image:
            disabled = self._disable_removed_validators(configured_slugs)
            if disabled:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nDisabled {disabled} validator(s) no longer in config"
                    )
                )

        # Summary
        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"DRY RUN complete: {len(images) - failed} would sync, "
                    f"{failed} failed"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sync complete: {created} created, {updated} updated, "
                    f"{failed} failed"
                )
            )

    def _pull_image(self, client, image: str) -> None:
        """Pull an image from the registry."""
        self.stdout.write("  Pulling image...")
        try:
            client.images.pull(image)
        except Exception as e:
            # Log but don't fail - image might be local only
            logger.warning("Failed to pull image %s: %s", image, e)
            self.stdout.write(
                self.style.WARNING(f"  Could not pull (using local): {e}")
            )

    def _extract_metadata(self, client, image: str) -> ValidatorMetadata | None:
        """Extract validator metadata from a container image."""
        try:
            img = client.images.get(image)
        except Exception:
            logger.exception("Image not found: %s", image)
            return None

        # Try label first
        labels = img.labels or {}
        if METADATA_LABEL in labels:
            try:
                data = json.loads(labels[METADATA_LABEL])
                return ValidatorMetadata.from_dict(data, image)
            except json.JSONDecodeError as e:
                logger.warning("Invalid JSON in metadata label for %s: %s", image, e)

        # Fallback: try to read metadata file from container
        try:
            container = client.containers.create(image)
            try:
                bits, _ = container.get_archive(METADATA_FILE_PATH)
                # Extract tar content
                import io
                import tarfile

                tar_stream = io.BytesIO()
                for chunk in bits:
                    tar_stream.write(chunk)
                tar_stream.seek(0)

                with tarfile.open(fileobj=tar_stream) as tar:
                    for member in tar.getmembers():
                        f = tar.extractfile(member)
                        if f:
                            data = json.loads(f.read().decode("utf-8"))
                            return ValidatorMetadata.from_dict(data, image)
            finally:
                container.remove(force=True)
        except Exception as e:
            logger.debug("Could not read metadata file from %s: %s", image, e)

        return None

    def _sync_validator(self, metadata: ValidatorMetadata) -> bool:
        """
        Create or update a validator from metadata.

        Returns True if created, False if updated.
        """
        with transaction.atomic():
            defaults = {
                "name": metadata.display_name,
                "validation_type": metadata.validation_type,
                "version": metadata.version,
                "description": metadata.description,
                "is_system": True,
                "release_state": ValidatorReleaseState.PUBLISHED,
            }

            # Add supported formats if provided
            if metadata.supported_data_formats:
                defaults["supported_data_formats"] = metadata.supported_data_formats
            if metadata.supported_file_types:
                defaults["supported_file_types"] = metadata.supported_file_types

            validator, created = Validator.objects.update_or_create(
                slug=metadata.slug,
                is_system=True,
                defaults=defaults,
            )

            return created

    def _disable_removed_validators(self, configured_slugs: set[str]) -> int:
        """
        Disable advanced validators that are no longer in the configuration.

        Uses DRAFT release state for soft delete - validators remain in DB
        but are hidden from users.

        Returns count of disabled validators.
        """
        # Find system validators of advanced types not in config
        to_disable = Validator.objects.filter(
            is_system=True,
            validation_type__in=ADVANCED_VALIDATOR_TYPES,
            release_state=ValidatorReleaseState.PUBLISHED,
        ).exclude(slug__in=configured_slugs)

        count = 0
        for validator in to_disable:
            validator.release_state = ValidatorReleaseState.DRAFT
            validator.save(update_fields=["release_state"])
            logger.info(
                "Disabled validator %s (removed from ADVANCED_VALIDATOR_IMAGES)",
                validator.slug,
            )
            count += 1

        return count
