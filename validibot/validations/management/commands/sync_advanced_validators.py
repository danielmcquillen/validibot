"""
Management command to sync advanced validators and their catalog entries.

Usage:
    python manage.py sync_advanced_validators

Advanced validators (EnergyPlus, FMI, etc.) are packaged as Docker containers
and have catalog entries defining their input/output signals. This command
reads seed data from validibot.validations.seeds and ensures the corresponding
Validator and ValidatorCatalogEntry rows exist in the database.

The catalog entries are required for the step editor UI to show separate
"Input Assertions" and "Output Assertions" sections.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.seeds import SYSTEM_VALIDATOR_SEEDS


class Command(BaseCommand):
    help = "Sync advanced validators and their catalog entries from seed data."

    def handle(self, *args, **options):
        total_validators_created = 0
        total_validators_updated = 0
        total_entries_created = 0
        total_entries_existing = 0

        for seed in SYSTEM_VALIDATOR_SEEDS:
            validator_data = seed["validator"]
            catalog_entries = seed.get("catalog_entries", [])

            slug = validator_data["slug"]
            self.stdout.write(f"Processing {slug}...")

            with transaction.atomic():
                # Create or update validator
                validator, created = Validator.objects.get_or_create(
                    slug=slug,
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
                for entry_data in catalog_entries:
                    entry_slug = entry_data["slug"]
                    entry_type = entry_data["entry_type"]

                    defaults = {
                        k: v for k, v in entry_data.items()
                        if k not in ("slug", "entry_type")
                    }

                    _, entry_created = ValidatorCatalogEntry.objects.get_or_create(
                        validator=validator,
                        slug=entry_slug,
                        entry_type=entry_type,
                        defaults=defaults,
                    )

                    if entry_created:
                        total_entries_created += 1
                    else:
                        total_entries_existing += 1

                if catalog_entries:
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
