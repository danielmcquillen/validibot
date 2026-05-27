"""Store ``Validator.version`` as a positive integer."""

from django.core.validators import MinValueValidator
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0056_collapse_validator_versions_to_integers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="validator",
            name="version",
            field=models.PositiveIntegerField(
                default=1,
                help_text=(
                    "Positive integer revision for this validator contract. "
                    "Domain versions such as EnergyPlus/FMI/JSON Schema "
                    "versions belong in tags or metadata, not this field."
                ),
                validators=[MinValueValidator(1)],
            ),
        ),
    ]
