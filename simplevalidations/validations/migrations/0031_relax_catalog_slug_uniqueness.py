from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0030_rename_validations_fmivariable_fmu_model_4e1d1e_idx_validations_fmu_mod_69d7e9_idx_and_more"),
    ]

    operations = [
        migrations.AlterConstraint(
            model_name="validatorcatalogentry",
            name="uq_validator_catalog_entry",
            constraint=models.UniqueConstraint(
                fields=["validator", "entry_type", "run_stage", "slug"],
                name="uq_validator_catalog_entry",
            ),
        ),
    ]
