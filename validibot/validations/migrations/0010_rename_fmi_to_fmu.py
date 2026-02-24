"""
Data migration: rename FMI → FMU in validation_type, ruleset_type, and
base_validation_type columns.

Part of the broader FMI → FMU rename across the codebase. The enum member
in ValidationType/RulesetType was renamed from FMI to FMU; this migration
brings existing database rows in line with the new value.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0009_alter_customvalidator_base_validation_type_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                (
                    "UPDATE validations_validator"
                    " SET validation_type = 'FMU'"
                    " WHERE validation_type = 'FMI'"
                ),
                (
                    "UPDATE validations_ruleset"
                    " SET ruleset_type = 'FMU'"
                    " WHERE ruleset_type = 'FMI'"
                ),
                (
                    "UPDATE validations_customvalidator"
                    " SET base_validation_type = 'FMU'"
                    " WHERE base_validation_type = 'FMI'"
                ),
            ],
            reverse_sql=[
                (
                    "UPDATE validations_validator"
                    " SET validation_type = 'FMI'"
                    " WHERE validation_type = 'FMU'"
                ),
                (
                    "UPDATE validations_ruleset"
                    " SET ruleset_type = 'FMI'"
                    " WHERE ruleset_type = 'FMU'"
                ),
                (
                    "UPDATE validations_customvalidator"
                    " SET base_validation_type = 'FMI'"
                    " WHERE base_validation_type = 'FMU'"
                ),
            ],
        ),
    ]
