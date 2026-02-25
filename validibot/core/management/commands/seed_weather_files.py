"""
Seed weather files as ValidatorResourceFile records for development.

This command creates ValidatorResourceFile records from EPW files in data/weather/,
making them available in the EnergyPlus step configuration dropdown.

These are created as system-wide resources (org=NULL) so they're visible to all
organizations.

Usage:
    python manage.py seed_weather_files

The command is idempotent - running it multiple times is safe.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# Weather files with friendly display names
# Format: (filename, display_name)
WEATHER_FILES = [
    ("USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw", "San Francisco, CA (TMY3)"),
    ("USA_CO_Golden-NREL.724666_TMY3.epw", "Golden/Denver, CO (TMY3)"),
    ("USA_FL_Tampa.Intl.AP.722110_TMY3.epw", "Tampa, FL (TMY3)"),
    ("USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw", "Chicago O'Hare, IL (TMY3)"),
    (
        "USA_VA_Sterling-Washington.Dulles.Intl.AP.724030_TMY3.epw",
        "Washington Dulles, VA (TMY3)",
    ),
]


class Command(BaseCommand):
    """Seed weather files as ValidatorResourceFile records for development."""

    help = (
        "Create ValidatorResourceFile records from EPW files in data/weather/. "
        "Creates system-wide resources visible to all organizations."
    )

    def add_arguments(self, parser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--source-dir",
            type=str,
            default=str(settings.BASE_DIR / "data" / "weather"),
            help="Directory containing weather files (default: data/weather)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace existing files with same filename",
        )

    def handle(self, *args, **options) -> str | None:
        """Execute the command."""
        from validibot.validations.constants import ResourceFileType
        from validibot.validations.constants import ValidationType
        from validibot.validations.models import Validator
        from validibot.validations.models import ValidatorResourceFile

        source_dir = Path(options["source_dir"])
        force = options["force"]

        if not source_dir.exists():
            self.stderr.write(
                self.style.ERROR(
                    f"Source directory does not exist: {source_dir}\n"
                    "Download EPW files or specify --source-dir."
                )
            )
            return None

        # Find the EnergyPlus validator
        try:
            energyplus_validator = Validator.objects.get(
                validation_type=ValidationType.ENERGYPLUS,
                is_system=True,
            )
        except Validator.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    "EnergyPlus validator not found. Run sync_validators first."
                )
            )
            return None

        self.stdout.write(f"Source directory: {source_dir}")
        self.stdout.write(f"Validator: {energyplus_validator.name}")
        self.stdout.write("")

        created = 0
        skipped = 0
        updated = 0
        missing = 0

        for filename, display_name in WEATHER_FILES:
            source_file = source_dir / filename

            if not source_file.exists():
                self.stdout.write(self.style.WARNING(f"  Missing: {filename}"))
                missing += 1
                continue

            # Check if resource file already exists (by filename + validator)
            existing = ValidatorResourceFile.objects.filter(
                validator=energyplus_validator,
                filename=filename,
                org__isnull=True,  # System-wide only
            ).first()

            if existing and not force:
                self.stdout.write(f"  Skipped (exists): {display_name}")
                skipped += 1
                continue

            if existing and force:
                # Delete existing file from storage before replacing
                if existing.file:
                    existing.file.delete(save=False)
                existing.delete()
                action = "Replaced"
                updated += 1
            else:
                action = "Created"
                created += 1

            # Create the resource file record
            with source_file.open("rb") as f:
                resource_file = ValidatorResourceFile(
                    validator=energyplus_validator,
                    org=None,  # System-wide
                    resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
                    name=display_name,
                    filename=filename,
                    is_default=True,
                    description=f"EnergyPlus TMY3 weather file for {display_name}",
                )
                resource_file.file.save(filename, File(f), save=True)

            self.stdout.write(self.style.SUCCESS(f"  {action}: {display_name}"))

        # Summary
        self.stdout.write("")
        if created > 0:
            self.stdout.write(
                self.style.SUCCESS(f"Created {created} weather file resource(s)")
            )
        if updated > 0:
            self.stdout.write(
                self.style.SUCCESS(f"Replaced {updated} weather file resource(s)")
            )
        if skipped > 0:
            self.stdout.write(f"Skipped {skipped} file(s) (already exist)")
        if missing > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Missing {missing} source file(s). "
                    "Download from EnergyPlus or set --source-dir."
                )
            )

        return None
