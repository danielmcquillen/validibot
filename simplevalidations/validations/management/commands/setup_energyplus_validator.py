from django.core.management.base import BaseCommand
from django.db import transaction

from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import CatalogValueType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import Validator
from simplevalidations.validations.models import ValidatorCatalogEntry


class Command(BaseCommand):
    help = (
        "Create the default EnergyPlus validator plus one input/output signal "
        "and one input/output derivation."
    )

    validator_slug = "energyplus-idf-validation"

    def handle(self, *args, **options):
        validator, _ = Validator.objects.get_or_create(
            slug=self.validator_slug,
            defaults={
                "name": "EnergyPlus Validation",
                "description": "Baseline EnergyPlus validator with demo catalog entries.",
                "validation_type": ValidationType.ENERGYPLUS,
                "version": "1.0",
                "order": 10,
                "is_system": True,
            },
        )
        validator.validation_type = ValidationType.ENERGYPLUS
        validator.is_system = True
        validator.org = None
        if not validator.processor_name:
            validator.processor_name = "EnergyPlus Simulation"
        validator.save()

        entries = [
            {
                "entry_type": CatalogEntryType.SIGNAL,
                "run_stage": CatalogRunStage.INPUT,
                "slug": "submission_floor_area_m2",
                "label": "Submission Floor Area (m²)",
                "data_type": CatalogValueType.NUMBER,
                "description": "Floor area captured from submission metadata.",
                "binding_config": {
                    "source": "submission.metadata",
                    "path": "floor_area_m2",
                },
                "order": 10,
            },
            {
                "entry_type": CatalogEntryType.SIGNAL,
                "run_stage": CatalogRunStage.OUTPUT,
                "slug": "facility_peak_demand_w",
                "label": "Facility Peak Demand (W)",
                "data_type": CatalogValueType.NUMBER,
                "description": "Peak electricity demand reported by the simulation.",
                "binding_config": {
                    "source": "metric",
                    "key": "facility_peak_demand_w",
                },
                "order": 20,
            },
            {
                "entry_type": CatalogEntryType.DERIVATION,
                "run_stage": CatalogRunStage.INPUT,
                "slug": "derived_density",
                "label": "Derived Occupancy Density",
                "data_type": CatalogValueType.NUMBER,
                "description": "Helper derivation combining occupants and floor area prior to simulation.",
                "binding_config": {
                    "expr": "value('occupants') / value('submission_floor_area_m2')",
                },
                "order": 30,
            },
            {
                "entry_type": CatalogEntryType.DERIVATION,
                "run_stage": CatalogRunStage.OUTPUT,
                "slug": "derived_peak_to_average_ratio",
                "label": "Peak/Average Demand Ratio",
                "data_type": CatalogValueType.NUMBER,
                "description": "Compares peak demand with average consumption after the simulation completes.",
                "binding_config": {
                    "expr": "value('facility_peak_demand_w') / value('average_demand_w')",
                },
                "order": 40,
            },
        ]

        created = 0
        with transaction.atomic():
            for entry in entries:
                defaults = entry.copy()
                slug = defaults.pop("slug")
                obj, was_created = ValidatorCatalogEntry.objects.update_or_create(
                    validator=validator,
                    slug=slug,
                    defaults=defaults,
                )
                if was_created:
                    created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"Created catalog entry '{obj.slug}'."),
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"EnergyPlus validator ready (catalog entries created: {created}).",
            ),
        )
