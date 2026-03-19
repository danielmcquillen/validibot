"""Drop the legacy ValidatorCatalogEntry model.

All data has been migrated to SignalDefinition / Derivation by earlier
migrations (0027, 0028, 0029).  The last FK that pointed to this table
(RulesetAssertion.target_catalog_entry) was removed in 0033.  The remaining
FMUVariable.catalog_entry FK (nullable, unused) is removed here before
dropping the table.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0033_remove_target_catalog_entry"),
    ]

    operations = [
        # 1. Drop the FMUVariable.catalog_entry FK (added in 0002, renamed in 0013).
        migrations.RemoveField(
            model_name="fmuvariable",
            name="catalog_entry",
        ),
        # 2. Remove the unique constraint before deleting the model.
        migrations.RemoveConstraint(
            model_name="validatorcatalogentry",
            name="uq_validator_catalog_entry",
        ),
        # 3. Delete the model (drops the table).
        migrations.DeleteModel(
            name="ValidatorCatalogEntry",
        ),
    ]
