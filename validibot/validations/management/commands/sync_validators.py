"""
Management command to sync system validators and their catalog entries.

Usage:
    python manage.py sync_validators

System validators (EnergyPlus, FMU, THERM, etc.) declare their metadata
in ``config.py`` files within their validator packages. This command
discovers those configs and ensures the corresponding ``Validator`` and
``ValidatorCatalogEntry`` rows exist in the database.

The catalog entries are required for the step editor UI to show separate
"Input Assertions" and "Output Assertions" sections.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.validators.base.config import discover_configs


class Command(BaseCommand):
    help = "Sync system validators and their catalog entries from config declarations."

    def handle(self, *args, **options):
        configs = discover_configs()
        total_validators_created = 0
        total_validators_updated = 0
        total_entries_created = 0
        total_entries_existing = 0

        for cfg in configs:
            self.stdout.write(f"Processing {cfg.slug}...")

            with transaction.atomic():
                # Build validator field dict from the Pydantic model,
                # excluding fields that aren't Validator model columns.
                validator_data = cfg.model_dump(
                    exclude={
                        "catalog_entries",
                        "allowed_extensions",
                        "resource_types",
                        "icon",
                        "card_image",
                    },
                )

                validator, created = Validator.objects.get_or_create(
                    slug=cfg.slug,
                    defaults=validator_data,
                )

                if created:
                    total_validators_created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  Created validator: {validator}"),
                    )
                else:
                    # Update existing validator fields
                    for key, value in validator_data.items():
                        if key != "slug":
                            setattr(validator, key, value)
                    validator.save()
                    total_validators_updated += 1
                    self.stdout.write(f"  Updated validator: {validator}")

                # Sync catalog entries
                for entry in cfg.catalog_entries:
                    entry_data = entry.model_dump()
                    entry_slug = entry_data.pop("slug")
                    entry_type = entry_data.pop("entry_type")

                    _, entry_created = ValidatorCatalogEntry.objects.get_or_create(
                        validator=validator,
                        slug=entry_slug,
                        entry_type=entry_type,
                        defaults=entry_data,
                    )

                    if entry_created:
                        total_entries_created += 1
                    else:
                        total_entries_existing += 1

                if cfg.catalog_entries:
                    self.stdout.write(
                        f"  Catalog entries: {total_entries_created} created, "
                        f"{total_entries_existing} existing",
                    )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: "
                f"{total_validators_created} validators created, "
                f"{total_validators_updated} updated. "
                f"{total_entries_created} catalog entries created."
            ),
        )
